"""The fee booked must be the whole cost of settling, not only the last operation.

Settling one authorization can take two or three on-chain operations: clearing an old allowance,
setting the exact one, then the guarded call. Each costs gas that this deployment absorbs, and
booking only the guarded call understates our own cost in the ledger — quietly, and in the
direction that flatters the numbers.

These tests pin the arithmetic, the per-operation record and the retry path (where a fee re-read
must merge rather than overwrite what was already measured).
"""

from __future__ import annotations

from pathlib import Path

from arc_payables.currency import USDCOnlyConverter
from arc_payables.domain import WorkflowState, add_operation_fee
from arc_payables.mock_adapters import MockAccountingConnector, MockPaymentProvider
from arc_payables.policy import DeterministicPolicy
from arc_payables.security import EIP712PermitSigner
from arc_payables.seed import seed_demo
from arc_payables.service import APWorkflow
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore

POLICY_KEY = "0x" + "11" * 32
GUARD_FEE = 3_130
APPROVE_FEE = 1_995


def _workflow(tmp_path: Path, *, fee_units: int = GUARD_FEE, approve_fee_units: int = APPROVE_FEE,
              deferred_fee: bool = False, accounting: bool = False):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "fees.sqlite3",
        accounting_provider="frappe" if accounting else "mock",
    )
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    invoice_id, _ = seed_demo(store)
    signer = EIP712PermitSigner(POLICY_KEY)
    provider = MockPaymentProvider(
        store,
        signer=signer,
        fee_units=fee_units,
        approve_fee_units=approve_fee_units,
        deferred_fee=deferred_fee,
    )
    workflow = APWorkflow(
        store,
        MockAccountingConnector(store),
        provider,
        signer,
        DeterministicPolicy(settings, USDCOnlyConverter("USD")),
        settings,
    )
    return settings, store, workflow, invoice_id, provider


def test_the_booked_fee_covers_every_operation_and_records_where_it_went(tmp_path):
    _, store, workflow, invoice_id, _ = _workflow(tmp_path)
    workflow.evaluate(invoice_id)
    workflow.submit_payment(invoice_id)
    payment = store.get_payment(invoice_id)

    assert payment["fee_units"] == GUARD_FEE + APPROVE_FEE
    assert payment["fee_breakdown"] == {"approve": APPROVE_FEE, "guard": GUARD_FEE}
    # The recorded breakdown must explain the total, or the total is just a number.
    assert sum(payment["fee_breakdown"].values()) == payment["fee_units"]


def test_a_payment_that_needed_no_approval_books_only_the_guard_call(tmp_path):
    """No allowance operation happened, so nothing extra may be invented."""
    _, store, workflow, invoice_id, _ = _workflow(tmp_path, approve_fee_units=0)
    workflow.evaluate(invoice_id)
    workflow.submit_payment(invoice_id)
    payment = store.get_payment(invoice_id)
    assert payment["fee_units"] == GUARD_FEE
    assert payment["fee_breakdown"] == {"guard": GUARD_FEE}


def test_a_deferred_fee_reread_merges_with_the_allowance_fee_already_measured(tmp_path):
    """The provider can only answer for the settlement operation, so the total must be a merge.

    Overwriting here is the plausible mistake: it would look right (a fee is recorded, a document
    is booked) while silently dropping the allowance cost that was already paid.
    """
    _, store, workflow, invoice_id, provider = _workflow(tmp_path, deferred_fee=True)
    workflow.evaluate(invoice_id)
    workflow.submit_payment(invoice_id)

    before = store.get_payment(invoice_id)
    assert before["fee_units"] == APPROVE_FEE, "only the approval operation could be measured yet"
    assert before["fee_breakdown"] == {"approve": APPROVE_FEE}

    # The provider can now name the settlement fee, as it can once the transfer is indexed.
    provider.deferred_fee = False
    workflow._reread_settlement_fee(workflow._invoice_or_404(invoice_id), before)

    after = store.get_payment(invoice_id)
    assert after["fee_units"] == GUARD_FEE + APPROVE_FEE
    assert after["fee_breakdown"] == {"guard": GUARD_FEE, "approve": APPROVE_FEE}


def test_a_failure_after_the_allowance_still_records_what_was_spent(tmp_path):
    """Gas spent on the allowance is spent even if the guarded call never succeeds."""
    _, store, workflow, invoice_id, provider = _workflow(tmp_path)
    provider.failure_mode = "reverted"
    workflow.evaluate(invoice_id)
    workflow.submit_payment(invoice_id)
    payment = store.get_payment(invoice_id)
    assert payment["state"] == WorkflowState.FAILED.value
    assert payment["fee_units"] == APPROVE_FEE
    assert payment["fee_breakdown"] == {"approve": APPROVE_FEE}


