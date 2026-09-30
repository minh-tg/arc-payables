from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from pathlib import Path
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .deliberation import build_order_planner
from .domain import InvoiceLine, InvoiceRecord, usdc_to_units
from .forecast import build_forecast
from .monitoring import rescreen_suppliers, supplier_risk_overview
from .prioritisation import PaymentPrioritiser
from .metrics import collect, render
from .runtime import DisabledPaymentProvider, build_workflow
from .service import WorkflowError
from .settings import Settings, get_settings
from .store import SQLiteEvidenceStore

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
HASH_RE = re.compile(r"^(?:0x)?[a-fA-F0-9]{64}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class InvoiceLineInput(StrictModel):
    item_code: str = Field(min_length=1, max_length=140)
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    purchase_order_line_id: str | None = Field(default=None, max_length=140)
    receipt_line_ids: list[str] = Field(default_factory=list, max_length=20)


class InvoiceCreateInput(StrictModel):
    supplier_id: str = Field(min_length=1, max_length=140)
    invoice_number: str = Field(min_length=1, max_length=140)
    invoice_date: date
    due_date: date
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    currency: str = Field(default="USDC", min_length=3, max_length=10)
    invoice_payee_address: str | None = Field(default=None, max_length=42)
    lines: list[InvoiceLineInput] = Field(default_factory=list, max_length=100)
    purchase_invoice_id: str | None = Field(default=None, max_length=140)
    purchase_order_ids: list[str] = Field(default_factory=list, max_length=20)
    receipt_ids: list[str] = Field(default_factory=list, max_length=20)
    discount_percent: Decimal | None = Field(default=None, gt=0, le=100, max_digits=5, decimal_places=2)
    discount_deadline: date | None = None
    payment_terms: str | None = Field(default=None, max_length=500)
    source_document_hash: str | None = Field(default=None, max_length=66)
    untrusted_text: str | None = Field(default=None, max_length=20000, exclude=True)

    @field_validator("invoice_payee_address")
    @classmethod
    def valid_address(cls, value: str | None) -> str | None:
        if value is not None and not ADDRESS_RE.fullmatch(value):
            raise ValueError("must be a 20-byte hex address")
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        value = value.upper()
        if not value.isalpha():
            raise ValueError("currency must contain letters only")
        return value

    @field_validator("source_document_hash")
    @classmethod
    def valid_document_hash(cls, value: str | None) -> str | None:
        if value and not HASH_RE.fullmatch(value):
            raise ValueError("must be a SHA-256 hash")
        return value


class InvoiceImportInput(StrictModel):
    external_invoice_id: str = Field(min_length=1, max_length=140)


class InvoiceLinkInput(StrictModel):
    purchase_invoice_id: str = Field(min_length=1, max_length=140)
    reviewer: str = Field(min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=1000)


class ApprovalInput(StrictModel):
    reviewer: str = Field(min_length=1, max_length=120)
    approved: bool
    note: str = Field(min_length=1, max_length=1000)
    acknowledged_checks: list[str] = Field(default_factory=list, max_length=30)


class APIError(BaseModel):
    code: str
    message: str


def create_app(
    settings: Settings | None = None,
    store: SQLiteEvidenceStore | None = None,
    accounting=None,
    payment_provider=None,
    signer=None,
) -> FastAPI:
    settings = settings or get_settings()
    workflow = build_workflow(
        settings,
        store=store,
        accounting=accounting,
        payment_provider=payment_provider,
        signer=signer,
    )
    store = workflow.store
    accounting = workflow.accounting
    payment_provider = workflow.payment_provider

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=(
            "Evidence-gated AP workflow. Invoice text and payee fields are untrusted; "
            "only a trusted Supplier record can supply the payment destination."
        ),
    )
    app.state.workflow = workflow
    app.state.settings = settings
    app.state.store = store

    # The operator console: static files, no build step. Serving the shell needs no key because it
    # contains no data; every request it makes is authenticated like any other API call.
    web_dir = Path(__file__).resolve().parent / "web"
    if web_dir.is_dir():
        app.mount("/console", StaticFiles(directory=web_dir, html=True), name="console")

    def require_api_key(x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None) -> None:
        external = settings.payment_provider == "circle" or settings.accounting_provider == "frappe"
        if external and not settings.api_key:
            raise HTTPException(status_code=503, detail={"code": "api_auth_not_configured", "message": "API authentication must be configured before enabling vendor integrations."})
        if settings.api_key and (not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key)):
            raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Valid API credentials are required."})

    def require_metrics_access(
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """The scrape credential. A bearer token is accepted because Prometheus cannot send X-API-Key."""
        if not settings.api_key:
            return
        presented = x_api_key
        if not presented and authorization and authorization.lower().startswith("bearer "):
            presented = authorization[7:].strip()
        if not presented or not hmac.compare_digest(presented, settings.api_key):
            raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Valid API credentials are required."})

    def require_human_approval_token(
        x_approval_token: Annotated[str | None, Header(alias="X-Approval-Token")] = None,
    ) -> None:
        if not settings.approval_token:
            raise HTTPException(status_code=503, detail={"code": "human_approval_not_configured", "message": "Human approval is disabled until an approval credential is configured."})
        if not x_approval_token or not hmac.compare_digest(x_approval_token, settings.approval_token):
            raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Valid human-review credentials are required."})

    def validate_idempotency_key(value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_idempotency_key", "message": "Idempotency-Key must be a UUID v4."}) from exc
        if parsed.version != 4:
            raise HTTPException(status_code=422, detail={"code": "invalid_idempotency_key", "message": "Idempotency-Key must be a UUID v4."})
        return str(parsed)

    @app.exception_handler(WorkflowError)
    async def workflow_error_handler(request: Request, exc: WorkflowError):
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": str(exc)}})

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError):
        # Do not echo request bodies: invoice/OCR text is untrusted and may contain PII.
        issues = [{"field": ".".join(str(part) for part in error.get("loc", ()) if part != "body"), "message": "invalid value"} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": "Request validation failed.", "issues": issues}})

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["health"])
    def ready() -> dict[str, Any]:
        db_ready = False
        try:
            db_ready = store.health()
        except Exception:
            pass
        provider_configured = settings.payment_provider == "mock" or (
            bool(settings.circle_ready) if settings.payment_provider == "circle" else bool(settings.local_payment_ready)
        )
        frappe_configured = settings.accounting_provider != "frappe" or bool(getattr(accounting, "configured", False))
        ready_value = db_ready and provider_configured and frappe_configured
        response = {
            "status": "ready" if ready_value else "not_ready",
            "database": db_ready,
            "payment_provider_configured": provider_configured,
            "accounting_connector_configured": frappe_configured,
            "live_erp_writeback_enabled": bool(settings.accounting_provider == "frappe" and settings.frappe_accounting_ready),
            "chain_id": 5042002 if settings.payment_provider in {"circle", "local"} else None,
        }
        if not ready_value:
            raise HTTPException(status_code=503, detail=response)
        return response

    @app.get("/metrics", tags=["health"], dependencies=[Depends(require_metrics_access)])
    def metrics() -> PlainTextResponse:
        """Prometheus scrape target. Numbers about the money, not the money itself."""
        return PlainTextResponse(
            render(collect(workflow)),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/worker/status", tags=["health"], dependencies=[Depends(require_api_key)])
    def worker_status() -> dict[str, Any]:
        """What the background loop did lately, and what it asked a human to look at.

        The worker runs in its own process and records each pass. This reads those records back.
        The alerts are the ones the latest pass computed and stored, not recomputed here, so this
        endpoint and the pass history can never disagree about what was raised.
        """
        summary = store.worker_summary()
        detail = ((summary.get("last") or {}).get("detail")) or {}
        return {
            **summary,
            "alerts": detail.get("alerts", []),
            "alert_delivery": detail.get("alert_delivery", []),
        }

    @app.get("/invoices", tags=["invoices"], dependencies=[Depends(require_api_key)])
    def list_invoices() -> list[dict[str, Any]]:
        return workflow.list_invoices()

    @app.post("/invoices", status_code=status.HTTP_201_CREATED, tags=["invoices"], dependencies=[Depends(require_api_key)])
    def create_invoice(
        body: InvoiceCreateInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        key = validate_idempotency_key(idempotency_key)
        invoice = _domain_invoice(body)
        created, is_new = workflow.create_invoice(invoice, key)
        response = workflow.get_invoice(created.id)
        response["created"] = is_new
        return response

    @app.post("/invoices/import", status_code=status.HTTP_201_CREATED, tags=["invoices"], dependencies=[Depends(require_api_key)])
    def import_invoice(
        body: InvoiceImportInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        key = validate_idempotency_key(idempotency_key)
        invoice, is_new = workflow.import_invoice(body.external_invoice_id, key)
        response = workflow.get_invoice(invoice.id)
        response["created"] = is_new
        return response

    @app.get("/invoices/{invoice_id}", tags=["invoices"], dependencies=[Depends(require_api_key)])
    def get_invoice(invoice_id: str) -> dict[str, Any]:
        return workflow.get_invoice(invoice_id)

    @app.post("/invoices/{invoice_id}/evaluate", tags=["workflow"], dependencies=[Depends(require_api_key)])
    def evaluate(invoice_id: str) -> dict[str, Any]:
        return workflow.evaluate(invoice_id)

    @app.post("/invoices/{invoice_id}/link", tags=["workflow"], dependencies=[Depends(require_api_key), Depends(require_human_approval_token)])
    def link_invoice(invoice_id: str, body: InvoiceLinkInput) -> dict[str, Any]:
        """Match a captured invoice to an ERPNext payable record.

        This is the only way a captured invoice becomes payable, and it adopts the accounting
        record's amount, currency and lines. Approving the captured document itself never does.
        """
        return workflow.link_to_purchase_invoice(invoice_id, body.purchase_invoice_id, body.reviewer)

    @app.post("/invoices/{invoice_id}/approval", tags=["workflow"], dependencies=[Depends(require_api_key), Depends(require_human_approval_token)])
    def approve(invoice_id: str, body: ApprovalInput) -> dict[str, Any]:
        return workflow.approve(invoice_id, body.model_dump())

    @app.post("/invoices/{invoice_id}/payment", tags=["payments"], dependencies=[Depends(require_api_key)])
    def submit_payment(invoice_id: str) -> dict[str, Any]:
        return workflow.submit_payment(invoice_id)

    @app.post("/invoices/{invoice_id}/payment/reconcile", tags=["payments"], dependencies=[Depends(require_api_key)])
    def reconcile_payment(invoice_id: str) -> dict[str, Any]:
        return workflow.submit_payment(invoice_id)

    @app.post("/invoices/{invoice_id}/payment/erp-writeback", tags=["accounting"], dependencies=[Depends(require_api_key)])
    def retry_erp_writeback(invoice_id: str) -> dict[str, Any]:
        return workflow.retry_erp_writeback(invoice_id)

    @app.get("/invoices/{invoice_id}/events", tags=["audit"], dependencies=[Depends(require_api_key)])
    def events(invoice_id: str) -> list[dict[str, Any]]:
        return workflow.events(invoice_id)

    @app.get("/suppliers", tags=["risk"], dependencies=[Depends(require_api_key)])
    def suppliers() -> list[dict[str, Any]]:
        """Counterparties with their latest screening, risk tier and resulting automatic limit."""
        return supplier_risk_overview(workflow)

    @app.post("/monitoring/rescreen", tags=["risk"], dependencies=[Depends(require_api_key)])
    def rescreen(force: bool = False, source: str = "api") -> list[dict[str, Any]]:
        """Re-screen counterparties with open invoices, recording each result.

        A change in risk profile is written to the audit chain of every open invoice it affects.
        Screening authorizes nothing: the result flows into the same evidence and policy.
        """
        return [item.to_dict() for item in rescreen_suppliers(workflow, force=force, source=source)]

    @app.get("/forecast", tags=["planning"], dependencies=[Depends(require_api_key)])
    def treasury_forecast(days: int = 30) -> dict[str, Any]:
        """Forward coverage: what is due, and the first date the balance stops covering it.

        Read-only. Obligations are walked in due-date order, keeping the treasury reserve
        intact, and obligations the agent may not pay are still counted as money owed.
        """
        return build_forecast(workflow, days=max(1, min(days, 365))).to_dict()

    @app.get("/plan", tags=["planning"], dependencies=[Depends(require_api_key)])
    def payment_plan() -> dict[str, Any]:
        """Which payable to pay first, given the treasury balance and the reserve floor.

        Read-only advice: it evaluates each invoice without recording a decision, and every
        invoice it ranks must still pass its own policy checks and settle through the same
        guarded payment path.
        """
        prioritiser = PaymentPrioritiser(workflow, planner=build_order_planner(settings))
        return prioritiser.plan().to_dict()

    @app.get("/audit/verify", tags=["audit"], dependencies=[Depends(require_api_key)])
    def verify_audit() -> dict[str, Any]:
        """Recompute the audit chain, so a reviewer can tell the record was not rewritten."""
        return store.verify_audit_chain()

    return app


def _domain_invoice(body: InvoiceCreateInput) -> InvoiceRecord:
    lines = tuple(
        InvoiceLine(
            item_code=line.item_code,
            quantity=str(line.quantity),
            amount_units=usdc_to_units(line.amount),
            purchase_order_line_id=line.purchase_order_line_id,
            receipt_line_ids=tuple(line.receipt_line_ids),
        )
        for line in body.lines
    )
    text_hash = hashlib.sha256(body.untrusted_text.encode("utf-8")).hexdigest() if body.untrusted_text else None
    source_hash = body.source_document_hash
    if source_hash:
        source_hash = source_hash.removeprefix("0x").lower()
    else:
        material = {
            "supplier_id": body.supplier_id,
            "invoice_number": body.invoice_number,
            "invoice_date": body.invoice_date.isoformat(),
            "due_date": body.due_date.isoformat(),
            "amount": str(body.amount),
            "currency": body.currency,
            "invoice_payee_address": body.invoice_payee_address,
            "lines": [line.model_dump(mode="json") for line in body.lines],
        }
        source_hash = hashlib.sha256(repr(sorted(material.items())).encode()).hexdigest()
    return InvoiceRecord(
        id=str(uuid.uuid4()),
        supplier_id=body.supplier_id,
        invoice_number=body.invoice_number,
        invoice_date=body.invoice_date,
        due_date=body.due_date,
        amount_units=usdc_to_units(body.amount),
        currency=body.currency,
        invoice_payee_address=body.invoice_payee_address,
        lines=lines,
        purchase_invoice_id=body.purchase_invoice_id,
        purchase_order_ids=tuple(body.purchase_order_ids),
        receipt_ids=tuple(body.receipt_ids),
        discount_percent=str(body.discount_percent) if body.discount_percent is not None else None,
        discount_deadline=body.discount_deadline,
        payment_terms=body.payment_terms,
        source_text_hash=text_hash,
        source_document_hash=source_hash,
    )


app = create_app()
