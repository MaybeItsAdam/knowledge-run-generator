"""
Command-line interface for ``knowledge_run_generator``.

Installed as two entry points (both pointing at the same group):

    krg ...             # short form
    knowledge-run ...   # long form (retained for back-compat)

Run ``krg --help`` to see the available subcommands.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import click


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="krg")
def cli():
    """Knowledge of London run generator."""


# ---------------------------------------------------------------------------
# route  (Session-based)
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("origin")
@click.argument("destination")
@click.option("--via", "-v", multiple=True,
              help="Force a route via this street (repeatable).")
@click.option("--geojson", "-g", type=click.Path(dir_okay=False),
              default=None, help="Save the route as GeoJSON to this path.")
@click.option("--steps/--no-steps", default=True,
              help="Print turn-by-turn steps (The Call).")
def route(origin, destination, via, geojson, steps):
    """Generate a single run from ORIGIN to DESTINATION."""
    import krg

    click.echo(f"Routing: {origin!r} -> {destination!r}")
    session = krg.Session()
    run = session.run(origin, destination, via=list(via) or None)

    click.echo(
        f"  {run.distance_m:.0f} m  /  {run.duration_s:.0f} s  /  "
        f"{len(run.steps)} steps  /  preflight_ok={run.preflight_ok}"
    )

    for w in run.preflight_warnings:
        click.echo(f"  [warn] {w}", err=True)
    for r in run.preflight_reasons:
        click.echo(f"  [fail] {r}", err=True)

    if steps:
        click.echo()
        click.echo("--- THE CALL ---")
        for s in run.steps:
            text = s.get("instruction", "")
            dist = s.get("distance", 0)
            if dist:
                click.echo(f"  {text}  ({dist:.0f} m)")
            else:
                click.echo(f"  {text}")

    if geojson:
        run.save_geojson(geojson)
        click.echo(f"\nSaved GeoJSON -> {geojson}")


# ---------------------------------------------------------------------------
# run  (legacy pipeline — landmarks + generate_call)
# ---------------------------------------------------------------------------

@cli.command("run")
@click.argument("origin")
@click.argument("destination")
@click.option("--plot", "-p", default=None,
              help="File path to save route visualization image.")
@click.option("--geojson", "-g", default=None,
              help="File path to save route as GeoJSON.")
def run_command(origin, destination, plot, geojson):
    """Legacy: generate a run via the landmarks+caller pipeline."""
    from .caller import generate_call
    from .geocoder import geocode_address
    from .landmarks import get_landmarks_on_route
    from .router import get_route, load_graph, plot_route, save_route_geojson

    click.echo(f"Calculating run from '{origin}' to '{destination}'...")

    start_coords = geocode_address(origin)
    end_coords = geocode_address(destination)
    if not start_coords or not end_coords:
        click.echo("Error: Could not find coordinates for one or both locations.")
        return

    G = load_graph()
    route_nodes = get_route(G, start_coords, end_coords)
    if not route_nodes:
        click.echo("Error: Could not calculate a route.")
        return

    click.echo("Fetching landmarks along the route...")
    landmarks = get_landmarks_on_route(G, route_nodes)
    instructions = generate_call(G, route_nodes, landmarks)

    click.echo("\n--- THE CALL ---\n")
    for step in instructions:
        click.echo(step)
    click.echo("\n----------------\n")

    if plot:
        plot_route(G, route_nodes, plot)
    if geojson:
        save_route_geojson(G, route_nodes, geojson)


# ---------------------------------------------------------------------------
# web
# ---------------------------------------------------------------------------

@cli.command("web")
@click.option("--host", default=None, show_default=True,
              help="Host interface to bind (env: KRG_WEB_HOST).")
@click.option("--port", type=int, default=None, show_default=True,
              help="Port to bind (env: KRG_WEB_PORT).")
@click.option("--debug", is_flag=True, help="Enable Flask debug mode.")
@click.option("--blue-book-file", default=None,
              help="Path to Blue Book runPoints JSON file (env: KRG_BLUE_BOOK_FILE).")
def web_command(host, port, debug, blue_book_file):
    """Launch the standalone web app."""
    from .webapp import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, run_webapp

    host = host or os.environ.get("KRG_WEB_HOST", DEFAULT_WEB_HOST)
    port = port or int(os.environ.get("KRG_WEB_PORT", str(DEFAULT_WEB_PORT)))
    run_webapp(host=host, port=port, debug=debug, blue_book_file=blue_book_file)


# ---------------------------------------------------------------------------
# bluebookdemo
# ---------------------------------------------------------------------------

@cli.command("bluebookdemo")
@click.argument("number", type=int, required=False)
@click.option("--output", "-o", type=click.Path(dir_okay=False), default=None,
              help="Output path for runPoints.json "
                   "(default: constants/runPoints.json).")
@click.option("--geojson/--no-geojson", default=False,
              help="Also export a GeoJSON FeatureCollection.")
@click.option("--fresh", is_flag=True,
              help="Delete existing runPoints.json and start over.")
def bluebookdemo(number, output, geojson, fresh):
    """
    Generate the first NUMBER Blue Book runs.

    If NUMBER is omitted, processes all 320 runs.

    Examples:

      krg bluebookdemo 5            # first 5 runs
      krg bluebookdemo 20 --fresh   # delete existing output, redo first 20
      krg bluebookdemo --geojson    # all runs, also emit routes.geojson
    """
    from knowledge_run_generator.blue_book_demo.run_pipeline import process_runs

    out = Path(output) if output else Path("constants/runPoints.json")
    out.parent.mkdir(parents=True, exist_ok=True)

    if fresh and out.exists():
        click.echo(f"Removing existing {out}")
        out.unlink()
        qa = out.parent / "qa_report.json"
        if qa.exists():
            qa.unlink()

    process_runs(out, limit=number, export_geojson=geojson)
    click.echo(f"\nOutput: {out}")
    click.echo(f"QA:     {out.parent / 'qa_report.json'}")


# ---------------------------------------------------------------------------
# resolve-street / resolve-place
# ---------------------------------------------------------------------------

@cli.command("resolve-street")
@click.argument("name")
def resolve_street(name):
    """Canonicalise a street name via the alias index."""
    import krg
    session = krg.Session()
    canonical = session.resolve_street(name)
    if canonical is None:
        click.echo("NOT FOUND", err=True)
        raise SystemExit(1)
    click.echo(canonical)


@cli.command("resolve-place")
@click.argument("name")
def resolve_place(name):
    """Look up a POI and show where it will snap to on the graph."""
    import krg
    session = krg.Session()
    entry = session.resolve_place(name)
    if entry is None:
        click.echo("NOT FOUND", err=True)
        raise SystemExit(1)
    click.echo(json.dumps(dataclasses.asdict(entry), indent=2))


# ---------------------------------------------------------------------------
# qa
# ---------------------------------------------------------------------------

@cli.command("qa")
@click.option("--report", "-r", type=click.Path(exists=True, dir_okay=False),
              default="constants/qa_report.json",
              help="Path to qa_report.json.")
@click.option("--top", "-n", type=int, default=20,
              help="Number of failing runs to list.")
def qa(report, top):
    """Summarise the last pipeline run's qa_report.json."""
    data = json.loads(Path(report).read_text())
    total = len(data)
    passed = sum(1 for v in data.values() if v.get("passed"))
    preflight_fails = [k for k, v in data.items() if not v.get("preflight_ok", True)]
    directness_fails = [
        k for k, v in data.items()
        if v.get("preflight_ok", True) and v.get("is_direct") is False
    ]
    legality_fails = [
        k for k, v in data.items()
        if v.get("preflight_ok", True) and v.get("legal") is False
    ]

    click.echo(f"Total: {total}   Passed: {passed}   Failed: {total - passed}")
    click.echo(f"  preflight failures: {len(preflight_fails)}")
    click.echo(f"  directness failures: {len(directness_fails)}")
    click.echo(f"  legality failures:   {len(legality_fails)}")

    click.echo(f"\nTop {top} preflight failures:")
    for k in preflight_fails[:top]:
        reasons = data[k].get("preflight_reasons", [])
        click.echo(f"  Run {k}: {'; '.join(reasons) or '(no reasons)'}")

    click.echo(f"\nTop {top} directness failures:")
    for k in directness_fails[:top]:
        r = data[k]
        click.echo(
            f"  Run {k}: ratio={r.get('ratio')}  offset={r.get('max_offset_m')}m"
        )


