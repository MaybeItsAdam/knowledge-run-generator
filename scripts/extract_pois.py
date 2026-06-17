"""
Extract the ~6,000 points of interest from the TfL "Knowledge of London
Points List" PDF into a clean JSON file.

The PDF (Edition 4, Nov 2025) has three kinds of entries:

  * standard  - two-column lists under postcode headers ("N1 - Barnsbury...").
  * curiosity - single-column entries ("Apsley House - W1") each followed by a
                prose description, under the "Curiosity Points" header.
  * outside   - two-column plain names under "Points Outside 6 Mile Radius".

Layout facts (confirmed against Edition 4 geometry, A4 595x842pt):
  * region headers  -> font size ~14, Arial-Bold
  * postcode / section headers -> font size ~12, Arial-Bold
  * point names / descriptions -> font size ~11, Arial
  * the two body columns split at x ~= 295 (left starts x0~72, right x0~310)
  * within a column, separate entries are ~20pt apart vertically; a name that
    wraps onto a second line sits only ~13pt below its first line.

Outputs constants/extracted_pois.json: a list of
  {name, postal_district, region, category, kind, source_page}.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_PDF = Path("/Users/adam/Downloads/knowledge-of-london-points-list.pdf")
DEFAULT_OUT = ROOT / "constants" / "extracted_pois.json"

# Geometry thresholds (points). See module docstring.
COLUMN_BOUNDARY = 295.0
REGION_MIN_SIZE = 13.5          # >= this is a region header (size ~14)
HEADER_MIN_SIZE = 11.5          # >= this (but < region) is a section header (size ~12)
WRAP_MAX_GAP = 16.0             # column gap <= this means a wrapped continuation
FOOTER_TOP = 770.0             # ignore the page-number footer below this y

# London postcode token, e.g. N1, N1C, EC1, WC2, SW1.
_POSTCODE_RE = re.compile(r"[A-Z]{1,2}\d{1,2}[A-Z]?")
# A postcode header line: "N1 - District / District" (hyphen, en- or em-dash).
_POSTCODE_HEADER_RE = re.compile(r"^([A-Z]{1,2}\d{1,2}[A-Z]?)\s*[-–—]\s*(.*)$")
# A curiosity title line ends with the point's postcode, e.g. "Apsley House - W1"
# or "George Inn PH SE1".
_CURIOSITY_TITLE_RE = re.compile(
    r"^(.*?)\s*[-–—]?\s*([A-Z]{1,2}\d{1,2}[A-Z]?)\s*$"
)

_CATEGORY_KEYWORDS = [
    ("station", "station"),
    ("theatre", "theatre"),
    ("cinema", "cinema"),
    ("hotel", "hotel"),
    ("museum", "museum"),
    ("gallery", "gallery"),
    ("hospital", "hospital"),
    ("library", "library"),
    ("church", "church"),
    ("park", "park"),
    ("square", "square"),
    ("bridge", "bridge"),
    ("restaurant", "restaurant"),
    ("school", "school"),
    ("college", "college"),
    ("university", "university"),
]


def _infer_category(name: str) -> str:
    low = name.lower()
    if re.search(r"\bph\b", low) or low.endswith(" ph") or " ph " in low:
        return "pub"
    for keyword, category in _CATEGORY_KEYWORDS:
        if keyword in low:
            return category
    if re.search(r"\b(road|street|lane|avenue|villas|gardens|place|way|walk|hill)\b", low):
        return "street"
    return "point"


def _cluster_lines(words):
    """Group extracted words into physical text lines keyed by rounded top."""
    lines = defaultdict(list)
    for w in words:
        if w["top"] >= FOOTER_TOP:
            continue  # skip page-number footer
        lines[round(w["top"])].append(w)
    out = []
    for top in sorted(lines):
        ws = sorted(lines[top], key=lambda x: x["x0"])
        text = " ".join(w["text"] for w in ws).strip()
        if not text:
            continue
        out.append(
            {
                "top": top,
                "words": ws,
                "text": text,
                "max_size": max(round(w["size"], 1) for w in ws),
                "x0": ws[0]["x0"],
            }
        )
    return out


def _split_columns(words):
    """Return (left_text, right_text) by splitting a line's words at the gutter."""
    left = [w for w in words if w["x0"] < COLUMN_BOUNDARY]
    right = [w for w in words if w["x0"] >= COLUMN_BOUNDARY]
    lt = " ".join(w["text"] for w in sorted(left, key=lambda x: x["x0"])).strip()
    rt = " ".join(w["text"] for w in sorted(right, key=lambda x: x["x0"])).strip()
    return lt, rt


