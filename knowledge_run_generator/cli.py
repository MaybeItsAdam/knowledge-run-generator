import click
from .geocoder import geocode_address
from .router import load_graph, get_route, plot_route, save_route_geojson
from .landmarks import get_landmarks_on_route
from .caller import generate_call

@click.command()
@click.argument('origin')
@click.argument('destination')
@click.option('--plot', '-p', default=None, help="Filepath to save the route visualization image.")
@click.option('--geojson', '-g', default=None, help="Filepath to save the route as GeoJSON.")
def cli(origin, destination, plot, geojson):
    """
    Generate a Knowledge run between ORIGIN and DESTINATION.
    """
    click.echo(f"Calculating run from '{origin}' to '{destination}'...")

    # 1. Geocode
    start_coords = geocode_address(origin)
    end_coords = geocode_address(destination)

    if not start_coords or not end_coords:
        click.echo("Error: Could not find coordinates for one or both locations.")
        return

    # 2. Load Graph
    G = load_graph()

    # 3. Get Route
    route = get_route(G, start_coords, end_coords)
    if not route:
        click.echo("Error: Could not calculate a route.")
        return

    # 4. Get Landmarks
    click.echo("Fetching landmarks along the route...")
    landmarks = get_landmarks_on_route(G, route)

    # 5. Generate Call
    instructions = generate_call(G, route, landmarks)

    # 6. Output
    click.echo("\n--- THE CALL ---\n")
    for step in instructions:
        click.echo(step)
    click.echo("\n----------------\n")

    if plot and route:
        plot_route(G, route, plot)

    if geojson and route:
        save_route_geojson(G, route, geojson)

if __name__ == '__main__':
    cli()
