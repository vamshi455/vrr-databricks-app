"""Pattern geometry — the picture an engineer draws on a whiteboard, from real columns.

A waterflood pattern is normally *shown* the way it is *reasoned about*: the injector in
the middle, the producers it sweeps toward arranged around it, and a line between them
whose weight is how much of that producer belongs to this pattern. That is a topology
diagram, and this repo has exactly the data for it — `completion_type` /  derived role,
and `pattern_contribution_factor`.

What this repo does NOT have is geometry: no coordinates, no deviation surveys, no
perforation depths, no formation tops. So this module places wells from CONTRIBUTION,
never from position, and the caller is expected to label the result a schematic. Drawing
a map-like cross-section out of invented coordinates would be a figure with no
provenance behind it, which is the one thing the rest of this codebase exists to prevent.

Two things are honest to encode, and both are:

* **Which canonical pattern this is.** One injector with four producers *is* a five-spot;
  with six, a seven-spot; with eight, a nine-spot; with two, a line drive. When the count
  matches, the producers go on the textbook angles, so the figure looks like the diagram
  in every reservoir-engineering text. Otherwise they are spaced evenly and it is called
  irregular — which is the truth for most real patterns.
* **How strongly each producer belongs.** Radius falls as the contribution factor rises,
  so the wells this pattern really owns sit close in and the marginal, shared ones sit
  out on the rim. Distance means allocation, not feet.

Pure: no I/O, no DB, no randomness — same completions in, same picture out, which is what
lets the README render and the running app agree.
"""
from __future__ import annotations

import math

# Textbook angles, degrees clockwise from north, keyed by producer count. A five-spot is
# a square with the injector at its centre, so its producers sit on the corners (45°) and
# not on the axes — get this wrong and the shape reads as a four-spot, which is not a
# thing. Anything not listed here falls back to even spacing.
CANONICAL: dict[int, tuple[str, list[float]]] = {
    2: ("line_drive", [90.0, 270.0]),
    3: ("triangular", [0.0, 120.0, 240.0]),
    4: ("five_spot", [45.0, 135.0, 225.0, 315.0]),
    6: ("seven_spot", [0.0, 60.0, 120.0, 180.0, 240.0, 300.0]),
    8: ("nine_spot", [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0]),
}

LABELS: dict[str, str] = {
    "line_drive": "Line drive",
    "triangular": "Triangular (three-spot)",
    "five_spot": "Five-spot",
    "seven_spot": "Seven-spot",
    "nine_spot": "Nine-spot",
    "irregular": "Irregular",
    "multi_injector": "Multi-injector",
    "no_injector": "No injector this period",
}

# Ring radius as a fraction of the plot half-width. A factor of 1.0 (the completion is
# wholly this pattern's) lands at 0.62; a factor of 0 drifts out to 0.98. The band is
# deliberately narrow — far enough that the ordering is legible, close enough that a
# weakly-shared well still reads as part of the pattern rather than as an outlier.
R_NEAR, R_FAR = 0.62, 0.98


# Width of one caption character, in the same units as the coordinates (half_width=100).
# Measured off the rendered SVG rather than guessed: at the caption's font size an
# uppercase well name like "ARCTURUS-I3" comes out ~5.1 units per character. Two
# successive guesses at this number were both too small, and the labels ran together.
CHAR_W = 5.1
# The second caption line, "f 0.52 · 28.0%": a fixed 14 glyphs at the smaller size.
SUB_CHARS, SUB_CHAR_W = 14, 4.9


def _hub_radius(n_injectors: int, max_name: int) -> float:
    """How wide the ring of injectors at the centre has to be.

    A fixed small radius put three injectors on top of each other with their captions
    merged into one unreadable string. Two things drive the answer, and the second is the
    one a count-only formula misses: the ring must be big enough that adjacent *labels*
    clear each other, so a pattern whose wells have long names needs a wider ring than
    one with short names at the same count.

    Adjacent nodes on a ring of radius R sit `2·R·sin(π/n)` apart, so solve that for the
    width the caption actually needs. Producers then move out by the same amount.
    """
    if n_injectors <= 1:
        return 0.0
    # Whichever of the two caption lines is wider. The value line is a fixed shape —
    # "f 0.52 · 28.0%" — and at 14 characters it is wider than most well names, which is
    # exactly how ALIOTH-I1 and ALIOTH-I2 ended up touching while their names did not.
    needed = max(CHAR_W * max_name, SUB_CHARS * SUB_CHAR_W) + 6.0
    by_label = needed / (2.0 * math.sin(math.pi / n_injectors)) / 100.0
    return max(0.17 + 0.085 * (n_injectors - 1), by_label)


def _polar(angle_deg: float, radius: float) -> tuple[float, float]:
    """Degrees-clockwise-from-north → SVG x/y in a −100…100 box, y already flipped."""
    rad = math.radians(angle_deg)
    return round(radius * math.sin(rad), 2), round(-radius * math.cos(rad), 2)


def classify(n_injectors: int, n_producers: int) -> str:
    """Name the pattern from its well counts alone — the same call an engineer makes."""
    if n_injectors == 0:
        return "no_injector"
    if n_injectors > 1:
        return "multi_injector"
    named = CANONICAL.get(n_producers)
    return named[0] if named else "irregular"


