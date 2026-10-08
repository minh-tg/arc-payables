"""One command to send a real Arc Testnet payment, with a preflight that refuses to guess.

A live payment should not be the moment the configuration is discovered to be wrong. This
command checks everything it can *before* anything is signed, and it is deliberately
unhelpful when something is missing:

* the chain must be Arc Testnet, from the node's own ``eth_chainId``;
* the deployed guard must have a real budget, and that budget must cover this payment;
* the destination must be the trusted Supplier record's wallet, and a human must have
  verified it — the invoice's own payee field is never consulted;
* the invoice must be linked to an accounting payable that the policy accepts;
* the treasury must cover the amount while preserving the configured reserve, and hold native
  USDC for gas.

Nothing is sent unless every check passes *and* ``--confirm`` is passed. Without it the command
is a dry run, so inspecting what would happen is free.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any

from .api import create_app
from .domain import ARC_TESTNET_CHAIN_ID, USDC_SCALE, units_to_usdc
from .settings import Settings, get_settings
from .verify_arc import RpcClient, provider_view, verify

EXPLORER_BASE = "https://testnet.arcscan.app/tx/"


@dataclass
class PreflightResult:
    ok: bool = True
    findings: list[str] = field(default_factory=list)
    invoice_id: str | None = None
    amount_usdc: str | None = None
    recipient: str | None = None

    def fail(self, message: str) -> None:
        self.ok = False
        self.findings.append(f"FAIL  {message}")

    def pass_(self, message: str) -> None:
        self.findings.append(f"OK    {message}")

    def note(self, message: str) -> None:
        self.findings.append(f"INFO  {message}")


def preflight(workflow, settings: Settings, invoice_id: str, *, rpc: RpcClient | None = None) -> PreflightResult:
    """Everything that can be checked before signing anything."""
    result = PreflightResult(invoice_id=invoice_id)
    view = provider_view(settings)
    rpc = rpc or RpcClient(view.rpc_url)

    # 1. The chain and the deployed guard, using the same checks as the read-only verifier.
    signer_address = (getattr(settings, "permit_signing_address", None) or "").strip() or None
    if signer_address is None and settings.permit_signing_private_key:
        from .security import EIP712PermitSigner

        signer_address = EIP712PermitSigner(settings.permit_signing_private_key).address
    try:
        chain_ok, findings = verify(rpc, settings, signer_address=signer_address, view=view)
    except Exception as exc:
        result.fail(f"could not verify the chain: {type(exc).__name__}: {exc}")
        return result
    result.findings.extend(findings)
    if not chain_ok:
        result.fail("the Arc Testnet or guard checks did not pass, so nothing will be signed")

    # 2. The invoice and its trusted destination.
    invoice = workflow.store.get_invoice(invoice_id)
    if invoice is None:
        result.fail(f"no invoice {invoice_id!r} exists in this database; import it first")
        return result
    if not invoice.purchase_invoice_id:
        result.fail("the invoice is not linked to an accounting payable, so it is not payable")
    context = workflow._load_context(invoice)
    evidence = context["accounting"]
    supplier = evidence.supplier
    if supplier is None or not supplier.approved_wallet:
        result.fail("the trusted Supplier record has no approved wallet")
        return result
    result.recipient = supplier.approved_wallet
    result.amount_usdc = units_to_usdc(invoice.amount_units)
    if supplier.payment_blocked:
        result.fail(f"the accounting system blocks this supplier: {supplier.blocked_reason or 'on hold or disabled'}")
    if not supplier.wallet_verified:
        result.fail(
            "the trusted Supplier wallet is not verified; a human must verify it in the accounting system first"
        )
    else:
        result.pass_(f"destination is the verified Supplier wallet {supplier.approved_wallet}")

    # 3. The guard's budget for this specific payment and recipient.
    provider = workflow.payment_provider
    per_payment_cap = getattr(settings, "max_invoice_units", None)
    if provider.__class__.__name__ == "DisabledPaymentProvider":
        result.fail(f"the {view.label} provider is not configured")
        return result
    limits = provider.guard_limits() if hasattr(provider, "guard_limits") else None
    if limits and limits.get("per_payment_cap") and invoice.amount_units > limits["per_payment_cap"]:
        result.fail(
            "the payment exceeds the guard's per-payment cap of "
            f"{units_to_usdc(limits['per_payment_cap'])} USDC, so the contract would reject it"
        )
    remaining = None
    if hasattr(provider, "remaining_budget"):
        remaining = provider.remaining_budget(supplier.approved_wallet)
        if remaining is None:
            result.note("the guard does not expose a remaining-budget view; relying on the payment itself to revert")
        else:
            result.note(f"guard budget remaining this epoch for this recipient: {units_to_usdc(remaining)} USDC")
            if invoice.amount_units > remaining:
                result.fail("this payment would exceed the guard's remaining on-chain budget")

    # 4. Treasury: the amount, the reserve floor, and native gas.
    balance = provider.get_balance()
    result.note(f"treasury balance: {units_to_usdc(balance.balance_units)} USDC")
    if balance.balance_units < invoice.amount_units:
        result.fail("the treasury does not hold enough USDC for this payment")
    elif balance.balance_units - invoice.amount_units < settings.min_reserve_units:
        result.fail(
            "paying this would breach the configured treasury reserve of "
            f"{settings.min_reserve_usdc} USDC; lower the invoice or raise the balance"
        )
    else:
        result.pass_(
            f"paying {result.amount_usdc} USDC leaves "
            f"{units_to_usdc(balance.balance_units - invoice.amount_units)} USDC, above the reserve floor"
        )
    if per_payment_cap and invoice.amount_units > per_payment_cap:
        result.fail(f"the amount exceeds the automatic limit of {settings.max_invoice_usdc} USDC")
    return result


def run(args, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    app = create_app(settings=settings)
    workflow = app.state.workflow

    result = preflight(workflow, settings, args.invoice)
    for line in result.findings:
        print(f"  {line}")
    if not result.ok:
        print("Preflight failed. Nothing was signed or sent.")
        return 1
    print("Preflight passed.")

    if not args.confirm:
        print(f"Dry run: re-run with --confirm to pay {result.amount_usdc} USDC to {result.recipient}.")
        return 0

    # Re-evaluate at the moment of payment: the policy and the evidence hash are recomputed,
    # so an approval or evaluation that went stale cannot be reused.
    evaluated = workflow.evaluate(args.invoice)
    if evaluated["state"] != "ELIGIBLE":
        print(f"Refusing to pay: the fresh evaluation returned {evaluated['state']} ({evaluated['decision']['reason']})")
        return 1
    outcome = workflow.submit_payment(args.invoice)
    tx_hash = (outcome.get("payment") or {}).get("transaction_hash")
    print(f"Result: {outcome['state']}")
    if tx_hash:
        print(f"Transaction: {tx_hash}")
        print(f"Explorer: {EXPLORER_BASE}{tx_hash}")
    return 0 if outcome["state"] in {"CONFIRMED", "ERP_PENDING", "ERP_RECORDED"} else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Preflight and optionally send one real Arc Testnet payment")
    parser.add_argument("--invoice", required=True, help="Local invoice id to pay")
    parser.add_argument("--confirm", action="store_true", help="Actually sign and send; without it this is a dry run")
    args = parser.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
