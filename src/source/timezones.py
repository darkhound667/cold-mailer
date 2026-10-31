# -*- coding: utf-8 -*-
"""IANA timezone for an institution, from OpenAlex's geo block (country_code + region).

Single-timezone countries map directly. The US, Canada and Australia map by state or
province, using the zone that covers the state's main population (a handful of split
states, e.g. western Texas, are approximated). Returns None when it cannot decide, so
the caller can flag the row instead of guessing.
"""

COUNTRY_TZ = {
    "GB": "Europe/London", "IE": "Europe/Dublin", "SG": "Asia/Singapore", "JP": "Asia/Tokyo",
    "NZ": "Pacific/Auckland", "CN": "Asia/Shanghai", "KR": "Asia/Seoul",
    "CH": "Europe/Zurich", "DE": "Europe/Berlin", "NL": "Europe/Amsterdam", "FR": "Europe/Paris",
    "SE": "Europe/Stockholm", "DK": "Europe/Copenhagen", "NO": "Europe/Oslo",
    "FI": "Europe/Helsinki", "AT": "Europe/Vienna", "BE": "Europe/Brussels",
    "ES": "Europe/Madrid", "IT": "Europe/Rome",
    # the rest of Europe (Ruturva, 2026-10-31: "all 500 mails/day to Europe professors")
    "PT": "Europe/Lisbon", "GR": "Europe/Athens", "PL": "Europe/Warsaw", "CZ": "Europe/Prague",
    "HU": "Europe/Budapest", "SK": "Europe/Bratislava", "SI": "Europe/Ljubljana", "HR": "Europe/Zagreb",
    "RO": "Europe/Bucharest", "BG": "Europe/Sofia", "EE": "Europe/Tallinn", "LV": "Europe/Riga",
    "LT": "Europe/Vilnius", "IS": "Atlantic/Reykjavik", "LU": "Europe/Luxembourg", "CY": "Asia/Nicosia",
    "MT": "Europe/Malta", "RS": "Europe/Belgrade",
}
# Countries counted as "Europe" for the mail-Europe-first rule (scripts/next_batch_ids.py).
EUROPE = {"GB", "IE", "CH", "DE", "NL", "FR", "SE", "DK", "NO", "FI", "AT", "BE", "ES", "IT", "PT", "GR", "PL",
          "CZ", "HU", "SK", "SI", "HR", "RO", "BG", "EE", "LV", "LT", "IS", "LU", "CY", "MT", "RS"}

_EASTERN = ("Connecticut Delaware Florida Georgia Maine Maryland Massachusetts Michigan "
            "New_Hampshire New_Jersey New_York North_Carolina Ohio Pennsylvania Rhode_Island "
            "South_Carolina Vermont Virginia West_Virginia District_of_Columbia Indiana Kentucky")
_CENTRAL = ("Alabama Arkansas Illinois Iowa Kansas Louisiana Minnesota Mississippi Missouri "
            "Nebraska North_Dakota Oklahoma South_Dakota Tennessee Texas Wisconsin")
_MOUNTAIN = "Colorado Idaho Montana New_Mexico Utah Wyoming"
_PACIFIC = "California Nevada Oregon Washington"

US_STATE_TZ = {
    **{s.replace("_", " "): "America/New_York" for s in _EASTERN.split()},
    **{s.replace("_", " "): "America/Chicago" for s in _CENTRAL.split()},
    **{s.replace("_", " "): "America/Denver" for s in _MOUNTAIN.split()},
    **{s.replace("_", " "): "America/Los_Angeles" for s in _PACIFIC.split()},
    "Arizona": "America/Phoenix", "Alaska": "America/Anchorage", "Hawaii": "Pacific/Honolulu",
    "Puerto Rico": "America/Puerto_Rico",
}

CA_PROVINCE_TZ = {
    "British Columbia": "America/Vancouver", "Alberta": "America/Edmonton",
    "Saskatchewan": "America/Regina", "Manitoba": "America/Winnipeg",
    "Ontario": "America/Toronto", "Quebec": "America/Toronto", "Québec": "America/Toronto",
    "New Brunswick": "America/Halifax", "Nova Scotia": "America/Halifax",
    "Prince Edward Island": "America/Halifax", "Newfoundland and Labrador": "America/St_Johns",
}

AU_STATE_TZ = {
    "New South Wales": "Australia/Sydney", "Australian Capital Territory": "Australia/Sydney",
    "Victoria": "Australia/Melbourne", "Queensland": "Australia/Brisbane",
    "South Australia": "Australia/Adelaide", "Western Australia": "Australia/Perth",
    "Tasmania": "Australia/Hobart", "Northern Territory": "Australia/Darwin",
}


def timezone_for(country_code, region):
    """IANA zone name, or None if the country/region pair is not covered."""
    if country_code in COUNTRY_TZ:
        return COUNTRY_TZ[country_code]
    table = {"US": US_STATE_TZ, "CA": CA_PROVINCE_TZ, "AU": AU_STATE_TZ}.get(country_code)
    if table is None or not region:
        return None
    return table.get(region.strip())
