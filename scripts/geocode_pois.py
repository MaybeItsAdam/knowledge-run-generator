from __future__ import annotations

import argparse
import os
import sys
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# Repo-relative paths so the script works on any checkout, not just one
# machine. ROOT is the knowledge-run-generator repo root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from knowledge_run_generator.cache import cache_dir as _krg_cache_dir, cache_path as _krg_cache_path
from knowledge_run_generator.aliases import normalise as _normalise
from knowledge_run_generator.gazetteer import load_district_centres

# Mapbox token resolution, in priority order:
#   1. an explicit --token,
#   2. the MAPBOX_TOKEN / EXPO_PUBLIC_MAPBOX_PK environment variables,
#   3. an explicit --env-file (a dotenv holding EXPO_PUBLIC_MAPBOX_PK= or
#      MAPBOX_TOKEN=).
# There is deliberately no implicit path to any sibling project: this tool is
# self-contained, and a consumer (e.g. an app) points it at config explicitly.
_TOKEN_ENV_KEYS = ("MAPBOX_TOKEN", "EXPO_PUBLIC_MAPBOX_PK")


def _load_mapbox_token(env_file: str | None = None, token: str | None = None) -> str | None:
    if token:
        return token.strip()
    for key in _TOKEN_ENV_KEYS:
        val = os.environ.get(key)
        if val:
            return val.strip()
    if env_file and Path(env_file).exists():
        for line in Path(env_file).read_text().splitlines():
            for key in _TOKEN_ENV_KEYS:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    return None

# Resolved in main() from env var / --env-file; search_mapbox reads it.
MAPBOX_TOKEN: str | None = None

# The SQLite cache holds paid Mapbox lookups — it must survive reboots.
CACHE_DIR = str(_krg_cache_dir())
DB_PATH = str(_krg_cache_path("geocoding_cache.db"))

# Bias Mapbox toward London/UK so e.g. "Albion pub" resolves near the run, not
# in another city. proximity is a soft ranking hint (central London); country
# is a hard filter. Both are safe for this dataset (all points are in the UK,
# including the "outside 6 mile radius" ones which are still Greater London).
LONDON_PROXIMITY = "-0.1278,51.5074"
COUNTRY = "gb"
# Greater London bounding box (lng,lat,lng,lat) - a hard filter that also covers
# the "outside 6 mile radius" points (Wimbledon, Alexandra Palace, etc.).
LONDON_BBOX = "-0.55,51.25,0.35,51.70"
# The Search Box forward endpoint has far better POI/landmark coverage than the
# legacy mapbox.places geocoder (which mis-resolved named venues).
SEARCHBOX_URL = "https://api.mapbox.com/search/searchbox/v1/forward"

DEFAULT_IN = str(ROOT / "constants" / "extracted_pois.json")
DEFAULT_OUT = str(ROOT / "constants" / "knowledge_pois.json")
DEFAULT_FAILED_OUT = str(ROOT / "constants" / "knowledge_pois_failed.json")

# Initialize SQLite cache
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute(
    "CREATE TABLE IF NOT EXISTS mapbox_geocodes (query TEXT PRIMARY KEY, lat REAL, lng REAL)"
)
# Whole feature lists, so a result can be checked against the query (name,
# postcode) instead of trusting whatever Mapbox ranked first. The legacy
# lat/lng table above cannot be validated and is no longer read.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS searchbox_features (query TEXT PRIMARY KEY, features TEXT)"
)
conn.commit()
conn.close()

# --- Query construction -----------------------------------------------------
# Many point names don't geocode verbatim because they carry an embedded
# postcode ("Mildmay Park N1"), a parenthetical descriptor ("Open Kitchen N1
# (OKN1)") or the abbreviation "PH" for public house ("Albion PH N1"). We clean
# the name and try a small ordered set of variants, most specific first.

_POSTCODE_TOKEN_RE = re.compile(r"\s+[A-Z]{1,2}\d{1,2}[A-Z]?\b")
_PH_RE = re.compile(r"\bPH\b")


