from .geocoder import geocode_and_snap
from .router import load_graph, get_route
from .caller import generate_call
from .gazetteer import Gazetteer

def generate_run(origin_addr, dest_addr, G=None, poi_overrides=None, gazetteer=None):
    """
    High-level API to generate a Knowledge of London run from plaintext addresses.

    Args:
        origin_addr (str): Start location name or address.
        dest_addr (str): End location name or address.
        G (ox.Graph, optional): The OSMNX graph. Loads Greater London if not provided.
        poi_overrides (dict, optional): Custom POI coordinates to fix geocoding.
            Accepts the legacy ``{"NAME": [lat, lon]}`` form and the richer
            ``{"NAME": {"lat":..., "lon":..., "on_street":..., "approach_from":...}}``
            form. If ``gazetteer`` is not supplied, one is built from this dict.
        gazetteer (Gazetteer, optional): Pre-built gazetteer. Takes precedence
            over ``poi_overrides``.

    Returns:
        dict: A dictionary containing the route and formatted "Turn-by-turn" steps.
    """
    # 1. Load graph if needed
    if G is None:
        G = load_graph()

    # 2. Build gazetteer from overrides if none supplied
    if gazetteer is None and poi_overrides:
        gazetteer = Gazetteer(overrides=poi_overrides)

    # 3. Geocode and snap
    start = geocode_and_snap(origin_addr, G, poi_overrides, gazetteer=gazetteer)
    end = geocode_and_snap(dest_addr, G, poi_overrides, gazetteer=gazetteer)
    
    if not start:
        return {"error": f"Could not geocode origin: {origin_addr}"}
    if not end:
        return {"error": f"Could not geocode destination: {dest_addr}"}
        
    start_lat, start_lon, start_node = start
    end_lat, end_lon, end_node = end
    
    # 3. Simple A->B shortest route (no constraints in this basic helper)
    route_nodes = get_route(G, (start_lat, start_lon), (end_lat, end_lon), 
                            orig_node=start_node, dest_node=end_node)
    
    if not route_nodes:
        return {"error": "No route found between points."}
        
    # 4. Generate the navigation call
    steps = generate_call(G, route_nodes)
    
    return {
        "origin": origin_addr,
        "destination": dest_addr,
        "start_coords": [start_lat, start_lon],
        "end_coords": [end_lat, end_lon],
        "route_nodes": route_nodes,
        "steps": steps
    }
