"""
Constraint compiler tests (ROADMAP Stage 2).

The compiler is what turns Blue Book text into the ordered router's input, so
these pin the properties the ordered search depends on:

  * resolution is tiered and honest — a line that resolves through nothing is
    a *gap*, never a guessed constraint (the legacy matcher returned its input
    on failure, which is fatal under a hard constraint);
  * hardness follows confidence — exact/junction/abbrev are hard, fuzzy and
    word-removal are soft;
  * roundabout lines survive parsing and become NODE constraints over the
    gyratory ring, located from their neighbours when the line is bare;
  * the multi-street source lines (Run 160) yield every street, not a phantom.
"""

import unittest
from pathlib import Path

import networkx as nx

from knowledge_run_generator.aliases import normalise
from knowledge_run_generator.constraints import (
    Constraint,
    collect_ring,
    compile_constraints,
    is_roundabout_line,
    resolve_street_tiered,
)
from knowledge_run_generator.blue_book_demo.run_pipeline import (
    parse_intermediary_file,
    parse_intermediary_lines,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
INTERMEDIARY = (
    REPO_ROOT
    / "knowledge_run_generator"
    / "blue_book_demo"
    / "blue_book_runs_intermediary.txt"
)


class RoundaboutLineDetectionTests(unittest.TestCase):
    def test_both_spellings_detected(self):
        self.assertTrue(is_roundabout_line("ROUNDABOUT"))
        self.assertTrue(is_roundabout_line("R/BOUT"))
        self.assertTrue(is_roundabout_line("CROWN GATE R/BOUT"))
        self.assertTrue(is_roundabout_line("HARROW ROAD ROUNDABOUT"))
        # The source file has run-together forms.
        self.assertTrue(is_roundabout_line("BRICKLAYER'S ARMSROUNDABOUT"))

    def test_ordinary_streets_are_not_flagged(self):
        self.assertFalse(is_roundabout_line("MARE STREET"))
        self.assertFalse(is_roundabout_line("ROUND HILL"))


class TieredResolutionTests(unittest.TestCase):
    KEYS = {
        normalise("MARE STREET"),
        normalise("MORNING LANE"),
        normalise("THEOBALDS ROAD"),
        normalise("HOLBORN"),
        normalise("SOUTHAMPTON ROW"),
    }

    def test_exact_match_is_hard(self):
        match, source = resolve_street_tiered("MARE STREET", self.KEYS)
        self.assertEqual(match, normalise("MARE STREET"))
        self.assertEqual(source, "exact")

    def test_fuzzy_match_is_soft(self):
        match, source = resolve_street_tiered("THEOBOLDS ROAD", self.KEYS)
        self.assertEqual(match, normalise("THEOBALDS ROAD"))
        self.assertEqual(source, "fuzzy")

    def test_junction_suffix_strip_is_word_removal(self):
        match, source = resolve_street_tiered("HOLBORN CIRCUS", self.KEYS)
        self.assertEqual(match, "HOLBORN")
        self.assertEqual(source, "word_removal")

    def test_total_failure_returns_none_not_the_input(self):
        match, source = resolve_street_tiered("ZZYZX BOULEVARD", self.KEYS)
        self.assertIsNone(match)
        self.assertEqual(source, "unresolved")

    def test_spelling_fixes_apply_before_matching(self):
        match, source = resolve_street_tiered(
            "MAER STREET", self.KEYS, spelling_fixes={"MAER STREET": "MARE STREET"}
        )
        self.assertEqual(match, normalise("MARE STREET"))
        self.assertEqual(source, "exact")


def _ring_graph():
    """Two named streets joined by a four-node roundabout ring.

    Layout:  A1 -- A2 -> (R1 R2 R3 R4 ring) -> B1 -- B2
    """
    G = nx.MultiDiGraph()
    G.graph["crs"] = "epsg:4326"
    coords = {
        "A1": (0.000, 0.000), "A2": (0.001, 0.000),
        "R1": (0.002, 0.000), "R2": (0.0025, 0.0005),
        "R3": (0.003, 0.000), "R4": (0.0025, -0.0005),
        "B1": (0.004, 0.000), "B2": (0.005, 0.000),
    }
    ids = {name: i for i, name in enumerate(coords)}
    for name, (x, y) in coords.items():
        G.add_node(ids[name], x=x, y=y)

    def road(u, v, name, oneway=False, **attrs):
        data = {"length": 100.0, "name": name, "highway": "residential"}
        data.update(attrs)
        G.add_edge(ids[u], ids[v], **data)
        if not oneway:
            G.add_edge(ids[v], ids[u], **data)

    road("A1", "A2", "Alpha Road")
    road("A2", "R1", "Alpha Road")
    # ring is one-way, tagged junction=roundabout
    for u, v in (("R1", "R2"), ("R2", "R3"), ("R3", "R4"), ("R4", "R1")):
        road(u, v, "Ring", oneway=True, junction="roundabout")
    road("R3", "B1", "Beta Street")
    road("B1", "B2", "Beta Street")
    return G, ids


def _street_index(G):
    index = {}
    for u, v, data in G.edges(data=True):
        norm = normalise(data.get("name", ""))
        if norm:
            index.setdefault(norm, set()).update((u, v))
    return index


class RingCompilationTests(unittest.TestCase):
    def setUp(self):
        self.G, self.ids = _ring_graph()
        self.index = _street_index(self.G)
        self.ring_nodes = {self.ids[n] for n in ("R1", "R2", "R3", "R4")}

    def test_collect_ring_finds_the_whole_gyratory(self):
        ring = collect_ring(self.G, self.ids["R1"])
        self.assertEqual(ring, self.ring_nodes)

    def test_bare_roundabout_line_is_located_from_neighbours(self):
        compiled = compile_constraints(
            ["ALPHA ROAD", "ROUNDABOUT", "BETA STREET"], self.index, G=self.G
        )
        self.assertEqual(compiled.gaps, [])
        kinds = [c.kind for c in compiled.constraints]
        self.assertEqual(kinds, ["STREET", "NODE", "STREET"])
        ring_c = compiled.constraints[1]
        self.assertEqual(ring_c.source, "ring")
        self.assertFalse(ring_c.hard)
        self.assertTrue(self.ring_nodes <= set(ring_c.key))

    def test_named_junction_resolves_through_the_junction_index(self):
        junction_index = {normalise("OMEGA CROSS"): {self.ids["R1"]}}
        compiled = compile_constraints(
            ["ALPHA ROAD", "OMEGA CROSS", "BETA STREET"],
            self.index, junction_index=junction_index, G=self.G,
        )
        self.assertEqual(compiled.gaps, [])
        node_c = compiled.constraints[1]
        self.assertEqual(node_c.kind, "NODE")
        self.assertEqual(node_c.source, "junction")
        self.assertTrue(node_c.hard)

    def test_unresolvable_line_is_an_explicit_gap(self):
        compiled = compile_constraints(
            ["ALPHA ROAD", "NOWHERE AVENUE", "BETA STREET"], self.index, G=self.G
        )
        self.assertEqual(compiled.gaps, ["NOWHERE AVENUE"])
        self.assertEqual(
            [c.key for c in compiled.constraints],
            [normalise("ALPHA ROAD"), normalise("BETA STREET")],
        )

    def test_consecutive_duplicates_collapse(self):
        compiled = compile_constraints(
            ["ALPHA ROAD", "ALPHA ROAD", "BETA STREET"], self.index, G=self.G
        )
        self.assertEqual(len(compiled.constraints), 2)

    def test_non_consecutive_repeats_are_kept(self):
        # "STRAND, ALDWYCH, STRAND" is a real Blue Book shape.
        compiled = compile_constraints(
            ["ALPHA ROAD", "BETA STREET", "ALPHA ROAD"], self.index, G=self.G
        )
        self.assertEqual(len(compiled.constraints), 3)

    def test_histograms(self):
        compiled = compile_constraints(
            ["ALPHA ROAD", "ROUNDABOUT", "BETA STREET"], self.index, G=self.G
        )
        self.assertEqual(compiled.kind_histogram(), {"STREET": 2, "NODE": 1})
        self.assertEqual(
            compiled.source_histogram(), {"exact": 2, "ring": 1}
        )


class MultiStreetParseTests(unittest.TestCase):
    """Run 160's 'R___ MORNING LANE R___ MARE STREET' line."""

    @classmethod
    def setUpClass(cls):
        cls.titles, cls.streets = parse_intermediary_file(INTERMEDIARY)
        _, cls.lines = parse_intermediary_lines(INTERMEDIARY)

    def test_run_160_gets_both_streets(self):
        seq = [s.upper() for s in self.streets[160]]
        self.assertIn("MORNING LANE", seq)
        self.assertIn("MARE STREET", seq)
        mo, ma = seq.index("MORNING LANE"), seq.index("MARE STREET")
        self.assertEqual(ma, mo + 1, "MARE STREET must directly follow MORNING LANE")

    def test_no_phantom_trailing_verb_streets(self):
        for run_id, seq in self.streets.items():
            for s in seq:
                self.assertFalse(
                    s.upper().endswith((" R", " L", " F")),
                    f"run {run_id}: {s!r} looks like an unstripped direction verb",
                )

    def test_roundabouts_survive_in_lines_but_not_streets(self):
        with_roundabouts = sum(
            1 for seq in self.lines.values()
            for s in seq if is_roundabout_line(s)
        )
        self.assertGreater(with_roundabouts, 50)
        for seq in self.streets.values():
            for s in seq:
                self.assertFalse(is_roundabout_line(s))

    def test_rbout_lines_are_no_longer_phantom_streets(self):
        # "R/BOUT" lines used to survive the ROUNDABOUT-only filter and reach
        # preflight as street names.
        for seq in self.streets.values():
            for s in seq:
                self.assertNotIn("R/BOUT", s.upper())

    def test_all_320_runs_still_parse(self):
        self.assertEqual(sorted(self.titles), list(range(1, 321)))
        empty = [rid for rid, seq in self.streets.items() if not seq]
        self.assertEqual(empty, [])


if __name__ == "__main__":
    unittest.main()
