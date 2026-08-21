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
"""

from __future__ import annotations

import re

_CATEGORY_KEYWORDS = [
    ("station", "station"),
    ("theatre", "theatre"),
    ("cinema", "cinema"),
    ("hotel", "hotel"),
    ("museum", "museum"),
    ("gallery", "gallery"),
    ("hospital", "hospital"),
    ("library", "library"),
    ("church", "church"),
    ("park", "park"),
    ("square", "square"),
    ("bridge", "bridge"),
    ("restaurant", "restaurant"),
    ("school", "school"),
    ("college", "college"),
    ("university", "university"),
]


def infer_category(name: str) -> str:
    """Return the category for a Points List entry name."""
    low = name.lower()
    if re.search(r"\bph\b", low) or low.endswith(" ph") or " ph " in low:
        return "pub"
    for keyword, category in _CATEGORY_KEYWORDS:
        if keyword in low:
            return category
    if re.search(r"\b(road|street|lane|avenue|villas|gardens|place|way|walk|hill)\b", low):
        return "street"
    return "point"


# Historic private name, kept so the extractor's call sites read unchanged.
_infer_category = infer_category