def test_resuming_a_payment_keeps_the_allowance_fee_and_the_total(tmp_path):
    """The failure mode that matters: a resumed payment must not shrink the recorded cost.

    Real adapters answer an inspection with the settlement operation's own receipt, which names one
    fee and no breakdown. Writing that straight onto the record erased the allowance fee measured at
    submission time, so a crash-and-resume quietly understated our cost and destroyed the evidence
    of where it went.
    """
    from arc_payables.domain import PaymentStatus, PaymentSubmission

    _, store, workflow, invoice_id, provider = _workflow(tmp_path)
    workflow.evaluate(invoice_id)
    workflow.submit_payment(invoice_id)
    settled = store.get_payment(invoice_id)
    assert settled["fee_units"] == GUARD_FEE + APPROVE_FEE

    # Rewind to the state a crash between approval and settlement leaves behind: the allowance fee
    # is recorded, the settlement has no local confirmation yet.
    store.update_payment(
        invoice_id,
        {"confirmation_status": "PENDING", "transaction_hash": None,
         "provider_transaction_id": "0x" + "ab" * 32,
         "fee_units": APPROVE_FEE, "fee_breakdown": {"approve": APPROVE_FEE}},
        "SUBMITTED",
        "TEST_REWIND_FOR_RESUME",
    )

    # What LocalKeyPaymentProvider._status_from_receipt and Circle's _circle_status return: the
    # settlement transaction's cost, with no per-stage breakdown.
    original = provider.inspect_payment
    provider.inspect_payment = lambda payment: PaymentSubmission(
        PaymentStatus.CONFIRMED, transaction_hash="0x" + "cd" * 32, fee_units=GUARD_FEE
    )
    try:
        result = workflow.submit_payment(invoice_id)
    finally:
        provider.inspect_payment = original

    assert result["state"] == WorkflowState.ERP_RECORDED.value
    resumed = store.get_payment(invoice_id)
    assert resumed["fee_units"] == GUARD_FEE + APPROVE_FEE, "the allowance fee was discarded on resume"
    assert resumed["fee_breakdown"] == {"approve": APPROVE_FEE, "guard": GUARD_FEE}


def test_a_repeated_inspection_does_not_double_count_a_stage(tmp_path):
    """Merging must be idempotent: two confirmations of the same settlement are not two fees."""
    from arc_payables.domain import PaymentStatus, PaymentSubmission
    from arc_payables.service import _merged_fee

    _, store, workflow, invoice_id, _ = _workflow(tmp_path)
    workflow.evaluate(invoice_id)
    workflow.submit_payment(invoice_id)
    payment = store.get_payment(invoice_id)

    same = PaymentSubmission(PaymentStatus.CONFIRMED, transaction_hash=payment["transaction_hash"],
                             fee_units=GUARD_FEE, fee_breakdown={"approve": APPROVE_FEE, "guard": GUARD_FEE})
    assert _merged_fee(payment, same) == {"fee_units": GUARD_FEE + APPROVE_FEE,
                                          "fee_breakdown": {"approve": APPROVE_FEE, "guard": GUARD_FEE}}


def test_a_payment_with_no_measured_fee_records_none_rather_than_zero(tmp_path):
    """No measurement is not the same as a free payment."""
    from arc_payables.domain import PaymentStatus, PaymentSubmission
    from arc_payables.service import _merged_fee

    empty = PaymentSubmission(PaymentStatus.CONFIRMED, transaction_hash="0x" + "ee" * 32)
    assert _merged_fee({}, empty) == {"fee_units": None, "fee_breakdown": None}


def test_a_legacy_single_figure_survives_a_provider_that_reports_nothing(tmp_path):
    """A recorded fee must never be erased just because a later report has no fee for it.

    A record written before per-stage capture holds one figure. Treating "no breakdown" as "no fee
    information" made the merge replace it - or, when the provider reported nothing at all, wipe a
    cost that had really been measured.
    """
    from arc_payables.domain import PaymentStatus, PaymentSubmission
    from arc_payables.service import _merged_fee

    silent = PaymentSubmission(PaymentStatus.CONFIRMED, transaction_hash="0x" + "ab" * 32)
    assert _merged_fee({"fee_units": 3130}, silent) == {
        "fee_units": 3130, "fee_breakdown": {"guard": 3130}
    }


def test_a_legacy_single_figure_is_updated_in_place_not_replaced(tmp_path):
    """The same stage is refreshed by a fresh measurement; other stages are never lost."""
    from arc_payables.domain import PaymentStatus, PaymentSubmission
    from arc_payables.service import _merged_fee

    later = PaymentSubmission(PaymentStatus.CONFIRMED, transaction_hash="0x" + "cd" * 32, fee_units=2500)
    assert _merged_fee({"fee_units": 3130}, later) == {
        "fee_units": 2500, "fee_breakdown": {"guard": 2500}
    }


def test_operation_fees_are_summed_per_stage_rather_than_overwritten():
    """Two operations of the same stage (a reset then a new allowance) both count."""
    breakdown: dict[str, int] = {}
    add_operation_fee(breakdown, "approve", 100)
    add_operation_fee(breakdown, "approve", 250)
    add_operation_fee(breakdown, "guard", 400)
    add_operation_fee(breakdown, "guard", None)  # a stage that could not be measured adds nothing
    assert breakdown == {"approve": 350, "guard": 400}
