"""
The canonical Blue Book run list: TfL Annex B.

Run numbers, start and end names, postal districts and order all come from
``blue_book_demo/tfl_blue_book_annex_b.txt``: Annex B ("The lists of the 320
Routes you must learn") of TfL's *Guide to Learning the Knowledge of London,
All London edition*, 2011, as released under FOI. The file is vendored
byte-for-byte from the-blue-app's verifier
(``scripts/verify-runs/sources/tfl-blue-book-annex-b.txt``); its header keeps
the provenance note and the source PDF's sha256, and :data:`ANNEX_B_SHA256`
pins the text itself so an edit cannot slip in unnoticed.

The Anki export (``blue_book_runs_intermediary.txt``) is still where the
*street sequence* of each run comes from, because Annex B lists endpoints only.
Where the two disagree on a name (run 171 "LAMBETH COLLEGE SW8" vs TfL "Union
Road, SW8"; run 174 "AMERICAN EMBASSY W1" vs "Grosvenor Square, W1") TfL wins.

Names are emitted in the generator's existing house form, upper case with the
district last ("CHANCERY LANE STATION WC1"), by :func:`format_point`. That is
a change of case and punctuation only: every word of the TfL name is kept,
including TfL's own misspellings ("THOMAS MOORE STREET E1"). Where such a
spelling would not resolve against OSM, the *lookup* is pointed at the real
name through :data:`GEOCODE_NAMES`; the displayed name stays TfL's.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

ANNEX_B_PATH = Path(__file__).resolve().parent / "blue_book_demo" / "tfl_blue_book_annex_b.txt"
GEOCODE_NAMES_PATH = Path(__file__).resolve().parent / "blue_book_demo" / "annex_b_geocode_names.json"

# sha256 of the vendored text. Identical to the verifier's copy in the app.
ANNEX_B_SHA256 = "bae85db4c48cf85f0265d7dc20f909258925849b0ec4ea2db65d27a41c03058f"

RUNS_PER_LIST = 16
LIST_COUNT = 20
RUN_COUNT = RUNS_PER_LIST * LIST_COUNT

# "Chancery Lane Station, WC1" / "Sadler's Wells Theatre. EC1": the district is
# the last token, after a comma (or, twice in the PDF, a full stop).
_DISTRICT_RE = re.compile(r"^(?P<name>.+?)[\s,.]+(?P<district>[A-Z]{1,2}\d{1,2}[A-Z]?)\s*[.,]?\s*$")
_RUN_RE = re.compile(r"^(?P<pos>\d+) (?P<start>.+?) to (?P<end>.+)$")
_LIST_RE = re.compile(r"^List (?P<list>\d+)$")


@dataclass(frozen=True)
class AnnexPoint:
    name: str       # TfL wording, e.g. "St. John’s Wood Station"
    district: str   # e.g. "NW8"

    @property
    def display(self) -> str:
        return format_point(self.name, self.district)


@dataclass(frozen=True)
class AnnexRun:
    id: int
    list: int
    position: int
    start: AnnexPoint
    end: AnnexPoint
    raw: str

    @property
    def title(self) -> str:
        return f"{self.start.display} to {self.end.display}"


def format_point(name: str, district: str) -> str:
    """TfL wording -> the generator's name form.

    Upper case, typographic apostrophes straightened, full stops dropped
    ("St." -> "ST", "B.B.C." -> "BBC"), then the district. Commas inside the
    name are kept ("HANOVER GATE, REGENT'S PARK NW1").
    """
    text = name.replace("’", "'").replace("‘", "'").replace(".", "")
    text = re.sub(r"\s+", " ", text).strip(" ,")
    return f"{text.upper()} {district.upper()}"


def _split_point(text: str) -> AnnexPoint:
    match = _DISTRICT_RE.match(text.strip())
    if not match:
        raise ValueError(f"Annex B point has no postal district: {text!r}")
    return AnnexPoint(name=match.group("name").strip(" ,."), district=match.group("district"))


def parse_annex_b(text: str) -> list[AnnexRun]:
    """Parse the Annex B text into runs ordered by id.

    Strict: an unparsed line, a missing or duplicated id, or anything other
    than 20 lists of 16 raises, because a silently short list is exactly the
    failure the canonical source is there to prevent.
    """
    runs: list[AnnexRun] = []
    current_list = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        list_match = _LIST_RE.match(line)
        if list_match:
            current_list = int(list_match.group("list"))
            continue
        run_match = _RUN_RE.match(line)
        if not run_match or current_list == 0:
            raise ValueError(f"unparsed Annex B line: {line!r}")
        position = int(run_match.group("pos"))
        if not 1 <= position <= RUNS_PER_LIST:
            raise ValueError(f"Annex B position out of range: {line!r}")
        runs.append(AnnexRun(
            id=(current_list - 1) * RUNS_PER_LIST + position,
            list=current_list,
            position=position,
            start=_split_point(run_match.group("start")),
            end=_split_point(run_match.group("end")),
            raw=line,
        ))

    ids = [r.id for r in runs]
    if sorted(ids) != list(range(1, RUN_COUNT + 1)):
        missing = sorted(set(range(1, RUN_COUNT + 1)) - set(ids))
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(
            f"Annex B must list runs 1..{RUN_COUNT} once each "
            f"(parsed {len(ids)}; missing {missing[:10]}; duplicated {dupes[:10]})"
        )
    if ids != sorted(ids):
        raise ValueError("Annex B runs are out of order")
    return runs


_cache: list[AnnexRun] | None = None


def load_annex_b(path: Path | None = None, verify_hash: bool = True) -> list[AnnexRun]:
    """The 320 canonical runs, from the vendored file unless *path* is given."""
    global _cache
    if path is None and _cache is not None:
        return _cache
    source = Path(path) if path else ANNEX_B_PATH
    data = source.read_bytes()
    if verify_hash and path is None:
        digest = hashlib.sha256(data).hexdigest()
        if digest != ANNEX_B_SHA256:
            raise ValueError(
                f"{source} sha256 {digest} does not match the pinned {ANNEX_B_SHA256}; "
                "the canonical run list must not be edited in place"
            )
    runs = parse_annex_b(data.decode("utf-8"))
    if path is None:
        _cache = runs
    return runs


def annex_titles(path: Path | None = None) -> dict[int, tuple[str, str]]:
    """``{run_id: (start, end)}`` in the generator's name form."""
    return {r.id: (r.start.display, r.end.display) for r in load_annex_b(path)}


_geocode_names: dict[str, str] | None = None


def load_geocode_names(path: Path | None = None) -> dict[str, str]:
    """Display name -> the name to *resolve* it by.

    Only for TfL spellings that name a real place under a different spelling
    (each entry in the JSON carries its reason). The returned name is a
    lookup key; it never replaces the displayed TfL name.
    """
    global _geocode_names
    if path is None and _geocode_names is not None:
        return _geocode_names
    source = Path(path) if path else GEOCODE_NAMES_PATH
    out: dict[str, str] = {}
    if source.exists():
        raw = json.loads(source.read_text())
        for key, value in raw.items():
            if key.startswith("_"):
                continue
            target = value["resolve_as"] if isinstance(value, dict) else value
            out[str(key).upper()] = str(target).upper()
    if path is None:
        _geocode_names = out
    return out


def geocode_name(display: str) -> str:
    """The name to resolve *display* by (itself unless listed)."""
    return load_geocode_names().get(display.upper(), display)
