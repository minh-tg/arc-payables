"""A stand-in for the ERPNext REST calls Arc Payables makes, for demonstrations and tests.

This is not ERPNext. It keeps a few documents in memory, answers the same requests the real
connector sends (read a document, list with filters, create, submit) and applies the one rule a
demonstration needs from an accounting system: a submitted Payment Entry that allocates against a
Purchase Invoice reduces that invoice's outstanding amount, and an invoice with nothing outstanding
reads Paid. It has no users, no permissions, no chart of accounts and no ledger. Its page says so.

    uvicorn arc_payables.sim_erpnext:app --host 127.0.0.1 --port 8081

Bind it to loopback only. Anything written here disappears when the process stops.
"""

from __future__ import annotations

import copy
import html
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

COMPANY = "Arc Demo Inc"
PAID_FROM = "USDC Wallet - ADI"
PAID_TO = "Creditors - ADI"
FEE_ACCOUNT = "Network Fees - ADI"
COST_CENTER = "Main - ADI"
MODE_OF_PAYMENT = "Arc Testnet"
API_USER = "ap-agent@sim-erpnext.test"

ACME_WALLET = "0x1111111111111111111111111111111111111111"
NORTHWIND_WALLET = "0x2222222222222222222222222222222222222222"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _purchase_chain(supplier: str, number: int, item: str, quantity: int, amount: float) -> list[tuple[str, dict]]:
    order, receipt = f"PUR-ORD-2026-{number:05d}", f"MAT-PRE-2026-{number:05d}"
    return [
        ("Purchase Order", {
            "name": order, "supplier": supplier, "status": "To Receive and Bill", "docstatus": 1,
            "items": [{"name": f"{order}-1", "item_code": item, "qty": quantity, "amount": amount}],
        }),
        ("Purchase Receipt", {
            "name": receipt, "supplier": supplier, "status": "To Bill", "docstatus": 1,
            "items": [{"name": f"{receipt}-1", "item_code": item, "qty": quantity, "purchase_order_item": f"{order}-1"}],
        }),
    ]


def seed_documents() -> dict[str, dict[str, dict[str, Any]]]:
    """Two suppliers and two unpaid bills. Northwind's payout wallet has not been verified yet."""
    docs: dict[str, dict[str, dict[str, Any]]] = {}

    def add(doctype: str, doc: dict[str, Any]) -> None:
        docs.setdefault(doctype, {})[doc["name"]] = {**doc, "modified": doc.get("modified", _now())}

    add("Supplier", {
        "name": "SUP-ACME-001", "supplier_name": "Acme Industrial Supply (Demo)",
        "custom_usdc_wallet_address": ACME_WALLET, "custom_usdc_wallet_verified": 1,
        "disabled": 0, "on_hold": 0,
    })
    add("Supplier", {
        "name": "SUP-NORTHWIND-002", "supplier_name": "Northwind Parts (Demo)",
        "custom_usdc_wallet_address": NORTHWIND_WALLET, "custom_usdc_wallet_verified": 0,
        "disabled": 0, "on_hold": 0,
    })
    # Dates follow the day the stand-in starts: billed ten days ago, due in two, which is inside
    # the policy's payment window. A fixed date would turn into "not due soon" or "overdue".
    today = date.today()
    bill_date, due = (today - timedelta(days=10)).isoformat(), (today + timedelta(days=2)).isoformat()
    for supplier, number, item, quantity, amount, pinv, bill in (
        ("SUP-ACME-001", 101, "FILTER-CARTRIDGE", 10, 250.0, "ACC-PINV-2026-00101", "ACME-INV-2026-001"),
        ("SUP-NORTHWIND-002", 102, "BRAKE-PAD-SET", 8, 480.0, "ACC-PINV-2026-00102", "NW-INV-0077"),
    ):
        for doctype, doc in _purchase_chain(supplier, number, item, quantity, amount):
            add(doctype, doc)
        order, receipt = f"PUR-ORD-2026-{number:05d}", f"MAT-PRE-2026-{number:05d}"
        add("Purchase Invoice", {
            "name": pinv, "supplier": supplier, "bill_no": bill, "bill_date": bill_date,
            "posting_date": bill_date, "due_date": due, "currency": "USD", "grand_total": amount,
            "outstanding_amount": amount, "status": "Unpaid", "docstatus": 1,
            "items": [{
                "item_code": item, "qty": quantity, "amount": amount,
                "po_detail": f"{order}-1", "purchase_order": order,
                "pr_detail": f"{receipt}-1", "purchase_receipt": receipt,
            }],
        })
    for account, currency in ((PAID_FROM, "USDC"), (PAID_TO, "USD"), (FEE_ACCOUNT, "USD")):
        add("Account", {"name": account, "company": COMPANY, "account_currency": currency, "is_group": 0})
    add("Cost Center", {"name": COST_CENTER, "company": COMPANY, "is_group": 0})
    add("Currency", {"name": "USD", "smallest_currency_fraction_value": 0.01})
    add("Currency", {"name": "USDC", "smallest_currency_fraction_value": 0.000001})
    return docs