def _clean_name(name):
    """Drop parenthetical descriptors and embedded/trailing postcode tokens."""
    n = re.sub(r"\s*\([^)]*\)", "", name)
    n = _POSTCODE_TOKEN_RE.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


def query_variants(name, postcode):
    """Return ordered, de-duplicated Mapbox query strings for one point."""
    clean = _clean_name(name)
    out, seen = [], set()

    def add(stem):
        stem = re.sub(r"\s+", " ", stem).strip(" ,")
        if not stem:
            return
        candidates = []
        if postcode and postcode.upper() not in stem.upper():
            candidates.append(f"{stem}, {postcode}, London, UK")
        candidates.append(f"{stem}, London, UK")
        for q in candidates:
            if q not in seen:
                seen.add(q)
                out.append(q)

    add(clean)
    primary = primary_name(clean)
    if primary and primary != clean:
        add(primary)
    if _PH_RE.search(clean):
        no_ph = _PH_RE.sub("", clean)
        add(f"{no_ph} pub")
        add(no_ph)
    return out


# --- Cache + Mapbox ---------------------------------------------------------

def _get_cached_features(key):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cursor = conn.cursor()
    cursor.execute("SELECT features FROM searchbox_features WHERE query=?", (key,))
    row = cursor.fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def _save_cached_features(key, features):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO searchbox_features VALUES (?, ?)",
                   (key, json.dumps(features)))
    conn.commit()
    conn.close()


# Several ranked candidates per query: the first is often a fuzzy fallback
# (a different street near the proximity point), and only the name and
# postcode checks below can tell.
SEARCH_LIMIT = 5


def _slim_feature(feature):
    props = feature.get("properties") or {}
    context = props.get("context") or {}
    lng, lat = feature["geometry"]["coordinates"]
    return {
        "name": props.get("name") or "",
        "street": (context.get("street") or {}).get("name") or "",
        "feature_type": props.get("feature_type") or "",
        "postcode": (context.get("postcode") or {}).get("name") or "",
        "lat": lat,
        "lng": lng,
    }


def search_mapbox(query, proximity=LONDON_PROXIMITY, retries=4):
    """Ranked Search Box candidates for *query*: a list (possibly empty), or
    ``None`` on a transient failure that should not be cached."""
    # The result depends on the proximity bias, so it forms part of the cache
    # key; "sb2|" namespaces the feature-list cache from the legacy one.
    cache_key = f"sb2|{query}|prox={proximity}|limit={SEARCH_LIMIT}"
    cached = _get_cached_features(cache_key)
    if cached is not None:
        return cached

    params = {
        "q": query,
        "access_token": MAPBOX_TOKEN,
        "limit": SEARCH_LIMIT,
        "country": COUNTRY,
        "proximity": proximity,
        "bbox": LONDON_BBOX,
    }

    # Distinguish a *transient* failure (DNS/connection/timeout/429/5xx) from a
    # genuine "no match". Only the former is retried with backoff, and only a
    # real answer (including an empty one) is cached.
    def _backoff(attempt):
        if attempt < retries - 1:
            time.sleep(min(2 ** attempt, 8))

    for attempt in range(retries):
        try:
            resp = requests.get(SEARCHBOX_URL, params=params, timeout=15)
        except requests.exceptions.RequestException:
            _backoff(attempt)
            continue
        if resp.status_code == 200:
            features = [_slim_feature(f) for f in (resp.json().get("features") or [])]
            _save_cached_features(cache_key, features)
            return features
        if resp.status_code in (429, 500, 502, 503, 504):
            _backoff(attempt)
            continue
        return None
    return None


# --- Result validation ------------------------------------------------------
# Mapbox returns *something* for almost any query. The collapse that put 69
# different W1 streets on one point in Kennington was its fuzzy fallback: a
# confident first result that was neither the named place nor in the named
# district. A candidate is only accepted when its name carries the query's
# significant words and it sits in (or, failing that, near) the query's
# postal district.

