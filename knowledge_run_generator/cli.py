import os

import click

from .caller import generate_call
from .geocoder import geocode_address
from .landmarks import get_landmarks_on_route
from .router import get_route, load_graph, plot_route, save_route_geojson
from .webapp import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, run_webapp

WEB_HOST_DEFAULT = os.environ.get("KRG_WEB_HOST", DEFAULT_WEB_HOST)
WEB_PORT_DEFAULT = int(os.environ.get("KRG_WEB_PORT", str(DEFAULT_WEB_PORT)))


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli():
    """Knowledge Run Generator CLI."""


@cli.command("run")
@click.argument("origin")
@click.argument("destination")
@click.option("--plot", "-p", default=None, help="File path to save route visualization image.")
@click.option("--geojson", "-g", default=None, help="File path to save route as GeoJSON.")
def run_command(origin, destination, plot, geojson):
    """Generate a Knowledge run between ORIGIN and DESTINATION."""
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


@cli.command("web")
@click.option(
    "--host",
    default=WEB_HOST_DEFAULT,
    show_default=True,
    help="Host interface to bind (env: KRG_WEB_HOST).",
)
@click.option(
    "--port",
    type=int,
    default=WEB_PORT_DEFAULT,
    show_default=True,
    help="Port to bind (env: KRG_WEB_PORT).",
)
@click.option("--debug", is_flag=True, help="Enable Flask debug mode.")
@click.option(
    "--blue-book-file",
    default=None,
    help="Path to Blue Book runPoints JSON file (env: KRG_BLUE_BOOK_FILE).",
)
def web_command(host, port, debug, blue_book_file):
    """Launch the standalone web app."""
    run_webapp(host=host, port=port, debug=debug, blue_book_file=blue_book_file)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("origin")
@click.argument("destination")
@click.option("--plot", "-p", default=None, help="File path to save route visualization image.")
@click.option("--geojson", "-g", default=None, help="File path to save route as GeoJSON.")
def legacy_cli(origin, destination, plot, geojson):
    """Legacy command entrypoint for knowledge-run."""
    run_command.callback(origin, destination, plot, geojson)


if __name__ == "__main__":
    cli()
