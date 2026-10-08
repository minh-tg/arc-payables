"""Reconcile what this deployment recorded against what the guard actually settled.

A payment record is the agent's belief. The guard's ``PaymentExecuted`` log is what happened.
This module compares the two and refuses to blur the difference:

* Every claim it makes about an *observed* event is safe, because it saw the event.
* It never claims "not settled" from an absence. Absence of evidence depends on the block range
  that was actually searched, and a payment submitted before that range would simply not appear.
  Those cases are reported as unresolved uncertainty for a human, never as a failed payment.

The distinction matters because the two mistakes are not symmetric. Treating a settled payment as
failed leads to paying a supplier twice; treating an unresolved one as settled hides a missing
payment. Both are reported explicitly rather than averaged into a single "ok".

Nothing here signs, sends or repairs anything. It is read-only, and deliberately so: a
reconciliation tool that could also fix things would be the wrong tool to point at an incident.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address

from .domain import ARC_TESTNET_CHAIN_ID, units_to_usdc
from .local_payment import PAYMENT_EXECUTED_TOPIC
from .settings import Settings, get_settings
from .verify_arc import RpcClient, provider_view

#: Severity. ``blocking`` means the books and the chain disagree and a person must act.
#: ``review`` means uncertainty a person must resolve; it is not proof of a fault.
BLOCKING = "blocking"
REVIEW = "review"

# Finding kinds. Stable strings: an operator's runbook and the console both key off these.
CHAIN_UNREACHABLE = "chain_unreachable"
WRONG_CHAIN = "wrong_chain"
NO_GUARD_CONFIGURED = "no_guard_configured"
PROVIDER_NOT_LIVE = "provider_not_live"
RANGE_TRUNCATED = "range_truncated"
MALFORMED_EVENT = "malformed_event"
DUPLICATE_SETTLEMENT_EVENTS = "duplicate_settlement_events"
SETTLED_NOT_RECORDED = "settled_not_recorded"
DUPLICATE_PAYMENT_ID_RECORDS = "duplicate_payment_id_records"
SHARED_TRANSACTION_HASH = "shared_transaction_hash"
RECORDED_TRANSACTION_LACKS_EVENT = "recorded_transaction_lacks_event"
SETTLEMENT_WITHOUT_EVIDENCE = "settlement_without_evidence"
SETTLED_BUT_RECORDED_FAILED = "settled_but_recorded_failed"
UNRESOLVED_UNCERTAINTY = "unresolved_uncertainty"
NOT_SETTLED_PROVEN = "not_settled_proven"
PROVIDER_RESOLUTION_REQUIRED = "provider_resolution_required"
AMOUNT_MISMATCH = "amount_mismatch"
RECIPIENT_MISMATCH = "recipient_mismatch"
TOKEN_MISMATCH = "token_mismatch"
EVIDENCE_MISMATCH = "evidence_mismatch"
PAYER_MISMATCH = "payer_mismatch"
GUARD_MISMATCH = "guard_mismatch"
CHAIN_ID_MISMATCH = "chain_id_mismatch"

#: States in which the record asserts the money moved. A record that says this and has no
#: on-chain evidence is a blocking disagreement, not a pending item.
SETTLED_STATES = frozenset({"CONFIRMED", "ERP_PENDING", "ERP_RECORDED"})
SETTLED_CONFIRMATIONS = frozenset({"CONFIRMED"})
FAILED_STATES = frozenset({"FAILED"})
#: States where a submission was attempted but nothing was proven either way. These must be
#: resolved, because the money may have moved.
ATTEMPTED_UNCERTAIN_STATES = frozenset({"SUBMITTED", "NEEDS_RECONCILIATION"})
#: AUTHORIZED means an authorization exists and nothing was sent. That is an ordinary state, not
#: an unresolved one, so it is deliberately absent from the set above and is never reported.

#: Bound on how much chain history one run will scan. Exceeding it is reported, never hidden.
MAX_SCAN_BLOCKS = 200_000
LOG_CHUNK_BLOCKS = 10_000


@dataclass(frozen=True)
class Observation:
    """One ``PaymentExecuted`` log, as the chain reports it."""

    payment_id: str
    evidence_hash: str
    payer: str
    recipient: str
    token: str
    amount_units: int
    transaction_hash: str
    block_number: int
    log_index: int


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str
    detail: str
    invoice_id: str | None = None
    payment_id: str | None = None
    transaction_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "detail": self.detail,
            "invoice_id": self.invoice_id,
            "payment_id": self.payment_id,
            "transaction_hash": self.transaction_hash,
        }


@dataclass
class Report:
    ok: bool
    coverage: dict[str, Any]
    findings: list[Finding] = field(default_factory=list)
    observations: int = 0
    records: int = 0

    @property
    def blocking(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == BLOCKING]

    @property
    def review(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == REVIEW]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "coverage": self.coverage,
            "observations": self.observations,
            "records": self.records,
            "blocking": len(self.blocking),
            "review": len(self.review),
            "findings": [finding.to_dict() for finding in self.findings],
        }


class ReconciliationUnavailable(RuntimeError):
    """The chain could not be read, so no comparison is possible."""


def _normalise_address(value: str | None) -> str:
    if not value:
        return ""
    try:
        return to_checksum_address(value).lower()
    except Exception:
        return str(value).lower()


def _decode_payment_log(log: dict[str, Any]) -> Observation | None:
    """Decode one log, or None when it is not a well-formed ``PaymentExecuted``.

    Returns None rather than raising so the caller can report the malformed entry: an
    undecodable event from the guard address is itself something worth telling an operator.
    """
    topics = log.get("topics") or []
    data = str(log.get("data") or "")
    if len(topics) != 4 or str(topics[0]).lower() != PAYMENT_EXECUTED_TOPIC.lower():
        return None
    try:
        raw = bytes.fromhex(data.removeprefix("0x"))
        if len(raw) != 96:
            return None
        recipient, token, amount = abi_decode(["address", "address", "uint256"], raw)
        return Observation(
            payment_id="0x" + str(topics[1]).removeprefix("0x"),
            evidence_hash="0x" + str(topics[2]).removeprefix("0x"),
            payer=_normalise_address("0x" + str(topics[3])[-40:]),
            recipient=_normalise_address(recipient),
            token=_normalise_address(token),
            amount_units=int(amount),
            transaction_hash=str(log.get("transactionHash") or ""),
            block_number=int(str(log.get("blockNumber") or "0x0"), 16),
            log_index=int(str(log.get("logIndex") or "0x0"), 16),
        )
    except Exception:
        return None


def _fetch_logs(rpc: Any, guard: str, from_block: int, to_block: int) -> list[dict[str, Any]]:
    """Fetch guard logs in chunks, so a wide range does not fail the whole run.

    A ``None`` result is treated as a failed query rather than an empty one: an RPC endpoint
    that answers with nothing is not evidence that nothing happened.
    """
    logs: list[dict[str, Any]] = []
    start = from_block
    while start <= to_block:
        end = min(start + LOG_CHUNK_BLOCKS - 1, to_block)
        batch = rpc.call(
            "eth_getLogs",
            [{"address": guard, "topics": [PAYMENT_EXECUTED_TOPIC], "fromBlock": hex(start), "toBlock": hex(end)}],
        )
        if batch is None:
            raise ReconciliationUnavailable(f"eth_getLogs returned nothing for blocks {start}-{end}")
        logs.extend(batch)
        start = end + 1
    return logs


def _records(store: Any) -> list[dict[str, Any]]:
    """Every recorded payment, joined to the invoice state it belongs to."""
    rows: list[dict[str, Any]] = []
    for invoice, invoice_state in store.list_invoices():
        payment = store.get_payment(invoice.id)
        if payment is None:
            continue
        rows.append(
            {
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "permit": payment.get("permit") or {},
                "payment_id": payment.get("payment_id"),
                "state": payment.get("state") or invoice_state,
                "confirmation_status": payment.get("confirmation_status"),
                "transaction_hash": payment.get("transaction_hash"),
                "provider_transaction_id": payment.get("provider_transaction_id"),
            }
        )
    return rows


def _claimed_settled(record: dict[str, Any]) -> bool:
    return (
        str(record["state"]).upper() in SETTLED_STATES
        or str(record["confirmation_status"] or "").upper() in SETTLED_CONFIRMATIONS
    )


def _compare(record: dict[str, Any], observed: Observation) -> list[Finding]:
    """Compare a record against the event that settles it, field by field."""
    findings: list[Finding] = []
    permit = record["permit"]
    identity = {
        "invoice_id": record["invoice_id"],
        "payment_id": record["payment_id"],
        "transaction_hash": observed.transaction_hash,
    }

    def add(kind: str, detail: str) -> None:
        findings.append(Finding(kind, BLOCKING, detail, **identity))

    recorded_amount = permit.get("amount_units")
    if recorded_amount is None or int(recorded_amount) != observed.amount_units:
        add(
            AMOUNT_MISMATCH,
            f"the record authorizes {units_to_usdc(int(recorded_amount or 0))} USDC but the guard "
            f"settled {units_to_usdc(observed.amount_units)} USDC",
        )
    if _normalise_address(permit.get("recipient")) != observed.recipient:
        add(
            RECIPIENT_MISMATCH,
            f"the record pays {permit.get('recipient')} but the guard paid {observed.recipient}",
        )
    if _normalise_address(permit.get("token")) != observed.token:
        add(TOKEN_MISMATCH, f"the record uses token {permit.get('token')} but the guard used {observed.token}")
    recorded_evidence = str(permit.get("evidence_hash") or "").lower()
    if recorded_evidence != observed.evidence_hash.lower():
        add(
            EVIDENCE_MISMATCH,
            "the evidence hash the record authorized does not match the hash the guard settled; "
            "the decision that was paid is not the decision on file",
        )
    if _normalise_address(permit.get("payer")) != observed.payer:
        add(PAYER_MISMATCH, f"the record pays from {permit.get('payer')} but the guard paid from {observed.payer}")
    return findings


def _check_deployment(record: dict[str, Any], guard: str) -> list[Finding]:
    """A record from another deployment must not be reconciled against this one."""
    permit = record["permit"]
    findings: list[Finding] = []
    identity = {"invoice_id": record["invoice_id"], "payment_id": record["payment_id"]}
    recorded_guard = permit.get("guard_address")
    if recorded_guard and _normalise_address(recorded_guard) != _normalise_address(guard):
        findings.append(
            Finding(
                GUARD_MISMATCH,
                BLOCKING,
                f"the record was authorized for guard {recorded_guard} but this deployment reconciles {guard}",
                **identity,
            )
        )
    recorded_chain = permit.get("chain_id")
    if recorded_chain is not None and int(recorded_chain) != ARC_TESTNET_CHAIN_ID:
        findings.append(
            Finding(
                CHAIN_ID_MISMATCH,
                BLOCKING,
                f"the record was authorized for chain {recorded_chain}, not {ARC_TESTNET_CHAIN_ID}",
                **identity,
            )
        )
    return findings


def reconcile(
    store: Any,
    settings: Settings,
    *,
    rpc: Any | None = None,
    from_block: int | None = None,
    to_block: int | None = None,
    lookback_blocks: int | None = None,
) -> Report:
    """Compare recorded authorizations with the guard's settlement log.

    ``from_block``/``to_block`` bound the search and are reported verbatim, because every
    conclusion drawn from an absence depends on them.
    """
    view = provider_view(settings)
    guard = view.guard_address
    coverage: dict[str, Any] = {
        "provider": view.name,
        "guard_address": guard,
        "rpc_url": view.rpc_url,
        "from_block": from_block,
        "to_block": to_block,
        "complete_from_block": from_block,
        "chain_id": None,
    }
    if not view.live:
        # A mock deployment settles against its own database. Comparing those rows with a chain
        # would manufacture findings, so the honest answer is that there is nothing to reconcile.
        return Report(
            ok=True,
            coverage=coverage,
            findings=[
                Finding(
                    PROVIDER_NOT_LIVE,
                    REVIEW,
                    f"the {view.label} provider settles against the local database and moves no money, so "
                    "there is no on-chain settlement to reconcile; this report says nothing about a real chain",
                )
            ],
        )
    if not guard:
        return Report(
            ok=False,
            coverage=coverage,
            findings=[
                Finding(
                    NO_GUARD_CONFIGURED,
                    BLOCKING,
                    f"the {view.label} provider has no guard address configured, so there is nothing to "
                    "reconcile against; a settlement cannot be confirmed without the contract that made it",
                )
            ],
        )

    rpc = rpc if rpc is not None else RpcClient(view.rpc_url)
    try:
        chain_id = int(rpc.call("eth_chainId", []), 16)
    except Exception as exc:
        coverage["error"] = f"{type(exc).__name__}"
        return Report(
            ok=False,
            coverage=coverage,
            findings=[
                Finding(
                    CHAIN_UNREACHABLE,
                    BLOCKING,
                    f"the chain could not be read ({type(exc).__name__}), so recorded payments were not "
                    "compared with anything; no conclusion about settlement can be drawn",
                )
            ],
        )
    coverage["chain_id"] = chain_id
    if chain_id != ARC_TESTNET_CHAIN_ID:
        return Report(
            ok=False,
            coverage=coverage,
            findings=[
                Finding(
                    WRONG_CHAIN,
                    BLOCKING,
                    f"the RPC endpoint reports chain id {chain_id}, not {ARC_TESTNET_CHAIN_ID}; refusing to "
                    "reconcile a testnet record against an endpoint that is not the deployment's chain",
                )
            ],
        )

    findings: list[Finding] = []
    try:
        latest = int(rpc.call("eth_blockNumber", []), 16)
        end = latest if to_block is None else int(to_block)
        if lookback_blocks is not None:
            start = max(0, end - int(lookback_blocks))
        elif from_block is not None:
            start = int(from_block)
        else:
            start = 0
        # A range wider than the cap is scanned from the newest end, and the report says so.
        # Silently narrowing the range would turn "not seen" into "did not happen".
        if end - start + 1 > MAX_SCAN_BLOCKS:
            truncated = end - MAX_SCAN_BLOCKS + 1
            findings.append(
                Finding(
                    RANGE_TRUNCATED,
                    REVIEW,
                    f"the requested range {start}-{end} exceeds the {MAX_SCAN_BLOCKS}-block scan cap; only "
                    f"{truncated}-{end} was searched, so an absent event in the older range proves nothing",
                )
            )
            start = truncated
        coverage["from_block"] = start
        coverage["to_block"] = end
        coverage["complete_from_block"] = start
        logs = _fetch_logs(rpc, guard, start, end)
    except ReconciliationUnavailable:
        raise
    except Exception as exc:
        coverage["error"] = f"{type(exc).__name__}"
        return Report(
            ok=False,
            coverage=coverage,
            findings=findings
            + [
                Finding(
                    CHAIN_UNREACHABLE,
                    BLOCKING,
                    f"the guard's logs could not be read ({type(exc).__name__}); no conclusion about "
                    "settlement can be drawn from an empty or failed log query",
                )
            ],
        )

    observations: list[Observation] = []
    for log in logs:
        decoded = _decode_payment_log(log)
        if decoded is None:
            findings.append(
                Finding(
                    MALFORMED_EVENT,
                    BLOCKING,
                    "a log from the guard address is not a decodable PaymentExecuted event; the "
                    "settlement history cannot be fully accounted for",
                    transaction_hash=str(log.get("transactionHash") or "") or None,
                )
            )
            continue
        observations.append(decoded)

    records = _records(store)

    by_payment_id: dict[str, list[Observation]] = {}
    for observed in observations:
        by_payment_id.setdefault(observed.payment_id, []).append(observed)

    # Reverse direction: money the chain says moved. Safe regardless of the scan range,
    # because these are events that were actually seen.
    recorded_ids = {str(record["payment_id"]) for record in records if record.get("payment_id")}
    for payment_id, group in by_payment_id.items():
        if len(group) > 1:
            findings.append(
                Finding(
                    DUPLICATE_SETTLEMENT_EVENTS,
                    BLOCKING,
                    f"the guard settled {payment_id} {len(group)} times; the contract is supposed to make "
                    "this impossible, so either the log is not the guard's or something is very wrong",
                    payment_id=payment_id,
                    transaction_hash=group[0].transaction_hash,
                )
            )
        if payment_id not in recorded_ids:
            first = group[0]
            findings.append(
                Finding(
                    SETTLED_NOT_RECORDED,
                    BLOCKING,
                    f"the guard settled {units_to_usdc(first.amount_units)} USDC to {first.recipient} with no "
                    "payments row at all; money moved without a record of who authorized it",
                    payment_id=payment_id,
                    transaction_hash=first.transaction_hash,
                )
            )

    seen_ids: dict[str, str] = {}
    seen_hashes: dict[str, str] = {}
    for record in records:
        payment_id = str(record.get("payment_id") or "")
        if payment_id:
            if payment_id in seen_ids and seen_ids[payment_id] != record["invoice_id"]:
                findings.append(
                    Finding(
                        DUPLICATE_PAYMENT_ID_RECORDS,
                        BLOCKING,
                        f"payment {payment_id} is recorded against more than one invoice "
                        f"({seen_ids[payment_id]} and {record['invoice_id']}); one authorization cannot "
                        "belong to two obligations",
                        invoice_id=record["invoice_id"],
                        payment_id=payment_id,
                    )
                )
            seen_ids.setdefault(payment_id, record["invoice_id"])
        tx_hash = record.get("transaction_hash")
        if tx_hash:
            if tx_hash in seen_hashes and seen_hashes[tx_hash] != record["invoice_id"]:
                findings.append(
                    Finding(
                        SHARED_TRANSACTION_HASH,
                        BLOCKING,
                        f"transaction {tx_hash} is recorded against more than one payment "
                        f"({seen_hashes[tx_hash]} and {record['invoice_id']}); the chain settled it once",
                        invoice_id=record["invoice_id"],
                        transaction_hash=tx_hash,
                    )
                )
            seen_hashes.setdefault(tx_hash, record["invoice_id"])

    # Absence only proves something when the search provably covers the whole chain. Scanning
    # from a later block leaves an older settlement invisible, so the two conclusions must never
    # be merged into one.
    coverage_is_complete = coverage.get("complete_from_block") == 0

    for record in records:
        payment_id = str(record.get("payment_id") or "")
        findings.extend(_check_deployment(record, guard))

        group = by_payment_id.get(payment_id, [])
        observed = group[0] if group else None

        if observed is None and record.get("transaction_hash"):
            # An exact transaction is better evidence than a window, and needs no range. When it
            # settles the question the record is finished: a second, weaker finding about the same
            # payment would only dilute the first.
            observed, concluded = _observation_from_receipt(rpc, record["transaction_hash"], findings, record, guard)
            if concluded and observed is None:
                continue

        if observed is not None:
            findings.extend(_compare(record, observed))
            if str(record["state"]).upper() in FAILED_STATES:
                findings.append(
                    Finding(
                        SETTLED_BUT_RECORDED_FAILED,
                        BLOCKING,
                        f"the record says {record['state']} but the guard settled {payment_id} in "
                        f"{observed.transaction_hash}; the payment moved money that the books say did not happen",
                        invoice_id=record["invoice_id"],
                        payment_id=payment_id,
                        transaction_hash=observed.transaction_hash,
                    )
                )
            continue

        claimed = _claimed_settled(record)
        uncertain = str(record["state"]).upper() in ATTEMPTED_UNCERTAIN_STATES
        if not claimed and not uncertain:
            # An authorization that was never sent, or a payment already proven failed on chain,
            # has nothing to reconcile. Saying nothing is the correct output.
            continue

        if record.get("provider_transaction_id") and not coverage_is_complete:
            findings.append(
                Finding(
                    PROVIDER_RESOLUTION_REQUIRED,
                    REVIEW,
                    "the payment was submitted through a custodial provider and was not seen in the "
                    "searched range, so only that provider can resolve its outcome; the chain alone cannot",
                    invoice_id=record["invoice_id"],
                    payment_id=payment_id,
                )
            )
            continue

        if not coverage_is_complete:
            findings.append(
                Finding(
                    UNRESOLVED_UNCERTAINTY,
                    REVIEW,
                    f"no settlement for {payment_id} was found in blocks {coverage['from_block']}-"
                    f"{coverage['to_block']}, but the search started after genesis, so this is not proof "
                    "that it did not settle. Re-run with --from-block 0 to settle the question, or resolve "
                    "it from the recorded transaction, the guard's used() mapping, or the provider. "
                    "Never resend it on this evidence alone.",
                    invoice_id=record["invoice_id"],
                    payment_id=payment_id,
                )
            )
            continue

        if claimed:
            findings.append(
                Finding(
                    SETTLEMENT_WITHOUT_EVIDENCE,
                    BLOCKING,
                    f"the record claims {record['state']}"
                    f"{'/' + str(record['confirmation_status']) if record.get('confirmation_status') else ''} "
                    f"but no settlement for {payment_id} exists anywhere on the chain; the ledger and the "
                    "chain disagree about whether money moved",
                    invoice_id=record["invoice_id"],
                    payment_id=payment_id,
                )
            )
            continue

        findings.append(
            Finding(
                NOT_SETTLED_PROVEN,
                REVIEW,
                f"no settlement for {payment_id} exists on the chain, and the search covered the whole "
                "chain, so this authorization did not settle. It can be re-evaluated rather than "
                "reconciled, and it must not be resent using the old authorization.",
                invoice_id=record["invoice_id"],
                payment_id=payment_id,
            )
        )

    ok = not any(finding.severity == BLOCKING for finding in findings)
    return Report(ok=ok, coverage=coverage, findings=findings, observations=len(observations), records=len(records))


def _observation_from_receipt(
    rpc: Any, transaction_hash: str, findings: list[Finding], record: dict[str, Any], guard: str
) -> tuple[Observation | None, bool]:
    """Look for the settlement inside a specific transaction the record names.

    A recorded transaction hash is exact evidence: it either contains the guard's event or it
    does not, with no dependence on a block range. A receipt that cannot be read is reported
    rather than treated as an empty transaction.

    Returns ``(observation, concluded)``. ``concluded`` is True when this settled the question
    either way, so the caller does not stack a weaker finding on top of a definitive one.
    """
    try:
        receipt = rpc.call("eth_getTransactionReceipt", [transaction_hash])
    except Exception as exc:
        findings.append(
            Finding(
                UNRESOLVED_UNCERTAINTY,
                REVIEW,
                f"the receipt for {transaction_hash} could not be read ({type(exc).__name__}), so this "
                "payment's outcome is unknown rather than absent",
                invoice_id=record["invoice_id"],
                payment_id=record.get("payment_id"),
                transaction_hash=transaction_hash,
            )
        )
        return None, False
    if not receipt:
        findings.append(
            Finding(
                RECORDED_TRANSACTION_LACKS_EVENT,
                BLOCKING,
                f"the record names transaction {transaction_hash} but the node has no receipt for it; the "
                "payment is recorded against a transaction that does not exist at this endpoint",
                invoice_id=record["invoice_id"],
                payment_id=record.get("payment_id"),
                transaction_hash=transaction_hash,
            )
        )
        return None, True
    for log in receipt.get("logs") or []:
        # The event must come from the guard being reconciled, not merely from whatever address the
        # transaction was sent to. Settlement can reach the guard through an intermediary, and
        # comparing against receipt["to"] would then skip a real settlement and report a blocking
        # mismatch about a transaction that did settle correctly.
        if _normalise_address(log.get("address")) != _normalise_address(guard):
            continue
        decoded = _decode_payment_log(log)
        if decoded is not None and decoded.payment_id == str(record.get("payment_id") or ""):
            return decoded, True
    findings.append(
        Finding(
            RECORDED_TRANSACTION_LACKS_EVENT,
            BLOCKING,
            f"the record names transaction {transaction_hash}, but that transaction contains no "
            "PaymentExecuted event for this payment; the recorded transaction is not the settlement",
            invoice_id=record["invoice_id"],
            payment_id=record.get("payment_id"),
            transaction_hash=transaction_hash,
        )
    )
    return None, True


def _format(report: Report) -> str:
    lines: list[str] = []
    coverage = report.coverage
    target = coverage.get("guard_address")
    lines.append(
        f"Settlement reconciliation ({coverage.get('provider')}"
        + (f" / {target}" if target else " / no guard configured")
        + ")"
    )
    if coverage.get("from_block") is not None:
        lines.append(
            f"  searched blocks {coverage['from_block']}-{coverage.get('to_block')} "
            f"on chain {coverage.get('chain_id')}"
        )
    lines.append(f"  {report.records} recorded payment(s), {report.observations} settlement event(s) seen")
    if not report.findings:
        lines.append("  nothing to report: every recorded payment is accounted for")
        return "\n".join(lines)
    for label, group in (("BLOCKING", report.blocking), ("REVIEW", report.review)):
        for finding in group:
            target = finding.invoice_id or finding.payment_id or finding.transaction_hash or "-"
            lines.append(f"  {label:8} {finding.kind}: {finding.detail} [{target}]")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare recorded payments with what the guard settled (read-only, signs nothing)"
    )
    parser.add_argument("--from-block", type=int, default=None, help="First block to search (default: 0)")
    parser.add_argument("--to-block", type=int, default=None, help="Last block to search (default: latest)")
    parser.add_argument("--lookback-blocks", type=int, default=None, help="Search this many blocks back instead")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    args = parser.parse_args(argv)

    settings = get_settings()
    from .runtime import build_workflow

    workflow = build_workflow(settings)
    try:
        report = reconcile(
            workflow.store,
            settings,
            from_block=args.from_block,
            to_block=args.to_block,
            lookback_blocks=args.lookback_blocks,
        )
    except ReconciliationUnavailable as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(_format(report))
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
