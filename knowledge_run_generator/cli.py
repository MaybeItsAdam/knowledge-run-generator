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
    completeness = data.get("_completeness")
    # Per-run records are keyed by stringified ids; drop the "_completeness"
    # sentinel (and anything else non-numeric) from the tallies.
    data = {k: v for k, v in data.items() if k.lstrip("-").isdigit()}
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
    if completeness:
        miss = completeness.get("missing_ids") or []
        click.echo(
            f"  completeness: {completeness.get('present')}/{completeness.get('expected')}"
            + (f"  MISSING {miss}" if miss else "  ✓")
        )
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
# diagnose  (per-run edge-by-edge dump for routing artefact triage)
# ---------------------------------------------------------------------------

@cli.command("diagnose")
@click.argument("run_id", type=int)
@click.option("--runs", "-r", type=click.Path(exists=True, dir_okay=False),
              default="constants/runPoints.json",
              help="Path to runPoints.json from the pipeline.")
@click.option("--direction", type=click.Choice(["forward", "reverse"]),
              default="forward",
              help="Which direction's route to inspect.")
@click.option("--full/--rings-only", default=False,
              help="Print every edge (default: only ring traversals + context).")
@click.option("--context", type=int, default=3,
              help="Edges of surrounding context to print around each ring.")
def diagnose(run_id, runs, direction, full, context):
    """Walk a run's node sequence edge-by-edge and report ring traversals.

    Reads the node sequence written by the pipeline and looks each edge up
    in the live graph. Lap artefacts (>50% of a roundabout's perimeter
    traversed) are flagged with ``<<< LAP`` in the output, which is the
    quick triage signal for things like Run 4's BFI IMAX roundabout bug.
    """
    import krg
    from knowledge_run_generator.diagnostics import diagnose as run_diagnose

    session = krg.Session()
    report = run_diagnose(
        Path(runs), run_id, session.graph,
        direction=direction,
        only_rings=not full,
        context_edges=context,
    )
    click.echo(report)


# ---------------------------------------------------------------------------
# osm-pois  (Quick Win 8 — harvest gazetteer seed data from OSM)
# ---------------------------------------------------------------------------

@cli.command("osm-pois")
@click.option("--cache", type=click.Path(dir_okay=False), default=None,
              help="Where to write the harvested POI dict "
                   "(default: constants/osm_pois.json).")
@click.option("--force", is_flag=True,
              help="Ignore the cache and re-query Overpass.")
@click.option("--bbox", nargs=4, type=float, default=None,
              help="south west north east (defaults to Greater London).")
def osm_pois(cache, force, bbox):
    """Fetch named POIs from OpenStreetMap into a gazetteer-ready JSON file."""
    from knowledge_run_generator.osm_pois import (
        LONDON_BBOX, fetch_pois, kind_counts,
    )

    _repo, _scripts, constants = _repo_paths()
    cache = cache or str(constants / "osm_pois.json")
    Path(cache).parent.mkdir(parents=True, exist_ok=True)
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


# ---------------------------------------------------------------------------
# generate  (standalone data generation: runs + POIs)
# ---------------------------------------------------------------------------
#
# krg is self-contained: it never reaches into a consumer project. A consumer
# (e.g. an app) points it at its own output location via ``--out-dir`` /
# ``--output`` and supplies secrets via ``--token`` / ``--env-file``.

def _repo_paths():
    repo = Path(__file__).resolve().parents[1]
    scripts = repo / "scripts"
    if not scripts.is_dir():
        # `krg generate` shells out to scripts/ next to the package, which only
        # exists in a source checkout (or an editable install). A plain
        # `pip install krg` puts the package in site-packages with no scripts/.
        raise click.ClickException(
            f"{scripts} not found. `krg generate` needs the repository "
            "checkout (clone it and `pip install -e .`)."
        )
    return repo, scripts, repo / "constants"


def _parse_id_spec(spec: str) -> set[int]:
    """Parse a run-id selector.

      ``"12"``            -> {12}
      ``"1-20"``          -> {1..20}
      ``"5,8,13"``        -> {5, 8, 13}
      ``"1-20,50,60-65"`` -> any combination of the above
    """
    ids: set[int] = set()
    try:
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
                if a > b:
                    a, b = b, a
                ids.update(range(a, b + 1))
            else:
                ids.add(int(part))
    except ValueError:
        raise click.ClickException(
            f"Invalid run selector {spec!r}. Use e.g. 12, 1-20, 5,8,13, or 1-20,50,60-65."
        )
    return ids


def _run_script(scripts: Path, name: str, *args: str) -> None:
    import subprocess
    import sys
    cmd = [sys.executable, str(scripts / name), *args]
    click.echo(f"  $ {' '.join(cmd)}")
    if subprocess.run(cmd).returncode != 0:
        raise click.ClickException(f"{name} failed")