# ---------------------------------------------------------------------------
# osm-pois  (Quick Win 8 — harvest gazetteer seed data from OSM)
# ---------------------------------------------------------------------------

@cli.command("osm-pois")
@click.option("--cache", type=click.Path(dir_okay=False),
              default="/tmp/app_cache/osm_pois.json",
              help="Cache path for the harvested POI dict.")
@click.option("--force", is_flag=True,
              help="Ignore the cache and re-query Overpass.")
@click.option("--bbox", nargs=4, type=float, default=None,
              help="south west north east (defaults to Greater London).")
def osm_pois(cache, force, bbox):
    """Fetch named POIs from OpenStreetMap into a gazetteer-ready JSON file."""
    from knowledge_run_generator.osm_pois import (
        LONDON_BBOX, fetch_pois, kind_counts,
    )

    target_bbox = tuple(bbox) if bbox else LONDON_BBOX
    click.echo(
        f"Harvesting OSM POIs (bbox={target_bbox}, force={force}, cache={cache}) ..."
    )
    pois = fetch_pois(bbox=target_bbox, cache_path=Path(cache), force_refresh=force)
    click.echo(f"Got {len(pois)} named POIs.")
    for kind, count in sorted(kind_counts(pois).items(),
                              key=lambda kv: (-kv[1], kv[0])):
        click.echo(f"  {kind:>10}: {count}")