_GENERIC_TOKENS = frozenset({
    "THE", "LONDON", "PH", "PUB", "UK", "AND", "OF", "LTD", "PLC",
    "STATION", "UNDERGROUND", "TUBE", "RAIL", "RAILWAY", "OVERGROUND",
})
_STREET_TYPES = frozenset({
    "ROAD", "STREET", "AVENUE", "SQUARE", "GARDENS", "LANE", "PLACE", "HILL",
    "GROVE", "TERRACE", "WALK", "CRESCENT", "WAY", "DRIVE", "CLOSE", "MEWS",
    "ROW", "VALE", "RISE", "COURT", "YARD", "PARADE", "GATE", "CIRCUS",
})
_POSTCODE_WORD_RE = re.compile(r"^[A-Z]{1,2}\d{1,2}[A-Z]?$")
_DISTRICT_OF_POSTCODE_RE = re.compile(r"^([A-Z]{1,2}\d{1,2})[A-Z]?\b")


# Words that describe a place rather than name it. A match need not carry
# them ("Umu - Japanese Restaurant" is Mapbox's "Umu Restaurant"; "Haz
# Restaurant Bishopsgate" is "Haz Restaurant"), but they may not be all that
# matched.
_DESCRIPTOR_TOKENS = frozenset({
    "RESTAURANT", "HOTEL", "BAR", "CAFE", "APARTMENTS", "THEATRE", "CHURCH",
    "CENTRE", "GALLERY", "CLUB", "COMPANY", "CO", "HOUSE", "BUILDING", "HALL",
    "LIMITED", "GROUP", "HQ", "OFFICE", "OFFICES", "SCHOOL", "COLLEGE",
    "HOSPITAL", "MUSEUM", "CASINO", "NIGHTCLUB", "KITCHEN", "GRILL", "SHOP",
})


def significant_tokens(name):
    text = re.sub(r"\s*\([^)]*\)", " ", name or "").replace("&", " AND ")
    return [
        t for t in _normalise(text).split()
        if t not in _GENERIC_TOKENS and not t.isdigit()
        and not _POSTCODE_WORD_RE.match(t)
    ]


def _token_eq(a, b):
    """Equal, or one edit apart for words of five letters or more, so a Points
    List typo ("GARDNES", "ABBOTTS", "HILLGROVE") still finds the place."""
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diffs) == 1 or (
            len(diffs) == 2 and diffs[1] == diffs[0] + 1
            and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]])
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _has(tokens, t):
    return any(_token_eq(t, x) for x in tokens)


def name_matches(query_name, feature):
    """Does *feature* carry *query_name*?

    On the naming words (descriptors such as Restaurant or Hotel aside):
    short names need every one, longer ones three quarters. A street-type
    word in the query must appear, so Graham Road is not Graham Street, and
    a street-type word in the feature must be in the query, so Buckingham
    Palace Road is not Buckingham Palace. Half the feature's own naming words
    must be the query's too, so "Buckingham Palace" is not answered by
    "Welcome London Buckingham Palace - Two-Bedroom Apartment". A text after
    " - " (or an en or em dash) in the query is a descriptor ("City Lit - Arts Educational
    Centre") and is tried without.
    """
    names = [query_name]
    primary = primary_name(query_name)
    if primary != query_name:
        names.append(primary)
    return any(_name_matches(n, feature) for n in names)


_DESCRIPTOR_SPLIT_RE = re.compile(r"\s+[-\u2013\u2014]\s+")


def primary_name(name):
    """The name before a " - " (or en/em dash) descriptor: "Umu - Japanese
    Restaurant" -> "Umu"."""
    return _DESCRIPTOR_SPLIT_RE.split(name or "", maxsplit=1)[0].strip()


