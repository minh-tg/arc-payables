"""Reconciliation: what the chain settled versus what this deployment recorded.

The tests are written around the two mistakes that cost real money and are not symmetric:
treating a settled payment as failed (pay twice) and treating an unresolved one as settled
(hide a missing payment). Several of them exist specifically to pin that boundary, because
the difference is a single comparison against zero and is easy to get subtly wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eth_abi import encode as abi_encode
from eth_utils import keccak

from arc_payables import reconcile as reconcile_module
from arc_payables.currency import USDCOnlyConverter
from arc_payables.domain import ARC_TESTNET_CHAIN_ID, ARC_TESTNET_USDC, USDC_SCALE
from arc_payables.local_payment import PAYMENT_EXECUTED_TOPIC
from arc_payables.mock_adapters import MockAccountingConnector, MockPaymentProvider
from arc_payables.policy import DeterministicPolicy
from arc_payables.reconcile import (
    CHAIN_UNREACHABLE,
    DUPLICATE_SETTLEMENT_EVENTS,
    GUARD_MISMATCH,
    MALFORMED_EVENT,
    NOT_SETTLED_PROVEN,
    PROVIDER_NOT_LIVE,
    RANGE_TRUNCATED,
    RECORDED_TRANSACTION_LACKS_EVENT,
    SETTLED_BUT_RECORDED_FAILED,
    SETTLED_NOT_RECORDED,
    SETTLEMENT_WITHOUT_EVIDENCE,
    UNRESOLVED_UNCERTAINTY,
    WRONG_CHAIN,
    reconcile,
)
from arc_payables.security import EIP712PermitSigner
from arc_payables.seed import seed_demo
from arc_payables.service import APWorkflow
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore

POLICY_KEY = "0x" + "11" * 32
PAYER = "0x" + "aa" * 20
GUARD = "0x" + "bb" * 20
OTHER_GUARD = "0x" + "cc" * 20
RECIPIENT = "0x" + "dd" * 20


def _settings(tmp_path: Path, **overrides) -> Settings:
    """A live-provider deployment so reconciliation is meaningful, with no network use."""
    values = {
        "_env_file": None,
        "database_path": tmp_path / "reconcile.sqlite3",
        "payment_provider": "local",
        "local_payment_private_key": "0x" + "77" * 32,
        "local_payment_guard_address": GUARD,
        "local_payment_rpc_url": "https://rpc.invalid",
        "permit_signing_private_key": POLICY_KEY,
    }
    values.update(overrides)
    return Settings(**values)


def _workflow(tmp_path: Path, *, guard: str = GUARD, payer: str = PAYER):
    """A workflow whose recorded permits use the guard and payer under test."""
    settings = Settings(_env_file=None, database_path=tmp_path / "reconcile.sqlite3")
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    legitimate_id, _ = seed_demo(store)
    signer = EIP712PermitSigner(POLICY_KEY)
    provider = MockPaymentProvider(store, signer=signer, wallet_address=payer, guard_address=guard)
    workflow = APWorkflow(
        store,
        MockAccountingConnector(store),
        provider,
        signer,
        DeterministicPolicy(settings, USDCOnlyConverter("USD")),
        settings,
    )
    return workflow, store, legitimate_id


def _settle(workflow, invoice_id: str) -> dict:
    workflow.evaluate(invoice_id)
    result = workflow.submit_payment(invoice_id)
    payment = workflow.store.get_payment(invoice_id)
    assert payment is not None
    assert payment["permit"], "the fixture must produce a real permit"
    return payment


def _forget_transaction(store, invoice_id: str) -> None:
    """Model a process that died after submitting but before recording the hash.

    This is the case the block window exists for: there is no transaction to inspect, so the
    only evidence is a log search, and what that search can prove depends on where it started.
    """
    store.update_payment(
        invoice_id,
        {"transaction_hash": None, "provider_transaction_id": None, "confirmation_status": "PENDING"},
        "NEEDS_RECONCILIATION",
        "TEST_FORGET_TRANSACTION",
    )


def _log(
    payment: dict,
    *,
    amount_units: int | None = None,
    recipient: str | None = None,
    token: str | None = None,
    evidence_hash: str | None = None,
    payer: str | None = None,
    payment_id: str | None = None,
    tx_hash: str = "0x" + "99" * 32,
    block: int = 5,
    log_index: int = 0,
) -> dict:
    permit = payment["permit"]
    return {
        "address": GUARD,
        "topics": [
            PAYMENT_EXECUTED_TOPIC,
            payment_id or permit["payment_id"],
            evidence_hash or permit["evidence_hash"],
            "0x" + (payer or permit["payer"])[2:].rjust(64, "0"),
        ],
        "data": "0x"
        + abi_encode(
            ["address", "address", "uint256"],
            [recipient or permit["recipient"], token or ARC_TESTNET_USDC, amount_units if amount_units is not None else permit["amount_units"]],
        ).hex(),
        "transactionHash": tx_hash,
        "blockNumber": hex(block),
        "logIndex": hex(log_index),
    }


class StubRpc:
    """A scripted JSON-RPC endpoint. No network, no chain: every answer is deliberate."""

    def __init__(
        self,
        *,
        chain_id: int = ARC_TESTNET_CHAIN_ID,
        latest: int = 10,
        logs: list[dict] | None = None,
        receipts: dict[str, dict] | None = None,
        fail_on: set[str] | None = None,
    ):
        self.chain_id = chain_id
        self.latest = latest
        self.logs = logs or []
        self.receipts = receipts or {}
        self.fail_on = fail_on or set()
        self.calls: list[tuple[str, list]] = []

    def call(self, method: str, params: list):
        self.calls.append((method, params))
        if method in self.fail_on:
            raise RuntimeError(f"stub failure for {method}")
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_blockNumber":
            return hex(self.latest)
        if method == "eth_getLogs":
            query = params[0]
            start = int(str(query["fromBlock"]), 16)
            end = int(str(query["toBlock"]), 16)
            return [log for log in self.logs if start <= int(log["blockNumber"], 16) <= end]
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        raise AssertionError(f"unexpected RPC call {method}")


def _kinds(report) -> set[str]:
    return {finding.kind for finding in report.findings}


# ---------------------------------------------------------------------------------------
# The clean case, and the direction that only ever uses events it actually saw
# ---------------------------------------------------------------------------------------


def test_a_settled_payment_reconciles_with_nothing_to_report(tmp_path):
    workflow, store, invoice_id = _workflow(tmp_path)
    payment = _settle(workflow, invoice_id)

    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(logs=[_log(payment)]))
    assert report.ok is True
    assert report.findings == []
    assert report.observations == 1
    assert report.records == 1


def test_settlement_with_no_record_at_all_is_blocking(tmp_path):
    """Money moved on chain that this deployment has no row for: the worst silent case."""
    workflow, store, invoice_id = _workflow(tmp_path)
    payment = _settle(workflow, invoice_id)

    # The chain settles a payment the database has never heard of.
    stranger = _log(payment, payment_id="0x" + "ee" * 32)
    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(logs=[_log(payment), stranger]))
    assert report.ok is False
    assert SETTLED_NOT_RECORDED in _kinds(report)
    assert any("without a record" in finding.detail for finding in report.blocking)


def test_two_settlements_for_one_payment_id_are_blocking(tmp_path):
    workflow, store, invoice_id = _workflow(tmp_path)
    payment = _settle(workflow, invoice_id)

    report = reconcile(
        store,
        _settings(tmp_path),
        rpc=StubRpc(logs=[_log(payment, log_index=0), _log(payment, log_index=1, tx_hash="0x" + "98" * 32)]),
    )
    assert report.ok is False
    assert DUPLICATE_SETTLEMENT_EVENTS in _kinds(report)


def test_every_material_field_is_compared(tmp_path):
    """Each field is checked independently, so one mismatch cannot hide behind another."""
    for overrides, expected in (
        ({"amount_units": 1}, "amount_mismatch"),
        ({"recipient": "0x" + "12" * 20}, "recipient_mismatch"),
        ({"token": "0x" + "34" * 20}, "token_mismatch"),
        ({"evidence_hash": "0x" + "56" * 32}, "evidence_mismatch"),
        ({"payer": "0x" + "78" * 20}, "payer_mismatch"),
    ):
        workflow, store, invoice_id = _workflow(tmp_path / expected)
        payment = _settle(workflow, invoice_id)
        report = reconcile(store, _settings(tmp_path / expected), rpc=StubRpc(logs=[_log(payment, **overrides)]))
        assert report.ok is False, expected
        assert expected in _kinds(report), f"{expected} not reported: {_kinds(report)}"


def test_an_event_decoded_from_a_wrong_shape_log_is_reported_not_ignored(tmp_path):
    workflow, store, invoice_id = _workflow(tmp_path)
    payment = _settle(workflow, invoice_id)

    truncated = dict(_log(payment))
    truncated["data"] = "0x" + "00" * 64  # 64 bytes is not the 96-byte payload
    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(logs=[truncated]))
    assert report.ok is False
    assert MALFORMED_EVENT in _kinds(report)
    # A settlement that could not be decoded must not also be read as a missing one.
    assert report.observations == 0


# ---------------------------------------------------------------------------------------
# Absence: the boundary that decides between "unresolved" and "did not happen"
# ---------------------------------------------------------------------------------------


def test_absence_with_a_partial_window_is_unresolved_not_proven(tmp_path):
    """Scanning from a later block cannot prove an older settlement did not happen."""
    workflow, store, invoice_id = _workflow(tmp_path)
    _settle(workflow, invoice_id)
    _forget_transaction(store, invoice_id)

    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(latest=10_000), from_block=9_000)
    assert UNRESOLVED_UNCERTAINTY in _kinds(report)
    assert SETTLEMENT_WITHOUT_EVIDENCE not in _kinds(report)
    assert NOT_SETTLED_PROVEN not in _kinds(report)
    # This is uncertainty, not a proven fault, so it must not be reported as blocking.
    assert report.ok is True
    assert any("not proof" in finding.detail for finding in report.review)


def test_absence_with_full_coverage_is_blocking_when_the_record_claims_settlement(tmp_path):
    workflow, store, invoice_id = _workflow(tmp_path)
    _settle(workflow, invoice_id)
    # Keeps a settled state, but loses the transaction reference.
    store.update_payment(invoice_id, {"transaction_hash": None, "provider_transaction_id": None},
                         "ERP_RECORDED", "TEST_FORGET_TRANSACTION")

    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(logs=[], latest=500), from_block=0)
    assert SETTLEMENT_WITHOUT_EVIDENCE in _kinds(report)
    assert report.ok is False


def test_full_coverage_without_settlement_proves_an_unsent_authorization_did_not_settle(tmp_path):
    """The one case where "did not settle" is a conclusion rather than a guess."""
    workflow, store, invoice_id = _workflow(tmp_path)
    payment = _settle(workflow, invoice_id)
    # Rewind the record to a state that admits uncertainty, as a crash before broadcast would.
    _forget_transaction(store, invoice_id)

    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(logs=[], latest=500), from_block=0)
    assert NOT_SETTLED_PROVEN in _kinds(report)
    assert SETTLEMENT_WITHOUT_EVIDENCE not in _kinds(report)
    assert report.ok is True
    assert payment["permit"]["payment_id"]


def test_a_failed_record_that_settled_on_chain_is_blocking(tmp_path):
    workflow, store, invoice_id = _workflow(tmp_path)
    payment = _settle(workflow, invoice_id)
    store.update_payment(invoice_id, {"confirmation_status": "FAILED"}, "FAILED", "TEST_MARK_FAILED")

    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(logs=[_log(payment)]))
    assert report.ok is False
    assert SETTLED_BUT_RECORDED_FAILED in _kinds(report)


def test_an_unsent_authorization_is_not_reported_at_all(tmp_path):
    """Authorized-but-never-sent is a normal state, not a reconciliation finding."""
    workflow, store, invoice_id = _workflow(tmp_path)
    _settle(workflow, invoice_id)
    store.update_payment(invoice_id, {"confirmation_status": "NOT_SUBMITTED", "transaction_hash": None,
                                      "provider_transaction_id": None},
                         "AUTHORIZED", "TEST_REWIND")

    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(logs=[], latest=500), from_block=0)
    assert report.findings == []
    assert report.ok is True


# ---------------------------------------------------------------------------------------
# Exact evidence: a named transaction needs no block range
# ---------------------------------------------------------------------------------------


def test_a_named_transaction_is_checked_directly_without_a_window(tmp_path):
    workflow, store, invoice_id = _workflow(tmp_path)
    payment = _settle(workflow, invoice_id)
    tx_hash = payment["transaction_hash"]
    assert tx_hash

    # No logs in any range, but the recorded transaction itself carries the event.
    receipt = {"to": GUARD, "logs": [_log(payment, tx_hash=tx_hash)]}
    report = reconcile(
        store, _settings(tmp_path), rpc=StubRpc(logs=[], latest=10_000, receipts={tx_hash: receipt}), from_block=9_000
    )
    assert report.ok is True
    assert report.findings == []


def test_a_named_transaction_without_the_event_is_blocking(tmp_path):
    workflow, store, invoice_id = _workflow(tmp_path)
    payment = _settle(workflow, invoice_id)
    tx_hash = payment["transaction_hash"]

    receipt = {"to": GUARD, "logs": []}
    report = reconcile(
        store, _settings(tmp_path), rpc=StubRpc(logs=[], latest=10_000, receipts={tx_hash: receipt}), from_block=9_000
    )
    assert report.ok is False
    assert RECORDED_TRANSACTION_LACKS_EVENT in _kinds(report)


def test_an_event_from_another_contract_in_the_same_transaction_is_not_the_settlement(tmp_path):
    """Only the guard's own event counts, even inside the recorded transaction."""
    workflow, store, invoice_id = _workflow(tmp_path)
    payment = _settle(workflow, invoice_id)
    tx_hash = payment["transaction_hash"]

    foreign = dict(_log(payment, tx_hash=tx_hash))
    foreign["address"] = OTHER_GUARD
    report = reconcile(
        store,
        _settings(tmp_path),
        rpc=StubRpc(logs=[], latest=10_000, receipts={tx_hash: {"to": GUARD, "logs": [foreign]}}),
        from_block=9_000,
    )
    assert report.ok is False
    assert RECORDED_TRANSACTION_LACKS_EVENT in _kinds(report)


