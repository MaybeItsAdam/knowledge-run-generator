"""Classify a Knowledge Points List entry by its name.

The Points List gives us a name and a postcode, nothing else: no OSM tags, no
amenity type. The category every downstream consumer reads (the app's filter
chips, the gazetteer's station handling) is therefore inferred from the name
alone, here.

This lives in the package rather than in ``scripts/extract_pois.py`` for two
reasons:

* the taxonomy is needed by more than one script (the extractor assigns it, the
  promotion gate in ``scripts/promote_to_app.py`` validates it), and
* tests can then ``import knowledge_run_generator.poi_categories`` directly.
  ``tests/test_pipeline_integrity.py`` has to reach the extractor through
  ``importlib.util.spec_from_file_location`` because ``scripts/`` is not a
  package, which is fine for one assertion about a default path and miserable
  for a test suite that wants to exercise the rules themselves.

It follows the shape of ``knowledge_run_generator.poi_enrichment``: the logic
sits in the package, the script on top of it stays thin.

How the rules work
------------------

An ordered list of ``(compiled regex, category)`` pairs, first match wins.
Order is the whole design; the levels below are the reasoning, and moving a
rule between them is a behaviour change, not a tidy-up.

0. **Exclusions.** A name where ``Station`` is part of a *street* name, and
   names where a hospitality noun sits next to a transport noun. Hospitality
   outranks transport: ``Old Millwall Fire Station Restaurant`` is a
   restaurant, ``Fire Station SE1 (Restaurant / Bar)`` is a restaurant, and
   ``Premier Inn London Southwark - Southwark Station Hotel`` is a hotel. One
   rule, three classes of leak.
1. **Specific compounds before generic nouns.** ``fire station`` is not a
   station in the sense a cab driver means, and neither are the police,
   ambulance, lifeboat, petrol, power or pumping varieties. ``bus garage`` has
   to be tested before ``park`` can fire, or ``Westbourne Park Bus Garage``
   comes back as a park.
2. **Generic transport.** Bare ``station``, ``dlr``, ``underground``, ``tube``.
3. **The historic keyword list**, now word-bounded. Substring matching filed
   ``Uxbridge Road`` as a bridge and ``Fenchurch Street`` as a church.
4. **Hospitality leaves.** Word boundaries are load-bearing: substring ``bar``
   matches Barbican, Barnes and Barking.
5. **The street-suffix fallback**, deliberately untouched. Widening it would
   move roughly 2,000 rows and drown any diff it travelled in.
"""

from __future__ import annotations

import re

# Every category ``infer_category`` can return. The promotion gate validates
# emitted records against this set, so adding a rule means adding its leaf here.
CATEGORIES = frozenset({
    # generic
    "point", "street",
    # transport
    "station", "bus_station", "coach_station", "bus_garage",
    # emergency and utility "stations", which are not transport at all
    "fire_station", "police_station", "ambulance_station", "lifeboat_station",
    "fuel",
    # hospitality
    "hotel", "restaurant", "pub", "bar", "cafe", "club", "hostel",
    # everything else the Points List names
    "theatre", "cinema", "museum", "gallery", "hospital", "library", "church",
    "park", "square", "bridge", "school", "college", "university",
})

# The closed vocabulary for a POI's ``transport_modes`` field.
#
# NOT YET POPULATED. Every emitted record carries ``transport_modes: []`` and
# ``transport_modes_source: null``; nothing fills them in. The Points List names
# a station without saying which lines serve it, and the only source that could
# is a fresh OSM harvest, which moves the tier-3 gazetteer for all 320 runs and
# is therefore deliberately out of scope. The vocabulary is fixed now so that
# whatever eventually populates the field has nothing to invent.
TRANSPORT_MODES = (
    "underground",
    "overground",
    "rail",
    "dlr",
    "elizabeth",
    "tram",
    "bus",
    "coach",
    "river",
    "cable_car",
    "air",
)

# "Art'otel" is a real brand: the apostrophe is a word boundary, so ``\botel\b``
# catches it while "Novotel" and "Motel One" are left alone (no boundary before
# their "otel"). "Aparthotel" is spelled solid and needs the explicit prefix.
_HOTEL = r"(?:apart)?h?otels?"

# Hospitality nouns, most specific first. The order decides ties: "Fire Station
# SE1 (Restaurant / Bar)" is filed as a restaurant, not a bar.
_HOSPITALITY_LEAVES = [
    ("restaurant", r"restaurants?"),
    ("hotel", _HOTEL),
    ("bar", r"bars?"),
    ("cafe", r"caf[eé]s?"),
    ("pub", r"pubs?|ph"),
    ("club", r"clubs?"),
    ("hostel", r"hostels?"),
]

# What makes a name look like transport, for the hospitality-outranks-transport
# test in level 0.
_TRANSPORT_TOKEN = r"stations?|dlr|underground|tube|overground|bus\s+garages?"