def _name_matches(query_name, feature):
    q_all = significant_tokens(query_name)
    q = [t for t in q_all if t not in _DESCRIPTOR_TOKENS] or q_all
    if not q:
        return False
    for label in (feature.get("name"), feature.get("street")):
        f_all = significant_tokens(label)
        f = [t for t in f_all if t not in _DESCRIPTOR_TOKENS] or f_all
        if not f:
            continue
        if q_all[-1] in _STREET_TYPES and not _has(f_all, q_all[-1]):
            continue
        if f_all[-1] in _STREET_TYPES and not _has(q_all, f_all[-1]):
            continue
        if sum(1 for t in f if _has(q_all, t)) / len(f) < 0.5:
            continue
        recall = sum(1 for t in q if _has(f_all, t)) / len(q)
        if recall >= (1.0 if len(q) <= 2 else 0.75):
            return True
    return False


def postcode_district(postcode):
    m = _DISTRICT_OF_POSTCODE_RE.match((postcode or "").upper().strip())
    return m.group(1) if m else None


def choose_candidate(query_name, district, features, center):
    """The best acceptable feature, or ``None``.

    Acceptable: the name matches, and the feature is either in *district*
    (by its own postcode), or within :data:`MAX_POSTCODE_DIST_M` of the
    district's centre when it has no postcode, or within
    :data:`OTHER_DISTRICT_DIST_M` when its postcode is another district's. In-district candidates rank first, then nearest to the
    centre. With no district there is nothing to rank by, so the first
    name match wins.
    """
    named = [f for f in features or [] if name_matches(query_name, f)]
    if not named:
        return None
    if not district or center is None:
        return named[0]
    scored = []
    for f in named:
        d = _haversine_m(f["lat"], f["lng"], center[0], center[1])
        feature_district = postcode_district(f.get("postcode"))
        in_district = feature_district == district
        # A feature whose own postcode is in another district is only this
        # place when it is close to the district (a border street); the
        # "Golden Square" Mapbox found in SE11 is 3.3 km from W1's centre.
        limit = MAX_POSTCODE_DIST_M if feature_district is None else OTHER_DISTRICT_DIST_M
        if in_district or d <= limit:
            scored.append((0 if in_district else 1, d, f))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[0][2]


# --- Postcode validation ----------------------------------------------------
# Loose fallback queries (e.g. "Albion pub, London, UK") can return a confident
# but wrong match in the wrong borough. For the run-radius highlight a wrong
# coordinate is worse than a miss, so we accept a candidate only if it sits near
# the centroid of its stated postcode district. Points with no postcode
# (outside-6-mile, some curiosity points) skip this check.
MAX_POSTCODE_DIST_M = 4000.0
# The same gate for a feature that carries a postcode in a different district.
# Not tighter: the Points List files much of Chelsea (SW3) under SW1, so Tite
# Street, 1.9 km from SW1's centre, is a correct SW3 result for "SW1".
OTHER_DISTRICT_DIST_M = 2500.0


def _haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def postcode_centroid(postcode):
    """Centre of a postal district, e.g. 'W1', as ``(lat, lng)`` or ``None``.

    From the vendored postcodes.io table
    (``knowledge_run_generator/data/postal_district_centres.json``). This used
    to be a Mapbox geocode of "W1, London, UK", which answered with a point in
    Kennington: every W1 query was then biased toward it and the distance
    gate accepted whatever Mapbox found there.
    """
    centre = load_district_centres().get((postcode or "").upper())
    return centre


# Free offline last-chance tier: the committed OSM harvest. Mapbox misses a
# fair number of pubs/venues that OSM names exactly; the same postcode-distance
# gate applies so a same-named place across town is still rejected.
_osm_pois = None
_osm_lock = threading.Lock()


