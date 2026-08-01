"""
Gazetteer resolution tiers: overrides > Points List > OSM > street fallback.

These are the lookups that decide whether a Blue Book run endpoint resolves
offline or costs a rate-limited geocoder round trip, so they're tested against
a small in-memory graph rather than the real London one.
"""

import json
import tempfile
import unittest
from pathlib import Path

import networkx as nx

from knowledge_run_generator.aliases import build_alias_index
from knowledge_run_generator.gazetteer import (
    Gazetteer,
    load_knowledge_pois,
)


def _graph():
    """Two same-named streets far apart, plus one uniquely named street."""
    G = nx.MultiDiGraph()
    G.graph["crs"] = "epsg:4326"

    def street(name, start_id, lat, lon, count=4):
        prev = None
        for i in range(count):
            nid = start_id + i
            G.add_node(nid, x=lon + i * 0.001, y=lat)
            if prev is not None:
                G.add_edge(prev, nid, 0, length=70.0, name=name, highway="residential")
                G.add_edge(nid, prev, 0, length=70.0, name=name, highway="residential")
            prev = nid

    street("High Street", 100, 51.540, -0.100)   # northern
    street("High Street", 200, 51.480, -0.070)   # southern
    street("Aberdeen Road", 300, 51.552, -0.098)
    return G


class LookupTierTests(unittest.TestCase):
    def setUp(self):
        self.G = _graph()
        self.alias_index = build_alias_index(self.G)

    def test_knowledge_pois_resolve_without_an_override(self):
        gz = Gazetteer(
            alias_index=self.alias_index,
            knowledge_pois={
                "ALMEIDA THEATRE": {"coordinates": [-0.100, 51.540], "postal_district": "N1"},
            },
        )
        entry = gz.resolve("ALMEIDA THEATRE N1", self.G)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.source, "knowledge_poi")

    def test_tier_precedence_override_beats_points_list_beats_osm(self):
        shared = "SOME PLACE"
        gz = Gazetteer(
            overrides={shared: [51.540, -0.100]},
            knowledge_pois={shared: {"coordinates": [-0.070, 51.480]}},
            osm_pois={shared: {"lat": 51.552, "lon": -0.098}},
            alias_index=self.alias_index,
        )
        self.assertEqual(gz.lookup_coords(shared)["_source"], "override")

        gz_no_override = Gazetteer(
            knowledge_pois={shared: {"coordinates": [-0.070, 51.480]}},
            osm_pois={shared: {"lat": 51.552, "lon": -0.098}},
            alias_index=self.alias_index,
        )
        self.assertEqual(gz_no_override.lookup_coords(shared)["_source"], "knowledge_poi")

    def test_abbreviated_endpoint_matches_the_expanded_points_list_name(self):
        gz = Gazetteer(
            knowledge_pois={"FITZHARDINGE STREET": {"coordinates": [-0.100, 51.540]}},
            alias_index=self.alias_index,
        )
        self.assertIsNotNone(gz.lookup_coords("FITZHARDINGE ST W1"))

    def test_postcode_disambiguates_same_named_points(self):
        gz = Gazetteer(
            knowledge_pois={
                "GRANARY SQUARE N1C": {"coordinates": [-0.100, 51.540], "postal_district": "N1C"},
                "GRANARY SQUARE SE1": {"coordinates": [-0.070, 51.480], "postal_district": "SE1"},
            },
            alias_index=self.alias_index,
        )
        self.assertAlmostEqual(gz.lookup_coords("GRANARY SQUARE SE1")["lat"], 51.480)
        self.assertAlmostEqual(gz.lookup_coords("GRANARY SQUARE N1C")["lat"], 51.540)


class StreetFallbackTests(unittest.TestCase):
    def setUp(self):
        self.G = _graph()
        self.alias_index = build_alias_index(self.G)

    def test_street_endpoint_resolves_off_the_graph(self):
        gz = Gazetteer(alias_index=self.alias_index)
        entry = gz.resolve("ABERDEEN ROAD N5", self.G)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.source, "street")
        # The entry *is* a graph node, so there is nothing to snap.
        self.assertEqual(entry.snap_distance_m, 0.0)
        self.assertIn(entry.snapped_node, range(300, 304))

    def test_district_centroid_picks_between_same_named_streets(self):
        # Points List entries give each district a centre of mass; the southern
        # High Street should win for an SE-district endpoint and vice versa.
        gz = Gazetteer(
            alias_index=self.alias_index,
            knowledge_pois={
                "A SOUTHERN POINT": {"coordinates": [-0.070, 51.481], "postal_district": "SE1"},
                "A NORTHERN POINT": {"coordinates": [-0.100, 51.541], "postal_district": "N1"},
            },
        )
        south = gz.resolve("HIGH STREET SE1", self.G)
        north = gz.resolve("HIGH STREET N1", self.G)
        self.assertIn(south.snapped_node, range(200, 204))
        self.assertIn(north.snapped_node, range(100, 104))

    def test_unknown_name_still_returns_none(self):
        gz = Gazetteer(alias_index=self.alias_index)
        self.assertIsNone(gz.resolve("NOWHERE AT ALL W1", self.G))


