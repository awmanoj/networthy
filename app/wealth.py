"""Net-worth distribution data + percentile ranking for the "Where do you stand?" feature.

Source: `wealth_distribution.xlsx` — UBS Global Wealth Report 2025/26, Knight Frank
Wealth Report, Forbes Billionaires, with a modeled Pareto tail between anchors.
Adults age 20+. Bands are defined in INR crore; USD equivalents use ₹96.5/$1 (RBI
reference rate, ~Jul 2026). These are modeled estimates, not a census — treat the
top bands as order-of-magnitude.

`rank_net_worth()` places a net worth within each geography. The band table is
coarse (11 bands), so between the known band edges we interpolate with a piecewise
power law (Pareto): the wealth tail follows N(>=w) = N0 * (w/w0)^(-alpha), so the
head-count is log-log linear between adjacent anchors. Above the highest anchor we
extrapolate with the last segment's slope. Below the lowest anchor (< ₹1 cr) the
workbook has no sub-band detail, so we interpolate log-log down to a floor at which
essentially the whole adult population is at-or-above (see ``BASE_FLOOR``) — bounded
and monotonic, and flagged as approximate in the UI. This mirrors how the workbook
itself built the bands, and keeps the placement smooth instead of jumping at
band boundaries.

The web layer computes placements client-side too (see static/standing.js) for live
interactivity via the same algorithm and constants; this module is the canonical,
tested reference.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

CRORE = 10_000_000  # INR in one crore
FX_INR_PER_USD = 96.5  # RBI reference rate used for the USD band equivalents

# Playful default when the visitor has no snapshot to pre-fill from (₹5 crore).
DEFAULT_NET_WORTH = 5 * CRORE

# Below ₹1 cr the workbook gives no sub-band structure (band 0 is just "everyone
# else"), so placement there is a rough interpolation down to this floor, at which
# we treat essentially the whole adult population as at-or-above. Kept in sync with
# the slider minimum in standing.js.
BASE_FLOOR = 100_000  # ₹1 lakh

# Display order for the geographies (India first — the primary audience).
GEO_ORDER = ["india", "indonesia", "singapore", "usa", "world"]

GEO_META: dict[str, dict] = {
    "india": {
        "name": "India", "flag": "\U0001F1EE\U0001F1F3",
        "adults": 1_000_000_000, "adults_label": "≈1 billion adults",
    },
    "indonesia": {
        "name": "Indonesia", "flag": "\U0001F1EE\U0001F1E9",
        "adults": 185_000_000, "adults_label": "≈185 million adults",
    },
    "singapore": {
        "name": "Singapore", "flag": "\U0001F1F8\U0001F1EC",
        "adults": 4_500_000, "adults_label": "≈4.5 million adults",
    },
    "usa": {
        "name": "USA", "flag": "\U0001F1FA\U0001F1F8",
        "adults": 260_000_000, "adults_label": "≈260 million adults",
    },
    "world": {
        "name": "World", "flag": "\U0001F30D",
        "adults": 3_800_000_000,
        "adults_label": "≈3.8 billion adults (UBS 56-market sample)",
    },
}

# Upper edge of each finite band, in crore INR. 11 bands -> 10 finite edges; the
# 11th band (> Rs 10,000 cr) is open-ended above.
BAND_EDGES_CR = [1, 5, 10, 25, 50, 100, 500, 1_000, 5_000, 10_000]

# Human labels for each of the 11 bands (INR crore).
BAND_LABELS = [
    "< ₹1 cr", "₹1–5 cr", "₹5–10 cr", "₹10–25 cr",
    "₹25–50 cr", "₹50–100 cr", "₹100–500 cr",
    "₹500–1,000 cr", "₹1,000–5,000 cr", "₹5,000–10,000 cr",
    "> ₹10,000 cr",
]

# USD equivalents of each band (for the pyramid subtitle), from the workbook.
BAND_USD_LABELS = [
    "< $104k", "$104k – 518k", "$518k – 1.04M", "$1.04M – 2.59M",
    "$2.59M – 5.18M", "$5.18M – 10.4M", "$10.4M – 51.8M",
    "$51.8M – 104M", "$104M – 518M", "$518M – 1.04B",
    "> $1.04B ($ billionaires)",
]

# Adults in each of the 11 bands (Section 1 of the workbook), index 0..10.
# Each row sums to the geography's adult population (asserted in tests).
BAND_COUNTS: dict[str, list[int]] = {
    "india": [973_594_955, 24_000_000, 1_530_000, 581_000, 165_000, 72_500,
              48_400, 4_680, 2_990, 270, 205],
    "indonesia": [179_316_550, 5_030_000, 398_000, 183_000, 44_500, 17_200,
                  9_480, 728, 468, 41, 33],
    "singapore": [2_250_726, 1_680_000, 254_000, 214_000, 58_400, 24_700,
                  15_900, 1_420, 766, 53, 35],
    "usa": [115_595_960, 105_100_000, 16_700_000, 15_970_000, 4_010_000,
            1_590_000, 923_000, 71_400, 36_900, 1_840, 900],
    "world": [3_114_934_040, 563_000_000, 65_000_000, 39_000_000, 10_500_000,
              4_620_000, 2_640_000, 206_000, 91_300, 5_660, 3_000],
}


@dataclass
class Placement:
    """Where a given net worth lands within one geography."""

    geo: str
    name: str
    flag: str
    adults: int
    adults_label: str
    top_pct: float          # you are in the top X% by net worth
    richer_than_pct: float  # you are wealthier than Y% of adults (100 - top_pct)
    rank: float             # approx. adults with at least your net worth (your rank from the top)
    one_in: float | None    # 1 in N adults is at least this wealthy (None if you'd top everyone)
    band_index: int
    band_label: str


def _alpha(w1: float, n1: float, w2: float, n2: float) -> float:
    """Pareto exponent of the segment between two anchors: N(>=w) = n1*(w/w1)^(-alpha)."""
    return -math.log(n2 / n1) / math.log(w2 / w1)


def _anchors(geo: str) -> list[tuple[float, float]]:
    """(boundary_inr, adults_at_or_above) pairs, ascending in wealth.

    N(>= edge_e) is the number of adults in every band strictly above that edge,
    i.e. the tail sum ``counts[e+1:]``.
    """
    counts = BAND_COUNTS[geo]
    return [
        (BAND_EDGES_CR[e] * CRORE, float(sum(counts[e + 1:])))
        for e in range(len(BAND_EDGES_CR))
    ]


def _head_count(net_worth: float, anchors: list[tuple[float, float]], pop: int) -> float:
    """Estimated adults with net worth >= ``net_worth`` (a Pareto head-count)."""
    if net_worth <= 0:
        return float(pop)

    lo_w, _ = anchors[0]
    hi_w, hi_n = anchors[-1]

    if net_worth <= BASE_FLOOR:
        return float(pop)  # at/below the floor, treat as the whole base

    if net_worth < lo_w:
        # Below the lowest data anchor (< Rs 1 cr) the source doesn't model the
        # distribution, so instead of extrapolating the steep tail slope (which
        # overshoots and saturates the whole population), interpolate log-log
        # between two real endpoints: (BASE_FLOOR -> everyone) and the Rs 1 cr
        # anchor. This stays bounded in [anchor, pop] and monotonic. It is only a
        # rough base estimate — see BASE_FLOOR — and the UI flags it as such.
        return float(pop) * (net_worth / BASE_FLOOR) ** (
            -_alpha(BASE_FLOOR, float(pop), lo_w, anchors[0][1])
        )

    if net_worth >= hi_w:
        # Above the top anchor: extend the last segment's slope upward.
        (w1, n1), (w2, n2) = anchors[-2], anchors[-1]
        n = hi_n * (net_worth / hi_w) ** (-_alpha(w1, n1, w2, n2))
        return max(0.0, n)

    for (w1, n1), (w2, n2) in zip(anchors, anchors[1:]):
        if w1 <= net_worth <= w2:
            return n1 * (net_worth / w1) ** (-_alpha(w1, n1, w2, n2))

    return float(pop)  # unreachable given the bracket above


def _band_index(net_worth: float) -> int:
    """Index (0..10) of the band ``net_worth`` falls into."""
    cr = net_worth / CRORE
    for i, edge in enumerate(BAND_EDGES_CR):
        if cr < edge:
            return i
    return len(BAND_EDGES_CR)  # 10 -> the open-ended top band


def place_one(net_worth: float, geo: str) -> Placement:
    """Place ``net_worth`` (INR) within a single geography."""
    meta = GEO_META[geo]
    pop = meta["adults"]
    n = _head_count(net_worth, _anchors(geo), pop)
    n = max(0.0, min(float(pop), n))
    top_pct = n / pop * 100.0
    band_index = _band_index(net_worth)
    return Placement(
        geo=geo,
        name=meta["name"],
        flag=meta["flag"],
        adults=pop,
        adults_label=meta["adults_label"],
        top_pct=top_pct,
        richer_than_pct=100.0 - top_pct,
        rank=n,
        one_in=(pop / n) if n > 0 else None,
        band_index=band_index,
        band_label=BAND_LABELS[band_index],
    )


def rank_net_worth(net_worth: float) -> list[Placement]:
    """Place ``net_worth`` (INR) across every geography, in display order."""
    return [place_one(net_worth, geo) for geo in GEO_ORDER]


def placement_dicts(net_worth: float) -> list[dict]:
    """JSON-serialisable placements (``one_in`` is None when it would be infinite)."""
    return [asdict(p) for p in rank_net_worth(net_worth)]


def client_dataset() -> dict:
    """The raw distribution, shaped for the browser so standing.js can rank live.

    Keeping the constants server-sourced means the client ranking can't drift from
    this tested module — the JS re-implements the same power-law interpolation over
    exactly these numbers.
    """
    return {
        "crore": CRORE,
        "geoOrder": GEO_ORDER,
        "geoMeta": GEO_META,
        "bandEdgesCr": BAND_EDGES_CR,
        "bandLabels": BAND_LABELS,
        "bandUsdLabels": BAND_USD_LABELS,
        "bandCounts": BAND_COUNTS,
    }
