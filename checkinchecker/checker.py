from fuzzywuzzy import fuzz
import requests
import logging
import os

from util import send_email
from worker import conn

logger = logging.getLogger('checker')


tags_to_check = [
    'name',
    'name:en',
    'alt_name',
    'loc_name',
    'official_name',
    'short_name',
    'ref',
]
default_overpass_radius = float(os.environ.get('OVERPASS_DEFAULT_RADIUS', "500.0"))
default_overpass_timeout = int(os.environ.get('OVERPASS_DEFAULT_TIMEOUT', "60"))
match_cutoff = int(os.environ.get('MATCH_CUTOFF', "60"))

# Stuff to add to the Overpass query based on Foursquare category ID
overrides_for_4sq_categories = {
    '4f2a25ac4b909258e854f55f': {"extra": '["place"]'}, # Neighborhood
    '4bf58dd8d48988d1ed931735': {"extra": '["aeroway"]', "radius": 1500.0}, # Airport
    '4d954b06a243a5684965b473': {"extra": '["building"]'}, # Residential buildings (apartments/condos)
    '4dfb90c6bd413dd705e8f897': {"extra": '["building"]'}, # Residential buildings (apartments/condos)
    '4bf58dd8d48988d17e941735': {"extra": '["amenity"]'}, # Indie movie theater
    '4bf58dd8d48988d1c4941735': {"extra": '["amenity"]'}, # Restaurant
    '4bf58dd8d48988d1f9941735': {"extra": '["amenity"]'}, # Food and Drink
    '4bf58dd8d48988d1d1941735': {"extra": '["amenity"]'}, # Noodle House
    '4bf58dd8d48988d110951735': {"extra": '["shop"]'}, # Salon / Barbershop
}

def build_overpass_query(lat, lon, radius, query_extra=None, timeout=None):
    radius = radius or default_overpass_radius
    timeout = timeout or default_overpass_timeout

    query_parts = []
    for t in tags_to_check:
        for i in ('node', 'way', 'relation'):
            query_part = '{prim_type}["{tag}"][!"highway"]{query_extra}(around:{radius},{lat},{lng});'.format(
                prim_type=i,
                tag=t,
                query_extra=query_extra if query_extra else "",
                radius=radius,
                lat=round(lat, 6),
                lng=round(lon, 6),
            )
            query_parts.append(query_part)

    query = '[out:json][timeout:{}];(\n{});out body;'.format(
            timeout,
            ''.join(query_parts),
        )

    return query

def query_overpass(lat, lon, radius, query_extra=None, timeout=None):
    query = build_overpass_query(lat, lon, radius, query_extra=query_extra, timeout=timeout)
    logger.info("Querying Overpass with: %s", query)

    response = requests.post('https://overpass-api.de/api/interpreter', data=query)

    response.raise_for_status()

    return response.json()

def match_amount(venue_name, osm_obj):
    osm_name = None
    tags = osm_obj.get('tags')
    for t in tags_to_check:
        osm_name = tags.get(t)
        if osm_name:
            break

    if not osm_name:
        logger.warn("OSM object %s/%s matched but no name tags matched", osm_obj['type'], osm_obj['id'])
        return

    distance = fuzz.token_sort_ratio(venue_name, osm_name)

    return distance

def filter_matches(venue_name, overpass_elements):
    # Attach match score to each element with a tuple
    potential_matches = [(match_amount(venue_name, elem), elem) for elem in overpass_elements]
    # Sort the tuples based on their match score
    potential_matches = sorted(potential_matches, key=lambda e: e[0], reverse=True)
    # Only pay attention to the tuples that are decent matches
    potential_matches = filter(lambda p: p[0] > match_cutoff, potential_matches)

    return potential_matches

