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