def _column_entries(body_lines):
    """Walk a section's body lines and yield names per column with wrap-merge.

    Each column is an independent vertical stream; a line whose vertical gap
    from the previous line in the same column is <= WRAP_MAX_GAP continues the
    previous name, otherwise it starts a new one.
    """
    streams = {"left": [], "right": []}  # list of {top, text}
    for line in body_lines:
        lt, rt = _split_columns(line["words"])
        for col, text in (("left", lt), ("right", rt)):
            if not text:
                continue
            stream = streams[col]
            gap = line["top"] - stream[-1]["top"] if stream else None
            if stream and 0 < gap <= WRAP_MAX_GAP:
                stream[-1]["text"] = (stream[-1]["text"] + " " + text).strip()
            else:
                stream.append({"top": line["top"], "text": text})
    names = []
    for col in ("left", "right"):
        names.extend(entry["text"] for entry in streams[col])
    return names


def extract(pdf_path: Path, limit: int | None = None):
    records = []
    region = ""
    postcode = ""
    district = ""
    mode = "standard"           # standard | curiosity | outside
    section_body = []           # body lines accumulated for the current section
    section_page = 0

    def flush_section():
        """Emit two-column names accumulated for the current standard/outside section."""
        nonlocal section_body
        if not section_body:
            return
        for name in _column_entries(section_body):
            name = name.strip()
            if not name or name.isdigit():
                continue
            records.append(
                {
                    "name": name,
                    "postal_district": postcode if mode == "standard" else "",
                    "region": region,
                    "category": _infer_category(name),
                    "kind": mode,
                    "source_page": section_page,
                }
            )
        section_body = []

    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages if limit is None else pdf.pages[:limit]
        for page_index, page in enumerate(pages):
            page_no = page_index + 1
            words = page.extract_words(extra_attrs=["size", "fontname"])
            lines = _cluster_lines(words)
            for line in lines:
                text = line["text"]
                size = line["max_size"]

                # Front matter / cover / index pages have no size-11 body under a
                # postcode header; we simply never enter a section there.
                if size >= REGION_MIN_SIZE:
                    # Region grouping header (size ~14). Only meaningful in
                    # standard mode; flush whatever preceded it.
                    flush_section()
                    region = text
                    continue

                if size >= HEADER_MIN_SIZE:
                    # Section header (size ~12): mode switch, postcode, or a
                    # wrapped continuation of the previous postcode header.
                    if re.search(r"curiosity points", text, re.I):
                        flush_section()
                        mode = "curiosity"
                        postcode = ""
                        district = ""
                        continue
                    if re.search(r"points?\s+outside", text, re.I):
                        flush_section()
                        mode = "outside"
                        postcode = ""
                        district = ""
                        continue
                    m = _POSTCODE_HEADER_RE.match(text)
                    if m:
                        flush_section()
                        mode = "standard"
                        postcode = m.group(1)
                        district = m.group(2).strip()
                        section_page = page_no
                        continue
                    # Continuation of the current postcode header's district.
                    district = (district + " " + text).strip()
                    continue

                # Body line (size ~11).
                if mode == "curiosity":
                    _handle_curiosity_line(records, line, region, page_no)
                    continue

                # Standard sections only begin once a real postcode header has
                # been seen. This skips the cover, preamble and index pages,
                # whose body text is also size ~11 but sits under no postcode.
                if mode == "standard" and not postcode:
                    continue

                if not section_body:
                    section_page = page_no
                section_body.append(line)

        flush_section()

    return records


# Curiosity points are single-column: a title line carrying the point name and
# its postcode, followed by 1-4 prose description lines we discard. We only keep
# lines that look like a title (end in a postcode token and read like a name,
# not a sentence).
def _handle_curiosity_line(records, line, region, page_no):
    text = line["text"]
    lt, _ = _split_columns(line["words"])  # curiosity body is left-aligned at x0~72
    text = lt or text
    m = _CURIOSITY_TITLE_RE.match(text)
    if not m:
        return
    name = m.group(1).strip(" -–—,")
    postcode = m.group(2)
    if not name:
        return
    # Reject prose: titles are short and don't end with sentence punctuation.
    if name.endswith((".", ",")) or len(name.split()) > 12:
        return
    records.append(
        {
            "name": name,
            "postal_district": postcode,
            "region": region,
            "category": _infer_category(name),
            "kind": "curiosity",
            "source_page": page_no,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF, help="Path to the Points List PDF.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output JSON path.")
    parser.add_argument("--limit", type=int, default=None, help="Only parse the first N pages (debug).")
    args = parser.parse_args()

    if not args.pdf.exists():
        raise SystemExit(f"PDF not found: {args.pdf}")

    print(f"Parsing {args.pdf} ...")
    records = extract(args.pdf, limit=args.limit)

    by_kind = defaultdict(int)
    for r in records:
        by_kind[r["kind"]] += 1
    print(f"Extracted {len(records)} points: " + ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(records, ensure_ascii=False, indent=2))
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