def foursquare_checkin_has_matches(checkin, user):
    from jinja2 import Environment, PackageLoader, select_autoescape

    jinja_env = Environment(
        loader=PackageLoader('checkinchecker', 'templates'),
        autoescape=select_autoescape(['html', 'xml'])
    )

    venue = checkin.get('venue')
    venue_name = venue.get('name')
    venue_url = venue.get('url')
    venue_id = venue.get('id')
    user_id = user.get('id')

    venue_user_key = 'checkin{}{}'.format(user_id, venue_id)
    if conn.getset(venue_user_key, True):
        logger.info("Skipping this checkin because uid %s has already checked in to %s",
                    user_id, venue_id)
        return

    logger.info("Looking for matches with Foursquare venue '%s'", venue_name)

    categories = venue.get('categories')
    primary_category = None
    for category in categories:
        if category.get('name').endswith('(private)'):
            logger.info("Skipping checkin at private venue")
            return

        if category.get('primary'):
            primary_category = category

    user_email = user.get('contact', {}).get('email')
    if not user_email:
        logger.warn("This checkin didn't have a user email, so I didn't do anything")
        return

    # Send emails for test pushes to me
    if user.get('id') == '1':
        user_email = 'ian@openstreetmap.us'

    radius = default_overpass_radius

    override = {}
    if primary_category:
        logger.info("Foursquare venue has primary category '%s' (%s)", primary_category['name'], primary_category['id'])
        override = overrides_for_4sq_categories.get(primary_category['id'], {})
        if override:
            logger.info("Found Overpass override %s because the primary category is %s", override, primary_category.get('name'))

            if override.get('radius'):
                radius = override.get('radius')

    overpass_results = query_overpass(
        venue.get('location').get('lat'),
        venue.get('location').get('lng'),
        radius,
        query_extra=override.get('extra'),
        timeout=default_overpass_timeout,
    )

    overpass_remark = overpass_results.get('remark')
    if overpass_remark and 'Query timed out' in overpass_remark:
        logger.warn("Overpass query timed out: %s", overpass_remark)
        return

    elements = overpass_results.get('elements')

    logger.info("Found %s things on Overpass", len(elements))

    potential_matches = filter_matches(venue_name, elements)

    if not potential_matches:
        logger.info("No matches! Send an e-mail to %s", user_email)

        templ = jinja_env.get_template('emails/foursquare_match_not_found.txt')
        message = templ.render(
            name=user.get('firstName', 'Friend'),
            venue_name=venue_name,
            venue_url=venue_url,
            user_id=user['id'],
            checkin_id=checkin['id'],
            mlat=round(venue.get('location').get('lat'), 6),
            mlon=round(venue.get('location').get('lng'), 6),
            email=user_email,
        )
        send_email(user_email, "Your Recent Foursquare Checkin Isn't On OpenStreetMap", message)
    else:
        logger.info(u"Matches: {}".format(u', '.join(map(lambda i: '{}/{} ({:0.2f})'.format(i[1]['type'], i[1]['id'], i[0]), potential_matches))))
        best_match_score, best_match = potential_matches[0]

        logger.info(u"A really great match found: %s/%s (%0.2f)", best_match['type'], best_match['id'], best_match_score)

        tags = best_match['tags']
        questions = []
        if 'addr:housenumber' in tags:
            questions.append(u" - Is the housenumber still '{}'?".format(tags['addr:housenumber']))
        else:
            questions.append(u" - What is the housenumber?")
        if 'addr:street' in tags:
            questions.append(u" - Is the venue still on '{}'?".format(tags['addr:street']))
        else:
            questions.append(u" - What is the street name?")
        if 'phone' in tags:
            questions.append(u" - Is the phone number still '{}'?".format(tags['phone']))
        else:
            questions.append(u" - What is the phone number?")

        templ = jinja_env.get_template('emails/foursquare_match_found.txt')
        message = templ.render(
            name=user.get('firstName', 'Friend'),
            venue_name=venue_name,
            venue_url=venue_url,
            user_id=user['id'],
            checkin_id=checkin['id'],
            mlat=round(venue.get('location').get('lat'), 6),
            mlon=round(venue.get('location').get('lng'), 6),
            osm_type=best_match['type'],
            osm_id=best_match['id'],
            questions='\n'.join(questions),
            email=user_email,
        )

        send_email(user_email, "Your Recent Foursquare Checkin Is On OpenStreetMap!", message)