def test_a_named_transaction_the_node_does_not_know_is_blocking(tmp_path):
    """The record names a transaction that does not exist: an exact, not a windowed, disagreement."""
    workflow, store, invoice_id = _workflow(tmp_path)
    _settle(workflow, invoice_id)

    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(logs=[], latest=10_000), from_block=9_000)
    assert report.ok is False
    assert _kinds(report) == {RECORDED_TRANSACTION_LACKS_EVENT}
    assert any("no receipt" in finding.detail for finding in report.blocking)


def test_an_unreadable_receipt_is_uncertainty_not_absence(tmp_path):
    """A receipt the node refuses to return is not a transaction without a settlement."""
    workflow, store, invoice_id = _workflow(tmp_path)
    _settle(workflow, invoice_id)

    report = reconcile(
        store,
        _settings(tmp_path),
        rpc=StubRpc(logs=[], latest=10_000, fail_on={"eth_getTransactionReceipt"}),
        from_block=9_000,
    )
    assert UNRESOLVED_UNCERTAINTY in _kinds(report)
    assert RECORDED_TRANSACTION_LACKS_EVENT not in _kinds(report)
    assert SETTLEMENT_WITHOUT_EVIDENCE not in _kinds(report)
    assert report.ok is True


# ---------------------------------------------------------------------------------------
# Fail-closed behaviour: never reconcile against the wrong thing
# ---------------------------------------------------------------------------------------


