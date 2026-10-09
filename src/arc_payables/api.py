from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import queue
import re
import uuid
from pathlib import Path
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import MUTATIONS, OIDCAuth, fail as auth_fail, mount_auth
from .attention import build_attention
from .audit_log import list_payments, payment_report, verify_payment
from .deliberation import build_order_planner
from .domain import InvoiceLine, InvoiceRecord, usdc_to_units, utcnow
from .forecast import build_forecast
from .monitoring import rescreen_suppliers, supplier_risk_overview
from .prioritisation import PaymentPrioritiser
from .metrics import collect, render
from .runtime import DisabledPaymentProvider, build_workflow
from .service import WorkflowError
from .settings import Settings, get_settings
from .setup_check import inventory, live_checks
from .store import SQLiteEvidenceStore

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
HASH_RE = re.compile(r"^(?:0x)?[a-fA-F0-9]{64}$")


class EventBroadcaster:
    def __init__(self) -> None:
        self._subscribers: set[queue.Queue] = set()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        self._subscribers.discard(q)

    def broadcast(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        payload = {"type": event_type, "timestamp": utcnow().isoformat(), "data": data or {}}
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except Exception:
                pass


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


class InvoiceUploadInput(StrictModel):
    document_content: str = Field(min_length=1, max_length=200000, exclude=True)
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
    payment_terms: str | None = Field(default=None, max_length=500)
    content_type: str = Field(default="text/plain", max_length=50)

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


class InvoiceImportInput(StrictModel):
    external_invoice_id: str = Field(min_length=1, max_length=140)


class InvoiceLinkInput(StrictModel):
    purchase_invoice_id: str = Field(min_length=1, max_length=140)
    reviewer: str | None = Field(default=None, min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=1000)


class ApprovalInput(StrictModel):
    reviewer: str | None = Field(default=None, min_length=1, max_length=120)
    approved: bool
    note: str = Field(min_length=1, max_length=1000)
    acknowledged_checks: list[str] = Field(default_factory=list, max_length=30)


class RestoreDrillInput(StrictModel):
    backup: str = Field(min_length=1, max_length=200)


class APIError(BaseModel):
    code: str
    message: str


def create_app(
    settings: Settings | None = None,
    store: SQLiteEvidenceStore | None = None,
    accounting=None,
    payment_provider=None,
    signer=None,
    oidc_client=None,
    converter=None,
) -> FastAPI:
    settings = settings or get_settings()
    workflow = build_workflow(
        settings,
        store=store,
        accounting=accounting,
        payment_provider=payment_provider,
        signer=signer,
        converter=converter,
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
    broadcaster = EventBroadcaster()
    app.state.broadcaster = broadcaster

    # The operator console: static files, no build step. Serving the shell needs no key because it
    # contains no data; every request it makes is authenticated like any other API call.
    web_dir = Path(__file__).resolve().parent / "web"
    if web_dir.is_dir():
        app.mount("/console", StaticFiles(directory=web_dir, html=True), name="console")

        @app.get("/", include_in_schema=False)
        @app.get("/app", include_in_schema=False)
        @app.get("/app/", include_in_schema=False)
        async def redirect_to_console():
            return RedirectResponse(url="/console/", status_code=307)

    from .mock_adapters import MockAccountingConnector, MockPaymentProvider
    demo_allowed = (
        settings.payment_provider == "mock" and settings.accounting_provider == "mock"
        and isinstance(accounting, MockAccountingConnector) and isinstance(payment_provider, MockPaymentProvider)
    )
    oidc = OIDCAuth(settings, store, oidc_client) if settings.auth_mode == "oidc" else None
    app.state.identity_auth = oidc
    mount_auth(app, oidc, settings.auth_mode if demo_allowed or settings.auth_mode != "demo" else "identity_required")

    @app.middleware("http")
    async def identity_cache_policy(request: Request, call_next):
        response = await call_next(request)
        if settings.auth_mode == "oidc" or request.url.path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def require_api_key(
        request: Request,
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
        api_key_query: Annotated[str | None, Query(alias="api_key")] = None,
    ) -> None:
        presented = x_api_key or api_key_query
        if oidc is not None:
            permission = "read" if request.method in {"GET", "HEAD"} else MUTATIONS.get(request.scope["route"].name)
            if permission is None:
                raise auth_fail(403, "forbidden")
            actor = oidc.authorize(request, permission)
            if request.method == "POST" and request.path_params.get("invoice_id"):
                try:
                    store.record_identity_action(request.path_params["invoice_id"], request.scope["route"].name, actor.record())
                except Exception as exc:
                    raise auth_fail(503, "oidc_unavailable") from exc
            return
        if settings.auth_mode == "demo" and not demo_allowed:
            raise HTTPException(status_code=503, detail={"code": "api_auth_not_configured", "message": "External providers require individual identity (AUTH_MODE=oidc) or an explicit testnet-token deployment (AUTH_MODE=testnet_tokens with API_KEY and APPROVAL_TOKEN). AUTH_MODE=demo only opens the mock adapters."})
        if settings.auth_mode == "testnet_tokens" and not settings.api_key:
            raise HTTPException(status_code=503, detail={"code": "api_auth_not_configured", "message": "Testnet API authentication must be configured."})
        if settings.api_key and (not presented or not hmac.compare_digest(presented.encode(), settings.api_key.encode())):
            raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Valid API credentials are required."})

    def require_metrics_access(
        request: Request,
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """Dedicated read-only scrape credential, never a staff/approval credential."""
        credential = settings.metrics_api_key if oidc is not None else settings.api_key
        presented = x_api_key
        if not presented and authorization and authorization.lower().startswith("bearer "):
            presented = authorization[7:].strip()
        if credential and presented and hmac.compare_digest(presented.encode(), credential.encode()):
            return
        require_api_key(request, x_api_key)

    def require_human_approval_token(
        request: Request,
        x_approval_token: Annotated[str | None, Header(alias="X-Approval-Token")] = None,
    ) -> None:
        if oidc is not None:
            # Endpoint permission was checked already. A distinct approver identity,
            # not a shared credential or client-declared reviewer, authorizes approval.
            return
        if not settings.approval_token:
            raise HTTPException(status_code=503, detail={"code": "human_approval_not_configured", "message": "Human approval is disabled until an approval credential is configured."})
        if not x_approval_token or not hmac.compare_digest(x_approval_token.encode(), settings.approval_token.encode()):
            raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Valid human-review credentials are required."})

    def reviewer(request: Request, supplied: str | None) -> str:
        if oidc is not None:
            if supplied is not None:
                raise HTTPException(status_code=422, detail={"code": "reviewer_managed", "message": "Reviewer identity is derived from your verified session, not request data."})
            return request.state.identity.id
        if not supplied:
            raise HTTPException(status_code=422, detail={"code": "reviewer_required", "message": "Development review requires a reviewer name."})
        return supplied

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

    @app.get("/explanations.json", tags=["console"], include_in_schema=False)
    def explanations() -> JSONResponse:
        """Plain words for every code the system can return, in the backend's own words.

        The console fetches this once and renders the words beside the codes. Publishing the table
        means the pages can never drift from the behaviour: a check renamed without updating its
        explanation is caught by a test that walks the real vocabulary. CONCEPTS rides along for the
        same reason: the definitions of USDC, the guard, and the rest are written here rather than
        in the page, so the console carries no copy of its own to fall out of date.
        """
        from . import explain as plain_words

        return JSONResponse(
            {
                name.lower(): {
                    code: {"plain": row["plain"], "action": row.get("action")}
                    for code, row in getattr(plain_words, name).items()
                }
                for name in plain_words.PUBLISHED
            }
        )

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
        identity_ready = oidc is not None or (settings.auth_mode == "demo" and demo_allowed) or (settings.auth_mode == "testnet_tokens" and bool(settings.api_key))
        ready_value = db_ready and provider_configured and frappe_configured and identity_ready
        response = {
            "status": "ready" if ready_value else "not_ready",
            "database": db_ready,
            "identity_configured": identity_ready,
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

    @app.get("/attention", tags=["health"], dependencies=[Depends(require_api_key)])
    def attention() -> dict[str, Any]:
        """Everything waiting on a person, and the alerts the last pass raised.

        Read-only. It reads the same snapshot the metrics are rendered from, so this view and a
        scraper cannot disagree about what is wrong. Nothing here resolves anything: acting on an
        item still goes through the same approval or reconciliation path as by hand.
        """
        return build_attention(workflow)

    @app.get("/setup", tags=["health"], dependencies=[Depends(require_api_key)])
    def setup() -> dict[str, Any]:
        """What this deployment still needs, and what stops working without it.

        Offline and free. A secret is reported as set or missing and never in full, and a URL is
        reported as its host with the path removed, because the RPC endpoints handed out with this
        project carry a token in the path.
        """
        body = inventory(settings)
        simulated = settings.payment_provider == "mock" and settings.accounting_provider == "mock"
        body["deployment"] = {
            "mode": "demo" if simulated else "mixed" if "mock" in {settings.payment_provider, settings.accounting_provider} else "testnet",
            "payment_provider": settings.payment_provider,
            "accounting_provider": settings.accounting_provider,
            "screening_provider": settings.screening_provider,
            "demo_available": simulated and settings.screening_provider == "fixture" and settings.decision_layer != "dual_process",
        }
        limits = getattr(workflow.payment_provider, "guard_limits", None)
        try:
            guard = limits() if limits else None
        except Exception:
            guard = None
        can_pause = hasattr(workflow.payment_provider, "set_guard_paused")
        if guard:
            body["guard"] = {
                "paused": bool(guard.get("paused", 0)),
                "can_pause": can_pause,
            }
        elif can_pause:
            body["guard"] = {
                "paused": bool(getattr(workflow.payment_provider, "_guard_paused", False)),
                "can_pause": True,
            }
        return body

    @app.post("/demo/start", tags=["console"], dependencies=[Depends(require_api_key)])
    def start_demo() -> dict[str, Any]:
        """Seed an empty, fully simulated deployment. Never reset records or submit a payment."""
        from .mock_adapters import MockAccountingConnector, MockPaymentProvider
        from .seed import seed_demo

        if not (
            settings.payment_provider == "mock"
            and settings.accounting_provider == "mock"
            and settings.screening_provider == "fixture"
            and settings.decision_layer != "dual_process"
            and isinstance(accounting, MockAccountingConnector)
            and isinstance(payment_provider, MockPaymentProvider)
        ):
            raise HTTPException(status_code=409, detail={
                "code": "demo_unavailable",
                "message": "The guided demo requires mock payment and accounting providers, fixture screening and no external planner. No configuration was changed.",
            })
        if store.list_invoices():
            return {"seeded": False, "reason": "Existing records were preserved."}
        if store.has_fixtures():
            raise HTTPException(status_code=409, detail={
                "code": "demo_records_exist",
                "message": "Local accounting fixtures already exist. Use a fresh demo database; nothing was overwritten.",
            })
        legitimate, suspicious = seed_demo(
            store,
            invoice_currency=settings.frappe_invoice_currency or "USD",
            settlement_to_invoice_rate=settings.settlement_to_invoice_rate,
        )
        return {"seeded": True, "invoice_ids": [legitimate, suspicious]}

    @app.post("/setup/checks", tags=["health"], dependencies=[Depends(require_api_key)])
    def setup_checks() -> dict[str, Any]:
        """Probe the ledger and the chain. Reads only: nothing is signed, sent or written."""
        return live_checks(workflow, settings)

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

    @app.post("/invoices/upload", status_code=status.HTTP_201_CREATED, tags=["invoices"], dependencies=[Depends(require_api_key)])
    def upload_invoice(
        body: InvoiceUploadInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        """Ingest raw invoice document with cryptographic isolation against prompt injection."""
        key = validate_idempotency_key(idempotency_key)
        raw_bytes = body.document_content.encode("utf-8")
        doc_hash = hashlib.sha256(raw_bytes).hexdigest()

        lines = body.lines
        if not lines:
            lines = [InvoiceLineInput(item_code="STANDARD-LINE-ITEM", quantity=Decimal("1"), amount=body.amount)]

        create_body = InvoiceCreateInput(
            supplier_id=body.supplier_id,
            invoice_number=body.invoice_number,
            invoice_date=body.invoice_date,
            due_date=body.due_date,
            amount=body.amount,
            currency=body.currency,
            invoice_payee_address=body.invoice_payee_address,
            lines=lines,
            purchase_invoice_id=body.purchase_invoice_id,
            purchase_order_ids=body.purchase_order_ids,
            receipt_ids=body.receipt_ids,
            payment_terms=body.payment_terms,
            source_document_hash=doc_hash,
            untrusted_text=body.document_content,
        )
        invoice = _domain_invoice(create_body)
        created, is_new = workflow.create_invoice(invoice, key)
        response = workflow.get_invoice(created.id)
        response["created"] = is_new
        response["document_hash"] = doc_hash
        response["prompt_defense"] = {
            "quarantined": True,
            "source_text_hash": invoice.source_text_hash,
            "isolation": "Untrusted text hashed and isolated from payment authorization",
        }
        return response

    @app.get("/invoices/{invoice_id}", tags=["invoices"], dependencies=[Depends(require_api_key)])
    def get_invoice(invoice_id: str) -> dict[str, Any]:
        return workflow.get_invoice(invoice_id)

    @app.post("/invoices/{invoice_id}/evaluate", tags=["workflow"], dependencies=[Depends(require_api_key)])
    def evaluate(invoice_id: str) -> dict[str, Any]:
        return workflow.evaluate(invoice_id)

    @app.post("/invoices/{invoice_id}/link", tags=["workflow"], dependencies=[Depends(require_api_key), Depends(require_human_approval_token)])
    def link_invoice(request: Request, invoice_id: str, body: InvoiceLinkInput) -> dict[str, Any]:
        """Match a captured invoice to an ERPNext payable record.

        This is the only way a captured invoice becomes payable, and it adopts the accounting
        record's amount, currency and lines. Approving the captured document itself never does.
        """
        return workflow.link_to_purchase_invoice(invoice_id, body.purchase_invoice_id, reviewer(request, body.reviewer))

    @app.post("/invoices/{invoice_id}/approval", tags=["workflow"], dependencies=[Depends(require_api_key), Depends(require_human_approval_token)])
    def approve(request: Request, invoice_id: str, body: ApprovalInput) -> dict[str, Any]:
        record = body.model_dump()
        record["reviewer"] = reviewer(request, body.reviewer)
        if oidc is not None:
            record["reviewer_identity"] = request.state.identity.record()
        return workflow.approve(invoice_id, record)

    @app.post("/invoices/{invoice_id}/payment", tags=["payments"], dependencies=[Depends(require_api_key)])
    def submit_payment(request: Request, invoice_id: str) -> dict[str, Any]:
        return workflow.submit_payment(invoice_id, authorization_identity=request.state.identity.record() if oidc else None,
                                       authorization_session_hash=request.state.session_hash if oidc else None)

    @app.post("/invoices/{invoice_id}/payment/reconcile", tags=["payments"], dependencies=[Depends(require_api_key)])
    def reconcile_payment(request: Request, invoice_id: str) -> dict[str, Any]:
        return workflow.submit_payment(invoice_id, authorization_identity=request.state.identity.record() if oidc else None,
                                       authorization_session_hash=request.state.session_hash if oidc else None)

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
        The guard's current budgets ride along so the treasury screen can show the hard caps
        beside the coverage they constrain.
        """
        body = build_forecast(workflow, days=max(1, min(days, 365))).to_dict()
        limits = getattr(workflow.payment_provider, "guard_limits", None)
        try:
            guard = limits() if limits else None
        except Exception:
            guard = None
        if guard:
            epoch_spent = 0
            reader = getattr(workflow.payment_provider, "guard_epoch_spent", None)
            try:
                epoch_spent = int(reader() or 0) if reader else 0
            except Exception:
                epoch_spent = 0
            body["guard_caps_usdc"] = {
                "per_payment": guard.get("per_payment_cap", 0) / 10**6,
                "epoch": guard.get("epoch_cap", 0) / 10**6,
                "recipient_epoch": guard.get("recipient_epoch_cap", 0) / 10**6,
                "epoch_days": round(guard.get("epoch_length", 0) / 86400, 2),
                "epoch_spent": epoch_spent / 10**6,
                "paused": bool(guard.get("paused", 0)),
            }
        return body

    @app.get("/plan", tags=["planning"], dependencies=[Depends(require_api_key)])
    def payment_plan() -> dict[str, Any]:
        """Which payable to pay first, given the treasury balance and the reserve floor.

        Read-only advice: it evaluates each invoice without recording a decision, and every
        invoice it ranks must still pass its own policy checks and settle through the same
        guarded payment path.
        """
        prioritiser = PaymentPrioritiser(workflow, planner=build_order_planner(settings))
        return prioritiser.plan().to_dict()

    @app.get("/plans", tags=["planning"], dependencies=[Depends(require_api_key)])
    def recorded_payment_plans() -> dict[str, Any]:
        """Recent durable worker advice and execution outcomes. Reading never executes a plan."""
        plans = workflow.store.payment_plan_history()
        return {"plans": plans, "count": len(plans)}

    @app.get("/payments", tags=["audit"], dependencies=[Depends(require_api_key)])
    def list_payments_endpoint(confirmation: str | None = None, ledger: str | None = None) -> dict[str, Any]:
        """Every payment the agent authorized, newest first, with what became of each one.

        The settlement log. A payment that failed is in here too, with the reason, because a log
        that only records successes cannot answer the question an operator actually has.
        """
        return list_payments(workflow, confirmation=confirmation, ledger=ledger)

    @app.get("/payments/export", tags=["audit"], dependencies=[Depends(require_api_key)])
    def export_payments(confirmation: str | None = None, ledger: str | None = None) -> Response:
        """The settlement log as a CSV an accountant can open.

        Same rows as GET /payments, flattened one per line. Amounts are plain decimals, the recipient
        and transaction hash are included for reconciliation against the chain, and the outcome
        column says in one word whether this payment succeeded. Read-only; it never invents a row.
        """
        import csv
        import io

        log = list_payments(workflow, confirmation=confirmation, ledger=ledger)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "invoice_number", "supplier_id", "purchase_invoice_id", "amount_usdc", "recipient",
            "outcome", "confirmation_status", "ledger_status", "fee_usdc", "transaction_hash",
            "payment_entry", "fee_entry",
        ])
        for row in log["payments"]:
            invoice = workflow.store.get_invoice(row["invoice_id"])
            writer.writerow([
                row["invoice_number"], row["supplier_id"], (invoice.purchase_invoice_id if invoice else ""),
                row["amount_usdc"], row["recipient"] or "",
                row["outcome"]["code"], row["confirmation_status"], row["erp_status"] or "",
                row["fee_usdc"] or "", row["transaction_hash"] or "",
                row["erp_entry_id"] or "", row["erp_fee_entry_id"] or "",
            ])
        return Response(
            buffer.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="arc-payables-settlements.csv"'},
        )

    @app.get("/payments/{reference}", tags=["audit"], dependencies=[Depends(require_api_key)])
    def payment_report_endpoint(reference: str) -> dict[str, Any]:
        """One payment's whole story: decision, authorization, submission, settlement, ledger.

        Reachable by invoice id, invoice number, payment id or transaction hash, because the thing
        an operator usually holds is a hash from a block explorer. The raw permit signature is not
        returned; whether it verifies against the configured signer is.
        """
        try:
            return payment_report(workflow, reference)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail={"code": "payment_not_found", "message": str(exc)}) from exc

    @app.post("/payments/{reference}/verify", tags=["audit"], dependencies=[Depends(require_api_key)])
    def verify_payment_endpoint(reference: str) -> dict[str, Any]:
        """Ask the provider and the chain again about one settlement. Reads only; sends nothing."""
        try:
            return verify_payment(workflow, reference)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail={"code": "payment_not_found", "message": str(exc)}) from exc

    @app.get("/audit/verify", tags=["audit"], dependencies=[Depends(require_api_key)])
    def verify_audit() -> dict[str, Any]:
        """Recompute the audit chain, so a reviewer can tell the record was not rewritten."""
        return store.verify_audit_chain()

    @app.post("/reconciliation/run", tags=["audit"], dependencies=[Depends(require_api_key)])
    def run_reconciliation() -> dict[str, Any]:
        """Compare every recorded payment with what the guard settled on chain. Reads only.

        An unreadable chain is refused rather than reported as clean: no comparison happened, and
        that is a different answer from "nothing is wrong".
        """
        from .reconcile import ReconciliationUnavailable, reconcile

        try:
            return reconcile(store, settings).to_dict()
        except ReconciliationUnavailable as exc:
            raise HTTPException(status_code=503, detail={"code": "chain_unreadable", "message": str(exc)}) from exc

    @app.post("/guard/pause", tags=["operations"], dependencies=[Depends(require_api_key)])
    def pause_guard() -> dict[str, Any]:
        """Emergency stop: pause the payment guard contract to halt all settlements immediately."""
        setter = getattr(workflow.payment_provider, "set_guard_paused", None)
        if setter is None:
            raise HTTPException(
                status_code=501,
                detail={"code": "guard_pause_unsupported", "message": "The active payment provider does not support remote guard pause"},
            )
        try:
            tx = setter(True)
            broadcaster.broadcast("guard_paused", {"paused": True})
            return {"ok": True, "paused": True, "transaction": tx if isinstance(tx, str) else None}
        except Exception as exc:
            raise HTTPException(status_code=500, detail={"code": "guard_pause_failed", "message": str(exc)}) from exc

    @app.post("/guard/unpause", tags=["operations"], dependencies=[Depends(require_api_key)])
    def unpause_guard() -> dict[str, Any]:
        """Resume settlements through the payment guard contract."""
        setter = getattr(workflow.payment_provider, "set_guard_paused", None)
        if setter is None:
            raise HTTPException(
                status_code=501,
                detail={"code": "guard_pause_unsupported", "message": "The active payment provider does not support remote guard pause"},
            )
        try:
            tx = setter(False)
            broadcaster.broadcast("guard_unpaused", {"paused": False})
            return {"ok": True, "paused": False, "transaction": tx if isinstance(tx, str) else None}
        except Exception as exc:
            raise HTTPException(status_code=500, detail={"code": "guard_unpause_failed", "message": str(exc)}) from exc

    @app.get("/events/stream", tags=["operations"])
    def events_stream(
        request: Request,
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
        api_key: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=0, le=1000)] = 0,
    ) -> StreamingResponse:
        """Server-Sent Events stream for real-time console updates."""
        require_api_key(request, x_api_key=x_api_key, api_key_query=api_key)
        q = broadcaster.subscribe()

        def generator():
            sent = 0
            try:
                yield f"event: connect\ndata: {json.dumps({'connected': True, 'time': utcnow().isoformat()})}\n\n"
                sent += 1
                if limit and sent >= limit:
                    return
                while True:
                    try:
                        event = q.get(timeout=10.0)
                        yield f"event: update\ndata: {json.dumps(event)}\n\n"
                        sent += 1
                        if limit and sent >= limit:
                            return
                    except queue.Empty:
                        yield f"event: ping\ndata: {json.dumps({'time': utcnow().isoformat()})}\n\n"
                        sent += 1
                        if limit and sent >= limit:
                            return
            finally:
                broadcaster.unsubscribe(q)

        return StreamingResponse(generator(), media_type="text/event-stream")

    @app.get("/backups", tags=["operations"], dependencies=[Depends(require_api_key)])
    def list_backup_files() -> dict[str, Any]:
        """Backups held on this host, newest first. A file without a manifest is shown as incomplete."""
        from .backup import list_backups

        rows = list_backups(settings.backup_directory)
        return {"backups": rows, "count": len(rows)}

    @app.post("/backups", status_code=status.HTTP_201_CREATED, tags=["operations"], dependencies=[Depends(require_api_key)])
    def create_backup_now() -> dict[str, Any]:
        """Back up the live database locally, verify the copy, then publish it with its manifest.

        Local only. An off-host copy is the CLI's --hook, which is deliberately not reachable from
        here: nothing leaves the host because a button was pressed.
        """
        from .backup import BackupError, create_backup, data_reaches

        try:
            result = create_backup(settings.database_path, settings.backup_directory, keep=settings.backup_keep)
        except BackupError as exc:
            raise HTTPException(status_code=409, detail={"code": "backup_failed", "message": str(exc)}) from exc
        manifest = result.manifest
        return {
            "name": result.path.name,
            "size_bytes": manifest.get("size_bytes"),
            "sha256": manifest.get("sha256"),
            "data_reaches": data_reaches((manifest.get("database") or {}).get("recovery_point")),
            "pruned": manifest.get("pruned", []),
        }

    @app.post("/backups/restore-drill", tags=["operations"], dependencies=[Depends(require_api_key)])
    def run_restore_drill(body: RestoreDrillInput) -> dict[str, Any]:
        """Restore one held backup into a disposable file and prove the copy is usable.

        The backup is chosen from the listing, never by a path, and the drill cannot target the live
        database. Nothing in the live store changes.
        """
        from .backup import BackupError, list_backups, restore_drill
        from .domain import utcnow

        names = {row["name"] for row in list_backups(settings.backup_directory)}
        if body.backup not in names:
            raise HTTPException(status_code=404, detail={"code": "backup_not_found", "message": "No held backup has that name."})
        scratch = settings.backup_directory / "drills" / f"restore-{utcnow():%Y%m%dT%H%M%S%fZ}.sqlite3"
        try:
            drill = restore_drill(settings.backup_directory / body.backup, scratch, live_database=settings.database_path)
        except BackupError as exc:
            raise HTTPException(status_code=409, detail={"code": "restore_drill_failed", "message": str(exc)}) from exc
        # The drill's own report names filesystem paths. The console needs the verdict, not the host layout.
        return {
            "ok": drill["ok"],
            "name": body.backup,
            "restore_seconds": drill["restore_seconds"],
            "checked_at": drill["checked_at"],
            "manifest_created_at": drill.get("manifest_created_at"),
            "checkpoint": drill.get("checkpoint"),
            "counts": (drill.get("database") or {}).get("counts"),
        }

    @app.get("/receivables", tags=["planning"], dependencies=[Depends(require_api_key)])
    def list_open_receivables_endpoint() -> dict[str, Any]:
        """Expected inflows not yet collected, in expected-date order. They inform the forecast only."""
        rows = store.list_open_receivables()
        return {
            "receivables": [{**row, "amount_usdc": row["amount_units"] / 10**6} for row in rows],
            "count": len(rows),
        }

    @app.post("/receivables/sync", tags=["planning"], dependencies=[Depends(require_api_key)])
    def sync_receivables() -> dict[str, Any]:
        """Read open Sales Invoices from the accounting system and record them as expected inflows.

        Re-running this replaces a row by its external id, so it never double counts. Nothing here
        authorizes or moves anything.
        """
        try:
            receivables = accounting.list_receivables()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "accounting_unreadable", "message": "The accounting system could not be read. Nothing was recorded."},
            ) from exc
        for receivable in receivables:
            store.record_receivable({
                "external_id": receivable.external_id,
                "customer": receivable.customer,
                "reference": receivable.reference,
                "amount_units": receivable.amount_units,
                "currency": receivable.currency,
                "expected_date": receivable.expected_date.isoformat(),
                "source": receivable.source,
            })
        return {"recorded": len(receivables)}

    @app.post("/receivables/{external_id}/collect", tags=["planning"], dependencies=[Depends(require_api_key)])
    def collect_receivable(external_id: str) -> dict[str, Any]:
        """Stop counting an expected inflow once its money has arrived."""
        if not store.mark_receivable_collected(external_id):
            raise HTTPException(status_code=404, detail={"code": "receivable_not_found", "message": "No open receivable has that id. Nothing changed."})
        return {"collected": external_id}

    @app.post("/alerts/test", tags=["operations"], dependencies=[Depends(require_api_key)])
    def send_test_alert() -> dict[str, Any]:
        """Send one clearly labelled test alert through the sink the worker uses.

        The destination is reported as its host only: a webhook URL can carry a credential in its path.
        """
        from urllib.parse import urlsplit

        from .alerting import WARNING, Alert, WebhookSink

        url = settings.alert_webhook_url
        if not url:
            raise HTTPException(
                status_code=409,
                detail={"code": "alert_destination_missing", "message": "No alert destination is configured (ALERT_WEBHOOK_URL). Alerts are still recorded on each pass, but no person is told."},
            )
        host = urlsplit(url).hostname or "configured destination"
        alert = Alert(
            code="delivery_test",
            severity=WARNING,
            summary="test alert: alert delivery is working",
            detail={"source": "operator console", "note": "Sent on request to verify that alerts reach this destination."},
        )
        failures = WebhookSink(url, min_interval_seconds=0, timeout=10.0).send([alert])
        if failures:
            reason = str(failures[0].get("error", "unknown error")).replace(url, host)
            return {"delivered": False, "destination": host, "error": reason}
        return {"delivered": True, "destination": host}

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