# ---------------------------------------------------------------------------
# regression  (Phase 4 golden-set)
# ---------------------------------------------------------------------------

@cli.group("regression")
def regression():
    """Golden-set snapshot + diff for qa_report.json."""


@regression.command("snapshot")
@click.option("--report", "-r", type=click.Path(exists=True, dir_okay=False),
              default="constants/qa_report.json",
              help="Path to qa_report.json to freeze.")
@click.option("--out", "-o", type=click.Path(dir_okay=False),
              default="tests/golden/qa_baseline.json",
              help="Where to write the snapshot.")
def regression_snapshot(report, out):
    """Capture the current qa_report.json as the regression baseline."""
    from knowledge_run_generator.regression import summarise, save_snapshot

    snap = summarise(Path(report))
    save_snapshot(snap, Path(out))
    click.echo(
        f"Snapshot -> {out}  "
        f"(total={snap.total}, passed={snap.passed}, "
        f"preflight_fails={snap.preflight_fails}, "
        f"directness_fails={snap.directness_fails}, "
        f"legality_fails={snap.legality_fails})"
    )


@regression.command("diff")
@click.option("--baseline", "-b", type=click.Path(exists=True, dir_okay=False),
              default="tests/golden/qa_baseline.json",
              help="Path to the frozen baseline snapshot.")
@click.option("--report", "-r", type=click.Path(exists=True, dir_okay=False),
              default="constants/qa_report.json",
              help="Path to the current qa_report.json.")
@click.option("--strict/--no-strict", default=False,
              help="Exit non-zero if any regressions are present.")
def regression_diff(baseline, report, strict):
    """Show what changed since the baseline snapshot."""
    from knowledge_run_generator.regression import (
        summarise, load_snapshot, diff as diff_fn, format_diff,
    )

    base = load_snapshot(Path(baseline))
    curr = summarise(Path(report))
    result = diff_fn(base, curr)
    click.echo(format_diff(result))
    if strict and result.has_regressions:
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
