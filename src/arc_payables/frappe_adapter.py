from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from .accounting import AccountingMappingError, compute_payment_entry_amounts, payload_fields
from .domain import (
    AccountingEvidence,
    ERPWriteResult,
    InvoiceLine,
    InvoiceRecord,
    PurchaseOrderEvidence,
    PurchaseOrderLine,
    ReceiptEvidence,
    ReceiptLine,
    ScreeningStatus,
    SupplierRecord,
    USDC_SCALE,
    utcnow,
)
from .ports import PaymentMapping
from .settings import Settings


class FrappeAdapterError(RuntimeError):
    def __init__(self, status_code: int | None, operation: str, *, uncertain: bool | None = None):
        self.status_code = status_code
        self.uncertain = (operation in {"request timeout", "request"} or (status_code is not None and status_code >= 500)) if uncertain is None else uncertain
        super().__init__(f"ERPNext {operation} failed" + (f" (HTTP {status_code})" if status_code else ""))


class FrappeAccountingConnector:
    """Frappe REST API v1 adapter; it never creates ERP documents before settlement."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.base_url = (settings.frappe_url or "").rstrip("/")
        self.client = client or httpx.Client(timeout=settings.frappe_timeout_seconds)
        self._verified_api_user = False

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.settings.frappe_api_key and self.settings.frappe_api_secret)

    def _headers(self) -> dict[str, str]:
        if not self.configured:
            raise FrappeAdapterError(None, "configuration")
        return {
            "Authorization": f"token {self.settings.frappe_api_key}:{self.settings.frappe_api_secret}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
        if path != "/api/method/frappe.auth.get_logged_user" and not self._verified_api_user:
            identity = self._send("GET", "/api/method/frappe.auth.get_logged_user")
            username = identity.get("message")
            if not isinstance(username, str) or not username.strip():
                raise FrappeAdapterError(None, "could not verify API user")
            if username.strip().lower() == "administrator":
                raise FrappeAdapterError(None, "Administrator credentials are not permitted")
            self._verified_api_user = True
        return self._send(method, path, params=params, json_body=json_body)

    def _send(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
        try:
            response = self.client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                params=params,
                json=json_body,
            )
        except httpx.TimeoutException as exc:
            raise FrappeAdapterError(None, "request timeout") from exc
        except httpx.HTTPError as exc:
            raise FrappeAdapterError(None, "request") from exc
        if response.status_code >= 400:
            raise FrappeAdapterError(response.status_code, "REST request")
        try:
            payload = response.json()
        except ValueError as exc:
            raise FrappeAdapterError(response.status_code, "invalid response", uncertain=method == "POST") from exc
        if not isinstance(payload, dict):
            raise FrappeAdapterError(response.status_code, "invalid response", uncertain=method == "POST")
        return payload

    def get_document(self, doctype: str, name: str) -> dict:
        path = f"/api/resource/{quote(doctype, safe='')}/{quote(name, safe='')}"
        return self._request("GET", path).get("data", {})

    def list_documents(self, doctype: str, filters: list, fields: list[str], limit: int = 20) -> list[dict]:
        params = {
            "filters": __import__("json").dumps(filters, separators=(",", ":")),
            "fields": __import__("json").dumps(fields, separators=(",", ":")),
            "limit_page_length": limit,
        }
        return self._request("GET", f"/api/resource/{quote(doctype, safe='')}", params=params).get("data", [])

    def import_invoice(self, external_id: str) -> tuple[InvoiceRecord, AccountingEvidence]:
        raw = self.get_document("Purchase Invoice", external_id)
        supplier_id = str(raw.get("supplier") or "")
        if not supplier_id:
            raise FrappeAdapterError(None, "Purchase Invoice missing Supplier")
        invoice_number = str(raw.get("bill_no") or raw.get("supplier_invoice_no") or raw.get("name") or external_id)
        invoice_date = self._date(raw.get("bill_date") or raw.get("posting_date"))
        due_date = self._date(raw.get("due_date"))
        currency = str(raw.get("currency") or "").upper()
        grand_total_erp = self._amount_units(raw.get("grand_total"))
        invoice_items = raw.get("items") or []
        # The ERP document is denominated in the accounting currency; the local invoice and the
        # on-chain authorization are denominated in the settlement currency. Convert once, at the
        # explicitly configured rate, and refuse anything that does not divide exactly.
        grand_total = self._settlement_units_from_erp(grand_total_erp, currency)
        lines = self._settlement_lines_from_erp(raw, currency)
        if lines and sum(line.amount_units for line in lines) != grand_total:
            raise FrappeAdapterError(
                None,
                "converted Purchase Invoice lines do not sum to the converted grand total at the configured rate",
            )
        po_ids = tuple(sorted({str(item.get("purchase_order")) for item in invoice_items if item.get("purchase_order")}))
        receipt_ids = tuple(sorted({str(item.get("purchase_receipt")) for item in invoice_items if item.get("purchase_receipt")}))
        supplier = self._supplier(supplier_id)
        payee_field = getattr(self.settings, "frappe_invoice_payee_field", None)
        invoice = InvoiceRecord(
            id=str(__import__("uuid").uuid4()),
            supplier_id=supplier_id,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            due_date=due_date,
            amount_units=grand_total,
            currency=self.settings.settlement_currency,
            invoice_payee_address=(raw.get(payee_field) if payee_field else None),
            lines=lines,
            purchase_invoice_id=external_id,
            purchase_order_ids=po_ids,
            receipt_ids=receipt_ids,
            payment_terms=str(raw.get("payment_terms_template") or "") or None,
            source_document_hash=None,
        )
        orders = tuple(self._purchase_order(order_id) for order_id in po_ids)
        receipts = tuple(self._receipt(receipt_id) for receipt_id in receipt_ids)
        matches = self.list_documents(
            "Purchase Invoice",
            [["supplier", "=", supplier_id], ["bill_no", "=", invoice_number]],
            ["name"],
            limit=20,
        )
        duplicate = next((row["name"] for row in matches if row.get("name") != external_id), None)
        evidence = AccountingEvidence(
            supplier=supplier,
            purchase_orders=orders,
            receipts=receipts,
            duplicate_invoice_id=duplicate,
            invoice_status=self._invoice_status(raw),
            screening=ScreeningStatus.UNAVAILABLE,
            invoice_linked=True,
            source_invoice_amount_units=grand_total,
            source_invoice_currency=currency,
            source_invoice_number=invoice_number,
            source_supplier_id=supplier_id,
            source_invoice_id=str(raw.get("name") or external_id),
            source_invoice_lines=lines,
        )
        return invoice, evidence

    def get_invoice_evidence(self, invoice: InvoiceRecord) -> AccountingEvidence:
        if not invoice.purchase_invoice_id:
            supplier = self._supplier(invoice.supplier_id)
            matches = self.list_documents(
                "Purchase Invoice",
                [["supplier", "=", invoice.supplier_id], ["bill_no", "=", invoice.invoice_number]],
                ["name"],
                limit=20,
            )
            duplicate = next((row["name"] for row in matches), None)
            # Fail closed: without a linked Purchase Invoice there is no accounting source
            # of truth for amount, currency, lines, or payable state.
            return AccountingEvidence(
                supplier=supplier,
                purchase_orders=(),
                receipts=(),
                duplicate_invoice_id=duplicate,
                invoice_status="UNVERIFIED",
                screening=ScreeningStatus.UNAVAILABLE,
                invoice_linked=False,
            )
        raw = self.get_document("Purchase Invoice", invoice.purchase_invoice_id)
        supplier_id = str(raw.get("supplier") or invoice.supplier_id)
        supplier = self._supplier(supplier_id)
        po_ids = invoice.purchase_order_ids or tuple(sorted({str(item.get("purchase_order")) for item in raw.get("items", []) if item.get("purchase_order")}))
        receipt_ids = invoice.receipt_ids or tuple(sorted({str(item.get("purchase_receipt")) for item in raw.get("items", []) if item.get("purchase_receipt")}))
        orders = tuple(self._purchase_order(item) for item in po_ids)
        receipts = tuple(self._receipt(item) for item in receipt_ids)
        source_lines = self._invoice_lines(raw)
        source_amount = self._amount_units(raw.get("grand_total"))
        source_currency = str(raw.get("currency") or "").upper()
        source_number = str(raw.get("bill_no") or raw.get("supplier_invoice_no") or raw.get("name") or invoice.purchase_invoice_id)
        matches = self.list_documents(
            "Purchase Invoice",
            [["supplier", "=", supplier_id], ["bill_no", "=", invoice.invoice_number]],
            ["name"],
            limit=20,
        )
        duplicate = next((row["name"] for row in matches if row.get("name") != invoice.purchase_invoice_id), None)
        return AccountingEvidence(
            supplier,
            orders,
            receipts,
            duplicate,
            self._invoice_status(raw),
            ScreeningStatus.UNAVAILABLE,
            True,
            source_amount,
            source_currency,
            source_number,
            supplier_id,
            str(raw.get("name") or invoice.purchase_invoice_id),
            source_lines,
        )

    def find_payment_entry(self, payment_reference: str) -> dict | None:
        rows = self.list_documents(
            "Payment Entry",
            [["reference_no", "=", payment_reference]],
            ["name", "reference_no", "party_type", "party", "paid_amount", "received_amount", "docstatus"],
            limit=3,
        )
        if len(rows) > 1:
            raise FrappeAdapterError(None, "duplicate Payment Entry reference; reconciliation required")
        return self.get_document("Payment Entry", str(rows[0]["name"])) if rows else None

    def create_payment_entry(
        self,
        invoice: InvoiceRecord,
        tx_hash: str,
        fee_units: int,
        mapping: PaymentMapping,
    ) -> ERPWriteResult:
        reference = f"ARC-TESTNET:{tx_hash}"
        existing = self.find_payment_entry(reference)
        if existing:
            self._verify_existing_entry(existing, invoice, fee_units, mapping)
            name = str(existing["name"])
            if int(existing.get("docstatus") or 0) == 1:
                return ERPWriteResult(name, 1, already_existed=True)
            self._submit_payment_entry(existing)
            return ERPWriteResult(name, 1, already_existed=True)
        if not invoice.purchase_invoice_id:
            raise FrappeAdapterError(None, "Purchase Invoice link is required for Payment Entry")
        if not self.settings.frappe_accounting_ready:
            raise FrappeAdapterError(None, "account/currency/exchange-rate/fee mapping is incomplete")
        self._verify_account_mapping(invoice, mapping)

        amount_usdc = Decimal(invoice.amount_units) / Decimal(USDC_SCALE)
        try:
            amounts = compute_payment_entry_amounts(
                invoice.amount_units,
                fee_units,
                source_currency=mapping.source_currency,
                target_currency=mapping.target_currency,
                company_currency=mapping.company_currency,
                source_exchange_rate=mapping.source_exchange_rate,
                target_exchange_rate=mapping.target_exchange_rate,
            )
        except AccountingMappingError as exc:
            raise FrappeAdapterError(None, f"accounting mapping is invalid: {exc}") from exc
        if amounts.supplier_amount != amount_usdc:  # defensive: the fee is never netted off
            raise FrappeAdapterError(None, "computed supplier amount differs from the authorized invoice amount")

        payload: dict[str, Any] = {
            "doctype": "Payment Entry",
            "payment_type": "Pay",
            "party_type": "Supplier",
            "party": invoice.supplier_id,
            "company": mapping.company,
            "posting_date": date.today().isoformat(),
            "mode_of_payment": mapping.mode_of_payment,
            "paid_from": mapping.paid_from,
            "paid_to": mapping.paid_to,
            "source_exchange_rate": mapping.source_exchange_rate,
            "target_exchange_rate": mapping.target_exchange_rate,
            "reference_no": reference,
            "reference_date": date.today().isoformat(),
            **payload_fields(amounts, invoice.purchase_invoice_id or "", mapping.fee_account),
        }
        try:
            created = self._request("POST", "/api/resource/Payment%20Entry", json_body=payload).get("data", {})
        except FrappeAdapterError:
            # POST may have committed before a timeout. The next explicit retry searches by
            # the stable Arc tx hash reference before attempting any second insert.
            raise
        name = str(created.get("name") or "")
        if not name:
            raise FrappeAdapterError(None, "Payment Entry create response missing record name", uncertain=True)
        self._submit_payment_entry(created)
        return ERPWriteResult(name, 1)

    def _submit_payment_entry(self, doc: dict) -> None:
        """Submit using the whole document, never a bare name.

        Frappe v15's ``frappe.client.submit`` reinstantiates whatever it receives
        (``frappe.get_doc(doc)``), so a payload of only ``doctype``/``name`` submits nothing at
        all: the document must be sent with its values, child rows and concurrency timestamp.
        Verified against a live ERPNext v15 sandbox.
        """
        name = str(doc.get("name") or "")
        if not name:
            raise FrappeAdapterError(None, "Payment Entry has no record name", uncertain=True)
        result = self._request(
            "POST",
            "/api/method/frappe.client.submit",
            json_body={"doc": {**doc, "doctype": "Payment Entry"}},
        )
        submitted = result.get("message") if isinstance(result.get("message"), dict) else result.get("data")
        if isinstance(submitted, dict):
            if submitted.get("name") not in (None, name):
                raise FrappeAdapterError(None, "submitted Payment Entry response names a different record", uncertain=True)
            # Do not report a completed writeback unless ERPNext confirms the submission.
            if str(submitted.get("docstatus")) not in {"1", "1.0"}:
                raise FrappeAdapterError(None, "ERPNext did not confirm the Payment Entry submission", uncertain=True)

    def _verify_existing_entry(self, row: dict, invoice: InvoiceRecord, fee_units: int, mapping: PaymentMapping) -> None:
        if row.get("party_type") != "Supplier" or row.get("party") != invoice.supplier_id:
            raise FrappeAdapterError(None, "existing Payment Entry reference conflicts with supplier")
        if (
            row.get("company") != mapping.company
            or row.get("paid_from") != mapping.paid_from
            or row.get("paid_to") != mapping.paid_to
            or row.get("mode_of_payment") != mapping.mode_of_payment
            or Decimal(str(row.get("source_exchange_rate", "0"))) != Decimal(mapping.source_exchange_rate)
            or Decimal(str(row.get("target_exchange_rate", "0"))) != Decimal(mapping.target_exchange_rate)
        ):
            raise FrappeAdapterError(None, "existing Payment Entry reference conflicts with configured accounts or rates")
        refs = row.get("references") or []
        if len(refs) != 1:
            raise FrappeAdapterError(None, "existing Payment Entry has unexpected invoice references")
        reference = refs[0]
        if reference.get("reference_doctype") != "Purchase Invoice" or reference.get("reference_name") != invoice.purchase_invoice_id:
            raise FrappeAdapterError(None, "existing Payment Entry reference conflicts with invoice")

        try:
            amounts = compute_payment_entry_amounts(
                invoice.amount_units,
                fee_units,
                source_currency=mapping.source_currency,
                target_currency=mapping.target_currency,
                company_currency=mapping.company_currency,
                source_exchange_rate=mapping.source_exchange_rate,
                target_exchange_rate=mapping.target_exchange_rate,
            )
        except AccountingMappingError as exc:
            raise FrappeAdapterError(None, f"accounting mapping is invalid: {exc}") from exc
        expected = payload_fields(amounts, invoice.purchase_invoice_id or "", mapping.fee_account)
        if Decimal(str(reference.get("allocated_amount", "0"))) != Decimal(expected["references"][0]["allocated_amount"]):
            raise FrappeAdapterError(None, "existing Payment Entry reference conflicts with the invoice amount")
        if Decimal(str(row.get("paid_amount", "0"))) != Decimal(expected["paid_amount"]):
            raise FrappeAdapterError(None, "existing Payment Entry conflicts with the expected outflow")
        if Decimal(str(row.get("received_amount", "0"))) != Decimal(expected["received_amount"]):
            raise FrappeAdapterError(None, "existing Payment Entry conflicts with the expected party amount")
        deductions = row.get("deductions") or []
        expected_deductions = expected.get("deductions", [])
        if len(deductions) != len(expected_deductions):
            raise FrappeAdapterError(None, "existing Payment Entry conflicts with network-fee deductions")
        for actual_row, expected_row in zip(deductions, expected_deductions):
            if actual_row.get("account") != expected_row["account"]:
                raise FrappeAdapterError(None, "existing Payment Entry conflicts with the network-fee account")
            if Decimal(str(actual_row.get("amount", "0"))) != Decimal(expected_row["amount"]):
                raise FrappeAdapterError(None, "existing Payment Entry conflicts with the network-fee amount")

    def _supplier(self, supplier_id: str) -> SupplierRecord:
        raw = self.get_document("Supplier", supplier_id)
        wallet = raw.get(self.settings.frappe_supplier_wallet_field)
        verified = raw.get(self.settings.frappe_supplier_wallet_verified_field) in (True, 1, "1")
        modified = str(raw.get("modified") or "unknown")
        blocked_reason = self._supplier_block_reason(raw)
        return SupplierRecord(
            id=str(raw.get("name") or supplier_id),
            name=str(raw.get("supplier_name") or raw.get("name") or supplier_id),
            approved_wallet=str(wallet) if wallet else None,
            wallet_verified=verified,
            wallet_version=modified,
            screening=ScreeningStatus.UNAVAILABLE,
            erp_supplier_id=str(raw.get("name") or supplier_id),
            payment_blocked=blocked_reason is not None,
            blocked_reason=blocked_reason,
        )

    @staticmethod
    def _supplier_block_reason(raw: dict) -> str | None:
        """Whether the accounting system itself refuses this supplier.

        Both fields are read because they mean different things and either one stops payment:
        `disabled` marks the record as retired, `on_hold` marks the relationship as suspended.
        """
        reasons = []
        if raw.get("disabled") in (True, 1, "1"):
            reasons.append("the supplier record is disabled")
        if raw.get("on_hold") in (True, 1, "1"):
            reasons.append("the supplier is on hold")
        return " and ".join(reasons) if reasons else None

    def _verify_account_mapping(self, invoice: InvoiceRecord, mapping: PaymentMapping) -> None:
        if invoice.currency.upper() != mapping.source_currency.upper():
            raise FrappeAdapterError(
                None,
                f"invoice currency {invoice.currency} is not the configured settlement currency "
                f"{mapping.source_currency}",
            )
        invoice_doc = self.get_document("Purchase Invoice", invoice.purchase_invoice_id or "")
        erp_currency = str(invoice_doc.get("currency") or "").upper()
        if erp_currency != mapping.invoice_currency.upper():
            raise FrappeAdapterError(
                None,
                f"Purchase Invoice currency {erp_currency} differs from the configured invoice currency "
                f"{mapping.invoice_currency}",
            )
        if mapping.invoice_currency.upper() != mapping.company_currency.upper():
            raise FrappeAdapterError(None, "the invoice currency must equal the company currency for this mapping")
        for account_name, expected_currency, role in (
            (mapping.paid_from, mapping.source_currency, "settlement"),
            (mapping.paid_to, mapping.target_currency, "payable"),
            (mapping.fee_account, mapping.fee_currency, "network-fee"),
        ):
            if not account_name:
                raise FrappeAdapterError(None, f"the {role} account is not configured")
            account = self.get_document("Account", account_name)
            if str(account.get("company") or "") != mapping.company:
                raise FrappeAdapterError(None, f"the {role} account belongs to a different company")
            if str(account.get("account_currency") or "").upper() != expected_currency.upper():
                raise FrappeAdapterError(
                    None,
                    f"the {role} account currency does not match the explicit mapping "
                    f"({expected_currency} expected)",
                )
            if account.get("is_group") in (True, 1, "1"):
                raise FrappeAdapterError(None, f"the {role} account must be a ledger account")
        if mapping.fee_currency.upper() != mapping.company_currency.upper():
            # ERPNext refuses a deduction whose account currency is not the company currency.
            raise FrappeAdapterError(None, "the network-fee account must be in the company currency")

    @staticmethod
    def _invoice_status(raw: dict) -> str:
        """Derive payable state from the authoritative docstatus; never assume SUBMITTED."""
        status = str(raw.get("status") or "").strip()
        docstatus = raw.get("docstatus")
        if docstatus is None:
            return status.upper() if status else "UNKNOWN"
        try:
            code = int(docstatus)
        except (TypeError, ValueError):
            return "UNKNOWN"
        if code == 2:
            return "CANCELLED"
        if code == 0:
            return "DRAFT"
        return status.upper() if status else "SUBMITTED"

    def _settlement_units_from_erp(self, erp_units: int, currency: str) -> int:
        """Convert an accounting-currency amount into the settlement currency."""
        configured = (self.settings.frappe_invoice_currency or "").upper()
        if not configured or currency != configured:
            raise FrappeAdapterError(
                None,
                f"accounting document currency {currency or 'missing'} is not the configured invoice currency {configured}",
            )
        rate = self.settings.settlement_to_invoice_rate
        if rate <= 0:
            raise FrappeAdapterError(None, "the settlement-to-invoice rate must be positive")
        settlement = Decimal(erp_units) / Decimal(rate)
        if settlement != settlement.to_integral_value():
            raise FrappeAdapterError(
                None,
                "accounting amount does not convert exactly into the settlement currency at the configured rate",
            )
        return int(settlement)

    def _settlement_lines_from_erp(self, raw: dict, currency: str) -> tuple[InvoiceLine, ...]:
        lines = self._invoice_lines(raw)
        return tuple(
            InvoiceLine(
                item_code=line.item_code,
                quantity=line.quantity,
                amount_units=self._settlement_units_from_erp(line.amount_units, currency),
                purchase_order_line_id=line.purchase_order_line_id,
                receipt_line_ids=line.receipt_line_ids,
            )
            for line in lines
        )

    def _invoice_lines(self, raw: dict) -> tuple[InvoiceLine, ...]:
        return tuple(
            InvoiceLine(
                item_code=str(item.get("item_code") or item.get("item_name") or "UNKNOWN"),
                quantity=str(item.get("qty") or "0"),
                amount_units=self._amount_units(item.get("amount") or "0"),
                purchase_order_line_id=item.get("po_detail") or item.get("purchase_order_item"),
                receipt_line_ids=tuple(filter(None, [item.get("pr_detail"), item.get("purchase_receipt_item")])),
            )
            for item in (raw.get("items") or [])
        )

    def _purchase_order(self, order_id: str) -> PurchaseOrderEvidence:
        raw = self.get_document("Purchase Order", order_id)
        lines = tuple(
            PurchaseOrderLine(
                id=str(item.get("name") or item.get("item_code") or ""),
                item_code=str(item.get("item_code") or "UNKNOWN"),
                quantity=str(item.get("qty") or "0"),
                amount_units=self._amount_units(item.get("amount") or "0"),
            )
            for item in raw.get("items", [])
        )
        return PurchaseOrderEvidence(
            id=str(raw.get("name") or order_id),
            supplier_id=str(raw.get("supplier") or ""),
            status=str(raw.get("status") or ("SUBMITTED" if raw.get("docstatus") == 1 else "DRAFT")),
            lines=lines,
        )

    def _receipt(self, receipt_id: str) -> ReceiptEvidence:
        raw = self.get_document("Purchase Receipt", receipt_id)
        lines = tuple(
            ReceiptLine(
                id=str(item.get("name") or item.get("item_code") or ""),
                item_code=str(item.get("item_code") or "UNKNOWN"),
                quantity=str(item.get("qty") or "0"),
                purchase_order_line_id=item.get("purchase_order_item") or item.get("purchase_order_detail"),
            )
            for item in raw.get("items", [])
        )
        return ReceiptEvidence(
            id=str(raw.get("name") or receipt_id),
            supplier_id=str(raw.get("supplier") or ""),
            status=str(raw.get("status") or ("SUBMITTED" if raw.get("docstatus") == 1 else "DRAFT")),
            lines=lines,
        )

    @staticmethod
    def _date(value: Any) -> date:
        if not value:
            raise FrappeAdapterError(None, "required invoice date is missing")
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError as exc:
            raise FrappeAdapterError(None, "invoice date is invalid") from exc

    @staticmethod
    def _amount_units(value: Any) -> int:
        try:
            amount = Decimal(str(value))
        except Exception as exc:
            raise FrappeAdapterError(None, "invoice amount is invalid") from exc
        scaled = amount * USDC_SCALE
        if scaled != scaled.to_integral_value() or scaled < 0:
            raise FrappeAdapterError(None, "invoice amount precision is unsupported")
        return int(scaled)
