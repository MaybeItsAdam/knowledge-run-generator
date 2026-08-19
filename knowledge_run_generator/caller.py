import networkx as nx
import math

from .router import _best_edge_data


def _edge(G, u, v):
    """The shortest parallel edge between u and v.

    Key 0 is not always the shortest of a parallel bundle; picking it made the
    step distances disagree with the route total reported by
    ``_extract_route_metadata``, which uses the shortest.
    """
    return _best_edge_data(G.get_edge_data(u, v)) or {}


def _first_tag(value):
    """OSM tags arrive as a list when a way carries several values."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value or None


def resolve_street_name(edge_data):
    """The name a driver would call this edge, or None if it has none.

    OSM leaves ~4% of drivable London edges without a ``name`` — overwhelmingly
    the short slip roads, roundabout arms and junction connectors that stitch
    named streets together (median length 30 m). Labelling those "Unknown Road"
    was actively harmful: it emitted steps no examiner would accept, and every
    one of them collapsed into a single "unknown road" node in the app's
    connection graph, giving it degree 293 when the busiest real road in London
    has 28. Returning None instead lets ``generate_call`` absorb the edge into
    the step around it.

    ``ref`` is checked before giving up because a signed route number ("A501")
    is what the driver actually sees on the plate.
    """
    name = _first_tag(edge_data.get('name'))
    if name:
        return name
    return _first_tag(edge_data.get('ref'))


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

    # helper to fetch node coords
    def get_coords(n):
        node = G.nodes[n]
        return [node['x'], node['y']]

    # Resolve every leg up front; legs[i] covers route_nodes[i] -> [i+1].
    legs = []
    for i in range(len(route_nodes) - 1):
        data = _edge(G, route_nodes[i], route_nodes[i + 1])
        legs.append((resolve_street_name(data), data.get('length', 0) or 0))

    # The call opens on the first street that has a name — beginning it on an
    # unnamed forecourt or slip road tells the learner nothing.
    current_name = next((name for name, _ in legs if name), None) or 'Unknown Road'

    steps = []
    current_step = {
        "instruction": f"Leave Origin on {current_name}",
        "name": current_name,
        "location": get_coords(route_nodes[0]),
        "distance": 0.0
    }

    # We accumulate distance for the current step until a turn happens
    step_accumulated_distance = legs[0][1]

    # Iterate through the route to find turns
    for i in range(1, len(route_nodes) - 1):
        next_name, edge_len = legs[i]

        # An unnamed leg never earns its own step: its distance belongs to the
        # street the driver is still nominally on. Same for staying put on the
        # current street.
        if next_name is None or next_name == current_name:
            step_accumulated_distance += edge_len
            continue

        # 1. Finish the previous step
        current_step['distance'] = round(step_accumulated_distance, 1)
        steps.append(current_step)

        # 2. Calculate turn for the NEW step
        p_node = G.nodes[route_nodes[i - 1]]
        c_node = G.nodes[route_nodes[i]]
        n_node = G.nodes[route_nodes[i + 1]]

        bearing_in = calculate_bearing(p_node['y'], p_node['x'], c_node['y'], c_node['x'])
        bearing_out = calculate_bearing(c_node['y'], c_node['x'], n_node['y'], n_node['x'])

        turn = get_turn_instruction(bearing_out - bearing_in)

        instr_text = f"Forward {next_name}" if turn == "Forward" else f"{turn} into {next_name}"

        # 3. Start new step
        current_step = {
            "instruction": instr_text,
            "name": next_name,
            "location": get_coords(route_nodes[i]),
            "distance": 0.0  # Will accumulate
        }
        current_name = next_name
        step_accumulated_distance = edge_len

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
