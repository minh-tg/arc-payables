"""Deterministic duplicate and supplier-history signals.

Each test drives `DeterministicPolicy.evaluate` with an explicit `InvoiceHistory`, so the
boundaries (normalization, windows, counts, ratios) are asserted without a model, a network or a
payment provider. The three load-bearing properties are that a same-supplier duplicate is a hard
hold no acknowledgement clears, that a review check is overridable, and that an advisory note
never changes the action or the eligibility of an invoice.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from arc_payables.currency import USDCOnlyConverter
from arc_payables.domain import (
    DecisionAction,
    InvoiceHistory,
    SettledPayment,
    USDC_SCALE,
)
from arc_payables.mock_adapters import MockAccountingConnector, MockPaymentProvider
from arc_payables.policy import (
    ADVISORY_CHECK_CODES,
    OVERRIDABLE_CHECKS,
    DeterministicPolicy,
    compute_evidence_hash,
    normalize_invoice_number,
)
from arc_payables.ports import AgentRecommendation
from arc_payables.prioritisation import PaymentPrioritiser
from arc_payables.seed import seed_demo
from arc_payables.service import APWorkflow
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore
from arc_payables.seed import _erp_payable, _replace_amount

TODAY = date.today()
ROOT = Path(__file__).resolve().parents[1]


def _units(amount: str) -> int:
    return int(Decimal(amount) * USDC_SCALE)


def _recommendation() -> AgentRecommendation:
    return AgentRecommendation(action="PAY_NOW", reason="Test recommendation", material_claims=())


@pytest.fixture
def runtime(tmp_path: Path):
    settings = Settings(_env_file=None, database_path=tmp_path / "signals.sqlite3")
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    legitimate_id, _suspicious_id = seed_demo(store)
    provider = MockPaymentProvider(store)
    accounting = MockAccountingConnector(store)
    policy = DeterministicPolicy(settings, USDCOnlyConverter("USD"))
    workflow = APWorkflow(store, accounting, provider, provider.signer, policy, settings)
    invoice = store.get_invoice(legitimate_id)
    evidence = accounting.get_invoice_evidence(invoice)
    return {
        "settings": settings,
        "store": store,
        "policy": policy,
        "workflow": workflow,
        "invoice": invoice,
        "accounting": evidence,
        "treasury": provider.get_balance(),
        "screening": workflow.screener.screen(evidence.supplier, evidence.supplier.approved_wallet),
    }


def _evaluate(runtime, history=None, *, invoice=None, accounting=None, approval=None, converter=None, settings=None, today=TODAY):
    invoice = invoice or runtime["invoice"]
    accounting = accounting or runtime["accounting"]
    settings = settings or runtime["settings"]
    treasury = runtime["treasury"]
    screening = runtime["screening"]
    policy = DeterministicPolicy(settings, converter or USDCOnlyConverter("USD"))
    return policy.evaluate(
        invoice,
        accounting,
        treasury,
        screening,
        _recommendation(),
        approval,
        now=today,
        history=history if history is not None else InvoiceHistory(),
    )


def _check(decision, code: str):
    return next((check for check in decision.checks if check.code == code), None)


def _settled(runtime, amounts: list[str], *, first_day: int = 10) -> tuple[SettledPayment, ...]:
    supplier_id = runtime["invoice"].supplier_id
    return tuple(
        SettledPayment(f"hist-{index}", supplier_id, _units(amount), TODAY - timedelta(days=first_day + index))
        for index, amount in enumerate(amounts)
    )


# --------------------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------------------


def test_normalization_ignores_case_spacing_punctuation_and_leading_zeros():
    assert normalize_invoice_number("ACME-INV-2026-001") == normalize_invoice_number("acme inv 2026 001")
    assert normalize_invoice_number("PINV-0007") == normalize_invoice_number("pinv-7")
    assert normalize_invoice_number("PINV-0000") == normalize_invoice_number("PINV-0")
    assert normalize_invoice_number("PINV-1") != normalize_invoice_number("PINV-2")
    assert normalize_invoice_number(None) == ""
    assert normalize_invoice_number("  ---  ") == ""
    # NFKC folds full-width digits and letters to their ASCII forms.
    assert normalize_invoice_number("ＰＩＮＶ-０００７") == normalize_invoice_number("PINV-7")


# --------------------------------------------------------------------------------------
# Duplicate numbers
# --------------------------------------------------------------------------------------


def test_same_supplier_duplicate_number_is_a_hard_hold(runtime):
    invoice = runtime["invoice"]
    earlier = replace(invoice, id="earlier-capture", invoice_number="acme inv 2026 001", purchase_invoice_id="PINV-0007")
    decision = _evaluate(runtime, InvoiceHistory(other_invoices=(earlier,)))
    check = _check(decision, "duplicate_number_same_supplier")
    assert check is not None
    assert check.passed is False
    assert check.requires_human is False
    assert check.overridable is False
    assert "PINV-0007" in check.detail
    assert decision.action == DecisionAction.HOLD
    assert "The same invoice number is already recorded for this supplier." in decision.conflicts
    assert "duplicate_number_same_supplier" not in OVERRIDABLE_CHECKS


def test_duplicate_number_look_back_window_boundary(runtime):
    invoice = runtime["invoice"]
    older = replace(
        invoice,
        id="old-duplicate",
        invoice_number="acme inv 2026 001",
        purchase_invoice_id="PINV-0007",
        invoice_date=TODAY - timedelta(days=365),
    )
    assert _check(_evaluate(runtime, InvoiceHistory(other_invoices=(older,))), "duplicate_number_same_supplier") is not None
    too_old = replace(older, invoice_date=TODAY - timedelta(days=366))
    assert _check(_evaluate(runtime, InvoiceHistory(other_invoices=(too_old,))), "duplicate_number_same_supplier") is None


def test_a_reviewer_acknowledgement_cannot_clear_the_duplicate_hold(runtime):
    invoice = runtime["invoice"]
    earlier = replace(invoice, id="earlier-capture", invoice_number="acme inv 2026 001", purchase_invoice_id="PINV-0007")
    approval = {
        "approved": True,
        "evidence_hash": compute_evidence_hash(invoice, runtime["accounting"], runtime["treasury"], runtime["screening"]),
        "acknowledged_checks": ["duplicate_number_same_supplier"],
    }
    decision = _evaluate(runtime, InvoiceHistory(other_invoices=(earlier,)), approval=approval)
    assert _check(decision, "duplicate_number_same_supplier").passed is False
    assert decision.action == DecisionAction.HOLD


def test_other_supplier_duplicate_number_is_advisory_only(runtime):
    invoice = runtime["invoice"]
    other = replace(invoice, id="other-supplier", supplier_id="SUP-OTHER-002", invoice_number="acme inv 2026 001")
    decision = _evaluate(runtime, InvoiceHistory(other_invoices=(other,)))
    check = _check(decision, "duplicate_number_other_supplier")
    assert check is not None
    assert check.passed is True
    assert check.requires_human is False
    assert decision.action == DecisionAction.PAY_NOW


# --------------------------------------------------------------------------------------
# Similar amounts
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "noted"),
    [("250", True), ("245", True), ("244", False), ("150", False)],
)
def test_similar_amount_tolerance_boundary(runtime, amount, noted):
    invoice = runtime["invoice"]
    other = replace(invoice, id="recent-same-supplier", invoice_number="ACME-INV-2026-009", amount_units=_units(amount))
    check = _check(_evaluate(runtime, InvoiceHistory(other_invoices=(other,))), "similar_amount_recent")
    assert (check is not None) is noted


@pytest.mark.parametrize(("age_days", "noted"), [(0, True), (30, True), (31, False)])
def test_similar_amount_date_window_boundary(runtime, age_days, noted):
    invoice = runtime["invoice"]
    other = replace(
        invoice,
        id="recent-same-supplier",
        invoice_number="ACME-INV-2026-009",
        invoice_date=TODAY - timedelta(days=age_days),
    )
    check = _check(_evaluate(runtime, InvoiceHistory(other_invoices=(other,))), "similar_amount_recent")
    assert (check is not None) is noted


# --------------------------------------------------------------------------------------
# Settlement history
# --------------------------------------------------------------------------------------


def test_four_settled_payments_are_not_enough_and_five_are(runtime):
    four = _evaluate(runtime, InvoiceHistory(settled_payments=_settled(runtime, ["50", "50", "50", "50"])))
    check = _check(four, "history_insufficient")
    assert check is not None and check.passed is True
    assert "Only 4 settled payments" in check.detail
    assert _check(four, "amount_outlier") is None
    assert four.action == DecisionAction.PAY_NOW

    five = _evaluate(runtime, InvoiceHistory(settled_payments=_settled(runtime, ["50", "50", "50", "50", "50"])))
    outlier = _check(five, "amount_outlier")
    assert outlier is not None and outlier.passed is False
    assert outlier.requires_human is True and outlier.overridable is True
    assert five.action == DecisionAction.ESCALATE


def test_amount_outlier_requires_strictly_more_than_the_multiple(runtime):
    invoice = replace(runtime["invoice"], amount_units=_units("300"))
    exact = _evaluate(runtime, InvoiceHistory(settled_payments=_settled(runtime, ["100"] * 5)), invoice=invoice)
    assert _check(exact, "amount_outlier") is None
    above = _evaluate(runtime, InvoiceHistory(settled_payments=_settled(runtime, ["99"] * 5)), invoice=invoice)
    assert _check(above, "amount_outlier") is not None


def test_history_insufficient_when_history_is_missing_or_unconvertible(runtime):
    empty = _check(_evaluate(runtime, InvoiceHistory()), "history_insufficient")
    assert empty is not None and empty.passed is True
    assert "Only 0 settled payments" in empty.detail

    euro = replace(runtime["invoice"], currency="EUR")
    unconvertible = _check(
        _evaluate(runtime, InvoiceHistory(settled_payments=_settled(runtime, ["50"] * 5)), invoice=euro),
        "history_insufficient",
    )
    assert unconvertible is not None and unconvertible.passed is True
    assert "no configured USDC settlement rate" in unconvertible.detail


def test_amount_outlier_acknowledgement_clears_the_review(runtime):
    invoice = runtime["invoice"]
    history = InvoiceHistory(settled_payments=_settled(runtime, ["50"] * 5))
    approval = {
        "approved": True,
        "evidence_hash": compute_evidence_hash(invoice, runtime["accounting"], runtime["treasury"], runtime["screening"]),
        "acknowledged_checks": ["amount_outlier"],
    }
    decision = _evaluate(runtime, history, approval=approval)
    assert _check(decision, "amount_outlier").passed is True
    assert decision.action == DecisionAction.PAY_NOW


# --------------------------------------------------------------------------------------
# Recently changed supplier record
# --------------------------------------------------------------------------------------


def _record_changed_runtime(runtime, days: int, *, window: int = 30):
    settings = Settings(
        _env_file=None,
        database_path=runtime["store"].path,
        risk_supplier_record_review_days=window,
    )
    supplier = replace(runtime["accounting"].supplier, wallet_version=(TODAY - timedelta(days=days)).isoformat())
    return settings, replace(runtime["accounting"], supplier=supplier)


def test_supplier_record_changed_is_disabled_by_default_and_needs_a_real_timestamp(runtime):
    supplier = replace(runtime["accounting"].supplier, wallet_version=(TODAY - timedelta(days=3)).isoformat())
    disabled = _evaluate(runtime, InvoiceHistory(), accounting=replace(runtime["accounting"], supplier=supplier))
    assert _check(disabled, "supplier_record_changed") is None

    version_only = _evaluate(runtime, InvoiceHistory(), accounting=runtime["accounting"])
    assert _check(version_only, "supplier_record_changed") is None


def test_supplier_record_changed_window_boundary(runtime):
    settings, accounting = _record_changed_runtime(runtime, 3)
    check = _check(_evaluate(runtime, InvoiceHistory(), accounting=accounting, settings=settings), "supplier_record_changed")
    assert check is not None and check.passed is False
    assert check.requires_human is True and check.overridable is True
    assert "3 days ago" in check.detail

    settings, accounting = _record_changed_runtime(runtime, 30)
    assert _check(_evaluate(runtime, InvoiceHistory(), accounting=accounting, settings=settings), "supplier_record_changed") is not None

    settings, accounting = _record_changed_runtime(runtime, 31)
    assert _check(_evaluate(runtime, InvoiceHistory(), accounting=accounting, settings=settings), "supplier_record_changed") is None


def test_supplier_record_changed_review_escalates_and_acknowledgement_clears_it(runtime):
    settings, accounting = _record_changed_runtime(runtime, 3)
    decision = _evaluate(runtime, InvoiceHistory(), accounting=accounting, settings=settings)
    assert decision.action == DecisionAction.ESCALATE
    approval = {
        "approved": True,
        "evidence_hash": compute_evidence_hash(runtime["invoice"], accounting, runtime["treasury"], runtime["screening"]),
        "acknowledged_checks": ["supplier_record_changed"],
    }
    cleared = _evaluate(runtime, InvoiceHistory(), accounting=accounting, settings=settings, approval=approval)
    assert _check(cleared, "supplier_record_changed").passed is True
    assert cleared.action == DecisionAction.PAY_NOW


# --------------------------------------------------------------------------------------
# Advisory notes never change the action, the eligibility or the case
# --------------------------------------------------------------------------------------


def _add_payable(store, *, amount: int, due_in_days: int, number: str, discount_percent: str | None = None, discount_in_days: int | None = None) -> None:
    """A same-supplier payable with a matching accounting record, as the ordering demo builds them."""
    base = store.get_invoice("demo-invoice-legitimate")
    today = date.today()
    invoice = replace(
        _replace_amount(base, amount, base.lines[0].quantity),
        id=f"plan-{number}",
        invoice_number=number,
        purchase_invoice_id=f"PINV-{number}",
        due_date=today + timedelta(days=due_in_days),
        discount_percent=discount_percent,
        discount_deadline=(today + timedelta(days=discount_in_days)) if discount_in_days is not None else None,
    )
    store.create_invoice(invoice, f"key-{number}")
    store.seed_fixture(
        "erp_payable",
        invoice.purchase_invoice_id,
        _erp_payable(
            external_id=invoice.purchase_invoice_id,
            invoice_number=number,
            amount_units=amount,
            invoice_currency="USD",
            invoice_date=invoice.invoice_date,
            due_date=invoice.due_date,
            lines=invoice.lines,
            purchase_order_ids=invoice.purchase_order_ids,
            receipt_ids=invoice.receipt_ids,
            payment_terms=invoice.payment_terms,
            status="SUBMITTED",
        ),
    )


def test_an_advisory_note_never_opens_a_case(runtime):
    store = runtime["store"]
    base = runtime["invoice"]
    close = replace(base, id="close-amount", invoice_number="ACME-INV-2026-009", purchase_invoice_id=None)
    store.create_invoice(close, "key-close-amount")

    result = runtime["workflow"].evaluate(base.id)
    decision = result["decision"]
    advisory = next(check for check in decision["policy_checks"] if check["code"] == "similar_amount_recent")
    assert advisory["passed"] is True
    assert decision["action"] == DecisionAction.PAY_NOW
    assert store.case_row(base.id) is None


def test_equal_amount_same_supplier_invoices_remain_eligible(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "queue.sqlite3", min_reserve_usdc=Decimal("0"))
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    seed_demo(store)
    provider = MockPaymentProvider(store, balance_units=300 * USDC_SCALE)
    workflow = APWorkflow(
        store,
        MockAccountingConnector(store),
        provider,
        provider.signer,
        DeterministicPolicy(settings, USDCOnlyConverter("USD")),
        settings,
    )
    _add_payable(store, amount=150 * USDC_SCALE, due_in_days=20, number="PLAN-DISCOUNT", discount_percent="2.00", discount_in_days=1)
    _add_payable(store, amount=150 * USDC_SCALE, due_in_days=-5, number="PLAN-OVERDUE")

    eligible = {item["invoice"].invoice_number for item in PaymentPrioritiser(workflow).candidates() if item["eligible"]}
    assert {"PLAN-DISCOUNT", "PLAN-OVERDUE"} <= eligible
    for invoice_id in ("plan-PLAN-DISCOUNT", "plan-PLAN-OVERDUE"):
        decision = workflow.evaluate(invoice_id)["decision"]
        advisory = next(check for check in decision["policy_checks"] if check["code"] == "similar_amount_recent")
        assert advisory["passed"] is True
        assert decision["action"] == DecisionAction.PAY_NOW


def test_every_advisory_code_is_recognised_by_the_console():
    source = (ROOT / "src/arc_payables/web/invoice.js").read_text()
    match = re.search(r"ADVISORY_CHECKS = new Set\(\[(.*?)\]\)", source)
    assert match is not None, "the console must declare its advisory code set"
    console_codes = set(re.findall(r"'([a-z_]+)'", match.group(1)))
    assert console_codes == set(ADVISORY_CHECK_CODES)
