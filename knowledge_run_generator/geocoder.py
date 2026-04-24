import math
import re

import osmnx as ox
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut
import numpy as np
from sklearn.neighbors import BallTree
from pathlib import Path
import json

from .gazetteer import Gazetteer, semantic_snap

_graph_trees = {}
def load_overrides(path):
    """Load POI overrides from JSON file."""
    if not path or not Path(path).exists():
        return {}
    with open(path, "r") as f:
        return json.load(f)

def get_nearest_node_cached(G, lat, lon):
    global _graph_trees
    gid = id(G)
    if gid not in _graph_trees:
        nodes = ox.graph_to_gdfs(G, edges=False, node_geometry=False)[["x", "y"]]
        nodes_rad = np.deg2rad(nodes[["y", "x"]].values)
        tree = BallTree(nodes_rad, metric="haversine")
        _graph_trees[gid] = (tree, nodes.index.values)
        
    tree, indices = _graph_trees[gid]
    dist, idx = tree.query(np.deg2rad([[lat, lon]]), k=1)
    return int(indices[idx[0][0]])


def geocode_address(address):
    """
    Convert an address string to (lat, lon) coordinates via Nominatim.
    """
    geolocator = Nominatim(user_agent="knowledge_run_generator", timeout=10)
    try:
        location = geolocator.geocode(f"{address}, London, UK")
        if location:
            return (location.latitude, location.longitude)
        else:
            print(f"Could not geocode address: {address}")
            return None
    except GeocoderTimedOut:
        print("Geocoding service timed out.")
        return None


def geocode_intersection(street1, street2):
    """
    Find the intersection of two streets in London.
    """
    geolocator = Nominatim(user_agent="knowledge_run_generator", timeout=10)
    query = f"{street1} and {street2}, London, UK"
    try:
        location = geolocator.geocode(query)
        if location:
            return (location.latitude, location.longitude)

        # Fallback: try reversed
        query_rev = f"{street2} and {street1}, London, UK"
        location = geolocator.geocode(query_rev)
        if location:
             return (location.latitude, location.longitude)

        # Fallback: try comma matching
        query_comma = f"{street1}, {street2}, London, UK"
        location = geolocator.geocode(query_comma)
        if location:
             return (location.latitude, location.longitude)

        print(f"Could not geocode intersection: {street1} & {street2}")
        return None
    except GeocoderTimedOut:
        print("Geocoding service timed out.")
        return None


def geocode_and_snap(address, G, poi_overrides=None, gazetteer=None):
    """
    Geocode *address* and snap the result to a graph node.

    Resolution order:
      1. ``gazetteer`` (if provided) — uses semantic snap that prefers a node
         on the hinted ``on_street`` and avoids motorway/trunk segments.
      2. ``poi_overrides`` dict (back-compat)  — exact match, then without
         postcode.
      3. Nominatim geocoder (with / without postcode suffix).

    Steps 2 and 3 fall through to :func:`semantic_snap` so every path benefits
    from the "avoid wrong side of dual carriageway" logic, even when the
    caller hasn't supplied a Gazetteer.

    A warning is printed if the snap distance exceeds 100 m.

    Returns ``(lat, lon, node_id)`` or ``None``.
    """
    # 1. Gazetteer (preferred). Owns its own snap so we return directly.
    if gazetteer is not None:
        entry = gazetteer.resolve(address, G)
        if entry is not None:
            if entry.snap_distance_m > 100:
                print(
                    f"  Warning: '{address}' snapped {entry.snap_distance_m:.0f}m "
                    f"to graph node {entry.snapped_node}"
                )
            n = G.nodes[entry.snapped_node]
            return (n["y"], n["x"], entry.snapped_node)

    coords = None

    # 2. Raw POI overrides (back-compat for callers not using Gazetteer)
    if poi_overrides:
        upper = address.upper()
        if upper in poi_overrides:
            c = poi_overrides[upper]
            coords = (c[0], c[1]) if isinstance(c, (list, tuple)) else None

        if coords is None:
            no_pc = re.sub(r'\s+[A-Z]{1,2}\d{1,2}[A-Z]?\s*$', '', upper)
            if no_pc in poi_overrides:
                c = poi_overrides[no_pc]
                coords = (c[0], c[1]) if isinstance(c, (list, tuple)) else None

    # 3. Nominatim
    if coords is None:
        coords = geocode_address(address)

    # 4. Nominatim without postcode
    if coords is None:
        no_postcode = re.sub(r'\s+[A-Z]{1,2}\d{1,2}[A-Z]?\s*$', '', address)
        if no_postcode != address:
            coords = geocode_address(no_postcode)

    if coords is None:
        return None

    lat, lon = coords

    # Semantic snap: avoid motorway/trunk segments where possible. This
    # benefits every code path, not only those using the Gazetteer.
    node_id, snap_dist = semantic_snap(G, lat, lon)
    snapped = G.nodes[node_id]
    snap_lat, snap_lon = snapped['y'], snapped['x']

    if snap_dist > 100:
        print(f"  Warning: '{address}' snapped {snap_dist:.0f}m to graph node {node_id}")

    return (snap_lat, snap_lon, node_id)


def _haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres between two points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
