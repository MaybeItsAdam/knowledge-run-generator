import networkx as nx
import math

def calculate_bearing(lat1, lon1, lat2, lon2):
    """
    Calculate the bearing between two points.
    """
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    initial_bearing = math.atan2(x, y)
    initial_bearing = math.degrees(initial_bearing)
    compass_bearing = (initial_bearing + 360) % 360
    return compass_bearing

def get_turn_instruction(bearing_diff):
    """
    Determine the turn direction based on bearing change.
    """
    # Normalize to -180 to 180
    if bearing_diff > 180:
        bearing_diff -= 360
    if bearing_diff <= -180:
        bearing_diff += 360
        
    if -45 <= bearing_diff <= 45:
        return "Forward"
    elif -135 < bearing_diff < -45:
        return "Turn Left"
    elif 45 < bearing_diff < 135:
        return "Turn Right"
    else:
        # U-turn or sharp turn, simplifying to Turn
        return "Turn"

def generate_call(G, route_nodes, landmarks_gdf=None):
    """
    Generate structured navigation steps for the route.
    Returns a list of dictionaries with:
    - instruction: Text description
    - distance: Distance for this step (meters)
    - name: Street name
    - location: [lon, lat] of the step start
    """
    if not route_nodes or len(route_nodes) < 2:
        return []

    steps = []
    
    # helper to fetch node coords
    def get_coords(n):
        node = G.nodes[n]
        return [node['x'], node['y']]

    # 1. Start Step
    u, v = route_nodes[0], route_nodes[1]
    edge_data = G.get_edge_data(u, v)[0]
    current_street = edge_data.get('name', 'Unknown Road')
    if isinstance(current_street, list): 
        current_street = current_street[0]
        
    current_step = {
        "instruction": f"Leave Origin on {current_street}",
        "name": current_street,
        "location": get_coords(u),
        "distance": 0.0 
    }
    
    # We accumulate distance for the current step until a turn happens
    step_accumulated_distance = edge_data.get('length', 0)

    # Iterate through the route to find turns
    for i in range(1, len(route_nodes) - 1):
        u = route_nodes[i]
        v = route_nodes[i+1]
        
        # Helper to get edge data safely
        curr_edge = G.get_edge_data(route_nodes[i-1], u)[0]
        next_edge = G.get_edge_data(u, v)[0]
        
        curr_name = curr_edge.get('name', 'Unknown Road')
        next_name = next_edge.get('name', 'Unknown Road')
        
        if isinstance(curr_name, list): curr_name = curr_name[0]
        if isinstance(next_name, list): next_name = next_name[0]
        
        edge_len = next_edge.get('length', 0)

        # distinct street name change usually implies a new instruction (turn or forward onto new street)
        if curr_name != next_name:
            # 1. Finish the previous step
            current_step['distance'] = round(step_accumulated_distance, 1)
            steps.append(current_step)
            
            # 2. Calculate turn for the NEW step
            p_ids = route_nodes[i-1]
            p_node = G.nodes[p_ids]
            c_node = G.nodes[u]
            n_node = G.nodes[v]
            
            bearing_in = calculate_bearing(p_node['y'], p_node['x'], c_node['y'], c_node['x'])
            bearing_out = calculate_bearing(c_node['y'], c_node['x'], n_node['y'], n_node['x'])
            
            diff = bearing_out - bearing_in
            turn = get_turn_instruction(diff)
            
            instr_text = f"Forward {next_name}" if turn == "Forward" else f"{turn} into {next_name}"
            
            # 3. Start new step
            current_step = {
                "instruction": instr_text,
                "name": next_name,
                "location": get_coords(u),
                "distance": 0.0 # Will accumulate
            }
            step_accumulated_distance = edge_len
        else:
            # Same street, just accumulate distance
            step_accumulated_distance += edge_len

    # Add the final accumulated step (the last leg)
    current_step['distance'] = round(step_accumulated_distance, 1)
    steps.append(current_step)
    
    # Add Arrival Step
    last_node = route_nodes[-1]
    steps.append({
        "instruction": "Arrive Destination",
        "name": "Destination",
        "location": get_coords(last_node),
        "distance": 0
    })
    
    return steps
