"""Operator-run provisioning for a disposable USD ERPNext sandbox, never for production.

The company/chart must already exist. Preview is read-only; --apply explicitly enables
writes. Existing records are validated, not patched. Unique seed keys let an operator rerun
the SAME inputs after a lost response; requests are never automatically retried. This is not
an atomic migration: a failure can leave drafts/master records for review.

No payment is made. New supplier wallets remain UNVERIFIED until a human verifies them in
ERPNext. Provisioning permissions/credentials must not be given to the agent/runtime user.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import shlex
import sys
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

SETTLEMENT_ACCOUNT_NAME = "USDC Wallet"
FEE_ACCOUNT_NAME = "Network Fees"
SUPPLIER_WALLET_FIELD = "custom_usdc_wallet_address"
SUPPLIER_WALLET_VERIFIED_FIELD = "custom_usdc_wallet_verified"
SEED_FIELD = "custom_arc_payables_seed_key"
DEMO_SUPPLIER = "Arc Payables Demo Supplier"
DEMO_ITEM = "ARC-PAYABLES-DEMO-FILTER"
TRANSACTION_TYPES = ("Purchase Order", "Purchase Receipt", "Purchase Invoice")


class BootstrapError(RuntimeError):
    """Safe to display: never contains HTTP bodies, headers, or credential values."""


def number(value: Any) -> Decimal:
    try:
        result = Decimal(str(int(value) if isinstance(value, bool) else value))
        if result.is_finite():
            return result
    except (InvalidOperation, ValueError, TypeError):
        pass
    raise BootstrapError("ERPNext or input contains an invalid numeric field")


def check(doc: dict, expected: dict, label: str) -> None:
    """Compare only the intended fields, but require exact child-table cardinality."""
    for field, value in expected.items():
        actual = doc.get(field)
        if isinstance(value, list):
            if not isinstance(actual, list) or len(actual) != len(value):
                raise BootstrapError(f"{label}: conflicting {field}")
            for row, wanted in zip(actual, value):
                if not isinstance(row, dict):
                    raise BootstrapError(f"{label}: invalid {field}")
                check(row, wanted, label)
        elif isinstance(value, (Decimal, int)):
            if number(actual) != value:
                raise BootstrapError(f"{label}: conflicting {field}")
        elif actual != value and not (value == "" and actual is None):
            # Frappe may normalize an absent optional Link from "" to null.
            raise BootstrapError(f"{label}: conflicting {field}")


def server_message(payload: dict) -> str:
    """Human-readable ERPNext failure reason, without the raw body or traceback.

    Only Frappe's structured error fields are read: ``_server_messages`` (the validation
    messages shown in the UI) and ``exc_type``/``exception``. The full ``exc`` traceback and
    the raw body are deliberately ignored so credentials or unrelated payloads cannot leak.
    """
    parts: list[str] = []
    raw = payload.get("_server_messages")
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except ValueError:
            decoded = []
        for entry in decoded if isinstance(decoded, list) else []:
            try:
                message = json.loads(entry).get("message") if isinstance(entry, str) else entry.get("message")
            except Exception:
                message = entry
            if message:
                parts.append(str(message))
    for key in ("exc_type", "exception"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip().splitlines()[-1])
    cleaned = [re.sub(r"<[^>]+>", "", part).strip() for part in parts]
    return " ".join(dict.fromkeys(part for part in cleaned if part))[:500]


def seed_key(doctype: str, company: str, reference: str) -> str:
    identity = json.dumps(["arc-payables-demo-v1", doctype, company, reference], separators=(",", ":"))
    return "arc-payables-demo:" + hashlib.sha256(identity.encode()).hexdigest()


class FrappeClient:
    """Small REST v1 client. It never retries writes or logs responses/credentials."""

    def __init__(self, url: str, api_key: str, api_secret: str, *, dry_run: bool = True,
                 timeout: float = 20.0, client: httpx.Client | None = None):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise BootstrapError("Use HTTPS, or HTTP on localhost; URL credentials/query/fragment are forbidden")
        if not api_key or not api_secret:
            raise BootstrapError("FRAPPE_API_KEY and FRAPPE_API_SECRET are required")
        self.base_url = url.rstrip("/")
        self.dry_run = dry_run
        self.headers = {"Authorization": f"token {api_key}:{api_secret}", "Accept": "application/json"}
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self._owns_client = client is None
        self.created: list[str] = []
        self.plan: list[str] = []
        self._planned: dict[tuple[str, str], dict] = {}
        self._planned_seed_fields: set[str] = set()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _request(self, method: str, path: str, *, missing_ok: bool = False, **kwargs) -> dict | None:
        if self.dry_run and method != "GET":
            raise BootstrapError("Preview must not send write requests")
        try:
            response = self.client.request(method, self.base_url + path, headers=self.headers,
                                           follow_redirects=False, **kwargs)
        except httpx.HTTPError:
            raise BootstrapError("ERPNext request failed; reconcile using the same company/reference before rerunning") from None
        if response.status_code == 404 and missing_ok:
            return None
        if not 200 <= response.status_code < 300:
            hint = ""
            if response.status_code in (401, 403):
                hint = "; check FRAPPE_API_KEY/FRAPPE_API_SECRET and the API user's roles/status"
            detail = ""
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                reason = server_message(payload)
                if reason:
                    detail = f": {reason}"
            raise BootstrapError(
                f"ERPNext {method} {path} failed (HTTP {response.status_code}){hint}{detail}; no automatic retry"
            )
        try:
            result = response.json()
        except ValueError:
            raise BootstrapError("ERPNext returned invalid JSON; outcome may require reconciliation") from None
        if not isinstance(result, dict):
            raise BootstrapError("ERPNext returned an invalid response object")
        return result

    @staticmethod
    def _path(doctype: str, name: str | None = None) -> str:
        path = "/api/resource/" + quote(doctype, safe="")
        return path if name is None else path + "/" + quote(name, safe="")

    @staticmethod
    def _doc(result: dict, key: str = "data") -> dict:
        doc = result.get(key)
        if not isinstance(doc, dict) or not isinstance(doc.get("name"), str) or not doc["name"]:
            raise BootstrapError("ERPNext response lacks a valid document identity")
        return doc

    def get(self, doctype: str, name: str) -> dict | None:
        if (doctype, name) in self._planned:
            return deepcopy(self._planned[doctype, name])
        result = self._request("GET", self._path(doctype, name), missing_ok=True)
        if result is None:
            return None
        doc = self._doc(result)
        if doc["name"] != name:
            raise BootstrapError("ERPNext returned a different document identity")
        return doc

    def require(self, doctype: str, name: str) -> dict:
        doc = self.get(doctype, name)
        if doc is None:
            raise BootstrapError(f"Required {doctype} record is missing; complete sandbox setup first")
        return doc

    def find(self, doctype: str, filters: list, fields: list[str] | None = None) -> list[dict]:
        # On a first preview the seed column does not exist remotely yet. Do not query it.
        if doctype in self._planned_seed_fields and any(row[0] == SEED_FIELD for row in filters):
            return []
        result = self._request("GET", self._path(doctype), params={
            "filters": json.dumps(filters), "fields": json.dumps(fields or ["name"]),
            "limit_page_length": 100,
        })
        rows = result.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get("name") for row in rows):
            raise BootstrapError("ERPNext returned an invalid document list")
        return rows

    def one(self, doctype: str, filters: list) -> dict | None:
        rows = self.find(doctype, filters)
        if len(rows) > 1:
            raise BootstrapError(f"Ambiguous {doctype} records; refusing to choose the first")
        return self.require(doctype, rows[0]["name"]) if rows else None

    def insert(self, doctype: str, payload: dict) -> dict:
        if self.dry_run:
            name = "PLANNED-" + str(len(self._planned) + 1)
            doc = {**deepcopy(payload), "doctype": doctype, "name": name, "docstatus": 0}
            for index, item in enumerate(doc.get("items", [])):
                item["name"] = f"{name}-LINE-{index + 1}"
            self._planned[doctype, name] = doc
            self.plan.append(f"CREATE {doctype} ({name})")
            if doctype == "Custom Field" and payload.get("fieldname") == SEED_FIELD:
                self._planned_seed_fields.add(payload["dt"])
            return deepcopy(doc)
        result = self._request("POST", self._path(doctype), json=payload)
        doc = self._doc(result)
        self.created.append(f"{doctype}: {doc['name']}")
        return doc

    def is_planned(self, doctype: str, doc: dict) -> bool:
        return (doctype, doc["name"]) in self._planned

    def submit(self, doctype: str, doc: dict) -> dict:
        # Frappe v15 client.submit calls get_doc(dict), not get_doc(doctype, name).
        # Send the validated full document (including modified/child rows), not just its name.
        if self.dry_run:
            self.plan.append(f"SUBMIT {doctype} ({doc['name']})")
            # Do not pretend server-side submission/GL effects occurred during preview.
            return deepcopy(doc)
        result = self._request("POST", "/api/method/frappe.client.submit", json={"doc": {**doc, "doctype": doctype}})
        submitted = self._doc(result, "message")
        if submitted["name"] != doc["name"] or submitted.get("docstatus") != 1:
            raise BootstrapError("ERPNext did not confirm submission of the expected document")
        current = self.require(doctype, doc["name"])
        if current.get("docstatus") != 1:
            raise BootstrapError("ERPNext submission readback disagrees; reconcile before continuing")
        return current

    def ensure(self, doctype: str, filters: list, payload: dict, expected: dict | None = None) -> dict:
        doc = self.one(doctype, filters)
        if doc is None:
            doc = self.insert(doctype, payload)
        check(doc, expected if expected is not None else payload, doctype)
        return doc

    def server_time_zone(self) -> str:
        """The site's configured time zone, used to resolve "today" correctly."""
        result = self._request("GET", "/api/method/frappe.client.get_time_zone")
        message = result.get("message")
        zone = message.get("time_zone") if isinstance(message, dict) else message
        if not isinstance(zone, str) or not zone.strip():
            raise BootstrapError("ERPNext did not report a site time zone")
        return zone.strip()

    def server_today(self) -> date:
        """Today in the site's time zone; a UTC-only date can be a day ahead of the site."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        try:
            zone = ZoneInfo(self.server_time_zone())
        except Exception:
            raise BootstrapError("ERPNext reported an unknown site time zone") from None
        return datetime.now(zone).date()

    def resolve_company(self, requested: str | None) -> dict:
        if requested:
            doc = self.require("Company", requested)
        else:
            companies = self.find("Company", [])
            if not companies:
                raise BootstrapError("No Company exists; complete the ERPNext setup wizard first")
            if len(companies) != 1:
                raise BootstrapError("Multiple companies exist; pass --company explicitly")
            doc = self.require("Company", companies[0]["name"])
        if doc.get("default_currency") != "USD":
            raise BootstrapError("This demo requires a USD company; never assume a 1:1 CAD/USDC or other FX rate")
        if not doc.get("abbr"):
            raise BootstrapError("Company abbreviation is missing")
        return doc


def _active_account(client: FrappeClient, name: str, company: str, currency: str, root: str,
                    account_type: str | None = None) -> dict:
    doc = client.require("Account", name)
    expected = {"company": company, "account_currency": currency, "root_type": root, "is_group": 0, "disabled": 0}
    if account_type:
        expected["account_type"] = account_type
    check(doc, expected, "Account")
    if doc.get("freeze_account") == "Yes":
        raise BootstrapError("Account is frozen")
    return doc


def _root_account(client: FrappeClient, company: str, root: str) -> str:
    groups = client.find("Account", [["company", "=", company], ["root_type", "=", root], ["is_group", "=", 1]],
                         ["name", "parent_account", "disabled"])
    roots = [row for row in groups if not row.get("parent_account") and row.get("disabled") == 0]
    if len(roots) != 1:
        raise BootstrapError(f"Exactly one active {root} root group account is required")
    return roots[0]["name"]


def _ensure_field(client: FrappeClient, doctype: str, fieldname: str, label: str, fieldtype: str,
                  *, unique: bool = False) -> None:
    payload = {"dt": doctype, "fieldname": fieldname, "label": label, "fieldtype": fieldtype,
               "insert_after": "supplier_name" if doctype == "Supplier" else "company",
               "description": "Disposable Arc Payables sandbox setup; not agent-managed supplier verification."}
    expected = {"dt": doctype, "fieldname": fieldname, "fieldtype": fieldtype}
    if unique:
        payload.update(unique=1, no_copy=1, read_only=1)
        expected.update(unique=1, no_copy=1, read_only=1)
    client.ensure("Custom Field", [["dt", "=", doctype], ["fieldname", "=", fieldname]], payload, expected)


def _transaction(client: FrappeClient, doctype: str, payload: dict, amount: Decimal) -> dict:
    doc = client.one(doctype, [[SEED_FIELD, "=", payload[SEED_FIELD]]])
    if doc is None:
        doc = client.insert(doctype, payload)

    def validate(current: dict) -> None:
        # Numeric inputs are transmitted as decimal strings; compare numerically on readback.
        expected = deepcopy(payload)
        for row in expected["items"]:
            for field in ("qty", "rate", "amount", "conversion_factor"):
                if field in row:
                    row[field] = number(row[field])
        check(current, expected, doctype)
        if current.get("docstatus") not in (0, 1):
            raise BootstrapError(f"{doctype} is cancelled or has an unknown submission state")
        if not current["items"][0].get("name"):
            raise BootstrapError(f"{doctype} is missing its authoritative child-row identity")
        if not client.is_planned(doctype, current):
            check(current, {"grand_total": amount, "base_grand_total": amount, "net_total": amount}, doctype)
            if doctype == "Purchase Invoice" and current["docstatus"] == 1:
                check(current, {"outstanding_amount": amount}, doctype)

    validate(doc)
    if doc["docstatus"] == 0:
        doc = client.submit(doctype, doc)
        validate(doc)
    return doc


def bootstrap(client: FrappeClient, *, company: str | None, supplier_wallet: str,
              invoice_reference: str, quantity: Any, rate: Any, posting_date: str | None) -> dict[str, Any]:
    """Provision only the selected USD demo company; never modify existing conflicting data."""
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", supplier_wallet or "") or int(supplier_wallet[2:], 16) == 0:
        raise BootstrapError("An explicit nonzero EVM demo supplier wallet is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", invoice_reference or ""):
        raise BootstrapError("Invoice reference must be 1-64 letters, digits, dots, underscores or hyphens")
    qty, price = number(quantity), number(rate)
    if qty <= 0 or qty > 1_000_000 or qty != qty.to_integral_value():
        raise BootstrapError("Demo Nos quantity must be a positive whole number no greater than 1000000")
    if price <= 0 or price > 1_000_000 or price * 100 != (price * 100).to_integral_value():
        raise BootstrapError("USD demo rate must be positive, at most 1000000, with at most two decimal places")
    parsed_date: date | None = None
    if posting_date is not None:
        try:
            parsed_date = date.fromisoformat(posting_date)
            if parsed_date.isoformat() != posting_date:
                raise ValueError
        except (ValueError, TypeError):
            raise BootstrapError("Posting date must be YYYY-MM-DD") from None

    # "Today" means today for the ERPNext site, not the host: ERPNext refuses a future posting
    # date for stock transactions, and a UTC host date can legitimately be a day ahead.
    today = client.server_today()
    if parsed_date is None:
        parsed_date = today
        posting_date = today.isoformat()
    if parsed_date > today:
        raise BootstrapError(
            f"Posting date {posting_date} is in the future for the ERPNext site time zone "
            f"(today there is {today.isoformat()})"
        )
    amount = qty * price
    company_doc = client.resolve_company(company)
    company_name, abbr = company_doc["name"], company_doc["abbr"]

    # Preflight the existing chart before any writes; never guess the first account/warehouse.
    payable_name = company_doc.get("default_payable_account")
    expense_name = company_doc.get("default_expense_account")
    cost_center = company_doc.get("cost_center")
    if not payable_name or not cost_center:
        raise BootstrapError("Company default payable account and cost center are required")
    _active_account(client, payable_name, company_name, "USD", "Liability", "Payable")
    if expense_name:
        # Kept as a preflight sanity check only: ERPNext derives each invoice line's expense or
        # interim stock account itself, so this value is not sent or asserted.
        _active_account(client, expense_name, company_name, "USD", "Expense")
    check(client.require("Cost Center", cost_center), {"company": company_name, "is_group": 0}, "Cost Center")
    warehouse = f"Stores - {abbr}"
    check(client.require("Warehouse", warehouse), {"company": company_name, "is_group": 0, "disabled": 0}, "Warehouse")
    client.require("Supplier Group", "All Supplier Groups")
    client.require("Item Group", "All Item Groups")
    # The UOM only has to exist and be enabled; the demo quantity is validated as a whole number
    # by this script, so requiring must_be_whole_number would be a spurious blocker (it defaults to 0).
    check(client.require("UOM", "Nos"), {"enabled": 1}, "UOM")
    # Every preflight above has passed; the writes start here.
    asset_parent = _root_account(client, company_name, "Asset")
    fee_parent = _root_account(client, company_name, "Expense")
    currency = {"currency_name": "USDC", "enabled": 1, "fraction": "Micro USDC", "fraction_units": 1_000_000,
                "smallest_currency_fraction_value": "0.000001", "symbol": "USDC"}
    client.ensure("Currency", [["name", "=", "USDC"]], currency,
                  {"currency_name": "USDC", "enabled": 1, "fraction_units": 1_000_000,
                   "smallest_currency_fraction_value": Decimal("0.000001")})

    accounts = []
    for label, root, account_type, currency_name, parent in (
        (SETTLEMENT_ACCOUNT_NAME, "Asset", "Bank", "USDC", asset_parent),
        (FEE_ACCOUNT_NAME, "Expense", "Expense Account", "USD", fee_parent),
    ):
        payload = {"account_name": label, "company": company_name, "parent_account": parent,
                   "root_type": root, "account_type": account_type, "account_currency": currency_name,
                   "is_group": 0, "disabled": 0}
        account = client.ensure("Account", [["company", "=", company_name], ["account_name", "=", label]], payload)
        if account.get("freeze_account") == "Yes":
            raise BootstrapError("Demo account is frozen")
        accounts.append(account["name"])
    settlement_account, fee_account = accounts
    mode_name = f"Arc Testnet Demo - {abbr}"
    mode = {"mode_of_payment": mode_name, "type": "Bank", "enabled": 1,
            "accounts": [{"company": company_name, "default_account": settlement_account}]}
    mode_doc = client.ensure("Mode of Payment", [["mode_of_payment", "=", mode_name]], mode)
    _ensure_field(client, "Supplier", SUPPLIER_WALLET_FIELD, "USDC Wallet Address", "Data")
    _ensure_field(client, "Supplier", SUPPLIER_WALLET_VERIFIED_FIELD, "USDC Wallet Verified", "Check")
    for doctype in TRANSACTION_TYPES:
        _ensure_field(client, doctype, SEED_FIELD, "Arc Payables Demo Seed Key", "Data", unique=True)

    supplier_name = f"{DEMO_SUPPLIER} - {abbr}"
    supplier_payload = {"supplier_name": supplier_name, "supplier_group": "All Supplier Groups",
                        "supplier_type": "Company", "default_currency": "USD", "disabled": 0,
                        SUPPLIER_WALLET_FIELD: supplier_wallet, SUPPLIER_WALLET_VERIFIED_FIELD: 0,
                        "accounts": [{"company": company_name, "account": payable_name}]}
    supplier = client.one("Supplier", [["supplier_name", "=", supplier_name]])
    if supplier is None:
        supplier = client.insert("Supplier", supplier_payload)
    expected_supplier = {k: v for k, v in supplier_payload.items()
                         if k not in (SUPPLIER_WALLET_FIELD, SUPPLIER_WALLET_VERIFIED_FIELD)}
    check(supplier, expected_supplier, "Supplier")
    if str(supplier.get(SUPPLIER_WALLET_FIELD, "")).lower() != supplier_wallet.lower():
        raise BootstrapError("Existing Supplier wallet differs; refusing to change a trusted destination")
    if supplier.get(SUPPLIER_WALLET_VERIFIED_FIELD) not in (0, 1, "0", "1", False, True):
        raise BootstrapError("Supplier wallet verification state is invalid")
    if supplier.get("on_hold") in (1, "1", True):
        raise BootstrapError("Supplier is on hold")
    item_payload = {"item_code": DEMO_ITEM, "item_name": DEMO_ITEM, "item_group": "All Item Groups",
                    "stock_uom": "Nos", "is_stock_item": 1, "disabled": 0, "has_batch_no": 0, "has_serial_no": 0}
    item = client.ensure("Item", [["item_code", "=", DEMO_ITEM]], item_payload)

    invoice_key = seed_key("Purchase Invoice", company_name, invoice_reference)
    # A legacy/unrelated PI with this bill reference must not silently become a second payable.
    for row in client.find("Purchase Invoice", [["company", "=", company_name],
                           ["supplier", "=", supplier["name"]], ["bill_no", "=", invoice_reference]]):
        existing = client.require("Purchase Invoice", row["name"])
        if existing.get(SEED_FIELD) != invoice_key:
            raise BootstrapError("Invoice reference is already used by an unrelated payable")

    base = {"company": company_name, "supplier": supplier["name"], "currency": "USD", "conversion_rate": 1,
            "taxes": [], "taxes_and_charges": "", "discount_amount": 0, "additional_discount_percentage": 0}
    line = {"item_code": item["name"], "qty": format(qty, "f"), "rate": format(price, "f"),
            "amount": format(amount, "f"), "warehouse": warehouse, "uom": "Nos", "stock_uom": "Nos",
            "conversion_factor": 1}
    po = _transaction(client, "Purchase Order", {**base, SEED_FIELD: seed_key("Purchase Order", company_name, invoice_reference),
                      "transaction_date": posting_date, "schedule_date": posting_date,
                      "items": [{**line, "schedule_date": posting_date}]}, amount)
    pr = _transaction(client, "Purchase Receipt", {**base, SEED_FIELD: seed_key("Purchase Receipt", company_name, invoice_reference),
                      "posting_date": posting_date, "set_posting_time": 1, "is_return": 0,
                      "items": [{**line, "purchase_order": po["name"], "purchase_order_item": po["items"][0]["name"]}]}, amount)
    pi = _transaction(client, "Purchase Invoice", {**base, SEED_FIELD: invoice_key,
                      "posting_date": posting_date, "set_posting_time": 1, "bill_date": posting_date,
                      "due_date": posting_date, "bill_no": invoice_reference, "is_return": 0, "is_paid": 0,
                      "update_stock": 0, "credit_to": payable_name, "disable_rounded_total": 1,
                      # The invoice's expense/stock leg is derived by ERPNext itself. With perpetual
                      # inventory it books the interim "Stock Received But Not Billed" account instead
                      # of the company expense account, so this is deliberately not asserted here.
                      "items": [{**line, "purchase_order": po["name"], "po_detail": po["items"][0]["name"],
                                 "purchase_receipt": pr["name"], "pr_detail": pr["items"][0]["name"],
                                 "cost_center": cost_center}]}, amount)
    return {"company": company_name, "company_currency": "USD", "settlement_account": settlement_account,
            "fee_account": fee_account, "payable_account": payable_name, "supplier": supplier["name"],
            "item": item["name"], "warehouse": warehouse, "purchase_order": po["name"],
            "purchase_receipt": pr["name"], "purchase_invoice": pi["name"],
            "env": {"FRAPPE_COMPANY": company_name, "FRAPPE_PAID_FROM_ACCOUNT": settlement_account,
                    "FRAPPE_PAID_TO_ACCOUNT": payable_name, "FRAPPE_FEE_ACCOUNT": fee_account,
                    "FRAPPE_COST_CENTER": cost_center,
                    "FRAPPE_COMPANY_CURRENCY": "USD", "FRAPPE_INVOICE_CURRENCY": "USD",
                    "FRAPPE_FEE_CURRENCY": "USD", "FRAPPE_SETTLEMENT_CURRENCY": "USDC",
                    "FRAPPE_SOURCE_EXCHANGE_RATE": "1", "FRAPPE_TARGET_EXCHANGE_RATE": "1",
                    "FRAPPE_MODE_OF_PAYMENT": mode_doc["name"],
                    "FRAPPE_SUPPLIER_WALLET_FIELD": SUPPLIER_WALLET_FIELD,
                    "FRAPPE_SUPPLIER_WALLET_VERIFIED_FIELD": SUPPLIER_WALLET_VERIFIED_FIELD}}


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or provision a disposable USD ERPNext demo. Never pays suppliers.")
    parser.add_argument("--url", help="Defaults to FRAPPE_URL; HTTP allowed only on localhost")
    parser.add_argument("--company", required=True, help="Exact existing USD Company name")
    parser.add_argument("--wallet", required=True, help="Demo recipient address; not automatically verified")
    parser.add_argument("--invoice-reference", default="ARC-PAYABLES-DEMO-001", help="Stable retry identity; reuse after errors")
    parser.add_argument("--quantity", default="10")
    parser.add_argument("--rate", default="25.00")
    parser.add_argument("--posting-date", default=None, help="YYYY-MM-DD; defaults to today on the ERPNext site; keep unchanged on retries")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read-only preview (the default)")
    mode.add_argument("--apply", action="store_true", help="Create and submit demo PO/PR/PI; disposable sandbox only")
    args = parser.parse_args()
    # Read only provisioning credentials, never Settings/the payment service's environment file.
    url = args.url or os.environ.get("FRAPPE_URL")
    api_key = os.environ.get("FRAPPE_API_KEY")
    api_secret = os.environ.get("FRAPPE_API_SECRET")
    if not url or not api_key or not api_secret:
        print("Set FRAPPE_URL, FRAPPE_API_KEY and FRAPPE_API_SECRET locally; never send them in chat.", file=sys.stderr)
        raise SystemExit(2)
    client = None
    try:
        client = FrappeClient(url, api_key, api_secret, dry_run=not args.apply)
        result = bootstrap(client, company=args.company, supplier_wallet=args.wallet,
                           invoice_reference=args.invoice_reference, quantity=args.quantity,
                           rate=args.rate, posting_date=args.posting_date)

    except BootstrapError as exc:
        print(f"Bootstrap stopped: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if client is not None:
            client.close()
    print("Sandbox provisioning complete." if args.apply else "Preview only: no remote writes or server-side validation performed.")
    for key, value in result.items():
        if key != "env":
            print(f"  {key}: {value}")
    for operation in client.created + client.plan:
        print(f"  {operation}")
    print("\nCandidate demo mappings ONLY; review before enabling writeback:")
    for key, value in result["env"].items():
        print(f"{key}={shlex.quote(value)}")
    print("\nUSD/USDC 1:1 is a demo assumption, not production FX/accounting guidance.")
    print("New supplier wallets remain UNVERIFIED. No payment was made. Fees must be expensed separately.")


if __name__ == "__main__":
    main()