class StationMatchingTests(unittest.TestCase):
    """OSM names stations without the word "Station"; the Blue Book always
    includes it, and sometimes the operator too."""

    def setUp(self):
        self.G = _graph()
        self.alias_index = build_alias_index(self.G)
        # Shaped like a real `krg osm-pois` harvest.
        self.harvest = {
            "CHARING CROSS": {"lat": 51.508, "lon": -0.124, "kind": "station"},
            "LONDON WATERLOO": {"lat": 51.503, "lon": -0.113, "kind": "station"},
            "LONDON KING'S CROSS": {"lat": 51.531, "lon": -0.124, "kind": "station"},
            "BOW CHURCH": {"lat": 51.527, "lon": -0.021, "kind": "station"},
            "BETHNAL GREEN": {"lat": 51.527, "lon": -0.060, "kind": "station"},
            "VICTORIA COACH STATION": {"lat": 51.492, "lon": -0.148, "kind": "station"},
            "FINSBURY PARK": {"lat": 51.564, "lon": -0.106, "kind": "station"},
        }
        self.gz = Gazetteer(osm_pois=self.harvest, alias_index=self.alias_index)

    def test_station_suffix_is_not_required_by_osm(self):
        for endpoint in ("CHARING CROSS STATION WC2", "BOW CHURCH STATION E3"):
            with self.subTest(endpoint=endpoint):
                self.assertIsNotNone(self.gz.lookup_coords(endpoint))

    def test_london_prefixed_national_rail_names_match(self):
        self.assertAlmostEqual(
            self.gz.lookup_coords("WATERLOO STATION SE1")["lat"], 51.503
        )
        self.assertAlmostEqual(
            self.gz.lookup_coords("KINGS CROSS STATION N1")["lat"], 51.531
        )

    def test_operator_qualifiers_are_ignored(self):
        # "B_R" is British Rail, as it appears in the Blue Book source.
        self.assertIsNotNone(self.gz.lookup_coords("BETHNAL GREEN B_R STATION E2"))

    def test_name_already_containing_station_still_matches(self):
        self.assertIsNotNone(self.gz.lookup_coords("VICTORIA COACH STATION SW1"))

    def test_station_aliases_are_only_reachable_by_station_queries(self):
        # "WATERLOO" exists solely as a station stem of "London Waterloo", so a
        # query without a station word must not reach it — otherwise the bare
        # name of every station would shadow streets and areas that share it.
        self.assertIsNone(self.gz.lookup_coords("WATERLOO SE1"))
        self.assertIsNotNone(self.gz.lookup_coords("WATERLOO STATION SE1"))

    def test_a_harvest_name_is_reachable_directly(self):
        # Entries are still indexed under their own name. Note the harvest is
        # keyed by name, so a station and a park called "Finsbury Park" collide
        # upstream in parse_overpass and only one survives.
        self.assertIsNotNone(self.gz.lookup_coords("FINSBURY PARK N4"))

    def test_curated_override_still_wins_over_the_harvest(self):
        gz = Gazetteer(
            overrides={"CHARING CROSS STATION": [51.500, -0.100]},
            osm_pois=self.harvest,
            alias_index=self.alias_index,
        )
        hit = gz.lookup_coords("CHARING CROSS STATION WC2")
        self.assertEqual(hit["_source"], "override")
        self.assertAlmostEqual(hit["lat"], 51.500)


class HarvestCoverageTests(unittest.TestCase):
    def test_tag_groups_cover_the_endpoint_categories_we_need(self):
        from knowledge_run_generator.osm_pois import _TAG_GROUPS

        values = " ".join(v for _k, v, _kind in _TAG_GROUPS)
        for tag in ("station", "halt", "bus_station", "prison", "marketplace",
                    "hospital", "stadium", "place_of_worship", "museum"):
            with self.subTest(tag=tag):
                self.assertIn(tag, values)

    def test_stations_are_kinded_as_stations(self):
        from knowledge_run_generator.osm_pois import _kind_for

        self.assertEqual(_kind_for({"railway": "halt"}), "station")
        self.assertEqual(_kind_for({"amenity": "bus_station"}), "station")


class LoaderTests(unittest.TestCase):
    def test_load_knowledge_pois_skips_failed_geocodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "knowledge_pois.json"
            path.write_text(json.dumps([
                {"name": "Angel Station", "coordinates": [-0.105, 51.532]},
                {"name": "Never Geocoded", "coordinates": None},
                {"name": "", "coordinates": [-0.1, 51.5]},
            ]))
            loaded = load_knowledge_pois(path)
            self.assertEqual(list(loaded), ["ANGEL STATION"])

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(load_knowledge_pois(Path("/nonexistent/knowledge_pois.json")), {})


class IndexCacheTests(unittest.TestCase):
    """A derived index must never outlive the graph it was built from."""

    def test_alias_index_cache_is_rebuilt_when_the_graph_changes(self):
        from knowledge_run_generator.aliases import load_or_build_alias_index

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "alias_index.pkl"
            first = load_or_build_alias_index(_graph(), cache)
            self.assertNotIn("GIBSON SQUARE", first.canonical_to_nodes)

            bigger = _graph()
            bigger.add_node(900, x=-0.106, y=51.537)
            bigger.add_node(901, x=-0.105, y=51.537)
            bigger.add_edge(900, 901, 0, length=50.0, name="Gibson Square",
                            highway="residential")

            second = load_or_build_alias_index(bigger, cache)
            self.assertIn("GIBSON SQUARE", second.canonical_to_nodes)

    def test_legacy_cache_without_a_fingerprint_is_discarded(self):
        import pickle

        from knowledge_run_generator.aliases import build_alias_index, load_or_build_alias_index

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "alias_index.pkl"
            # Pre-fingerprint format: a bare AliasIndex from an unrelated graph.
            cache.write_bytes(pickle.dumps(build_alias_index(nx.MultiDiGraph())))
            index = load_or_build_alias_index(_graph(), cache)
            self.assertIn("ABERDEEN ROAD", index.canonical_to_nodes)


if __name__ == "__main__":
    unittest.main()