def _matches(doc: dict[str, Any], filters: list) -> bool:
    for field, operator, value in filters:
        actual = doc.get(field)
        if operator == "=":
            ok = actual == value or str(actual) == str(value)
        elif operator == "!=":
            ok = actual != value
        elif operator == ">":
            ok = actual is not None and float(actual) > float(value)
        elif operator == "<":
            ok = actual is not None and float(actual) < float(value)
        elif operator == "in":
            ok = actual in value
        else:
            raise HTTPException(417, detail=f"filter operator {operator} is not supported by the stand-in")
        if not ok:
            return False
    return True


def _next_name(docs: dict[str, dict[str, dict[str, Any]]], doctype: str) -> str:
    prefix = {"Payment Entry": "ACC-PAY-2026-", "Journal Entry": "ACC-JV-2026-"}.get(doctype, "DOC-")
    return f"{prefix}{len(docs.get(doctype, {})) + 1:05d}"


def create_app() -> FastAPI:
    app = FastAPI(title="Simulated ERPNext", docs_url=None, redoc_url=None, openapi_url=None)
    docs = seed_documents()
    app.state.docs = docs

    @app.get("/api/method/frappe.auth.get_logged_user")
    def logged_user():
        return {"message": API_USER}

    @app.get("/api/resource/{doctype}")
    def list_documents(doctype: str, filters: str = "[]", fields: str = '["name"]', limit_page_length: int = 20):
        wanted = json.loads(fields)
        rows = [d for d in docs.get(doctype, {}).values() if _matches(d, json.loads(filters))]
        return {"data": [{field: row.get(field) for field in wanted} for row in rows[:limit_page_length]]}

    @app.get("/api/resource/{doctype}/{name}")
    def get_document(doctype: str, name: str):
        doc = docs.get(doctype, {}).get(name)
        if doc is None:
            raise HTTPException(404, detail=f"{doctype} {name} not found")
        return {"data": copy.deepcopy(doc)}

    @app.post("/api/resource/{doctype}")
    async def create_document(doctype: str, request: Request):
        body = await request.json()
        name = _next_name(docs, doctype)
        doc = {**body, "name": name, "docstatus": 0, "modified": _now()}
        docs.setdefault(doctype, {})[name] = doc
        return {"data": copy.deepcopy(doc)}

    @app.post("/api/method/frappe.client.submit")
    async def submit(request: Request):
        sent = (await request.json()).get("doc") or {}
        doctype, name = sent.get("doctype"), sent.get("name")
        stored = docs.get(str(doctype), {}).get(str(name))
        if stored is None:
            raise HTTPException(404, detail=f"{doctype} {name} not found")
        # The live v15 contract: a name-only payload submits nothing, so a whole document is required.
        required = ("accounts", "user_remark") if doctype == "Journal Entry" else ("paid_amount", "references")
        if not sent.get("modified") or any(not sent.get(field) for field in required):
            raise HTTPException(417, detail="A full document is required to submit")
        if int(stored.get("docstatus") or 0) == 1:
            return {"message": copy.deepcopy(stored)}
        stored["docstatus"] = 1
        stored["modified"] = _now()
        if doctype == "Payment Entry":
            for row in stored.get("references", []):
                invoice = docs.get(row.get("reference_doctype", ""), {}).get(row.get("reference_name", ""))
                if invoice is None:
                    continue
                invoice["outstanding_amount"] = max(0.0, round(float(invoice["outstanding_amount"]) - float(row["allocated_amount"]), 6))
                invoice["status"] = "Paid" if invoice["outstanding_amount"] <= 0 else "Partly Paid"
                invoice["modified"] = _now()
        return {"message": copy.deepcopy(stored)}

    @app.post("/sim/verify-wallet/{supplier}")
    def verify_wallet(supplier: str):
        """The one human step: a person in the accounting system confirms the payout wallet."""
        record = docs.get("Supplier", {}).get(supplier)
        if record is None:
            raise HTTPException(404, detail="supplier not found")
        record["custom_usdc_wallet_verified"] = 1
        record["modified"] = _now()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/sim/reset")
    def reset():
        docs.clear()
        docs.update(seed_documents())
        return RedirectResponse(url="/", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def page():
        return HTMLResponse(_render(docs))

    return app


_STYLE = """
body{margin:0;font:14px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif;background:#f4f5f6;color:#1f272e}
header{background:#fff;border-bottom:1px solid #d1d8dd;padding:12px 24px;display:flex;gap:16px;align-items:baseline}
header b{font-size:18px} header span{color:#6c7680}
.note{background:#fff8e1;border-bottom:1px solid #f0d78a;padding:8px 24px;font-size:13px}
main{max-width:1100px;margin:0 auto;padding:20px 24px}
h2{font-size:16px;margin:24px 0 8px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d1d8dd}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #ebeef0;vertical-align:top}
th{font-size:12px;color:#6c7680;font-weight:600;background:#fafbfc}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.red{background:#fde8e8;color:#9b1c1c}.green{background:#def7ec;color:#03543f}.grey{background:#edf0f2;color:#4b5563}
code{font:12px ui-monospace,monospace}
button{font:inherit;padding:3px 10px;cursor:pointer}
.empty{color:#6c7680;padding:8px 12px}
"""


def _pill(text: str, tone: str) -> str:
    return f'<span class="pill {tone}">{html.escape(text)}</span>'


def _money(value: Any) -> str:
    return f"{float(value):,.2f}"


def _render(docs: dict[str, dict[str, dict[str, Any]]]) -> str:
    esc = html.escape

    def table(headers: list[str], rows: list[str], empty: str) -> str:
        if not rows:
            return f'<div class="empty">{esc(empty)}</div>'
        head = "".join(f"<th>{esc(h)}</th>" for h in headers)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"

    suppliers = []
    for s in docs.get("Supplier", {}).values():
        verified = s.get("custom_usdc_wallet_verified") in (1, True, "1")
        action = (
            _pill("Wallet verified", "green")
            if verified
            else f'{_pill("Wallet not verified", "red")} '
                 f'<form method="post" action="/sim/verify-wallet/{esc(s["name"])}" style="display:inline">'
                 f'<button type="submit">Verify wallet</button></form>'
        )
        suppliers.append(
            f"<tr><td>{esc(s['name'])}</td><td>{esc(s['supplier_name'])}</td>"
            f"<td><code>{esc(str(s['custom_usdc_wallet_address']))}</code></td><td>{action}</td></tr>"
        )
    invoices = []
    for p in docs.get("Purchase Invoice", {}).values():
        tone = "green" if p["status"] == "Paid" else "red" if p["status"] == "Unpaid" else "grey"
        invoices.append(
            f"<tr><td>{esc(p['name'])}</td><td>{esc(p['supplier'])}</td><td>{esc(p['bill_no'])}</td>"
            f"<td>{esc(p['due_date'])}</td><td class='num'>{_money(p['grand_total'])}</td>"
            f"<td class='num'>{_money(p['outstanding_amount'])}</td><td>{_pill(p['status'], tone)}</td></tr>"
        )
    payments = []
    for e in docs.get("Payment Entry", {}).values():
        ref = (e.get("references") or [{}])[0]
        payments.append(
            f"<tr><td>{esc(e['name'])}</td><td>{esc(str(e.get('party', '')))}</td>"
            f"<td>{esc(str(ref.get('reference_name', '')))}</td><td class='num'>{_money(e.get('paid_amount', 0))}</td>"
            f"<td><code>{esc(str(e.get('reference_no', '')))}</code></td>"
            f"<td>{_pill('Submitted' if int(e.get('docstatus') or 0) == 1 else 'Draft', 'green' if int(e.get('docstatus') or 0) == 1 else 'grey')}</td></tr>"
        )
    journals = []
    for j in docs.get("Journal Entry", {}).values():
        debit = sum(float(row.get("debit_in_account_currency") or row.get("debit") or 0) for row in j.get("accounts", []))
        journals.append(
            f"<tr><td>{esc(j['name'])}</td><td>{esc(str(j.get('user_remark', '')))}</td>"
            f"<td class='num'>{_money(debit)}</td><td><code>{esc(str(j.get('cheque_no', '')))}</code></td>"
            f"<td>{_pill('Submitted' if int(j.get('docstatus') or 0) == 1 else 'Draft', 'green' if int(j.get('docstatus') or 0) == 1 else 'grey')}</td></tr>"
        )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Simulated ERPNext</title><style>{_STYLE}</style></head><body>"
        f"<header><b>Simulated ERPNext</b><span>{esc(COMPANY)}</span><span>Accounting system (stand-in)</span></header>"
        "<div class='note'>This is a small stand-in that answers the same REST calls as ERPNext. It is not ERPNext, "
        "it holds demo data in memory, and it is cleared when it stops.</div>"
        "<main>"
        "<h2>Suppliers</h2>" + table(["Supplier", "Name", "Payout wallet", "Status"], suppliers, "No suppliers") +
        "<h2>Purchase invoices</h2>" + table(["Invoice", "Supplier", "Bill no.", "Due", "Total (USD)", "Outstanding (USD)", "Status"], invoices, "No invoices") +
        "<h2>Payment entries</h2>"
        "<p class='empty' style='padding:0 0 8px'>The reference is the payment's transaction id. In this demo the payment is simulated, so that id is not on any chain.</p>"
        + table(["Entry", "Supplier", "Invoice", "Paid (USD)", "Reference", "Status"], payments, "No payments recorded yet") +
        "<h2>Journal entries (network fees)</h2>" + table(["Entry", "Remark", "Amount (USD)", "Reference", "Status"], journals, "No fee entries yet") +
        "<p style='margin-top:24px'><a href='/'>Refresh</a></p></main></body></html>"
    )


app = create_app()
