import argparse
import os
import sys
import json
import math
import re
import sqlite3
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# Try to find the Mapbox Access Token in the environment or read it from The-Blue-App/.env.local
MAPBOX_TOKEN = os.environ.get("EXPO_PUBLIC_MAPBOX_PK")

if not MAPBOX_TOKEN:
    # Try reading from The-Blue-App/.env.local
    env_path = "/Users/adam/Documents/Programming/The Knowledge Things/The-Blue-App/.env.local"
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if line.startswith("EXPO_PUBLIC_MAPBOX_PK="):
                    MAPBOX_TOKEN = line.split("=", 1)[1].strip()
                    break

if not MAPBOX_TOKEN:
    print("Error: EXPO_PUBLIC_MAPBOX_PK not found in environment or .env.local")
    sys.exit(1)

# Ensure cache directory exists
CACHE_DIR = "/tmp/app_cache"
os.makedirs(CACHE_DIR, exist_ok=True)
DB_PATH = os.path.join(CACHE_DIR, "geocoding_cache.db")

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

DEFAULT_IN = "/Users/adam/Documents/Programming/The Knowledge Things/knowledge_run_generator/constants/extracted_pois.json"
DEFAULT_OUT = "/Users/adam/Documents/Programming/The Knowledge Things/knowledge_run_generator/constants/knowledge_pois.json"
DEFAULT_FAILED_OUT = "/Users/adam/Documents/Programming/The Knowledge Things/knowledge_run_generator/constants/knowledge_pois_failed.json"

# Initialize SQLite cache
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute(
    "CREATE TABLE IF NOT EXISTS mapbox_geocodes (query TEXT PRIMARY KEY, lat REAL, lng REAL)"
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
    if _PH_RE.search(clean):
        no_ph = _PH_RE.sub("", clean)
        add(f"{no_ph} pub")
        add(no_ph)
    return out


# --- Cache + Mapbox ---------------------------------------------------------

def get_cached_geocode(query):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cursor = conn.cursor()
    cursor.execute("SELECT lat, lng FROM mapbox_geocodes WHERE query=?", (query,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return None


def save_cached_geocode(query, lat, lng):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO mapbox_geocodes VALUES (?, ?, ?)", (query, lat, lng))
    conn.commit()
    conn.close()


def geocode_mapbox(query, proximity=LONDON_PROXIMITY):
    # The result depends on the proximity bias, so it forms part of the cache
    # key; the "sb|" prefix namespaces Search Box results from any legacy entries.
    cache_key = f"sb|{query}|prox={proximity}"
    cached = get_cached_geocode(cache_key)
    if cached:
        return cached

    # Cache miss -> hit the Mapbox Search Box forward API (good POI coverage).
    params = {
        "q": query,
        "access_token": MAPBOX_TOKEN,
        "limit": 1,
        "country": COUNTRY,
        "proximity": proximity,
        "bbox": LONDON_BBOX,
    }

    try:
        resp = requests.get(SEARCHBOX_URL, params=params, timeout=15)
        if resp.status_code == 200:
            features = resp.json().get("features")
            if features:
                lng, lat = features[0]["geometry"]["coordinates"]
                save_cached_geocode(cache_key, lat, lng)
                return lat, lng
    except Exception as e:
        print(f"Error geocoding {query}: {e}")
    return None


# --- Postcode validation ----------------------------------------------------
# Loose fallback queries (e.g. "Albion pub, London, UK") can return a confident
# but wrong match in the wrong borough. For the run-radius highlight a wrong
# coordinate is worse than a miss, so we accept a candidate only if it sits near
# the centroid of its stated postcode district. Points with no postcode
# (outside-6-mile, some curiosity points) skip this check.
MAX_POSTCODE_DIST_M = 4000.0

_postcode_centroids = {}
_pc_lock = threading.Lock()


def _haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def postcode_centroid(postcode):
    """Geocode (and cache) the centroid of a postcode district, e.g. 'N1'."""
    pc = postcode.upper()
    with _pc_lock:
        if pc in _postcode_centroids:
            return _postcode_centroids[pc]
    center = geocode_mapbox(f"{pc}, London, UK")
    with _pc_lock:
        _postcode_centroids[pc] = center
    return center


def process_poi(poi):
    name = poi.get("name", "").strip()
    pd = poi.get("postal_district", "").strip()
    if pd.lower() == "outside 6 mile radius":
        pd = ""

    center = postcode_centroid(pd) if pd else None
    # Bias Mapbox toward the point's own postcode district (not just central
    # London) so it ranks the nearby match first, e.g. the N1 town hall over a
    # same-named place across town.
    proximity = f"{center[1]},{center[0]}" if center else LONDON_PROXIMITY

    coords = None
    for query in query_variants(name, pd):
        cand = geocode_mapbox(query, proximity=proximity)
        if not cand:
            continue
        if center and _haversine_m(cand[0], cand[1], center[0], center[1]) > MAX_POSTCODE_DIST_M:
            continue  # right name, wrong borough -> reject, try next variant
        coords = cand
        break

    # [lng, lat] to match the GeoJSON standard used by runPoints.json
    poi["coordinates"] = [coords[1], coords[0]] if coords else None
    return poi


def main():
    parser = argparse.ArgumentParser(description="Geocode extracted Knowledge POIs via Mapbox.")
    parser.add_argument("--in", dest="in_path", default=DEFAULT_IN, help="Extracted POIs JSON.")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output geocoded JSON.")
    parser.add_argument("--failed-out", default=DEFAULT_FAILED_OUT, help="Where to record names that failed to geocode.")
    parser.add_argument("--limit", type=int, default=None, help="Only geocode the first N points (debug).")
    args = parser.parse_args()

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
    print(f"Resolving {len(districts)} postcode centroids...")
    for pc in districts:
        postcode_centroid(pc)

    geocoded_pois = []
    failed_names = []

    # Run geocoding in parallel using a ThreadPoolExecutor.
    # 10 workers stays well within Mapbox's default rate limit (600 req/min).
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(process_poi, poi): poi for poi in pois}

        for i, future in enumerate(as_completed(futures)):
            poi = future.result()
            if poi["coordinates"]:
                geocoded_pois.append(poi)
            else:
                failed_names.append(poi.get("name", ""))

            if (i + 1) % 500 == 0 or (i + 1) == len(pois):
                print(f"Progress: {i+1}/{len(pois)} processed. Success: {len(geocoded_pois)}, Failed: {len(failed_names)}")

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
