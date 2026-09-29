"""Local contract/failure tests; NOT a replacement for a live ERPNext integration test.

The fake models REST collection vs document routes, naming, server-calculated totals,
unique Custom Fields, full-document frappe.client.submit, and lost committed responses.
See ERPNext/Frappe v15 schemas and frappe/client.py for the independent contract sources.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
from urllib.parse import unquote

import httpx
import pytest

from arc_payables.erpnext_bootstrap import (
    BootstrapError, FEE_ACCOUNT_NAME, FrappeClient, SEED_FIELD, SETTLEMENT_ACCOUNT_NAME,
    SUPPLIER_WALLET_FIELD, SUPPLIER_WALLET_VERIFIED_FIELD, TRANSACTION_TYPES, bootstrap, main,
)

COMPANY = "Arc Demo Inc"
ABBR = "ADI"
WALLET = "0x1111111111111111111111111111111111111111"
REFERENCE = "ARC-PAYABLES-DEMO-001"
# Deliberately in the past: a fixed date must not be "tomorrow" for the fake site time zone.
POSTING_DATE = "2026-01-15"


class FakeFrappe:
    def __init__(self, *, company_currency="USD", with_company=True, time_zone="America/Los_Angeles"):
        self.docs: dict[str, dict[str, dict]] = {}
        self.submitted: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.lose_response: tuple[str, str] | None = None
        self.corrupt_insert = None
        self.time_zone = time_zone
        self.error_payload: dict | None = None
        if with_company:
            self.add("Company", COMPANY, default_currency=company_currency, abbr=ABBR,
                     default_payable_account=f"Creditors - {ABBR}",
                     default_expense_account=f"Cost of Goods Sold - {ABBR}", cost_center=f"Main - {ABBR}")
            for name, root in (("Assets", "Asset"), ("Expenses", "Expense")):
                self.add("Account", f"{name} - {ABBR}", company=COMPANY, root_type=root,
                         is_group=1, parent_account="", disabled=0)
            self.add("Account", f"Creditors - {ABBR}", company=COMPANY, root_type="Liability",
                     is_group=0, account_type="Payable", account_currency=company_currency, disabled=0)
            self.add("Account", f"Cost of Goods Sold - {ABBR}", company=COMPANY, root_type="Expense",
                     is_group=0, account_currency=company_currency, disabled=0)
            self.add("Cost Center", f"Main - {ABBR}", company=COMPANY, is_group=0)
            self.add("Warehouse", f"Stores - {ABBR}", company=COMPANY, is_group=0, disabled=0)
            self.add("Supplier Group", "All Supplier Groups", is_group=1)
            self.add("Item Group", "All Item Groups", is_group=1)
            self.add("UOM", "Nos", enabled=1)

    def add(self, doctype, name, **fields):
        doc = {"doctype": doctype, "name": name, "docstatus": 0, **fields}
        self.docs.setdefault(doctype, {})[name] = doc
        return doc

    def _maybe_lose(self, operation, doctype, request):
        if self.lose_response == (operation, doctype):
            self.lose_response = None
            raise httpx.ReadTimeout("lost response after commit", request=request)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.raw_path.decode().split("?", 1)[0]
        if self.error_payload is not None and path.startswith("/api/resource/"):
            return httpx.Response(417, json=self.error_payload)
        if path == "/api/method/frappe.client.get_time_zone":
            return httpx.Response(200, json={"message": {"time_zone": self.time_zone}})
        if path == "/api/method/frappe.client.submit":
            body = json.loads(request.content)["doc"]
            doctype, name = body["doctype"], body["name"]
            # Real frappe.client.submit uses get_doc(dict), not a reload by name.
            if not body.get("items") or not body.get("company") or not body.get("modified"):
                return httpx.Response(400, json={"error": "a full document is required"})
            doc = self.docs[doctype][name]
            if doc["docstatus"] != 0 or body["modified"] != doc["modified"]:
                return httpx.Response(409, json={"error": "not a current draft"})
            doc["docstatus"] = 1
            if doctype == "Purchase Invoice":
                doc["outstanding_amount"] = doc["grand_total"]
            self.submitted.append((doctype, name))
            self._maybe_lose("submit", doctype, request)
            return httpx.Response(200, json={"message": deepcopy(doc)})
        if not path.startswith("/api/resource/"):
            return httpx.Response(404)
        parts = [unquote(part) for part in path[len("/api/resource/"):].split("/")]
        doctype = parts[0]
        if request.method == "GET" and len(parts) == 2:
            doc = self.docs.get(doctype, {}).get(parts[1])
            return httpx.Response(200, json={"data": deepcopy(doc)}) if doc else httpx.Response(404)
        if request.method == "GET" and len(parts) == 1:
            filters = json.loads(request.url.params.get("filters", "[]"))
            fields = json.loads(request.url.params.get("fields", '["name"]'))
            if any(row[0] == SEED_FIELD for row in filters) and not any(
                field.get("dt") == doctype and field.get("fieldname") == SEED_FIELD
                for field in self.docs.get("Custom Field", {}).values()
            ):
                return httpx.Response(400, json={"error": "unknown seed column"})
            rows = []
            for doc in self.docs.get(doctype, {}).values():
                assert all(row[1] == "=" for row in filters)
                if all(str(doc.get(field)) == str(value) for field, _, value in filters):
                    rows.append({field: doc.get(field) for field in fields})
            return httpx.Response(200, json={"data": rows[:int(request.url.params.get("limit_page_length", "20"))]})
        if request.method == "POST" and len(parts) == 1:
            payload = json.loads(request.content)
            if doctype in TRANSACTION_TYPES:
                unique = any(field.get("dt") == doctype and field.get("fieldname") == SEED_FIELD and field.get("unique") == 1
                             for field in self.docs.get("Custom Field", {}).values())
                if unique and any(doc.get(SEED_FIELD) == payload.get(SEED_FIELD)
                                  for doc in self.docs.get(doctype, {}).values()):
                    return httpx.Response(409, json={"error": "duplicate seed key"})
                # Real Frappe assigns transaction/child names; bill_no is NOT the document name.
                name = f"{doctype.replace(' ', '-')}-{len(self.docs.get(doctype, {})) + 1:04d}"
            elif doctype == "Account":
                abbr = self.docs["Company"][payload["company"]]["abbr"]
                name = f"{payload['account_name']} - {abbr}"
            elif doctype == "Custom Field":
                name = f"{payload['dt']}-{payload['fieldname']}"
            else:
                field = {"Currency": "currency_name", "Mode of Payment": "mode_of_payment",
                         "Supplier": "supplier_name", "Item": "item_code"}[doctype]
                name = payload[field]
            if name in self.docs.get(doctype, {}):
                return httpx.Response(409)
            doc = self.add(doctype, name, **payload)
            if doctype in TRANSACTION_TYPES:
                doc["items"] = [{**line, "name": f"{name}-row-{index + 1}"} for index, line in enumerate(payload["items"])]
                total = sum(Decimal(str(line["qty"])) * Decimal(str(line["rate"])) for line in doc["items"])
                doc.update(grand_total=str(total), base_grand_total=str(total), net_total=str(total),
                           outstanding_amount="0", modified="2026-09-29 12:00:00.000001")
            if self.corrupt_insert:
                self.corrupt_insert(doctype, doc)
            self._maybe_lose("insert", doctype, request)
            return httpx.Response(200, json={"data": deepcopy(doc)})
        return httpx.Response(405)


def client_for(fake, *, dry_run=False):
    return FrappeClient("https://erp.example.test", "fake-key", "fake-secret", dry_run=dry_run,
                        client=httpx.Client(transport=httpx.MockTransport(fake.handler)))


def run(fake, *, dry_run=False, **overrides):
    args = dict(company=COMPANY, supplier_wallet=WALLET, invoice_reference=REFERENCE,
                quantity="10", rate="25.00", posting_date=POSTING_DATE)
    args.update(overrides)
    return bootstrap(client_for(fake, dry_run=dry_run), **args)


def writes(fake):
    return [request for request in fake.requests if request.method != "GET"]


def test_creates_explicit_usd_mapping_and_real_child_links_but_does_not_verify_a_wallet():
    fake = FakeFrappe()
    result = run(fake)
    settlement = fake.docs["Account"][result["settlement_account"]]
    assert (settlement["account_currency"], settlement["account_type"]) == ("USDC", "Bank")
    fee = fake.docs["Account"][result["fee_account"]]
    assert (fee["account_currency"], fee["root_type"]) == ("USD", "Expense")
    supplier = fake.docs["Supplier"][result["supplier"]]
    assert supplier[SUPPLIER_WALLET_FIELD] == WALLET
    assert supplier[SUPPLIER_WALLET_VERIFIED_FIELD] == 0
    invoice = fake.docs["Purchase Invoice"][result["purchase_invoice"]]
    assert invoice["docstatus"] == 1 and Decimal(invoice["grand_total"]) == 250
    assert invoice["currency"] == "USD" and invoice["due_date"] == POSTING_DATE
    line = invoice["items"][0]
    assert line["purchase_order"] == result["purchase_order"]
    assert line["purchase_receipt"] == result["purchase_receipt"]
    assert line["po_detail"] == fake.docs["Purchase Order"][result["purchase_order"]]["items"][0]["name"]
    assert line["pr_detail"] == fake.docs["Purchase Receipt"][result["purchase_receipt"]]["items"][0]["name"]
    assert len(fake.submitted) == 3
    assert "Payment Entry" not in fake.docs
    assert "ACCOUNTING_PROVIDER" not in result["env"]  # Never activate writeback automatically.
    assert result["env"]["FRAPPE_SOURCE_EXCHANGE_RATE"] == "1"
    assert result["env"]["FRAPPE_FEE_CURRENCY"] == "USD"
    assert fake.docs["Mode of Payment"][result["env"]["FRAPPE_MODE_OF_PAYMENT"]]["accounts"] == [
        {"company": COMPANY, "default_account": result["settlement_account"]}]
    assert fake.docs["Currency"]["USDC"]["fraction_units"] == 1_000_000


def test_repeat_run_has_no_writes_and_preserves_manual_wallet_verification():
    fake = FakeFrappe()
    first = run(fake)
    fake.docs["Supplier"][first["supplier"]][SUPPLIER_WALLET_VERIFIED_FIELD] = 1
    snapshot, write_count = deepcopy(fake.docs), len(writes(fake))
    second = run(fake)
    assert first == second and fake.docs == snapshot and len(writes(fake)) == write_count
    assert len(fake.submitted) == 3


@pytest.mark.parametrize("currency", ["CAD", "EUR", None, ""])
@pytest.mark.parametrize("dry_run", [False, True])
def test_non_usd_or_missing_currency_stops_before_any_write(currency, dry_run):
    fake = FakeFrappe(company_currency=currency)
    with pytest.raises(BootstrapError, match="USD company"):
        run(fake, dry_run=dry_run)
    assert writes(fake) == []


def test_multi_company_requires_selection_and_never_touches_cad_company():
    fake = FakeFrappe()
    cad = fake.add("Company", "My CAD Company", default_currency="CAD", abbr="CAD")
    with pytest.raises(BootstrapError, match="Multiple companies"):
        run(fake, company=None)
    assert writes(fake) == []
    snapshot = deepcopy(cad)
    result = run(fake)
    assert result["company"] == COMPANY
    assert cad == snapshot
    assert all(doc.get("company", COMPANY) == COMPANY for dt in TRANSACTION_TYPES for doc in fake.docs[dt].values())


@pytest.mark.parametrize("requested", [None, COMPANY])
def test_missing_company_is_never_invented_even_in_preview(requested):
    fake = FakeFrappe(with_company=False)
    with pytest.raises(BootstrapError):
        run(fake, company=requested, dry_run=True)
    assert writes(fake) == []


@pytest.mark.parametrize("field", ["default_payable_account", "cost_center", "abbr"])
def test_missing_company_mapping_does_not_guess(field):
    fake = FakeFrappe()
    del fake.docs["Company"][COMPANY][field]
    with pytest.raises(BootstrapError):
        run(fake)
    assert writes(fake) == []


@pytest.mark.parametrize("field,value", [("company", "CAD Company"), ("account_currency", "CAD"),
                                        ("disabled", 1), ("is_group", 1), ("freeze_account", "Yes")])
def test_invalid_payable_account_stops_before_writes(field, value):
    fake = FakeFrappe()
    fake.docs["Account"][f"Creditors - {ABBR}"][field] = value
    with pytest.raises(BootstrapError):
        run(fake)
    assert writes(fake) == []


def test_existing_wallet_mismatch_does_not_patch_the_trusted_supplier():
    fake = FakeFrappe()
    result = run(fake)
    supplier = fake.docs["Supplier"][result["supplier"]]
    supplier[SUPPLIER_WALLET_FIELD] = "0x2222222222222222222222222222222222222222"
    supplier[SUPPLIER_WALLET_VERIFIED_FIELD] = 1
    snapshot, count = deepcopy(fake.docs), len(writes(fake))
    with pytest.raises(BootstrapError, match="wallet differs"):
        run(fake)
    assert snapshot == fake.docs and len(writes(fake)) == count


@pytest.mark.parametrize("operation", ["insert", "submit"])
@pytest.mark.parametrize("doctype", TRANSACTION_TYPES)
def test_lost_committed_response_reconciles_without_duplicate_documents_or_submissions(operation, doctype):
    fake = FakeFrappe()
    fake.lose_response = (operation, doctype)
    with pytest.raises(BootstrapError, match="reconcile"):
        run(fake)
    # No implicit retry is allowed within the failing run.
    assert len(fake.docs.get(doctype, {})) == 1
    result = run(fake)
    assert result["purchase_invoice"] in fake.docs["Purchase Invoice"]
    assert all(len(fake.docs[dt]) == 1 for dt in TRANSACTION_TYPES)
    assert len(fake.submitted) == len(set(fake.submitted)) == 3


def test_seed_fields_are_unique_and_unrelated_orders_are_not_reused():
    fake = FakeFrappe()
    unrelated = fake.add("Purchase Order", "REAL-ORDER", supplier=f"Arc Payables Demo Supplier - {ABBR}",
                         company=COMPANY, docstatus=0, items=[{"item_code": "DIFFERENT"}])
    snapshot = deepcopy(unrelated)
    result = run(fake)
    assert result["purchase_order"] != "REAL-ORDER" and unrelated == snapshot
    for dt in TRANSACTION_TYPES:
        field = fake.docs["Custom Field"][f"{dt}-{SEED_FIELD}"]
        assert field["unique"] == field["no_copy"] == field["read_only"] == 1


def test_legacy_duplicate_invoice_reference_is_rejected_not_adopted():
    fake = FakeFrappe()
    fake.add("Purchase Invoice", "LEGACY", company=COMPANY, supplier=f"Arc Payables Demo Supplier - {ABBR}",
             bill_no=REFERENCE, docstatus=1)
    with pytest.raises(BootstrapError, match="unrelated payable"):
        run(fake)
    assert fake.submitted == []
    assert len(fake.docs["Purchase Invoice"]) == 1


@pytest.mark.parametrize("doctype", TRANSACTION_TYPES)
@pytest.mark.parametrize("mutation", ["cancelled", "quantity", "currency", "total", "tax", "seed_duplicate"])
def test_conflicting_existing_transaction_stops_without_writes(doctype, mutation):
    fake = FakeFrappe()
    run(fake)
    doc = next(iter(fake.docs[doctype].values()))
    if mutation == "cancelled":
        doc["docstatus"] = 2
    elif mutation == "quantity":
        doc["items"][0]["qty"] = "11"
    elif mutation == "currency":
        doc["currency"] = "CAD"
    elif mutation == "total":
        doc["grand_total"] = "251"
    elif mutation == "tax":
        doc["taxes"] = [{"rate": "5"}]
    elif mutation == "seed_duplicate":
        fake.docs[doctype]["DUPLICATE"] = {**deepcopy(doc), "name": "DUPLICATE"}
    snapshot, count = deepcopy(fake.docs), len(writes(fake))
    with pytest.raises(BootstrapError):
        run(fake)
    assert fake.docs == snapshot and len(writes(fake)) == count


def test_server_added_tax_is_detected_before_submission_and_draft_is_left_for_review():
    fake = FakeFrappe()
    def add_tax(doctype, doc):
        if doctype == "Purchase Order":
            doc["grand_total"] = "262.50"
    fake.corrupt_insert = add_tax
    with pytest.raises(BootstrapError, match="grand_total"):
        run(fake)
    assert fake.submitted == []
    assert next(iter(fake.docs["Purchase Order"].values()))["docstatus"] == 0


def test_already_paid_invoice_does_not_get_recreated_as_a_payable():
    fake = FakeFrappe()
    result = run(fake)
    fake.docs["Purchase Invoice"][result["purchase_invoice"]]["outstanding_amount"] = "0"
    count = len(writes(fake))
    with pytest.raises(BootstrapError, match="outstanding_amount"):
        run(fake)
    assert len(writes(fake)) == count


def test_submission_must_be_confirmed_by_readback_before_next_document():
    class InconsistentReadback(FakeFrappe):
        def handler(self, request):
            response = super().handler(request)
            if request.url.path == "/api/method/frappe.client.submit":
                # The POST response says submitted, but a subsequent authoritative GET does not.
                body = json.loads(request.content)["doc"]
                self.docs[body["doctype"]][body["name"]]["docstatus"] = 0
            return response
    fake = InconsistentReadback()
    with pytest.raises(BootstrapError, match="readback disagrees"):
        run(fake)
    assert "Purchase Receipt" not in fake.docs and "Purchase Invoice" not in fake.docs


def test_a_second_reference_creates_its_own_chain_without_reusing_the_first():
    fake = FakeFrappe()
    first = run(fake)
    second = run(fake, invoice_reference="ARC-PAYABLES-DEMO-002", quantity="2", rate="17.43")
    for key in ("purchase_order", "purchase_receipt", "purchase_invoice"):
        assert first[key] != second[key]
    invoice = fake.docs["Purchase Invoice"][second["purchase_invoice"]]
    assert Decimal(invoice["grand_total"]) == Decimal("34.86")
    assert invoice["items"][0]["purchase_order"] == second["purchase_order"]
    assert invoice["items"][0]["purchase_receipt"] == second["purchase_receipt"]
    assert all(len(fake.docs[dt]) == 2 for dt in TRANSACTION_TYPES)


def test_server_rejects_an_insert_race_on_the_unique_seed_key():
    fake = FakeFrappe()
    first = run(fake)
    doc = fake.docs["Purchase Order"][first["purchase_order"]]
    count = len(fake.docs["Purchase Order"])
    with pytest.raises(BootstrapError, match="HTTP 409"):
        client_for(fake).insert("Purchase Order", doc)
    assert len(fake.docs["Purchase Order"]) == count


@pytest.mark.parametrize("uom_fields", [{"enabled": 1}, {"enabled": 1, "must_be_whole_number": 1}])
def test_uom_only_needs_to_exist_and_be_enabled(uom_fields):
    fake = FakeFrappe()
    fake.docs["UOM"]["Nos"] = {"doctype": "UOM", "name": "Nos", "docstatus": 0, **uom_fields}
    assert run(fake)["purchase_invoice"]


def test_disabled_uom_is_rejected_before_writes():
    fake = FakeFrappe()
    fake.docs["UOM"]["Nos"]["enabled"] = 0
    with pytest.raises(BootstrapError, match="UOM"):
        run(fake)
    assert writes(fake) == []


def test_dry_run_is_read_only_and_does_not_query_synthetic_ids_or_missing_columns():
    fake = FakeFrappe()
    snapshot = deepcopy(fake.docs)
    client = client_for(fake, dry_run=True)
    result = bootstrap(client, company=COMPANY, supplier_wallet=WALLET, invoice_reference=REFERENCE,
                       quantity="10", rate="25.00", posting_date=POSTING_DATE)
    assert fake.docs == snapshot and writes(fake) == []
    assert client.created == [] and client.plan
    assert sum(op.startswith("SUBMIT") for op in client.plan) == 3
    assert result["purchase_invoice"].startswith("PLANNED-")
    assert all("PLANNED-" not in request.url.path for request in fake.requests)


def test_preview_existing_draft_plans_submission_but_does_not_submit():
    fake = FakeFrappe()
    fake.lose_response = ("insert", "Purchase Invoice")
    with pytest.raises(BootstrapError):
        run(fake)
    snapshot = deepcopy(fake.docs)
    count = len(writes(fake))
    run(fake, dry_run=True)
    assert snapshot == fake.docs and len(writes(fake)) == count


@pytest.mark.parametrize("overrides", [
    {"quantity": "0"}, {"quantity": "1.5"}, {"quantity": "NaN"}, {"quantity": "Infinity"},
    {"rate": "0.001"}, {"rate": "-1"}, {"rate": "NaN"}, {"rate": "Infinity"},
    {"supplier_wallet": "0x" + "0" * 40}, {"supplier_wallet": "not-an-address"},
    {"invoice_reference": "../bad"}, {"posting_date": "2026-02-30"},
])
def test_bad_inputs_fail_before_any_http_request(overrides):
    fake = FakeFrappe()
    with pytest.raises(BootstrapError):
        run(fake, **overrides)
    assert fake.requests == []


def test_one_cent_invoice_is_exact_not_rounded_or_float_truncated():
    fake = FakeFrappe()
    result = run(fake, quantity="1", rate="0.01")
    invoice = fake.docs["Purchase Invoice"][result["purchase_invoice"]]
    assert Decimal(invoice["grand_total"]) == Decimal("0.01")


@pytest.mark.parametrize("url", ["http://erp.example.test", "https://key:secret@erp.example.test",
                                "https://erp.example.test?token=secret", "file:///etc/passwd"])
def test_insecure_or_credential_bearing_urls_are_rejected(url):
    with pytest.raises(BootstrapError):
        FrappeClient(url, "key", "secret")


@pytest.mark.parametrize("status,body", [(403, {"exc": "sensitive-response-secret"}),
                                       (500, {"headers": "fake-key:fake-secret"}), (302, {})])
def test_errors_are_sanitized_and_redirects_are_not_followed(status, body):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=body, headers={"Location": "https://untrusted.test"})
    client = FrappeClient("https://erp.example.test", "fake-key", "fake-secret",
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(BootstrapError) as exc:
        client.require("Company", COMPANY)
    assert "fake-secret" not in str(exc.value) and "sensitive-response" not in str(exc.value)
    assert len(requests) == 1


@pytest.mark.parametrize("status,hint", [(401, "FRAPPE_API_KEY"), (403, "FRAPPE_API_KEY"), (500, "no automatic retry")])
def test_error_message_names_the_request_but_never_the_credentials(status, hint):
    def handler(request):
        return httpx.Response(status, json={"exc": "secret-response-body"})
    client = FrappeClient("https://erp.example.test", "fake-key", "fake-secret",
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(BootstrapError) as exc:
        client.require("Company", COMPANY)
    message = str(exc.value)
    assert "GET /api/resource/Company/Arc%20Demo%20Inc" in message and hint in message
    assert "fake-secret" not in message and "secret-response-body" not in message


def test_server_validation_message_is_surfaced_without_the_raw_body_or_traceback():
    fake = FakeFrappe()
    fake.error_payload = {
        "exc_type": "ValidationError",
        "exception": "frappe.exceptions.ValidationError: Posting Date is in the future",
        "exc": "Traceback (most recent call last):\n  File \"/home/frappe/private-secret-path.py\", line 1\nfake-key:fake-secret",
        "_server_messages": json.dumps([json.dumps({"message": "Row 1: <strong>Warehouse is mandatory</strong>"})]),
    }
    with pytest.raises(BootstrapError) as exc:
        run(fake)
    message = str(exc.value)
    assert "HTTP 417" in message
    assert "Warehouse is mandatory" in message and "future" in message
    # The traceback and the leaked credential string inside it are never echoed.
    assert "private-secret-path" not in message and "fake-secret" not in message


def test_server_derived_invoice_expense_account_is_accepted():
    """Perpetual inventory makes ERPNext choose the interim stock account, not the company expense account."""
    fake = FakeFrappe()
    def derive(doctype, doc):
        if doctype == "Purchase Invoice":
            doc["items"][0]["expense_account"] = f"Stock Received But Not Billed - {ABBR}"
    fake.corrupt_insert = derive
    result = run(fake)
    assert fake.docs["Purchase Invoice"][result["purchase_invoice"]]["items"][0]["expense_account"] == f"Stock Received But Not Billed - {ABBR}"
    assert fake.docs["Purchase Invoice"][result["purchase_invoice"]]["docstatus"] == 1


def test_missing_default_expense_account_is_tolerated_because_erpnext_derives_it():
    fake = FakeFrappe()
    del fake.docs["Company"][COMPANY]["default_expense_account"]
    assert run(fake)["purchase_invoice"]


@pytest.mark.parametrize("posting_date,allowed", [("2999-01-01", False), ("2000-01-01", True)])
def test_posting_date_is_checked_against_the_site_time_zone(posting_date, allowed):
    fake = FakeFrappe(time_zone="America/Los_Angeles")
    if allowed:
        assert run(fake, posting_date=posting_date)["purchase_invoice"]
    else:
        with pytest.raises(BootstrapError, match="future for the ERPNext site time zone"):
            run(fake, posting_date=posting_date)
        assert writes(fake) == []


def test_default_posting_date_uses_the_site_local_date_not_the_host_date():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    fake = FakeFrappe(time_zone="America/Los_Angeles")
    result = run(fake, posting_date=None)
    expected = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    assert fake.docs["Purchase Invoice"][result["purchase_invoice"]]["posting_date"] == expected


def test_unknown_site_time_zone_is_rejected():
    fake = FakeFrappe(time_zone="Not/AZone")
    with pytest.raises(BootstrapError, match="time zone"):
        run(fake)
    assert writes(fake) == []


def test_document_names_are_encoded_as_path_segments():
    fake = FakeFrappe()
    fake.add("Company", "Demo / #1?", default_currency="USD", abbr="D")
    client = client_for(fake)
    doc = client.require("Company", "Demo / #1?")
    assert doc["name"] == "Demo / #1?"
    assert b"%2F" in fake.requests[-1].url.raw_path and b"%23" in fake.requests[-1].url.raw_path


@pytest.mark.parametrize("apply", [False, True])
def test_cli_requires_explicit_apply_and_never_prints_credentials(monkeypatch, capsys, apply):
    import arc_payables.erpnext_bootstrap as module
    fake = FakeFrappe()
    factory = FrappeClient
    monkeypatch.setattr(module, "FrappeClient", lambda url, key, secret, **kw: factory(
        url, key, secret, **kw, client=httpx.Client(transport=httpx.MockTransport(fake.handler))))
    monkeypatch.setenv("FRAPPE_URL", "https://erp.example.test")
    monkeypatch.setenv("FRAPPE_API_KEY", "test-private-key")
    monkeypatch.setenv("FRAPPE_API_SECRET", "test-private-secret")
    monkeypatch.setenv("PERMIT_SIGNING_PRIVATE_KEY", "invalid-and-must-not-be-read")
    argv = ["bootstrap", "--company", COMPANY, "--wallet", WALLET, "--posting-date", POSTING_DATE]
    monkeypatch.setattr("sys.argv", argv + (["--apply"] if apply else []))
    main()
    output = capsys.readouterr().out
    assert "UNVERIFIED" in output
    assert "test-private" not in output and "invalid-and-must" not in output
    if apply:
        assert "Sandbox provisioning complete" in output
        assert len(fake.submitted) == 3
    else:
        assert "Preview only" in output and "CREATE Purchase Invoice" in output
        assert writes(fake) == []