def build(completions: list[dict], *, half_width: float = 100.0) -> dict:
    """Place every completion of one pattern and name the shape they make.

    `completions` are rows as `tools.list_completions` returns them: `role`, `factor`,
    `share_of_production` / `share_of_injection`, `pvt_methods`, and optionally
    `completion_name` and `n_patterns`. Anything missing degrades to a safe default
    rather than raising — a half-populated period should still draw.
    """
    injectors = [c for c in completions if c.get("role") == "injector"]
    producers = [c for c in completions if c.get("role") == "producer"]
    idle = [c for c in completions if c.get("role") not in ("injector", "producer")]

    # Biggest contributor first, then by id, so the figure is stable across reloads and
    # the dominant well is always the one at the top of the ring.
    producers.sort(key=lambda c: (-(c.get("share_of_production") or 0.0),
                                  c.get("completion_id", "")))
    injectors.sort(key=lambda c: (-(c.get("share_of_injection") or 0.0),
                                  c.get("completion_id", "")))

    geometry = classify(len(injectors), len(producers))
    named = CANONICAL.get(len(producers))
    angles = (list(named[1]) if named and len(injectors) == 1
              else [i * 360.0 / max(len(producers), 1) for i in range(len(producers))])

    nodes: list[dict] = []

    # Injectors hold the middle. One sits dead centre; several ride an inner ring sized
    # to fit them, which is what a multi-injector pattern looks like on a plat.
    hub = _hub_radius(
        len(injectors),
        max((len(str(c.get("completion_name") or c.get("completion_id", ""))[:14])
             for c in injectors), default=0),
    )
    for i, c in enumerate(injectors):
        if len(injectors) == 1:
            x, y = 0.0, 0.0
        else:
            x, y = _polar(i * 360.0 / len(injectors), half_width * hub)
        nodes.append(_node(c, "injector", x, y))

    # Producers clear the whole injector ring, not just the centre point.
    near, far = R_NEAR + hub * 0.9, R_FAR + hub * 0.9
    for c, angle in zip(producers, angles):
        factor = _clamp(c.get("factor"), 1.0)
        radius = half_width * (far - (far - near) * factor)
        x, y = _polar(angle, radius)
        nodes.append(_node(c, "producer", x, y))

    # Idle completions are real — a well shut in for the month still belongs to the
    # pattern — but they carry no flow, so they park on the rim and draw no connector.
    for i, c in enumerate(idle):
        x, y = _polar(i * 360.0 / max(len(idle), 1) + 22.5, half_width * (far + 0.14))
        nodes.append(_node(c, "idle", x, y))

    # ONE sweep line per producer, drawn from the centre of the injection group rather
    # than from each injector in turn. Two reasons, and the second is the real one:
    # three injectors against eight producers is twenty-four crossing lines and reads as
    # noise — but more importantly, the allocation data does not say which injector feeds
    # which producer. A line per pair would draw twenty-four relationships the database
    # has no opinion about. The pattern's injection as a whole sweeps toward each
    # producer, and that is exactly what one line from the hub says.
    links = [
        {
            "to": p["completion_id"],
            "factor": _clamp(p.get("factor"), 1.0),
            "share_of_production": p.get("share_of_production") or 0.0,
        }
        for p in producers
    ]

    return {
        "geometry": geometry,
        "geometry_label": LABELS[geometry],
        "n_injectors": len(injectors), "n_producers": len(producers), "n_idle": len(idle),
        "nodes": nodes, "links": links,
        # Where the sweep lines start: the centre of the injection group. Emitted so the
        # renderer never has to work out the geometry for itself.
        "hub": {"x": 0.0, "y": 0.0, "radius": round(half_width * hub, 2)},
        "shared": sorted(n["completion_name"] for n in nodes if n["shared"]),
        "low_confidence": sorted(n["completion_name"] for n in nodes if n["low_confidence"]),
        # Said in one line so the caption cannot drift from the picture it explains.
        "caption": _caption(geometry, len(injectors), len(producers), len(idle)),
        "is_schematic": True,
    }


def _node(c: dict, role: str, x: float, y: float) -> dict:
    cid = c.get("completion_id", "")
    share = (c.get("share_of_injection") if role == "injector"
             else c.get("share_of_production")) or 0.0
    return {
        "completion_id": cid,
        "completion_name": c.get("completion_name") or cid[:8],
        "role": role,
        "x": x, "y": y,
        "factor": _clamp(c.get("factor"), 1.0),
        "share": share,
        # Node area tracks the reservoir barrels it moved, so the well doing the work is
        # visibly the biggest thing on the plot. Square-rooted: area, not radius, is what
        # the eye compares.
        "size": round(0.45 + 0.55 * math.sqrt(min(max(share, 0.0), 1.0)), 3),
        "res_bbl": (c.get("inj_res") if role == "injector" else c.get("prod_res")) or 0.0,
        "shared": (c.get("n_patterns") or 1) > 1,
        "n_patterns": c.get("n_patterns") or 1,
        "low_confidence": "extrapolated" in (c.get("pvt_methods") or ""),
        "pvt_methods": c.get("pvt_methods") or "",
    }


def _clamp(v: float | None, default: float) -> float:
    if v is None:
        return default
    return round(min(max(float(v), 0.0), 1.0), 4)


def _caption(geometry: str, n_inj: int, n_prod: int, n_idle: int) -> str:
    wells = f"{n_inj} injector{'s' if n_inj != 1 else ''}, {n_prod} producer" \
            f"{'s' if n_prod != 1 else ''}"
    if n_idle:
        wells += f", {n_idle} idle"
    if geometry in ("irregular", "multi_injector", "no_injector"):
        return f"{LABELS[geometry]} — {wells}. Placement follows contribution factor."
    return (f"{LABELS[geometry]} — {wells}, on the textbook angles. "
            "Distance from the injector is allocation, not feet.")
