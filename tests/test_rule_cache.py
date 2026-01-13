import datetime

from pathlib import Path

from tally.merchant_engine import load_merchants_file
from tally.rule_cache import RuleCache


def test_rule_cache_rebuild_and_counts(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    rules_path = config_dir / "merchants.rules"
    rules_path.write_text(
        """[Netflix]
match: contains("NETFLIX")
category: Subscriptions

[Spotify]
match: contains("SPOTIFY")
category: Subscriptions
""",
        encoding="utf-8",
    )

    engine = load_merchants_file(rules_path)
    transactions = [
        {
            "description": "NETFLIX",
            "raw_description": "NETFLIX",
            "amount": -12.34,
            "date": datetime.date(2025, 1, 5),
            "source": "Test",
        }
    ]

    cache = RuleCache(config_dir)
    cache.rebuild(rules_path, [], engine, transactions)

    assert cache.is_valid(rules_path, [], require_data=True)

    rules = cache.get_rules()
    assert [rule.name for rule in rules] == ["Netflix", "Spotify"]

    counts = cache.get_match_counts()
    assert counts["Netflix"] == 1
    assert counts["Spotify"] == 0

    unused = cache.get_unused_rules()
    assert [rule.name for rule in unused] == ["Spotify"]
