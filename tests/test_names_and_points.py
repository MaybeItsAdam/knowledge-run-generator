"""
Run names and endpoint points.

Names: TfL Annex B is the canonical run list, so the numbers, names, districts
and order of every generated run are Annex B's.

Points: one test per root cause behind the misplaced endpoints the app's
independent verifier found (reports/run-verification.md in the-blue-app):

  * district centres geocoded by Mapbox ("W1" answered in Kennington), which
    collapsed dozens of W1 and N7 streets onto one point;
  * geocoder results accepted without checking they carried the name asked
    for ("Buckingham Palace" answered by Buckingham Palace Road);
  * a machine geocode shared by two different names accepted as a place;
  * street names resolved to rooftop geocodes instead of the street;
  * stations resolved to Points List guesses (Holland Park on Latimer Road)
    instead of OSM's railway=station, or to the park of the same name;
  * area endpoints snapped to the road nearest their centre, and two-way
    trunk roads refused as set-down roads, stranding stations 55 to 210 m
    from a node;
  * no build guard against two different names on one coordinate.
"""

import importlib.util
import json
import unittest
from pathlib import Path

import networkx as nx

from knowledge_run_generator.aliases import build_alias_index
from knowledge_run_generator.annex_b import (
    ANNEX_B_PATH,
    RUN_COUNT,
    annex_titles,
    format_point,
    geocode_name,
    load_annex_b,
    parse_annex_b,
)
from knowledge_run_generator.endpoint_guard import (
    check_endpoint_collisions,
    check_run_collisions,
    check_runs_match_annex_b,
)
from knowledge_run_generator.gazetteer import (
    Gazetteer,
    _dual_carriageway_edges,
    _dual_cache,
    _haversine,
    _node_is_routable,
    drop_shared_coordinates,
    kerb_snap,
    load_district_centres,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = REPO_ROOT / "knowledge_run_generator" / "blue_book_demo"


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Names: TfL Annex B
# ---------------------------------------------------------------------------


class AnnexBTests(unittest.TestCase):
    def test_vendored_annex_b_parses_to_320_runs_in_order(self):
        runs = load_annex_b()
        self.assertEqual(len(runs), RUN_COUNT)
        self.assertEqual([r.id for r in runs], list(range(1, RUN_COUNT + 1)))
        self.assertEqual(runs[0].title, "MANOR HOUSE STATION N4 to GIBSON SQUARE N1")
        self.assertEqual(runs[-1].id, 320)
        self.assertEqual({r.list for r in runs}, set(range(1, 21)))

    def test_tfl_wins_where_the_anki_export_differs(self):
        titles = annex_titles()
        self.assertEqual(titles[171][0], "UNION ROAD SW8")        # Anki: LAMBETH COLLEGE SW8
        self.assertEqual(titles[174][0], "GROSVENOR SQUARE W1")   # Anki: AMERICAN EMBASSY W1
        self.assertEqual(titles[102][0], "BETHNAL GREEN STATION E2")  # Anki: ... B_R STATION

    def test_point_formatting(self):
        self.assertEqual(format_point("St. John’s Wood Station", "NW8"), "ST JOHN'S WOOD STATION NW8")
        self.assertEqual(format_point("B.B.C. Television Centre", "W12"), "BBC TELEVISION CENTRE W12")
        self.assertEqual(format_point("Hanover Gate, Regent’s Park", "NW1"), "HANOVER GATE, REGENT'S PARK NW1")

    def test_full_stop_before_the_district_is_accepted(self):
        # The PDF writes "Sadler’s Wells Theatre. EC1" twice.
        runs = load_annex_b()
        names = {r.end.display for r in runs} | {r.start.display for r in runs}
        self.assertIn("SADLER'S WELLS THEATRE EC1", names)

    def test_parser_rejects_a_short_or_garbled_list(self):
        text = ANNEX_B_PATH.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            parse_annex_b(text.replace("\n3 Chancery Lane Station, WC1 to Rolls Road, SE1", ""))
        with self.assertRaises(ValueError):
            parse_annex_b(text.replace("3 Chancery Lane Station, WC1 to Rolls Road, SE1",
                                       "3 Chancery Lane Station WC1 Rolls Road SE1"))

    def test_the_pipeline_takes_its_titles_from_annex_b(self):
        from knowledge_run_generator.blue_book_demo.run_pipeline import parse_intermediary_file

        titles, streets = parse_intermediary_file(DEMO_DIR / "blue_book_runs_intermediary.txt")
        self.assertEqual(titles, annex_titles())
        self.assertEqual(set(streets), set(range(1, RUN_COUNT + 1)))

    def test_all_320_generated_runs_match_annex_b(self):
        # Exactly the records the pipeline writes: start/end names from its
        # titles. The same check gates `krg generate all` and promotion.
        from knowledge_run_generator.blue_book_demo.run_pipeline import parse_intermediary_file

        titles, _streets = parse_intermediary_file(DEMO_DIR / "blue_book_runs_intermediary.txt")
        runs = [
            {"id": rid, "title": f"{o} to {d}", "start": {"name": o}, "end": {"name": d}}
            for rid, (o, d) in sorted(titles.items())
        ]
        self.assertEqual(len(runs), 320)
        self.assertEqual(check_runs_match_annex_b(runs), [])

        wrong = [dict(r) for r in runs]
        wrong[170] = dict(wrong[170], start={"name": "LAMBETH COLLEGE SW8"})
        problems = check_runs_match_annex_b(wrong[:-1])
        self.assertTrue(any("run 171" in p for p in problems))
        self.assertTrue(any("320" in p for p in problems))

    def test_tfl_spellings_resolve_through_documented_lookup_names(self):
        # The run shows TfL's spelling; the gazetteer looks up the real one.
        self.assertEqual(geocode_name("THOMAS MOORE STREET E1"), "THOMAS MORE STREET E1")
        self.assertEqual(geocode_name("GIBSON SQUARE N1"), "GIBSON SQUARE N1")
        entries = json.loads((DEMO_DIR / "annex_b_geocode_names.json").read_text())
        for key, value in entries.items():
            if key.startswith("_"):
                continue
            self.assertTrue(value.get("why"), f"{key} has no reason")


# ---------------------------------------------------------------------------
# Points: Points List geocoding (scripts/geocode_pois.py)
# ---------------------------------------------------------------------------


class DistrictCentreTests(unittest.TestCase):
    def test_w1_centre_is_in_the_west_end_not_kennington(self):
        # Mapbox answered "W1, London, UK" at 51.4987,-0.1046 (Kennington).
        lat, lon = load_district_centres()["W1"]
        self.assertTrue(51.505 < lat < 51.525 and -0.16 < lon < -0.125, (lat, lon))
        self.assertGreater(_haversine(lat, lon, 51.498659, -0.104573), 2500)

    def test_split_districts_have_a_centre(self):
        centres = load_district_centres()
        for district in ("W1", "WC1", "WC2", "EC1", "EC2", "EC3", "EC4", "SW1", "N7", "SE1"):
            self.assertIn(district, centres)


class PointsListGeocodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = _load_script("geocode_pois")

    def test_centroid_comes_from_the_vendored_table(self):
        self.assertEqual(self.g.postcode_centroid("W1"), load_district_centres()["W1"])

    def test_a_result_must_carry_the_name_asked_for(self):
        m = self.g.name_matches
        self.assertFalse(m("Buckingham Palace", {"name": "Buckingham Palace Road"}))
        self.assertFalse(m("Buckingham Palace",
                           {"name": "Welcome London Buckingham Palace - Two-Bedroom Apartment"}))
        self.assertFalse(m("Berkeley Street W1", {"name": "Curzon Street"}))
        self.assertFalse(m("Graham Road", {"name": "Graham Street"}))
        self.assertTrue(m("Golden Square", {"name": "1 Golden Square"}))
        self.assertTrue(m("Umu - Japanese Restaurant", {"name": "Umu Restaurant"}))
        self.assertTrue(m("Ashburn Gardnes", {"name": "Ashburn Gardens"}))

    def test_a_result_outside_the_district_is_rejected(self):
        w1 = load_district_centres()["W1"]
        kennington = {"name": "Golden Square", "lat": 51.48861, "lng": -0.10751, "postcode": "SE11 4AA"}
        soho = {"name": "Golden Square", "lat": 51.51225, "lng": -0.13691, "postcode": "W1F 9JB"}
        self.assertIsNone(self.g.choose_candidate("Golden Square", "W1", [kennington], w1))
        self.assertIs(self.g.choose_candidate("Golden Square", "W1", [kennington, soho], w1), soho)

    def test_differently_named_points_on_one_coordinate_are_rejected(self):
        pois = [
            {"name": "Travelodge Euston", "coordinates": [-0.114, 51.527], "_match_name": "Travelodge"},
            {"name": "Travelodge Farringdon", "coordinates": [-0.114, 51.527], "_match_name": "Travelodge"},
            {"name": "Euston Square", "coordinates": [-0.131, 51.527], "_match_name": "Euston Square"},
            {"name": "Euston Square Station", "coordinates": [-0.131, 51.527], "_match_name": "Euston Square"},
            {"name": "Granary Square", "coordinates": [-0.125, 51.535], "_match_name": "Granary Square"},
            {"name": "Granary Square", "coordinates": [-0.125, 51.535], "_match_name": "Granary Square"},
        ]
        nulled = self.g.reject_shared_coordinates(pois)
        self.assertEqual(sorted(nulled), ["Travelodge Euston", "Travelodge Farringdon"])
        # "Station" is not a naming word: the square and its station are one
        # name to the matcher, and a name listed twice is one place.
        self.assertIsNotNone(pois[3]["coordinates"])
        self.assertIsNotNone(pois[4]["coordinates"])


# ---------------------------------------------------------------------------
# Points: run endpoint resolution (gazetteer)
# ---------------------------------------------------------------------------


def _street(G, name, start_id, lat, lon, count=4, step=0.001, highway="residential", oneway=False):
    prev = None
    for i in range(count):
        nid = start_id + i
        G.add_node(nid, x=lon + i * step, y=lat)
        if prev is not None:
            G.add_edge(prev, nid, 0, length=70.0, name=name, highway=highway, oneway=oneway)
            if not oneway:
                G.add_edge(nid, prev, 0, length=70.0, name=name, highway=highway, oneway=oneway)
        prev = nid


def _graph():
    G = nx.MultiDiGraph()
    G.graph["crs"] = "epsg:4326"
    _street(G, "Golden Square", 100, 51.5122, -0.1375)
    _street(G, "Station Road", 200, 51.5300, -0.1000)
    _street(G, "Park Lane", 300, 51.5400, -0.1200)
    return G


class EndpointResolutionTests(unittest.TestCase):
    def setUp(self):
        self.G = _graph()
        self.ai = build_alias_index(self.G)

    def test_machine_geocodes_shared_by_different_names_are_dropped(self):
        pois = {
            "GOLDEN SQUARE": {"coordinates": [-0.10751, 51.48861], "postal_district": "W1"},
            "BRYANSTON SQUARE": {"coordinates": [-0.10751, 51.48861], "postal_district": "W1"},
            "ALMEIDA THEATRE": {"coordinates": [-0.1030, 51.5390], "postal_district": "N1"},
        }
        kept, rejected = drop_shared_coordinates(pois)
        self.assertEqual(sorted(rejected), ["BRYANSTON SQUARE", "GOLDEN SQUARE"])
        self.assertEqual(list(kept), ["ALMEIDA THEATRE"])

    def test_street_name_resolves_to_the_street_not_a_collapsed_geocode(self):
        gz = Gazetteer(
            alias_index=self.ai,
            knowledge_pois={"GOLDEN SQUARE": {"coordinates": [-0.1005, 51.5300], "postal_district": "W1"}},
        )
        entry = gz.resolve("GOLDEN SQUARE W1", self.G)
        self.assertEqual(entry.source, "street")
        self.assertIn(entry.snapped_node, range(100, 104))

    def test_station_prefers_osm_railway_station_over_a_points_list_guess(self):
        # Holland Park station's Points List geocode sat on Latimer Road station.
        gz = Gazetteer(
            alias_index=self.ai,
            knowledge_pois={"HOLLAND PARK STATION": {"coordinates": [-0.1200, 51.5400], "postal_district": "W1"}},
            osm_pois={"HOLLAND PARK": {"lat": 51.5300, "lon": -0.0990, "kind": "station"}},
        )
        self.assertEqual(gz.lookup_coords("HOLLAND PARK STATION W1")["_source"], "osm")

    def test_station_query_does_not_take_the_park_of_that_name(self):
        gz = Gazetteer(
            alias_index=self.ai,
            osm_pois={"WANDSWORTH COMMON": {"lat": 51.4535, "lon": -0.1730, "kind": "leisure"}},
        )
        self.assertIsNone(gz.lookup_coords("WANDSWORTH COMMON STATION SW12"))
        self.assertIsNotNone(gz.lookup_coords("WANDSWORTH COMMON SW18"))

    def test_unresolved_station_never_falls_to_a_word_dropped_street(self):
        # "LONDON BRIDGE STATION" used to become the bridge (London Bridge, EC4).
        G = nx.MultiDiGraph()
        _street(G, "London Bridge", 1, 51.5080, -0.0877)
        gz = Gazetteer(alias_index=build_alias_index(G))
        self.assertIsNone(gz.resolve("LONDON BRIDGE STATION SE1", G))
        self.assertEqual(gz.resolve("LONDON BRIDGE SE1", G).source, "street")

    def test_area_endpoint_sets_down_at_a_way_in(self):
        # A park whose centre is ~330 m from any road, with a gate on Park Lane.
        park = (51.5430, -0.1185)
        gate = {"lat": 51.54003, "lon": -0.1185, "kind": "gate", "parents": ["CLISSOLD PARK"]}
        gz = Gazetteer(
            alias_index=self.ai,
            osm_pois={"CLISSOLD PARK": {"lat": park[0], "lon": park[1], "kind": "leisure"}},
            access={"1": gate},
        )
        entry = gz.resolve("CLISSOLD PARK N16", self.G)
        self.assertEqual(entry.access_kind, "gate")
        self.assertLessEqual(entry.snap_distance_m, 50.0)
        self.assertIn(entry.snapped_node, range(300, 304))

        no_gate = Gazetteer(
            alias_index=self.ai,
            osm_pois={"CLISSOLD PARK": {"lat": park[0], "lon": park[1], "kind": "leisure"}},
        ).resolve("CLISSOLD PARK N16", self.G)
        self.assertIsNone(no_gate.access_kind)
        self.assertGreater(no_gate.snap_distance_m, 50.0)   # preflight still fails it

    def test_kerb_snap_finds_the_road_beside_a_point(self):
        node, kerb_m, along_m = kerb_snap(self.G, 51.53005, -0.0985)
        self.assertLess(kerb_m, 10)
        self.assertIn(node, range(200, 204))
        self.assertIsNone(kerb_snap(self.G, 51.60, -0.20))


class SnappableRoadTests(unittest.TestCase):
    def setUp(self):
        _dual_cache.clear()

    def test_two_way_trunk_road_is_a_set_down_road(self):
        G = nx.MultiDiGraph()
        _street(G, "Balham High Road", 1, 51.4356, -0.1600, highway="trunk")
        self.assertTrue(_node_is_routable(G, 2))

    def test_dual_carriageway_is_not(self):
        G = nx.MultiDiGraph()
        _street(G, "Loampit Vale", 1, 51.4650, -0.0200, highway="trunk", oneway=True)
        # The opposite carriageway, 11 m away, running the other way.
        for i, nid in enumerate(range(11, 15)):
            G.add_node(nid, x=-0.0200 + (3 - i) * 0.001, y=51.4651)
        for a, b in ((11, 12), (12, 13), (13, 14)):
            G.add_edge(a, b, 0, length=70.0, name="Loampit Vale", highway="trunk", oneway=True)
        self.assertIn((1, 2), _dual_carriageway_edges(G))
        self.assertFalse(_node_is_routable(G, 2))

    def test_one_way_trunk_street_in_a_one_way_system_is(self):
        # Earl's Court Road: one-way trunk, no twin carriageway beside it.
        G = nx.MultiDiGraph()
        _street(G, "Earl's Court Road", 1, 51.4920, -0.1930, highway="trunk", oneway=True)
        _street(G, "Earl's Court Gardens", 20, 51.4920, -0.1920, count=2, oneway=False)
        G.add_edge(2, 20, 0, length=70.0, name="Earl's Court Gardens", highway="residential", oneway=False)
        G.add_edge(20, 2, 0, length=70.0, name="Earl's Court Gardens", highway="residential", oneway=False)
        self.assertEqual(_dual_carriageway_edges(G), set())
        self.assertTrue(_node_is_routable(G, 2))

    def test_motorway_never_is(self):
        G = nx.MultiDiGraph()
        _street(G, "Westway", 1, 51.52, -0.20, highway="motorway")
        self.assertFalse(_node_is_routable(G, 2))


# ---------------------------------------------------------------------------
# Build guard and curated overrides
# ---------------------------------------------------------------------------


class OsmHarvestMergeTests(unittest.TestCase):
    def test_a_station_harvested_after_a_same_named_attraction_is_kept(self):
        from knowledge_run_generator.osm_pois import merge_chunk

        pois = {}
        merge_chunk(pois, {"LONDON BRIDGE": {"lat": 51.508, "lon": -0.0877, "kind": "tourism"}})
        merge_chunk(pois, {"LONDON BRIDGE": {"lat": 51.5049, "lon": -0.0851, "kind": "station"}})
        merge_chunk(pois, {"LONDON BRIDGE": {"lat": 51.5050, "lon": -0.0852, "kind": "station"}})
        self.assertEqual(pois["LONDON BRIDGE"]["kind"], "tourism")
        self.assertEqual([o["kind"] for o in pois["LONDON BRIDGE"]["_others"]], ["station"])
        gz = Gazetteer(osm_pois=pois)
        hit = gz.lookup_coords("LONDON BRIDGE STATION SE1")
        self.assertAlmostEqual(hit["lat"], 51.5049)


class CollisionGuardTests(unittest.TestCase):
    def test_two_different_names_on_one_coordinate_fail(self):
        problems = check_endpoint_collisions({
            "CHANCERY LANE STATION WC1": [-0.1111, 51.5185],
            "FARRINGDON STATION EC1": [-0.1111, 51.5185],
        })
        self.assertEqual(len(problems), 1)
        self.assertIn("FARRINGDON", problems[0])

    def test_the_same_place_in_two_runs_passes(self):
        runs = [
            {"id": 1, "start": {"name": "FARRINGDON STATION EC1", "coordinates": [-0.105, 51.520]},
             "end": {"name": "GIBSON SQUARE N1", "coordinates": [-0.106, 51.537]}},
            {"id": 2, "start": {"name": "GIBSON SQUARE N1", "coordinates": [-0.106, 51.537]},
             "end": {"name": "FARRINGDON STATION EC1", "coordinates": [-0.105, 51.520]}},
        ]
        self.assertEqual(check_run_collisions(runs), [])


class OverrideDocumentationTests(unittest.TestCase):
    def test_new_set_down_overrides_say_why_and_where_from(self):
        overrides = json.loads((DEMO_DIR / "poi_overrides.json").read_text())
        documented = {k: v for k, v in overrides.items() if isinstance(v, dict) and "note" in v}
        self.assertGreaterEqual(len(documented), 18)
        for key, value in documented.items():
            self.assertTrue(value.get("source"), key)
            self.assertTrue(value.get("on_street"), key)
            place = value.get("place")
            # The set-down is near the place it stands for.
            self.assertLess(_haversine(place[0], place[1], value["lat"], value["lon"]), 200, key)

    def test_removed_overrides_that_pinned_the_wrong_street_stay_removed(self):
        overrides = json.loads((DEMO_DIR / "poi_overrides.json").read_text())
        for key in ("ROLLS ROAD SE1", "KNATCHBULL ROAD SE5", "ST JULIAN'S FARM ROAD SE27",
                    "ST JOHN'S WOOD HIGH STREET NW8"):
            self.assertNotIn(key, overrides)


if __name__ == "__main__":
    unittest.main()