def _osm_lookup(name, center=None):
    """``(lat, lng)`` of the OSM feature carrying *name*, or ``None``.

    Exact upper-cased name first, then any harvested name that
    :func:`name_matches` accepts (nearest the district centre when there is
    one), so "Roundhouse Theatre" finds OSM's "Roundhouse".
    """
    global _osm_pois, _osm_index
    with _osm_lock:
        if _osm_pois is None:
            path = ROOT / "constants" / "osm_pois.json"
            try:
                _osm_pois = json.loads(path.read_text()).get("pois", {})
            except Exception:
                _osm_pois = {}
            _osm_index = {}
            for key, entry in _osm_pois.items():
                for t in significant_tokens(key):
                    _osm_index.setdefault(t, []).append(key)
    entry = _osm_pois.get(name.upper())
    if entry and "lat" in entry and "lon" in entry:
        return entry["lat"], entry["lon"]
    tokens = [t for t in significant_tokens(name) if t not in _DESCRIPTOR_TOKENS]
    if not tokens:
        return None
    keys = set(_osm_index.get(tokens[0], ()))
    best = None
    for key in keys:
        entry = _osm_pois[key]
        if "lat" not in entry or not name_matches(name, {"name": key}):
            continue
        d = _haversine_m(entry["lat"], entry["lon"], center[0], center[1]) if center else 0.0
        if best is None or d < best[0]:
            best = (d, entry["lat"], entry["lon"])
    return (best[1], best[2]) if best else None


_osm_index = None


def process_poi(poi):
    name = poi.get("name", "").strip()
    pd = poi.get("postal_district", "").strip().upper()
    if pd.lower() == "outside 6 mile radius":
        pd = ""

    center = postcode_centroid(pd) if pd else None
    # Bias Mapbox toward the point's own postcode district (not just central
    # London) so it ranks the nearby match first, e.g. the N1 town hall over a
    # same-named place across town.
    proximity = f"{center[1]},{center[0]}" if center else LONDON_PROXIMITY

    coords = None
    match_name = None
    for query in query_variants(name, pd):
        features = search_mapbox(query, proximity=proximity)
        chosen = choose_candidate(name, pd, features, center)
        if chosen is None:
            continue  # no feature is this place -> try the next variant
        coords = (chosen["lat"], chosen["lng"])
        match_name = chosen.get("name") or chosen.get("street")
        break

    if not coords:
        cand = _osm_lookup(name, center)
        if cand and not (center and _haversine_m(cand[0], cand[1], center[0], center[1]) > MAX_POSTCODE_DIST_M):
            coords = cand
            match_name = name

    # [lng, lat] to match the GeoJSON standard used by runPoints.json
    poi["coordinates"] = [coords[1], coords[0]] if coords else None
    poi["_match_name"] = match_name
    return poi


def reject_shared_coordinates(pois):
    """Null the coordinate of points that share it with a differently named point.

    Two different names on the identical coordinate is a geocoder fallback,
    not two places: the chains (seven Travelodges on one spot), the W1 street
    collapse. Within such a group a point keeps the coordinate only when the
    geocoder's own name for the feature *is* that point's name (same
    significant words); if that singles out exactly one point, the others
    lose it, and if it singles out none or several, all do. Returns the
    names that were nulled.
    """
    groups = {}
    for poi in pois:
        if poi.get("coordinates"):
            key = (round(poi["coordinates"][0], 7), round(poi["coordinates"][1], 7))
            groups.setdefault(key, []).append(poi)
    nulled = []
    for members in groups.values():
        distinct = {" ".join(significant_tokens(p["name"])) for p in members}
        if len(distinct) < 2:
            continue
        exact = [
            p for p in members
            if significant_tokens(p["name"]) and
            set(significant_tokens(p["name"])) == set(significant_tokens(p.get("_match_name") or ""))
        ]
        keep_names = {" ".join(significant_tokens(p["name"])) for p in exact}
        for p in members:
            if len(keep_names) == 1 and " ".join(significant_tokens(p["name"])) in keep_names:
                continue
            p["coordinates"] = None
            nulled.append(p["name"])
    return nulled


