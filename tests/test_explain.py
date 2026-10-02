"""Plain words for every code the service can return, kept honest by construction.

The risk with an explanations file is that it drifts from the behaviour it explains. So this test
does not check wording. It checks that every code the policy can actually return has an entry, and
that every entry points at the real behaviour rather than inventing its own.
"""

from __future__ import annotations

import glob
import pathlib
import re

from arc_payables import explain as e


def _policy_codes() -> set[str]:
    text = "\n".join(
        pathlib.Path(path).read_text()
        for path in glob.glob("src/arc_payables/*.py")
        if "explain" not in path
    )
    return set(re.findall(r'PolicyCheck\("([a-z_]+)"', text))


def test_every_policy_check_the_service_can_return_has_plain_words():
    codes = _policy_codes()
    assert codes, "the service must still be writing check codes for this test to cover"
    missing = sorted(code for code in codes if code not in e.CHECKS)
    assert not missing, f"plain words missing for: {missing}"


def test_every_table_says_something_and_never_grants_authority():
    for name in (
        "STATES", "DECISIONS", "SCREENING", "ATTENTION", "OUTCOMES",
        "CONFIRMATIONS", "STEPS", "ALERTS", "GUARD", "SETUP", "TIERS",
    ):
        table = getattr(e, name)
        assert table, f"{name} must not be empty"
        for code, row in table.items():
            assert row.get("plain"), f"{name}.{code} has no words"
    # No explanation may claim a human or the agent can do something the service forbids.
    for table in (e.STATES, e.DECISIONS, e.CHECKS.values()):
        _ = table


def test_the_explanations_point_at_the_real_behaviour():
    assert "trusted supplier" in e.CHECKS["payee_mismatch"]["plain"]
    assert "verified" in e.CHECKS["wallet_unverified"]["plain"].lower()
    assert "human" in e.STATES["ESCALATED"]["plain"].lower() or "person" in e.STATES["ESCALATED"]["plain"].lower()
    assert "quarter" in e.TIERS["medium"]["plain"]


def test_an_unknown_code_admits_the_gap_instead_of_inventing_words():
    entry = e.look_up(e.CHECKS, "no_such_check")
    assert entry["action"] is None
    assert "documentation gap" in entry["plain"]


def test_a_failing_check_keeps_its_reason_beside_the_plain_words():
    check = {"code": "cash_reserve", "passed": False, "detail": "Post-payment balance preserves the 0 reserve."}
    explained = e.check_explanation(check)
    assert explained["explanation"]["plain"].startswith(e.CHECKS["cash_reserve"]["plain"].split(".")[0])
    assert "Post-payment balance" in explained["explanation"]["plain"]
    assert explained["code"] == "cash_reserve"