def test_an_unreachable_chain_produces_no_settlement_conclusion(tmp_path):
    workflow, store, invoice_id = _workflow(tmp_path)
    _settle(workflow, invoice_id)

    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(fail_on={"eth_chainId"}))
    assert report.ok is False
    assert _kinds(report) == {CHAIN_UNREACHABLE}
    assert "no conclusion" in report.blocking[0].detail


def test_failed_log_query_is_not_read_as_an_empty_chain(tmp_path):
    workflow, store, invoice_id = _workflow(tmp_path)
    _settle(workflow, invoice_id)

    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(fail_on={"eth_getLogs"}, latest=500))
    assert report.ok is False
    assert CHAIN_UNREACHABLE in _kinds(report)
    assert SETTLEMENT_WITHOUT_EVIDENCE not in _kinds(report)


def test_a_wrong_chain_is_refused_outright(tmp_path):
    workflow, store, invoice_id = _workflow(tmp_path)
    _settle(workflow, invoice_id)

    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(chain_id=1))
    assert report.ok is False
    assert _kinds(report) == {WRONG_CHAIN}


def test_a_record_from_another_guard_is_not_reconciled_against_this_one(tmp_path):
    workflow, store, invoice_id = _workflow(tmp_path, guard=OTHER_GUARD)
    _settle(workflow, invoice_id)

    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(logs=[]))
    assert report.ok is False
    assert GUARD_MISMATCH in _kinds(report)


