"""Where a shipped route comes from, and why.

Every run ships one of two routes:

* ``blue_book``: the Blue Book street sequence, routed by the ordered search.
  Used whenever that route passes every gate (legal, sane, taxi-legal, no
  hard gap).
* ``crow_flies``: the legal taxi route that stays closest to the straight
  line (``router.route_crow_flies``). Used when the Blue Book sequence can't
  be driven on today's roads (Hammersmith Bridge, a modal filter, a one-way
  that forces a lap) or fails a gate. It faces the same hard gates.

``route_source_reason`` says, in one app-facing sentence, why the Blue Book
route was not shipped. App-facing strings here must not contain em dashes.
"""

from __future__ import annotations

import math

from .taxi_profile import (
    describe_barrier, describe_filtered_section, describe_violation)

BLUE_BOOK = "blue_book"
CROW_FLIES = "crow_flies"

HAMMERSMITH_BRIDGE_REASON = "Hammersmith Bridge is closed to motor vehicles"
# How far from the run's start/end box a barrier on a demoted street may be
# and still be blamed for the demotion.
BARRIER_NEAR_M = 1500.0


def _title(raw: str) -> str:
    words = str(raw or "").strip().split()
    out = []
    for w in words:
        low = w.lower()
        out.append(low if low in {"of", "the", "and", "on", "in"} and out
                   else low[:1].upper() + low[1:])
    return " ".join(out).replace("'S ", "'s ")


def _near(lat, lon, box, margin_m=BARRIER_NEAR_M):
    (s, w, n, e) = box
    dlat = margin_m / 111_000.0
    dlon = margin_m / (111_000.0 * math.cos(math.radians(51.5)))
    return (s - dlat) <= lat <= (n + dlat) and (w - dlon) <= lon <= (e + dlon)


def blue_book_failure_reason(*, demoted=(), loop_demotions=(), hard_gap_names=(),
                             sanity_reasons=(), legal=True, rev_legal=True,
                             taxi_violations=(), rev_taxi_violations=(),
                             rules=None, run_box=None, no_route=False) -> str:
    """One sentence saying why the Blue Book route of a run can't ship.

    The most specific cause wins: a closed bridge, then a taxi-legality
    violation on the Blue Book route itself, then the prescribed street the
    ordered search had to give up (blamed on a modal filter if the taxi rules
    have one on that street near the run), then the sanity gate, then turn
    legality.
    """
    names = [str(d[0] if isinstance(d, (list, tuple)) else d)
             for d in list(demoted) + list(loop_demotions) + list(hard_gap_names)]
    if any("HAMMERSMITH BRIDGE" in n.upper() for n in names):
        return HAMMERSMITH_BRIDGE_REASON
    if no_route:
        return "The Blue Book sequence has no legal route on today's roads"
    for v in list(taxi_violations):
        return describe_violation(v)

    loop_names = [str(d[0] if isinstance(d, (list, tuple)) else d)
                  for d in loop_demotions]
    gap_names = [n for n in (str(d[0] if isinstance(d, (list, tuple)) else d)
                             for d in hard_gap_names)]
    blamed = list(dict.fromkeys(loop_names + gap_names))
    if blamed and rules is not None:
        by_name = rules.names_with_barriers()
        for name in blamed:
            for node in by_name.get(name.upper(), []):
                b = rules.barriers.get(str(node)) or {}
                if run_box is None or _near(b.get("lat", 0), b.get("lon", 0), run_box):
                    return describe_barrier(b.get("reason"), _title(name))
        for name in blamed:
            for lat, lon, _hw in rules.filtered_sections(name):
                if run_box is None or _near(lat, lon, run_box):
                    return describe_filtered_section(_title(name))
    if loop_names:
        return (f"The Blue Book order at {_title(loop_names[0])} needs a loop "
                "of 1 km or more on today's roads")
    if gap_names:
        return (f"The Blue Book route can no longer reach {_title(gap_names[0])} "
                "in order")
    if sanity_reasons:
        reason = str(sanity_reasons[0])
        for prefix in ("AB ", "BA "):
            if reason.startswith(prefix):
                reason = reason[len(prefix):]
        return f"The Blue Book route fails the sanity check ({reason})"
    if not legal or not rev_legal:
        return "The Blue Book route needs a banned turn"
    for v in list(rev_taxi_violations):
        return f"Reverse run: {describe_violation(v)}"
    return "The Blue Book route fails QA"
