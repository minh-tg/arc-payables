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
        "CONFIRMATIONS", "STEPS", "PASSES", "ALERTS", "WRITEBACK", "GUARD", "SETUP", "TIERS", "CONCEPTS", "AUDIT",
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


def test_the_writeback_words_match_which_disables_are_permanent():
    """The console promises a retry to the operator. That promise has to match the worker.

    If a code moved into the permanent set while its words still said the worker would ask again,
    the screen would tell someone to wait for something that has stopped happening.
    """
    from arc_payables import worker

    assert set(e.WRITEBACK) == {"NETWORK_FEE_UNAVAILABLE", "ACCOUNTING_MAPPING_INCOMPLETE"}
    assert "ACCOUNTING_MAPPING_INCOMPLETE" in worker.PERMANENT_WRITEBACK_DISABLES
    assert "NETWORK_FEE_UNAVAILABLE" not in worker.PERMANENT_WRITEBACK_DISABLES
    assert "asks the provider again" in e.WRITEBACK["NETWORK_FEE_UNAVAILABLE"]["action"]
    assert "refuse again" in e.WRITEBACK["ACCOUNTING_MAPPING_INCOMPLETE"]["action"]


def test_a_pass_outcome_is_never_worded_as_a_payment_outcome():
    """Both vocabularies contain "failed", and they do not mean the same thing.

    A failed pass stopped the loop. A failed payment is money that did not move. Reading one as the
    other tells an operator that nothing was sent when the truth is that a loop gave up, so the two
    are held apart here.
    """
    assert set(e.PASSES) == {"ok", "degraded", "failed"}
    assert e.PASSES["failed"]["plain"] != e.OUTCOMES["failed"]["plain"]
    assert "pass stopped" in e.PASSES["failed"]["plain"]
    assert "payment did not go through" in e.OUTCOMES["failed"]["plain"]
    # A clean pass is not a payment outcome at all, so looking one up must not invent anything.
    assert "ok" not in e.OUTCOMES
    assert e.PASSES["ok"].get("action") is None


def test_the_worker_reads_pass_outcomes_from_the_pass_table():
    """The screen must use PASSES for passes and OUTCOMES for payments, not whichever is handy."""
    worker = (pathlib.Path("src/arc_payables/web") / "worker.js").read_text()
    assert "plainWords('passes'" in worker
    assert "plainWords('outcomes'" not in worker


def test_every_published_table_reaches_a_screen():
    """A table the console never renders is a table nobody reads.

    Seven of these were served, tested against the real vocabulary, and shown on no screen at all,
    so an operator still read 'writeback · 3 failed' with the words for it sitting one HTTP call
    away. The console has to consume what it publishes.
    """
    sources = "\n".join(
        path.read_text() for path in pathlib.Path("src/arc_payables/web").glob("*.js")
    )
    unused = []
    for name in e.PUBLISHED:
        kind = name.lower()
        if name == "CONCEPTS":
            # Rendered through concept(), keyed by the term rather than by the table.
            if "concept(" not in sources:
                unused.append(kind)
            continue
        if not any(
            f"{helper}('{kind}'" in sources
            for helper in ("explain", "nextStep", "plainWords")
        ):
            unused.append(kind)
    assert not unused, f"published but rendered on no screen: {unused}"


def test_the_published_list_matches_the_tables_that_exist():
    for name in e.PUBLISHED:
        assert isinstance(getattr(e, name, None), dict), f"PUBLISHED names {name}, which is not a table"


def test_an_unknown_code_admits_the_gap_instead_of_inventing_words():
    entry = e.look_up(e.CHECKS, "no_such_check")
    assert entry["action"] is None
    assert "documentation gap" in entry["plain"]


def _concepts_the_console_names() -> set[str]:
    """Every concept key the browser asks the backend to define."""
    named: set[str] = set()
    for path in pathlib.Path("src/arc_payables/web").glob("*.js"):
        text = path.read_text()
        named |= set(re.findall(r"concept\(\s*'([a-z_]+)'", text))
        named |= set(re.findall(r"concept:\s*'([a-z_]+)'", text))
    return named


def test_every_concept_the_console_names_has_a_definition():
    """A concept key with no definition renders nothing at all, and silently.

    This is the failure mode server-side tests cannot see: the term draws its dotted underline, the
    reader points at it, and the popover is empty. The backend has to hold every key the console
    names, so the two cannot drift apart.
    """
    named = _concepts_the_console_names()
    assert named, "the console must still be naming concepts for this test to cover"
    missing = sorted(name for name in named if name not in e.CONCEPTS)
    assert not missing, f"the console names concepts with no definition: {missing}"


def test_the_glossary_covers_the_money_itself():
    """The reader may have no idea what USDC is. That is the point of the guided view."""
    for term in ("usdc", "stablecoin", "wallet", "testnet"):
        assert term in e.CONCEPTS, f"a beginner needs {term} defined"
    assert "digital dollar" in e.CONCEPTS["usdc"]["plain"]


def test_the_concepts_agree_with_the_behaviour_they_describe():
    """Definitions drift the same way explanations do, so they are pinned to the real rules."""
    def whole(code: str) -> str:
        row = e.CONCEPTS[code]
        return f"{row['plain']} {row.get('action') or ''}"

    # Three caps, named the same way the guard table names them.
    assert "per recipient" in whole("guard")
    assert "per recipient" in e.GUARD["caps"]["plain"]
    # An unclear screening earns a quarter of the limit, exactly as the tier table says.
    assert "quarter" in whole("tier")
    assert "quarter" in e.TIERS["medium"]["plain"]
    # A flagged counterparty earns nothing, in both places.
    assert "nothing" in whole("tier")
    assert "nothing" in e.TIERS["high"]["plain"]
    # Screening that never ran is not a clearance, which is the whole reason invoices escalate here.
    assert "never ran" in whole("screening")
    assert "never ran" in e.SCREENING["UNAVAILABLE"]["plain"]


def test_a_failing_check_keeps_its_reason_beside_the_plain_words():
    check = {"code": "cash_reserve", "passed": False, "detail": "Post-payment balance preserves the 0 reserve."}
    explained = e.check_explanation(check)
    assert explained["explanation"]["plain"].startswith(e.CHECKS["cash_reserve"]["plain"].split(".")[0])
    assert "Post-payment balance" in explained["explanation"]["plain"]
    assert explained["code"] == "cash_reserve"
