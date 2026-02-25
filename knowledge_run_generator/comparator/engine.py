import numpy as np
from shapely.geometry import LineString
from shapely.ops import nearest_points
import math

def calculate_hausdorff_distance(line1, line2):
    """
    Calculate the Hausdorff distance between two LineStrings.
    This is the maximum distance from any point on line1 to line2.
    """
    return line1.hausdorff_distance(line2)

def calculate_average_proximity(line1, line2, samples=100):
    """
    Sample points along line1 and find their distance to the nearest point on line2.
    Return the average distance.
    """
    distances = []
    for i in range(samples + 1):
        point = line1.interpolate(i / samples, normalized=True)
        # Distance to the entire other line
        dist = point.distance(line2)
        distances.append(dist)
    return np.mean(distances)

def calculate_path_metrics(generated_coords, reference_coords):
    """
    generated_coords: list of [lon, lat]
    reference_coords: list of [lon, lat]
    
    Returns a dict of metrics.
    """
    if not generated_coords or not reference_coords:
        return {"error": "Missing coordinates"}
        
    g_line = LineString(generated_coords)
    r_line = LineString(reference_coords)
    
    # Degrees to meters roughly (London is ~51.5N)
    # 1 deg lat ~ 111km
    # 1 deg lon ~ 111km * cos(51.5) ~ 69km
    
    hausdorff = calculate_hausdorff_distance(g_line, r_line)
    avg_prox = calculate_average_proximity(g_line, r_line)
    
    g_len = g_line.length
    r_len = r_line.length
    
    return {
        "hausdorff_deg": hausdorff,
        "avg_proximity_deg": avg_prox,
        "length_ratio": g_len / r_len if r_len > 0 else 0,
        "generated_len_deg": g_len,
        "reference_len_deg": r_len
    }

def _haversine(lat1, lon1, lat2, lon2):
    R = 6371000  # meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))