def _build_pois(scripts, constants, pdf, token, env_file, limit, enrich):
    """Extract -> geocode -> (optionally) enrich into constants/knowledge_pois.json."""
    extract_args = ["--out", str(constants / "extracted_pois.json")]
    if pdf:
        extract_args += ["--pdf", pdf]
    _run_script(scripts, "extract_pois.py", *extract_args)

    geo_args = [
        "--in", str(constants / "extracted_pois.json"),
        "--out", str(constants / "knowledge_pois.json"),
    ]
    if token:
        geo_args += ["--token", token]
    if env_file:
        geo_args += ["--env-file", env_file]
    if limit:
        geo_args += ["--limit", str(limit)]
    _run_script(scripts, "geocode_pois.py", *geo_args)

    if enrich:
        # Borough + yellow-badge sector enrichment lives in its own module so
        # this stays a no-op (with a warning) if it isn't present yet.
        try:
            from knowledge_run_generator.poi_enrichment import enrich_pois, missing_inputs
        except Exception as exc:  # noqa: BLE001
            click.echo(f"  [enrich] skipped (poi_enrichment unavailable: {exc})")
            return
        # The boundary/sector reference files are inputs we don't generate. A
        # missing one used to raise FileNotFoundError *after* the whole
        # geocode pass, throwing away the expensive part of the build; skip
        # with an actionable message instead.
        missing = missing_inputs()
        if missing:
            click.echo(
                "  [enrich] skipped: missing reference data "
                + ", ".join(str(p) for p in missing)
                + " (pass --no-enrich to silence)"
            )
            return
        path = constants / "knowledge_pois.json"
        pois = enrich_pois(json.loads(path.read_text()))
        path.write_text(json.dumps(pois, indent=2, ensure_ascii=False))
        click.echo(f"  [enrich] borough + yellow_badge_sector added to {len(pois)} POIs")


def _validate_outputs(constants, expected=320):
    """Gate the generated data set. Presence *and* usability.

    Counting ids was never enough: a run written with a two-position geometry
    between endpoints a mile apart satisfies a presence check and breaks every
    consumer downstream.
    """
    from knowledge_run_generator.validator import check_run_shape

    problems = []
    rp = constants / "runPoints.json"
    runs = json.loads(rp.read_text()) if rp.exists() else []
    present = {r.get("id") for r in runs}
    missing = [i for i in range(1, expected + 1) if i not in present]
    if missing:
        problems.append(f"runs incomplete: missing {missing}")

    malformed = {}
    for run in runs:
        shape_problems = check_run_shape(run)
        if shape_problems:
            malformed[run.get("id")] = shape_problems
    if malformed:
        sample = sorted(malformed)[:10]
        detail = "; ".join(f"{i}: {malformed[i][0]}" for i in sample)
        problems.append(
            f"{len(malformed)} run(s) structurally invalid ({detail}"
            + (" ..." if len(malformed) > len(sample) else "") + ")"
        )

    kp = constants / "knowledge_pois.json"
    pois = json.loads(kp.read_text()) if kp.exists() else []
    if not pois:
        problems.append("knowledge_pois.json empty or missing")
    return problems, len(present), len(pois)


def _mirror(constants, out_dir):
    import shutil
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name in ("runPoints.json", "knowledge_pois.json", "qa_report.json"):
        src = constants / name
        if src.exists():
            shutil.copy2(src, out / name)
            click.echo(f"  + {name} -> {out / name}")


@cli.group("generate")
def generate():
    """Generate the run + POI data sets."""


@generate.command("runs")
@click.argument("spec", required=False)
@click.option("--output", "-o", type=click.Path(dir_okay=False), default=None,
              help="Output runPoints.json (default: constants/runPoints.json).")
@click.option("--fresh", is_flag=True, help="Delete existing output before generating.")
@click.option("--geojson", is_flag=True, help="Also emit a routes.geojson alongside.")
def generate_runs(spec, output, fresh, geojson):
    """Generate Blue Book runs. SPEC selects which:

      \b
      (omitted)        all 320 runs
      12               just run 12
      1-20             runs 1 through 20
      5,8,13           runs 5, 8 and 13
      1-20,50,60-65    any combination
    """
    from knowledge_run_generator.blue_book_demo.run_pipeline import process_runs

    _repo, _scripts, constants = _repo_paths()
    out = Path(output) if output else constants / "runPoints.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    select_ids = _parse_id_spec(spec) if spec else None
    if fresh and select_ids is not None:
        # --fresh deletes the whole file; combined with a subset it would write
        # back only the selected runs and silently truncate the other ~300.
        # A subset already merges into the existing output via process_runs'
        # resume, so --fresh is only meaningful for a full rebuild.
        raise click.ClickException(
            "--fresh cannot be combined with a run subset (it would truncate the "
            "other runs). Drop --fresh to merge the subset, or omit SPEC to rebuild all."
        )
    if fresh and out.exists():
        out.unlink()
        qa = out.parent / "qa_report.json"
        if qa.exists():
            qa.unlink()

    if select_ids is not None:
        click.echo(f"Generating {len(select_ids)} run(s): {sorted(select_ids)}")
    else:
        click.echo("Generating all runs")
    process_runs(out, select_ids=select_ids, export_geojson=geojson)
    click.echo(f"\nOutput: {out}\nQA:     {out.parent / 'qa_report.json'}")


