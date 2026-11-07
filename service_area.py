"""Service-area helpers shared by the dashboard form, public pages, previews and AI/SEO data.

A business is "local" by default (it is found by distance from its address). When it ticks
"my service area extends beyond local" it picks a scope:

    statewide        Statewide (the state in its HQ address)
    states           Selected states
    usa              Nationwide (USA)
    usa_countries    USA + selected countries
    countries        Selected countries
    worldwide        Worldwide

For anything other than "local" the address is shown as "Address (HQ)" and the service area
becomes the label built here.
"""

LOCAL = "local"

SCOPES = [
    ("statewide", "Statewide (the state of my HQ address)"),
    ("states", "Selected states"),
    ("usa", "Nationwide (USA)"),
    ("usa_countries", "USA + selected countries"),
    ("countries", "Selected countries"),
    ("worldwide", "Worldwide"),
]
SCOPE_KEYS = {k for k, _ in SCOPES}
STATE_SCOPES = {"states"}
COUNTRY_SCOPES = {"usa_countries", "countries"}

US_STATES = [
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"), ("CA", "California"),
    ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"), ("DC", "District of Columbia"),
    ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"), ("IL", "Illinois"),
    ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"), ("KY", "Kentucky"), ("LA", "Louisiana"),
    ("ME", "Maine"), ("MD", "Maryland"), ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"),
    ("MS", "Mississippi"), ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
    ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"), ("OR", "Oregon"),
    ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"), ("SD", "South Dakota"),
    ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"), ("VT", "Vermont"), ("VA", "Virginia"),
    ("WA", "Washington"), ("WV", "West Virginia"), ("WI", "Wisconsin"), ("WY", "Wyoming"),
]
STATE_NAMES = dict(US_STATES)

# Countries other than the USA (ISO 3166-1 alpha-2). The USA is chosen through its own scope.
COUNTRIES = [
    ("CA", "Canada"), ("MX", "Mexico"), ("GB", "United Kingdom"), ("IE", "Ireland"), ("AU", "Australia"),
    ("NZ", "New Zealand"), ("DE", "Germany"), ("FR", "France"), ("ES", "Spain"), ("IT", "Italy"),
    ("PT", "Portugal"), ("NL", "Netherlands"), ("BE", "Belgium"), ("CH", "Switzerland"), ("AT", "Austria"),
    ("SE", "Sweden"), ("NO", "Norway"), ("DK", "Denmark"), ("FI", "Finland"), ("PL", "Poland"),
    ("CZ", "Czechia"), ("GR", "Greece"), ("RO", "Romania"), ("HU", "Hungary"), ("UA", "Ukraine"),
    ("TR", "Turkey"), ("IL", "Israel"), ("AE", "United Arab Emirates"), ("SA", "Saudi Arabia"),
    ("QA", "Qatar"), ("EG", "Egypt"), ("ZA", "South Africa"), ("NG", "Nigeria"), ("KE", "Kenya"),
    ("GH", "Ghana"), ("MA", "Morocco"), ("IN", "India"), ("PK", "Pakistan"), ("BD", "Bangladesh"),
    ("LK", "Sri Lanka"), ("CN", "China"), ("HK", "Hong Kong"), ("TW", "Taiwan"), ("JP", "Japan"),
    ("KR", "South Korea"), ("SG", "Singapore"), ("MY", "Malaysia"), ("TH", "Thailand"),
    ("VN", "Vietnam"), ("PH", "Philippines"), ("ID", "Indonesia"), ("BR", "Brazil"), ("AR", "Argentina"),
    ("CL", "Chile"), ("CO", "Colombia"), ("PE", "Peru"), ("EC", "Ecuador"), ("UY", "Uruguay"),
    ("CR", "Costa Rica"), ("PA", "Panama"), ("DO", "Dominican Republic"), ("JM", "Jamaica"),
    ("BS", "Bahamas"), ("PR", "Puerto Rico"), ("GT", "Guatemala"), ("SV", "El Salvador"),
]
COUNTRY_NAMES = dict(COUNTRIES)
COUNTRY_NAMES["US"] = "United States"


def split_regions(value):
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value or "").split(",")
    seen, out = set(), []
    for item in items:
        code = str(item).strip().upper()
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def clean(scope, regions):
    """Validate form input. Returns (scope, regions_csv, error_message_or_None)."""
    scope = (scope or "").strip().lower()
    if scope in ("", LOCAL):
        return LOCAL, "", None
    if scope not in SCOPE_KEYS:
        return LOCAL, "", "Please choose a valid service area."
    codes = split_regions(regions)
    if scope in STATE_SCOPES:
        codes = [c for c in codes if c in STATE_NAMES]
        if not codes:
            return LOCAL, "", "Pick at least one state for your service area."
    elif scope in COUNTRY_SCOPES:
        codes = [c for c in codes if c in COUNTRY_NAMES and c != "US"]
        if not codes:
            return LOCAL, "", "Pick at least one country for your service area."
    else:
        codes = []
    return scope, ",".join(codes), None


def is_expanded(scope):
    return (scope or LOCAL) in SCOPE_KEYS


def label(scope, regions="", hq_state="", limit=None):
    """Human text for the service area, e.g. 'Nationwide (USA)' or 'Nevada, Arizona and Utah'."""
    scope = (scope or LOCAL).lower()
    codes = split_regions(regions)
    if scope == "statewide":
        name = STATE_NAMES.get((hq_state or "").upper())
        return f"Statewide ({name})" if name else "Statewide"
    if scope == "states":
        names = [STATE_NAMES[c] for c in codes if c in STATE_NAMES]
        if len(names) == 1:
            return f"{names[0]} (statewide)"
        return _list(names, limit)
    if scope == "usa":
        return "Nationwide (USA)"
    if scope == "usa_countries":
        names = [COUNTRY_NAMES[c] for c in codes if c in COUNTRY_NAMES and c != "US"]
        return "USA + " + _list(names, limit) if names else "Nationwide (USA)"
    if scope == "countries":
        return _list([COUNTRY_NAMES[c] for c in codes if c in COUNTRY_NAMES], limit)
    if scope == "worldwide":
        return "Worldwide"
    return ""


def _list(names, limit=None):
    if not names:
        return ""
    more = ""
    if limit and len(names) > limit:
        more = f" +{len(names) - limit} more"
        names = names[:limit]
    if len(names) == 1:
        return names[0] + more
    if more:
        return ", ".join(names) + more
    return ", ".join(names[:-1]) + " and " + names[-1]


def schema_areas(scope, regions="", hq_state=""):
    """schema.org areaServed value (list of nodes/strings) or None."""
    scope = (scope or LOCAL).lower()
    codes = split_regions(regions)
    us = {"@type": "Country", "name": "United States"}

    def state(code):
        return {"@type": "State", "name": STATE_NAMES[code], "containedInPlace": us}

    if scope == "statewide" and (hq_state or "").upper() in STATE_NAMES:
        return [state(hq_state.upper())]
    if scope == "states":
        return [state(c) for c in codes if c in STATE_NAMES] or None
    if scope == "usa":
        return [us]
    if scope == "usa_countries":
        return [us] + [{"@type": "Country", "name": COUNTRY_NAMES[c]} for c in codes if c in COUNTRY_NAMES and c != "US"]
    if scope == "countries":
        return [{"@type": "Country", "name": COUNTRY_NAMES[c]} for c in codes if c in COUNTRY_NAMES] or None
    if scope == "worldwide":
        return ["Worldwide"]
    return None
