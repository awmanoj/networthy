"""Tests for the net-worth percentile ranking.

Pins the placement contract so tuning the interpolation later can't silently
regress: band counts must reconcile to the population totals, placement must be
monotonic and land in the right band, and the known anchor points must reproduce
their tabulated head-counts exactly.
"""

import math

import pytest

from app.wealth import (
    BAND_COUNTS,
    BAND_EDGES_CR,
    BASE_FLOOR,
    CRORE,
    GEO_META,
    GEO_ORDER,
    place_one,
    rank_net_worth,
)


def test_band_counts_reconcile_to_population():
    # A data-entry guard: each geography's bands must sum to its adult population.
    for geo in GEO_ORDER:
        assert sum(BAND_COUNTS[geo]) == GEO_META[geo]["adults"], geo


def test_rank_covers_every_geography_in_order():
    placements = rank_net_worth(10 * CRORE)
    assert [p.geo for p in placements] == GEO_ORDER


def test_anchor_boundary_reproduces_tabulated_head_count():
    # At an exact band edge, the head-count is the tail sum above it — no
    # interpolation error. Rs 10 cr in India: bands above 10 cr sum to 875,045.
    p = place_one(10 * CRORE, "india")
    assert p.rank == pytest.approx(875_045, rel=1e-9)
    assert p.top_pct == pytest.approx(875_045 / 1_000_000_000 * 100, rel=1e-9)


def test_top_edge_head_count_is_the_billionaire_band():
    # The highest anchor (> Rs 10,000 cr) equals that band's raw count.
    p = place_one(10_000 * CRORE, "india")
    assert p.rank == pytest.approx(BAND_COUNTS["india"][-1], rel=1e-9)  # 205


def test_placement_is_monotonic_in_net_worth():
    # More money -> never a larger top_pct (you can only move up the tail).
    prev = None
    for cr in [0.5, 1, 3, 10, 50, 100, 1_000, 10_000, 50_000]:
        p = place_one(cr * CRORE, "india")
        if prev is not None:
            assert p.top_pct <= prev + 1e-9
        prev = p.top_pct


def test_band_index_and_label_match_the_amount():
    assert place_one(0.5 * CRORE, "india").band_label == "< ₹1 cr"
    assert place_one(3 * CRORE, "india").band_label == "₹1–5 cr"
    assert place_one(12 * CRORE, "india").band_label == "₹10–25 cr"
    assert place_one(20_000 * CRORE, "india").band_label == "> ₹10,000 cr"


def test_same_money_is_more_exclusive_in_india_than_usa():
    # The headline insight: Rs 10 cr is far rarer in India than in the USA.
    india = place_one(10 * CRORE, "india")
    usa = place_one(10 * CRORE, "usa")
    assert india.top_pct < usa.top_pct
    assert india.top_pct == pytest.approx(0.0875, abs=0.01)
    assert usa.top_pct == pytest.approx(8.69, abs=0.1)


def test_richer_than_and_one_in_are_consistent():
    p = place_one(10 * CRORE, "india")
    assert p.richer_than_pct == pytest.approx(100 - p.top_pct)
    assert p.one_in == pytest.approx(p.adults / p.rank, rel=1e-9)


def test_band_split_partitions_the_band_around_you():
    # ₹21 cr sits in the ₹10-25 cr band; that band's total must split into the
    # adults above you and below you, and match the tabulated band population.
    p = place_one(21 * CRORE, "india")
    assert p.band_label == "₹10–25 cr"
    assert p.band_total == BAND_COUNTS["india"][3]                 # 581,000
    assert p.band_above + p.band_below == pytest.approx(p.band_total)
    # richer-than-you within the band = rank minus the tail above the whole band.
    tail_above = sum(BAND_COUNTS["india"][4:])
    assert p.band_above == pytest.approx(p.rank - tail_above)
    # Near the top of a wide band, most of the band is below you.
    assert p.band_below > p.band_above


def test_band_split_edges_are_bounded():
    # At a band's lower edge you're at the bottom: (almost) all of the band is above.
    lo = place_one(10 * CRORE, "india")   # exactly the ₹10-25 cr floor
    assert lo.band_below == pytest.approx(0.0, abs=1.0)
    assert lo.band_above == pytest.approx(lo.band_total, rel=1e-6)
    # Never negative, never exceeds the band.
    for cr in (0.5, 3, 12, 40, 800, 20_000):
        p = place_one(cr * CRORE, "india")
        assert 0.0 <= p.band_above <= p.band_total + 1
        assert 0.0 <= p.band_below <= p.band_total + 1


def test_zero_and_negative_place_at_the_bottom():
    for nw in (0, -5):
        p = place_one(nw, "india")
        assert p.rank == p.adults
        assert p.top_pct == pytest.approx(100.0)
        assert p.band_index == 0


def test_at_or_below_floor_is_whole_population():
    # At/below the base floor we treat essentially everyone as at-or-above.
    for nw in (BASE_FLOOR, BASE_FLOOR / 2, 1000):
        p = place_one(nw, "singapore")
        assert p.rank == pytest.approx(p.adults)
        assert p.top_pct == pytest.approx(100.0)


def test_base_band_is_bounded_and_monotonic():
    # Below Rs 1 cr the estimate stays within [Rs 1 cr-club size, population] and
    # never saturates the whole population prematurely (the bug the fix addresses).
    club = float(sum(BAND_COUNTS["india"][1:]))  # adults with >= Rs 1 cr
    pop = GEO_META["india"]["adults"]
    p5 = place_one(5_00_000, "india")   # Rs 5 lakh
    assert club < p5.rank < pop         # strictly inside — not clamped to pop
    assert 0 < p5.top_pct < 100
    # More money within the base band -> fewer people above.
    assert place_one(50_00_000, "india").rank < p5.rank


def test_base_band_is_continuous_at_one_crore():
    # The base interpolation meets the tail exactly at the Rs 1 cr anchor.
    p = place_one(CRORE, "india")
    assert p.rank == pytest.approx(sum(BAND_COUNTS["india"][1:]), rel=1e-9)


def test_extreme_wealth_tops_the_geography():
    # Beyond any plausible fortune, you'd be rarer than 1 in the whole population.
    p = place_one(1_000_000 * CRORE, "india")
    assert p.rank < 1
    assert p.one_in is None or p.one_in > p.adults


def test_edges_cover_all_bands():
    assert len(BAND_EDGES_CR) == 10
    assert all(math.isfinite(e) for e in BAND_EDGES_CR)