@generate.command("pois")
@click.option("--output", "-o", type=click.Path(dir_okay=False), default=None,
              help="Output knowledge_pois.json (default: constants/knowledge_pois.json).")
@click.option("--pdf", type=click.Path(dir_okay=False), default=None,
              help="Knowledge Points List PDF (default: extract_pois.py's default path).")
@click.option("--token", default=None, help="Mapbox access token (else env / --env-file).")
@click.option("--env-file", default=None,
              help="Dotenv holding MAPBOX_TOKEN= or EXPO_PUBLIC_MAPBOX_PK=.")
@click.option("--limit", type=int, default=None, help="Only geocode the first N points (debug).")
@click.option("--no-enrich", is_flag=True,
              help="Skip borough + yellow-badge sector enrichment.")
def generate_pois(output, pdf, token, env_file, limit, no_enrich):
    """Extract the Knowledge Points List, geocode it (Mapbox), and enrich each
    point with its London borough and yellow-badge sector."""
    _repo, scripts, constants = _repo_paths()
    constants.mkdir(parents=True, exist_ok=True)
    _build_pois(scripts, constants, pdf, token, env_file, limit, not no_enrich)
    if output:
        import shutil
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(constants / "knowledge_pois.json", output)
        click.echo(f"Output: {output}")
    else:
        click.echo(f"Output: {constants / 'knowledge_pois.json'}")


@generate.command("all")
@click.option("--out-dir", type=click.Path(file_okay=False), default=None,
              help="Also mirror the validated runPoints.json / knowledge_pois.json / "
                   "qa_report.json into this directory, keeping these filenames. To "
                   "promote into the-blue-app (which reads knowledgePois.json), use "
                   "scripts/promote_to_app.py instead.")
@click.option("--pdf", type=click.Path(dir_okay=False), default=None,
              help="Knowledge Points List PDF (default: extract_pois.py's default path).")
@click.option("--token", default=None, help="Mapbox access token (else env / --env-file).")
@click.option("--env-file", default=None,
              help="Dotenv holding MAPBOX_TOKEN= or EXPO_PUBLIC_MAPBOX_PK=.")
@click.option("--skip-pois", is_flag=True,
              help="Reuse the existing knowledge_pois.json; skip extract + geocode.")
@click.option("--skip-osm", is_flag=True,
              help="Skip the OSM gazetteer harvest (reuse the cache if present).")
@click.option("--resume", is_flag=True,
              help="Resume run generation instead of regenerating from scratch.")
@click.option("--no-enrich", is_flag=True,
              help="Skip borough + yellow-badge sector enrichment.")
def generate_all(out_dir, pdf, token, env_file, skip_pois, skip_osm, resume, no_enrich):
    """Rebuild every data set: POIs -> OSM gazetteer -> all 320 runs.

    With ``--out-dir`` the validated outputs are also copied there (only if all
    320 runs are present and the POI set is non-empty), which is how a consumer
    project pulls a fresh build into its own constants folder.
    """
    import time

    from knowledge_run_generator.blue_book_demo.run_pipeline import process_runs
    from knowledge_run_generator.osm_pois import LONDON_BBOX, fetch_pois

    _repo, scripts, constants = _repo_paths()
    constants.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    if not skip_pois:
        click.echo(f"\n{'='*60}\nPOIs: extract -> geocode -> enrich\n{'='*60}")
        _build_pois(scripts, constants, pdf, token, env_file, None, not no_enrich)
    else:
        click.echo("Skipping POIs (--skip-pois).")

    if not skip_osm:
        click.echo(f"\n{'='*60}\nOSM gazetteer harvest\n{'='*60}")
        fetch_pois(bbox=LONDON_BBOX, cache_path=constants / "osm_pois.json")
    else:
        click.echo("Skipping OSM harvest (--skip-osm).")

    click.echo(f"\n{'='*60}\nRuns: all 320 (turn restrictions fetched inside)\n{'='*60}")
    runs_out = constants / "runPoints.json"
    if not resume and runs_out.exists():
        click.echo(f"  Fresh build: removing {runs_out}")
        runs_out.unlink()
        qa = constants / "qa_report.json"
        if qa.exists():
            qa.unlink()
    process_runs(runs_out, limit=None)

    problems, nruns, npois = _validate_outputs(constants)
    click.echo(f"\nValidation: {nruns}/320 runs present, {npois} POIs")
    for p in problems:
        click.echo(f"  ! {p}")

    if out_dir and not problems:
        click.echo(f"\nCopying validated outputs -> {out_dir}")
        _mirror(constants, out_dir)

    click.echo(f"\nDone in {time.time() - t0:.0f}s.")

    # The completeness gate has to be a non-zero exit, not just a printed
    # warning: an incomplete build is exactly the failure this command exists
    # to catch, and CI/callers only see the status code.
    if problems:
        raise click.ClickException(
            ("Not copying to --out-dir: validation failed. " if out_dir else "Validation failed. ")
            + "Fix with `krg generate runs <missing>` / `krg generate pois`, then rerun."
        )


if __name__ == "__main__":
    cli()
