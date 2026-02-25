import folium
from folium import plugins
import json
import os

def create_side_by_side_report(generated_run, reference_run, metrics, output_path):
    """
    generated_run: dict with 'geometry' (GeoJSON) and 'id', 'title'
    reference_run: dict with 'geometry' (GeoJSON)
    metrics: dict from engine.calculate_path_metrics
    output_path: path to save the HTML
    """
    
    # Create the base map for both
    # We'll create two separate maps and then embed them in a custom HTML
    
    def create_map(run_data, color='blue', title="Run"):
        coords = run_data['geometry']['coordinates']
        # GeoJSON is [lon, lat], Folium is [lat, lon]
        path_coords = [[c[1], c[0]] for c in coords]
        
        m = folium.Map(location=path_coords[0], zoom_start=14, tiles="cartodbpositron")
        folium.PolyLine(path_coords, color=color, weight=5, opacity=0.8, popup=title).add_to(m)
        
        # Add start/end markers
        folium.Marker(path_coords[0], popup="Start", icon=folium.Icon(color="green")).add_to(m)
        folium.Marker(path_coords[-1], popup="End", icon=folium.Icon(color="red")).add_to(m)
        
        return m

    m1 = create_map(generated_run, color='blue', title="Generated")
    m2 = create_map(reference_run, color='red', title="Reference")
    
    # Save them to temporary strings
    map1_html = m1._repr_html_()
    map2_html = m2._repr_html_()
    
    html_template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Run {generated_run.get('id')} Comparison</title>
        <style>
            body {{ font-family: sans-serif; margin: 20px; }}
            .container {{ display: flex; gap: 20px; }}
            .map-box {{ flex: 1; border: 1px solid #ccc; }}
            .metrics {{ background: #f9f9f9; padding: 15px; border-radius: 8px; margin-bottom: 20px; }}
            h2 {{ margin-top: 0; }}
            .metric-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }}
            .metric-item {{ border-left: 4px solid #3498db; padding-left: 10px; }}
            .metric-label {{ font-size: 0.8em; color: #666; }}
            .metric-value {{ font-size: 1.2em; font-weight: bold; }}
        </style>
    </head>
    <body>
        <h1>Comparison: {generated_run.get('title', 'Unknown Run')}</h1>
        
        <div class="metrics">
            <h2>Metrics</h2>
            <div class="metric-grid">
                <div class="metric-item">
                    <div class="metric-label">Hausdorff Distance (deg)</div>
                    <div class="metric-value">{metrics.get('hausdorff_deg', 0):.6f}</div>
                </div>
                <div class="metric-item">
                    <div class="metric-label">Avg Proximity (deg)</div>
                    <div class="metric-value">{metrics.get('avg_proximity_deg', 0):.6f}</div>
                </div>
                <div class="metric-item">
                    <div class="metric-label">Length Ratio (Gen/Ref)</div>
                    <div class="metric-value">{metrics.get('length_ratio', 0):.3f}</div>
                </div>
            </div>
        </div>
        
        <div class="container">
            <div class="map-box">
                <h3 style="text-align:center">Generated (Blue)</h3>
                {map1_html}
            </div>
            <div class="map-box">
                <h3 style="text-align:center">Reference (Red)</h3>
                {map2_html}
            </div>
        </div>
    </body>
    </html>
    """
    
    with open(output_path, "w") as f:
        f.write(html_template)
    
    return os.path.abspath(output_path)

def create_split_viewer(generated_run, ideal_url, output_path):
    """
    generated_run: dict with 'geometry' (GeoJSON) and 'id', 'title'
    ideal_url: URL to the OS Maps or KOL map
    output_path: path to save the HTML
    """
    
    def create_map(run_data, color='blue', title="Run"):
        coords = run_data['geometry']['coordinates']
        path_coords = [[c[1], c[0]] for c in coords]
        m = folium.Map(location=path_coords[0], zoom_start=14, tiles="cartodbpositron")
        folium.PolyLine(path_coords, color=color, weight=5, opacity=0.8, popup=title).add_to(m)
        folium.Marker(path_coords[0], popup="Start", icon=folium.Icon(color="green")).add_to(m)
        folium.Marker(path_coords[-1], popup="End", icon=folium.Icon(color="red")).add_to(m)
        return m

    m = create_map(generated_run)
    map_html = m._repr_html_()
    
    html_template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Run {generated_run.get('id')} Split View</title>
        <style>
            body, html {{ margin: 0; padding: 0; height: 100%; overflow: hidden; font-family: sans-serif; }}
            .header {{ height: 50px; background: #0019a8; color: white; display: flex; align-items: center; padding: 0 20px; justify-content: space-between; }}
            .container {{ display: flex; height: calc(100% - 50px); }}
            .pane {{ flex: 1; border: none; height: 100%; position: relative; }}
            .pane-left {{ border-right: 2px solid #fff; }}
            .pane-title {{ position: absolute; top: 10px; left: 50%; transform: translateX(-50%); z-index: 1000; background: rgba(255,255,255,0.8); padding: 5px 15px; border-radius: 20px; font-weight: bold; border: 1px solid #ccc; }}
            iframe {{ width: 100%; height: 100%; border: none; }}
            .overlay-msg {{ 
                position: absolute; top: 0; left: 0; width: 100%; height: 100%; 
                background: #f0f0f0; display: flex; flex-direction: column; 
                justify-content: center; align-items: center; text-align: center; padding: 20px;
                box-sizing: border-box;
            }}
            .btn {{ background: #0019a8; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; margin-top: 10px; display: inline-block; }}
        </style>
    </head>
    <body>
        <div class="header">
            <span>Run {generated_run.get('id')}: {generated_run.get('title')}</span>
            <span style="font-size: 0.8em;">Side-by-Side Comparison</span>
        </div>
        <div class="container">
            <div class="pane pane-left">
                <div class="pane-title">Generated Route</div>
                {map_html}
            </div>
            <div class="pane">
                <div class="pane-title">Ideal Route (Knowledge of London)</div>
                <iframe src="{ideal_url}" id="ideal-frame"></iframe>
                <div id="fallback" class="overlay-msg" style="display:none;">
                    <p>OS Maps might block embedding for security reasons.</p>
                    <a href="{ideal_url}" target="_blank" class="btn">Open Ideal Map in New Tab</a>
                </div>
            </div>
        </div>
        <script>
            // Note: We can't easily detect iframe blocking from JS due to same-origin policy,
            // but we can suggest the button if the user doesn't see anything.
            setTimeout(() => {{
                // Show fallback link after 3 seconds just in case
                document.getElementById('fallback').style.display = 'flex';
                document.getElementById('fallback').style.opacity = '0.9';
                document.getElementById('ideal-frame').style.opacity = '0.1';
            }}, 5000);
        </script>
    </body>
    </html>
    """
    
    with open(output_path, "w") as f:
        f.write(html_template)
    
    return os.path.abspath(output_path)