# "Station" qualified into a street name. Level 5 would eventually call these
# streets anyway, but only after "station" had already claimed them.
_STATION_AS_STREET = (
    r"\bstation\s+(?:road|approach|parade|street|hill|lane|way|crescent|terrace)\b"
)

# ``X station`` compounds that are not transport. Ordered pairs so the promotion
# gate can rebuild the same alternation for its regression check.
NON_TRANSPORT_STATIONS = [
    (r"fire", "fire_station"),
    (r"police", "police_station"),
    (r"ambulance", "ambulance_station"),
    (r"lifeboat", "lifeboat_station"),
    (r"petrol|filling|service", "fuel"),
    (r"power|pumping", "point"),
]

# A power station that is also a real tube station ("Battersea Power Station
# Underground Station") must not be caught by the power-station exclusion.
_TRANSPORT_QUALIFIER = r"underground|tube|dlr|overground|rail|railway"


def _non_transport_station_pattern(prefix: str, leaf: str) -> str:
    """The level-1 pattern for one ``X station`` compound."""
    if leaf == "point":  # power / pumping: yield to a genuine transport name
        return rf"\b(?:{prefix})\s+stations?\b(?!.*\b(?:{_TRANSPORT_QUALIFIER})\b)"
    return rf"\b(?:{prefix})\s+stations?\b"


_NON_TRANSPORT_STATION_RES = [
    re.compile(_non_transport_station_pattern(prefix, leaf))
    for prefix, leaf in NON_TRANSPORT_STATIONS
]


def is_non_transport_station_name(name: str) -> bool:
    """True if the name is an ``X station`` that is not a transport station.

    Independent of ``infer_category`` on purpose. The promotion gate uses this
    to check *data*, which may have been produced by an older build, so asking
    the classifier again would only tell us that the classifier agrees with
    itself. It shares the rules' patterns, including the carve-out that lets
    "Battersea Power Station Underground Station" stay a station.
    """
    low = name.lower()
    return any(rule.search(low) for rule in _NON_TRANSPORT_STATION_RES)

# The original substring list, minus "station" (now handled above), in its
# original order, with word boundaries and plurals.
_KEYWORDS = [
    (r"theatres?", "theatre"),
    (r"cinemas?", "cinema"),
    (_HOTEL, "hotel"),
    (r"museums?", "museum"),
    (r"galler(?:y|ies)", "gallery"),
    (r"hospitals?", "hospital"),
    (r"librar(?:y|ies)", "library"),
    (r"church(?:es)?", "church"),
    (r"parks?", "park"),
    (r"squares?", "square"),
    (r"bridges?", "bridge"),
    (r"restaurants?", "restaurant"),
    (r"schools?", "school"),
    (r"colleges?", "college"),
    (r"universit(?:y|ies)", "university"),
]

# Unchanged from the original implementation. See the module docstring.
_STREET_SUFFIXES = r"\b(road|street|lane|avenue|villas|gardens|place|way|walk|hill)\b"


def _build_rules() -> list[tuple[re.Pattern[str], str]]:
    rules: list[tuple[str, str]] = []

    # Level 0: exclusions.
    rules.append((_STATION_AS_STREET, "street"))
    for leaf, pattern in _HOSPITALITY_LEAVES:
        rules.append((
            rf"(?=.*\b(?:{_TRANSPORT_TOKEN})\b)(?=.*\b(?:{pattern})\b)", leaf,
        ))

    # Level 1: specific compounds, before any generic noun can claim them.
    for prefix, leaf in NON_TRANSPORT_STATIONS:
        rules.append((_non_transport_station_pattern(prefix, leaf), leaf))
    rules += [
        (r"\bbus\s+garages?\b", "bus_garage"),
        (r"\bbus\s+stations?\b", "bus_station"),
        (r"\bcoach\s+stations?\b", "coach_station"),
    ]

    # Level 2: generic transport.
    rules += [
        (r"\bstations?\b", "station"),
        (r"\bdlr\b", "station"),
        (r"\bunderground\b", "station"),
        (r"\btube\b", "station"),
    ]

    # Level 3: the historic keyword list. ``PH`` keeps its place ahead of it,
    # so "Canal Cafe Theatre at The Bridge House PH" is still a pub.
    rules.append((r"\bph\b", "pub"))
    rules += [(rf"\b{pattern}\b", leaf) for pattern, leaf in _KEYWORDS]

    # Level 4: hospitality leaves.
    rules += [
        (r"\bbars?\b", "bar"),
        (r"\bclubs?\b", "club"),
        (r"\bcaf[eé]s?\b", "cafe"),
        (r"\bhostels?\b", "hostel"),
    ]

    # Level 5: the street-suffix fallback, unchanged.
    rules.append((_STREET_SUFFIXES, "street"))

    return [(re.compile(pattern), leaf) for pattern, leaf in rules]


_RULES = _build_rules()


def infer_category(name: str) -> str:
    """Return the category for a Points List entry name."""
    low = name.lower()
    for rule, category in _RULES:
        if rule.search(low):
            return category
    return "point"


# Historic private name, kept so the extractor's call sites read unchanged.
_infer_category = infer_category
