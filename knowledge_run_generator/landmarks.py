import osmnx as ox
from shapely.geometry import LineString, Point
import geopandas as gpd

def get_landmarks_on_route(G, route_nodes, buffer_dist=25):
    """
    Fetch POIs along the route within a specified buffer distance.
    """
    if not route_nodes or len(route_nodes) < 2:
        return gpd.GeoDataFrame()

    # Create a LineString from the route nodes
    node_points = [Point(G.nodes[n]['x'], G.nodes[n]['y']) for n in route_nodes]
    route_line = LineString(node_points)
    
    # Create a buffer around the route (approximate meters conversion if simpler, 
    # but strictly ox works in degrees if unprojected. 
    # For robust meters buffering, we should project the graph, but for simplicity 
    # and speed in this specific task, we can use a rough degree approximation 
    # or project the line. 0.00025 degrees is roughly 25-30 meters at London latitude).
    
    # 1 degree lat ~= 111km. 25m ~= 0.000225 degrees.
    buffer_degrees = 0.00025
    route_buffer = route_line.buffer(buffer_degrees)

    # Define the tags we are interested in for "The Knowledge"
    # Focusing on distinct landmarks.
    tags = {
        'amenity': ['theatre', 'place_of_worship', 'courthouse', 'police', 'embassy', 'townhall', 'university', 'prison'],
        'tourism': ['hotel', 'museum', 'attraction', 'gallery'],
        'historic': ['memorial', 'monument', 'castle'],
        'building': ['government', 'public']
    }

    try:
        # Fetch features within the polygon
        pois = ox.features.features_from_polygon(route_buffer, tags)
        
        # If no POIs found, return empty GDF
        if pois.empty:
             return gpd.GeoDataFrame()
             
        # Filter to keep only relevant columns if needed, or just return all
        cols = ['name', 'amenity', 'tourism', 'historic', 'geometry']
        existing_cols = [c for c in cols if c in pois.columns]
        return pois[existing_cols]

    except Exception as e:
        print(f"Error fetching landmarks: {e}")
        return gpd.GeoDataFrame()
