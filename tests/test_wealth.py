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


def test_zero_and_negative_place_at_the_bottom():
    for nw in (0, -5):
        p = place_one(nw, "india")
        assert p.rank == p.adults
        assert p.top_pct == pytest.approx(100.0)
        assert p.band_index == 0


def test_head_count_clamped_to_population():
    # A tiny positive net worth extrapolates below the lowest anchor but can
    # never imply more adults than exist.
    p = place_one(1000, "singapore")  # Rs 1,000
    assert p.rank <= p.adults
    assert p.top_pct <= 100.0


def test_extreme_wealth_tops_the_geography():
    # Beyond any plausible fortune, you'd be rarer than 1 in the whole population.
    p = place_one(1_000_000 * CRORE, "india")
    assert p.rank < 1
    assert p.one_in is None or p.one_in > p.adults


def test_edges_cover_all_bands():
    assert len(BAND_EDGES_CR) == 10
    assert all(math.isfinite(e) for e in BAND_EDGES_CR)
