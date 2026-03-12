"""
Tests for BLOCKED_MARKETS in config_production.py.

Prevents chronic-loser markets from being accidentally un-blocked
or new entries being added as integers instead of strings.
"""
from config_production import BLOCKED_MARKETS


# Markets that must never be un-blocked (documented heavy losers)
KNOWN_LOSERS = [
    '1402904',  # 0% win, -$150
    '1402902',  # 0% win, -$150
    '1403073',  # 2% win, -$480 (52 trades)
    '1403228',  # 0% win, -$917
    '1403232',  # 20% win, -$884
    '1403143',  # 17% win, -$873
    '1445001',  # 0% win, -$942.95
    '1447205',  # 14% win, -$958.75
    '1448902',  # 17% win, -$873
    '1448693',  # 17% win, -$917
    '1450993',  # 14% win, -$919
    '1451181',  # 14% win, -$906
    '1450921',  # 12% win, -$889
    '1411624',  # 20% win, -$870
    '1437414',  # 17% win, -$686
    '1445053',  # 17% win, -$597
    '1487045',  # 0% win, -$468 (added 2026-03-06)
    '1487027',  # 25% win, -$375 (added 2026-03-06)
]


def test_blocked_markets_minimum_count():
    """Must have at least 18 blocked markets — all documented losers."""
    assert len(BLOCKED_MARKETS) >= 18, (
        f"Only {len(BLOCKED_MARKETS)} markets blocked. "
        "Looks like losers were accidentally removed."
    )


def test_all_known_losers_are_blocked():
    """Every documented chronic loser must remain in the blocklist."""
    for market_id in KNOWN_LOSERS:
        assert market_id in BLOCKED_MARKETS, (
            f"Market {market_id} is a documented chronic loser but is NOT blocked! "
            "This will cost real money."
        )


def test_all_entries_are_strings():
    """All market IDs must be strings, not integers."""
    for entry in BLOCKED_MARKETS:
        assert isinstance(entry, str), (
            f"Entry {entry!r} is type {type(entry).__name__}, expected str. "
            "Integer IDs will not match string comparisons at scan time."
        )


def test_1403073_is_blocked():
    """Spot-check: 52-trade 2%-win chronic loser must be blocked."""
    assert '1403073' in BLOCKED_MARKETS


def test_recent_losers_are_blocked():
    """2026-03-06 additions must be present."""
    assert '1487045' in BLOCKED_MARKETS
    assert '1487027' in BLOCKED_MARKETS


def test_no_empty_strings():
    """No empty strings in blocklist."""
    for entry in BLOCKED_MARKETS:
        assert entry.strip() != "", "Empty string found in BLOCKED_MARKETS!"