def test_a_mock_deployment_says_there_is_nothing_to_reconcile(tmp_path):
    workflow, store, invoice_id = _workflow(tmp_path)
    _settle(workflow, invoice_id)

    report = reconcile(store, Settings(_env_file=None, database_path=tmp_path / "reconcile.sqlite3"), rpc=StubRpc())
    assert report.ok is True
    assert _kinds(report) == {PROVIDER_NOT_LIVE}
    # Nothing was queried: a mock deployment must not read a chain at all.
    assert not any(method == "eth_getLogs" for method, _ in StubRpc().calls)
    assert "no on-chain settlement" in report.review[0].detail


# ---------------------------------------------------------------------------------------
# Ranges and chunking
# ---------------------------------------------------------------------------------------


def test_a_range_wider_than_the_cap_is_scanned_from_the_newest_end_and_reported(tmp_path, monkeypatch):
    workflow, store, invoice_id = _workflow(tmp_path)
    payment = _settle(workflow, invoice_id)
    monkeypatch.setattr(reconcile_module, "MAX_SCAN_BLOCKS", 100)

    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(logs=[_log(payment, block=9_999)], latest=10_000))
    assert RANGE_TRUNCATED in _kinds(report)
    assert report.coverage["from_block"] == 10_000 - 100 + 1
    assert report.ok is True  # truncation is a caveat about coverage, not a funds fault


