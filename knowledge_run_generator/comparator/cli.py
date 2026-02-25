import click
import json
import os
from pathlib import Path
import webbrowser
from .engine import calculate_path_metrics
from .report import create_side_by_side_report, create_split_viewer

@click.group()
def cli():
    """Comparison framework for Knowledge of London runs."""
    pass

@cli.command()
@click.argument('run_id', type=int)
@click.option('--reference', '-r', type=click.Path(exists=True), help='Path to reference GeoJSON file.')
@click.option('--output', '-o', type=click.Path(), default='comparison_report.html', help='Path to save HTML report.')
def compare(run_id, reference, output):
    """Compare a generated run with a reference GeoJSON."""
    
    # 1. Load generated run
    generated_path = Path("constants/runPoints.json")
    if not generated_path.exists():
        click.echo(f"Error: {generated_path} not found.")
        return
        
    with open(generated_path, 'r') as f:
        all_runs = json.load(f)
        
    gen_run_data = next((r for r in all_runs if r['id'] == run_id), None)
    if not gen_run_data:
        click.echo(f"Error: Run {run_id} not found in constants/runPoints.json")
        return

    gen_run = {
        "id": gen_run_data.get("id"),
        "title": gen_run_data.get("title"),
        "geometry": gen_run_data.get("route", {}).get("geometry")
    }
    
    if not gen_run["geometry"]:
        click.echo(f"Error: Run {run_id} in runPoints.json has no valid geometry.")
        return
        
    # 2. Load reference run
    if not reference:
        # Check default data directory
        data_dir = Path(__file__).parent / "data"
        ref_path = data_dir / f"{run_id}.json"
        if not ref_path.exists():
            click.echo(f"Error: No reference found for Run {run_id}. Provide one with --reference.")
            return
        reference = ref_path
        
    with open(reference, 'r') as f:
        ref_data = json.load(f)
        # Handle different GeoJSON structures
        if 'geometry' not in ref_data:
            # If it's just a raw list of coords or a SimpleFeature
            if isinstance(ref_data, list):
                ref_data = {"geometry": {"coordinates": ref_data}}
            elif 'coordinates' in ref_data:
                 ref_data = {"geometry": ref_data}
            else:
                click.echo("Error: Invalid reference format. Need GeoJSON or list of coordinates.")
                return

    # 3. Calculate metrics
    metrics = calculate_path_metrics(
        gen_run['geometry']['coordinates'],
        ref_data['geometry']['coordinates']
    )
    
    # 4. Generate report
    report_abspath = create_side_by_side_report(gen_run, ref_data, metrics, output)
    
    click.echo(f"Comparison complete for Run {run_id}:")
    click.echo(f"  Hausdorff Distance: {metrics['hausdorff_deg']:.6f}")
    click.echo(f"  Avg Proximity:      {metrics['avg_proximity_deg']:.6f}")
    click.echo(f"  Length Ratio:       {metrics['length_ratio']:.3f}")
    click.echo(f"\nReport saved to: {report_abspath}")

@cli.command()
@click.argument('run_ids', nargs=-1, type=int)
@click.option('--browser/--no-browser', default=True, help='Automatically open in browser.')
def view(run_ids, browser):
    """View one or more runs in a side-by-side browser comparison."""
    if not run_ids:
        click.echo("Error: Provide at least one Run ID.")
        return

    # 1. Load generated runs
    generated_path = Path("constants/runPoints.json")
    if not generated_path.exists():
        click.echo(f"Error: {generated_path} not found.")
        return
    with open(generated_path, 'r') as f:
        all_runs = json.load(f)

    # 2. Load KOL map mapping
    maps_path = Path(__file__).parent / "data" / "kol_maps.json"
    if not maps_path.exists():
        click.echo(f"Error: {maps_path} not found. Run the scraper first.")
        return
    with open(maps_path, 'r') as f:
        kol_maps = json.load(f)

    for run_id in run_ids:
        gen_run_data = next((r for r in all_runs if r['id'] == run_id), None)
        if not gen_run_data:
            click.echo(f"Run {run_id}: Not found in runPoints.json")
            continue

        gen_run = {
            "id": gen_run_data.get("id"),
            "title": gen_run_data.get("title"),
            "geometry": gen_run_data.get("route", {}).get("geometry")
        }
        
        ideal_url = kol_maps.get(str(run_id)) or kol_maps.get(run_id)
        if not ideal_url:
            click.echo(f"Run {run_id}: No ideal map found in KOL mapping.")
            continue

        output_file = f"comparison_run_{run_id}.html"
        report_path = create_split_viewer(gen_run, ideal_url, output_file)
        
        click.echo(f"Comparison view created for Run {run_id}: {report_path}")
        if browser:
            webbrowser.open(f"file://{report_path}")

if __name__ == '__main__':
    cli()