def main():
    parser = argparse.ArgumentParser(description="Geocode extracted Knowledge POIs via Mapbox.")
    parser.add_argument("--in", dest="in_path", default=DEFAULT_IN, help="Extracted POIs JSON.")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output geocoded JSON.")
    parser.add_argument("--failed-out", default=DEFAULT_FAILED_OUT, help="Where to record names that failed to geocode.")
    parser.add_argument("--token", default=None, help="Mapbox access token (overrides env / --env-file).")
    parser.add_argument("--env-file", default=None, help="Path to a dotenv holding MAPBOX_TOKEN= or EXPO_PUBLIC_MAPBOX_PK=.")
    parser.add_argument("--limit", type=int, default=None, help="Only geocode the first N points (debug).")
    args = parser.parse_args()

    global MAPBOX_TOKEN
    MAPBOX_TOKEN = _load_mapbox_token(args.env_file, args.token)
    if not MAPBOX_TOKEN:
        print("Error: no Mapbox token. Set MAPBOX_TOKEN / EXPO_PUBLIC_MAPBOX_PK, "
              "or pass --token / --env-file.")
        sys.exit(1)

    if not os.path.exists(args.in_path):
        print(f"Error: {args.in_path} not found. Run extract_pois.py first.")
        sys.exit(1)

    with open(args.in_path, "r") as f:
        pois = json.load(f)
    if args.limit is not None:
        pois = pois[:args.limit]

    print(f"Loaded {len(pois)} POIs. Starting geocoding...")

    # Warm postcode centroids sequentially so the proximity bias is deterministic
    # and the worker threads don't redundantly geocode the same district.
    districts = sorted({
        poi.get("postal_district", "").strip()
        for poi in pois
        if poi.get("postal_district", "").strip()
        and poi.get("postal_district", "").strip().lower() != "outside 6 mile radius"
    })
    unknown = [pc for pc in districts if postcode_centroid(pc) is None]
    if unknown:
        print(f"No vendored centre for {len(unknown)} district(s) (no district bias/gate): {unknown}")

    geocoded_pois = []
    failed_names = []

    # Run geocoding in parallel using a ThreadPoolExecutor.
    # 10 workers stays well within Mapbox's default rate limit (600 req/min).
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(process_poi, poi): poi for poi in pois}

        processed = []
        for i, future in enumerate(as_completed(futures)):
            processed.append(future.result())
            if (i + 1) % 500 == 0 or (i + 1) == len(pois):
                print(f"Progress: {i+1}/{len(pois)} processed.")

    nulled = reject_shared_coordinates(processed)
    if nulled:
        print(f"Rejected {len(nulled)} point(s) sharing a coordinate with a differently "
              f"named point: {', '.join(sorted(nulled)[:15])}{' ...' if len(nulled) > 15 else ''}")
    # Keep the input order so the output diffs cleanly between builds.
    order = {id(p): i for i, p in enumerate(pois)}
    for poi in sorted(processed, key=lambda p: order.get(id(p), 0)):
        poi.pop("_match_name", None)
        if poi["coordinates"]:
            geocoded_pois.append(poi)
        else:
            failed_names.append(poi.get("name", ""))
    print(f"Success: {len(geocoded_pois)}, Failed: {len(failed_names)}")

    # Save the geocoded list
    with open(args.out, "w") as f:
        json.dump(geocoded_pois, f, indent=2)

    # Save the failures so coverage gaps are inspectable rather than silent.
    with open(args.failed_out, "w") as f:
        json.dump(sorted(failed_names), f, indent=2)

    print(f"\nGeocoding complete! Total POIs geocoded: {len(geocoded_pois)} (failed: {len(failed_names)})")
    print(f"Saved results to {args.out}")
    if failed_names:
        sample = ", ".join(sorted(failed_names)[:15])
        print(f"Failed names written to {args.failed_out}. Sample: {sample}{' ...' if len(failed_names) > 15 else ''}")


if __name__ == "__main__":
    main()