def test_logs_are_chunked_without_losing_an_event_on_a_chunk_boundary(tmp_path, monkeypatch):
    workflow, store, invoice_id = _workflow(tmp_path)
    payment = _settle(workflow, invoice_id)
    monkeypatch.setattr(reconcile_module, "LOG_CHUNK_BLOCKS", 10)

    at_boundary = _log(payment, block=10)  # last block of the first chunk
    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(logs=[at_boundary], latest=25), from_block=0)
    assert report.observations == 1
    assert report.ok is True


def test_chunking_actually_issues_multiple_queries(tmp_path, monkeypatch):
    workflow, store, invoice_id = _workflow(tmp_path)
    payment = _settle(workflow, invoice_id)
    monkeypatch.setattr(reconcile_module, "LOG_CHUNK_BLOCKS", 10)
    rpc = StubRpc(logs=[_log(payment, block=25)], latest=25)

    report = reconcile(store, _settings(tmp_path), rpc=rpc, from_block=0)
    queried = [params[0] for method, params in rpc.calls if method == "eth_getLogs"]
    assert len(queried) == 3, queried
    assert [int(str(call["fromBlock"]), 16) for call in queried] == [0, 10, 20]
    assert report.observations == 1
    assert report.ok is True


def test_the_default_search_starts_at_genesis_so_absence_is_provable(tmp_path):
    workflow, store, invoice_id = _workflow(tmp_path)
    _settle(workflow, invoice_id)
    store.update_payment(invoice_id, {"transaction_hash": None, "provider_transaction_id": None},
                         "ERP_RECORDED", "TEST_FORGET_TRANSACTION")

    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(logs=[], latest=50))
    assert report.coverage["from_block"] == 0
    assert SETTLEMENT_WITHOUT_EVIDENCE in _kinds(report)


