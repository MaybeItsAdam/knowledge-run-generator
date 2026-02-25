"""
RFC 7946 GeoJSON export for Knowledge of London routes.

Produces a FeatureCollection where each route is a Feature with a LineString
geometry and metadata properties (run_id, origin, destination, distance,
streets traversed).
"""

import json
from pathlib import Path

from .router import nodes_to_coords_geometry


def route_to_geojson_feature(G, route_nodes, run_id, origin_name, dest_name,
                              metadata, direction="forward"):
    """
    Create a single GeoJSON Feature for a route.

    Parameters
    ----------
    G : networkx.MultiDiGraph
    route_nodes : list of node IDs
    run_id : int
    origin_name, dest_name : str
    metadata : dict with ``total_distance`` and ``streets_traversed``
    direction : ``"forward"`` or ``"reverse"``

    Returns
    -------
    dict — RFC 7946 Feature
    """
    coords = nodes_to_coords_geometry(G, route_nodes)

    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": coords,
        },
        "properties": {
            "run_id": run_id,
            "origin": origin_name,
            "destination": dest_name,
            "total_distance_m": round(metadata.get("total_distance", 0), 1),
            "streets_traversed": metadata.get("streets_traversed", []),
            "direction": direction,
        },
    }


def export_all_runs_geojson(features, output_path):
    """
    Write a list of GeoJSON Features as a FeatureCollection.

    Parameters
    ----------
    features : list of dicts (GeoJSON Feature objects)
    output_path : str or Path
    """
    collection = {
        "type": "FeatureCollection",
        "features": features,
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(collection, f, indent=2)
    print(f"Exported {len(features)} features to {output_path}")