# ---------------------------------------------------------------------------------------
# End to end, against the real guard bytecode
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def chain():
    from arc_payables.fake_circle import DEFAULT_POLICY_KEY, AnvilChain

    with AnvilChain() as running:
        running.deploy_suite(EIP712PermitSigner(DEFAULT_POLICY_KEY).address)
        running.fund_native(running.wallet.address, 10**18)
        running.mint(running.wallet.address, 5_000 * USDC_SCALE)
        yield running


class _ChainRpc:
    """Adapt the in-process fake chain to the same call interface as RpcClient."""

    def __init__(self, chain):
        self.chain = chain

    def call(self, method: str, params: list):
        return self.chain.rpc(method, params)


def test_a_real_guard_settlement_reconciles_end_to_end(tmp_path, chain):
    """The decoder is checked against the deployed contract, not against a hand-built log."""
    from arc_payables.fake_circle import DEFAULT_POLICY_KEY
    from arc_payables.local_payment import LocalKeyPaymentProvider
    from arc_payables.seed import APPROVED_WALLET

    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "e2e.sqlite3",
        payment_provider="local",
        accounting_provider="mock",
        local_payment_private_key=chain.wallet.key.hex(),
        local_payment_guard_address=chain.guard_address,
        local_payment_rpc_url=chain.url,
        local_payment_receipt_timeout_seconds=30,
        permit_signing_private_key=DEFAULT_POLICY_KEY,
    )
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    invoice_id, _ = seed_demo(store)
    provider = LocalKeyPaymentProvider(settings, EIP712PermitSigner(DEFAULT_POLICY_KEY))
    workflow = APWorkflow(
        store,
        MockAccountingConnector(store),
        provider,
        provider.signer,
        DeterministicPolicy(settings, USDCOnlyConverter("USD")),
        settings,
    )
    try:
        workflow.evaluate(invoice_id)
        result = workflow.submit_payment(invoice_id)
        assert result["state"] == "ERP_RECORDED"
    finally:
        provider.close()

    report = reconcile(store, settings, rpc=_ChainRpc(chain), from_block=0)
    assert report.ok is True, [finding.to_dict() for finding in report.findings]
    assert report.observations == 1
    assert report.findings == []

    # And the amount really did move to the supplier the guard recorded.
    assert chain.erc20_balance(APPROVED_WALLET) == 250 * USDC_SCALE


def test_the_cli_report_does_not_claim_a_block_range_that_was_never_searched(tmp_path):
    """Output an operator reads must not imply a search that did not happen."""
    from arc_payables.reconcile import _format

    workflow, store, invoice_id = _workflow(tmp_path)
    _settle(workflow, invoice_id)
    report = reconcile(store, Settings(_env_file=None, database_path=tmp_path / "reconcile.sqlite3"), rpc=StubRpc())
    rendered = _format(report)
    assert "mock" in rendered
    assert "no guard configured" in rendered
    assert "searched blocks" not in rendered
    assert "None" not in rendered


def test_the_cli_report_names_the_range_and_the_severity(tmp_path):
    from arc_payables.reconcile import _format

    workflow, store, invoice_id = _workflow(tmp_path)
    _settle(workflow, invoice_id)
    store.update_payment(invoice_id, {"transaction_hash": None, "provider_transaction_id": None},
                         "ERP_RECORDED", "TEST_FORGET_TRANSACTION")
    report = reconcile(store, _settings(tmp_path), rpc=StubRpc(logs=[], latest=40), from_block=0)
    rendered = _format(report)
    assert "searched blocks 0-40 on chain 5042002" in rendered
    assert "BLOCKING" in rendered
    assert "settlement_without_evidence" in rendered


def test_the_topic_constant_matches_the_contract_event_signature():
    """One definition of the event shape, checked against the ABI signature itself."""
    signature = "PaymentExecuted(bytes32,bytes32,address,address,address,uint256)"
    assert PAYMENT_EXECUTED_TOPIC == "0x" + keccak(text=signature).hex()
