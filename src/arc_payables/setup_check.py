"""What a deployment still needs, in the operator's words.

Configuration lives in environment variables, and a missing one is usually discovered at the moment a
payment is refused. This turns the facts the servers already decide on into a list a person can read:
every setting that matters, whether it is set, and what stops working without it.

Two rules hold here. A secret is reported as set or missing and never in full, and a URL is reported
as its host with the path removed, because the RPC endpoints handed out by Arc's tooling carry a
token in the path and a setup screen is a bad place to leak one.

The live probes are deliberately separate from the inventory. Reading the inventory is free and
offline; probing the ledger or the chain reaches the world and can fail for reasons that are not
configuration at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

MAX_DISPLAY = 80


@dataclass(frozen=True)
class Requirement:
    """One setting and the consequence of not having it."""

    env: str
    attribute: str
    breaks: str
    kind: str = "text"
    """``text``, ``number``, ``bool``, ``url`` or ``secret``. Only ``url`` and ``secret`` are ever
    hidden, and they are hidden because they routinely carry a credential."""

    demo_default: Any = None
    """The value shipped as a demo example. Flagged rather than treated as a mistake."""

    required: bool = True
    """False when the selected providers make this unnecessary. A missing optional value is shown,
    because an operator should see it, but it does not make the deployment incomplete."""


@dataclass(frozen=True)
class Group:
    name: str
    summary: str
    requirements: tuple[Requirement, ...]


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _state(requirement: Requirement, value: Any) -> str:
    if requirement.kind == "secret":
        if _present(value):
            return "set"
        return "missing" if requirement.required else "not_needed"
    if not _present(value):
        return "missing" if requirement.required else "not_needed"
    if requirement.demo_default is not None and str(value) == str(requirement.demo_default):
        return "demo_default"
    return "set"


def _display(requirement: Requirement, value: Any) -> Any:
    if requirement.kind == "secret" or not _present(value):
        return None
    if requirement.kind == "url":
        parts = urlsplit(str(value))
        return f"{parts.scheme}://{parts.netloc}/" if parts.netloc else "<unparseable url>"
    return str(value)[:MAX_DISPLAY]


def _rows(group: Group, settings: Any) -> list[dict[str, Any]]:
    rows = []
    for requirement in group.requirements:
        value = getattr(settings, requirement.attribute, None)
        if (requirement.env == "AUTH_MODE" and value == "demo" and
                (settings.payment_provider != "mock" or settings.accounting_provider != "mock")):
            value = None  # Demo credentials cannot open an external-provider deployment.
        rows.append(
            {
                "env": requirement.env,
                "state": _state(requirement, value),
                "value": _display(requirement, value),
                "breaks": requirement.breaks,
            }
        )
    return rows


def _accounting_group(settings: Any) -> Group:
    if settings.accounting_provider == "frappe":
        return Group(
            "Accounting (ERPNext)",
            "The payable, the supplier wallet and the accounts the writeback posts to.",
            (
                Requirement("ACCOUNTING_PROVIDER", "accounting_provider", "The payable would be read from mock records."),
                Requirement("FRAPPE_URL", "frappe_url", "No payable can be read and nothing can be written back.", kind="url"),
                Requirement("FRAPPE_API_KEY", "frappe_api_key", "The connector cannot authenticate.", kind="secret"),
                Requirement("FRAPPE_API_SECRET", "frappe_api_secret", "The connector cannot authenticate.", kind="secret"),
                Requirement("FRAPPE_COMPANY", "frappe_company", "The writeback has no company to post under."),
                Requirement("FRAPPE_PAID_FROM_ACCOUNT", "frappe_paid_from_account", "The settlement leg has no account."),
                Requirement("FRAPPE_PAID_TO_ACCOUNT", "frappe_paid_to_account", "The payable leg has no account."),
                Requirement("FRAPPE_MODE_OF_PAYMENT", "frappe_mode_of_payment", "ERPNext refuses a Payment Entry without one."),
                Requirement("FRAPPE_FEE_ACCOUNT", "frappe_fee_account", "The network fee could not be expensed separately."),
                Requirement("FRAPPE_COST_CENTER", "frappe_cost_center", "ERPNext refuses an expense row without a cost centre."),
                Requirement("FRAPPE_SETTLEMENT_CURRENCY", "frappe_settlement_currency", "The settlement leg currency is unknown."),
                Requirement("FRAPPE_COMPANY_CURRENCY", "frappe_company_currency", "Amounts could not be checked against the ledger."),
                Requirement("FRAPPE_INVOICE_CURRENCY", "frappe_invoice_currency", "A payable in another currency would be refused, not converted."),
                Requirement("FRAPPE_FEE_CURRENCY", "frappe_fee_currency", "The fee account currency is unknown."),
                Requirement("FRAPPE_SOURCE_EXCHANGE_RATE", "frappe_source_exchange_rate", "Company currency per USDC is unstated, so amounts are not comparable."),
                Requirement("FRAPPE_TARGET_EXCHANGE_RATE", "frappe_target_exchange_rate", "The party-currency rate is unstated."),
            ),
        )
    return Group(
        "Accounting (mock)",
        "Nothing to configure: the connector reads the records in the local database.",
        (
            Requirement("ACCOUNTING_PROVIDER", "accounting_provider", "The payable would be read from ERPNext."),
        ),
    )


def _settlement_group(settings: Any) -> Group:
    backend = (getattr(settings, "signer_backend", "env") or "env").lower()
    if backend == "pkcs11":
        return Group(
            "Settlement (managed PKCS#11 policy key)",
            "The policy key lives on a PKCS#11 token and cannot be exported. Real HSM for production; SoftHSM2 is a test double only.",
            (
                Requirement("SIGNER_BACKEND", "signer_backend", "No signing backend is selected."),
                Requirement("PKCS11_LIB_PATH", "pkcs11_lib_path", "No PKCS#11 library is configured, so the token cannot be reached."),
                Requirement("PKCS11_SLOT", "pkcs11_slot", "No token slot is pinned; several tokens would be ambiguous.", required=False),
                Requirement("PKCS11_KEY_LABEL", "pkcs11_key_label", "No signing key is named, so no permit can be signed."),
                Requirement("PKCS11_KEY_ID_HEX", "pkcs11_key_id_hex", "No key id is configured."),
                Requirement("PKCS11_USER_PIN", "pkcs11_user_pin", "No token login is configured, so the key cannot be used.", kind="secret"),
                Requirement("PERMIT_SIGNING_ADDRESS", "permit_signing_address", "No expected signing address is pinned; a wrong key would not be noticed at startup.", required=False),
            ),
        )
    common = (
        Requirement("PERMIT_SIGNING_PRIVATE_KEY", "permit_signing_private_key", "No permit can be signed, so no guarded payment can be authorized.", kind="secret"),
        Requirement("PERMIT_SIGNING_ADDRESS", "permit_signing_address", "No expected signing address is pinned; a wrong key would not be noticed at startup.", required=False),
    )
    if settings.payment_provider == "circle":
        return Group(
            "Settlement (Circle, developer-controlled wallet)",
            "The payer key stays with Circle. The guard's caps bind it exactly as they bind anything else.",
            (
                Requirement("PAYMENT_PROVIDER", "payment_provider", "The payer would be the local key instead."),
                Requirement("CIRCLE_API_KEY", "circle_api_key", "Circle cannot be reached to submit the call.", kind="secret"),
                Requirement("CIRCLE_ENTITY_SECRET", "circle_entity_secret", "Circle refuses every mutating request without it.", kind="secret"),
                Requirement("CIRCLE_WALLET_ID", "circle_wallet_id", "The payer wallet cannot be identified."),
                Requirement("CIRCLE_WALLET_ADDRESS", "circle_wallet_address", "The permit's payer would not match the wallet Circle holds."),
                Requirement("CIRCLE_GUARD_ADDRESS", "circle_guard_address", "There is no budget to enforce, so nothing would be paid."),
                Requirement("CIRCLE_RPC_URL", "circle_rpc_url", "The chain cannot be read to confirm the settlement.", kind="url"),
                *common,
            ),
        )
    if settings.payment_provider == "local":
        return Group(
            "Settlement (local key)",
            "The payer key is held on this machine. Arc Testnet only, and the same guard caps apply.",
            (
                Requirement("PAYMENT_PROVIDER", "payment_provider", "The payer would be the mock provider."),
                Requirement("LOCAL_PAYMENT_PRIVATE_KEY", "local_payment_private_key", "Nothing can be signed, so nothing can be sent.", kind="secret"),
                Requirement("LOCAL_PAYMENT_GUARD_ADDRESS", "local_payment_guard_address", "There is no budget to enforce, so nothing would be paid."),
                Requirement("LOCAL_PAYMENT_RPC_URL", "local_payment_rpc_url", "The chain cannot be reached to send or confirm.", kind="url"),
                *common,
            ),
        )
    return Group(
        "Settlement (mock)",
        "Nothing to configure: the provider settles against the local database and moves no money.",
        (
            Requirement("PAYMENT_PROVIDER", "payment_provider", "Live settlement would need a real provider and its credentials."),
        ),
    )


def _screening_group(settings: Any) -> Group:
    if settings.screening_provider == "opensanctions":
        return Group(
            "Screening (OpenSanctions)",
            "Counterparty risk against public sanction and PEP lists. The result scales the automatic limit.",
            (
                Requirement("SCREENING_PROVIDER", "screening_provider", "Screening would never run."),
                Requirement("OPENSANCTIONS_API_KEY", "opensanctions_api_key", "Every invoice escalates for a human, because screening cannot complete.", kind="secret"),
                Requirement("OPENSANCTIONS_DATASET", "opensanctions_dataset", "Screening would query the default collection."),
                Requirement("SCREENING_MEDIUM_TIER_HANDLING", "screening_medium_tier_handling", "An unclear result requires a human rather than a reduced limit."),
            ),
        )
    return Group(
        "Screening (fixture or unavailable)",
        "Deterministic local answers, or none at all. A live deployment wants a real provider here.",
        (
            Requirement("SCREENING_PROVIDER", "screening_provider", "No counterparty would be screened against public lists."),
        ),
    )


def _limits_group(settings: Any) -> Group:
    return Group(
        "Limits and policy",
        "What the agent may do without a person. The guard enforces its own copy of the budget on chain.",
        (
            Requirement("MAX_INVOICE_USDC", "max_invoice_usdc", "There would be no automatic limit, so nothing is paid unattended.", kind="number", demo_default="1000"),
            Requirement("MIN_RESERVE_USDC", "min_reserve_usdc", "No reserve would be preserved.", kind="number", demo_default="2000"),
            Requirement("PAYMENT_DUE_WINDOW_DAYS", "payment_due_window_days", "Payment timing would follow the default window.", kind="number"),
            Requirement("PERMIT_LIFETIME_SECONDS", "permit_lifetime_seconds", "A permit would live for the default period.", kind="number"),
            Requirement("DISCOUNT_MIN_PERCENT", "discount_min_percent", "Every early-payment discount would be considered.", kind="number"),
        ),
    )


def _access_group(settings: Any) -> Group:
    external = settings.payment_provider != "mock" or settings.accounting_provider != "mock"
    mode = getattr(settings, "auth_mode", "demo")
    if mode == "oidc":
        return Group("Access", "Individual, MFA-verified identities and server-assigned roles. Shared tokens are ignored.", (
            Requirement("AUTH_MODE", "auth_mode", "External access requires individual identity or explicit testnet-token mode."),
            Requirement("OIDC_ISSUER", "oidc_issuer", "No trusted identity provider is configured.", kind="url"),
            Requirement("OIDC_CLIENT_ID", "oidc_client_id", "The registered client audience cannot be verified."),
            Requirement("OIDC_REDIRECT_URI", "oidc_redirect_uri", "No canonical browser origin or callback is configured.", kind="url"),
            Requirement("OIDC_SUBJECT_ROLES", "oidc_subject_roles", "No individual has an assigned role.", kind="secret"),
            Requirement("OIDC_MFA_CLAIM", "oidc_mfa_claim", "The IdP's MFA proof cannot be checked."),
            Requirement("OIDC_MFA_VALUES", "oidc_mfa_values", "The approved MFA assurance values are unknown."),
            Requirement("OIDC_CLIENT_SECRET", "oidc_client_secret", "Required only for a registered confidential client.", kind="secret", required=False),
        ))
    external = external or mode == "testnet_tokens"
    return Group(
        "Access",
        "Shared credentials are demo/testnet-only, not individual production identity. External providers require an explicit auth mode.",
        (
            Requirement("AUTH_MODE", "auth_mode", "Demo mode blocks external-provider access; configure OIDC or explicit testnet-token mode."),
            Requirement("API_KEY", "api_key", "The API refuses to start an external integration without it, so nothing external would run.", kind="secret", required=external),
            Requirement("APPROVAL_TOKEN", "approval_token", "Human approval is disabled outright, so an escalated invoice cannot be cleared.", kind="secret", required=external),
        ),
    )


def _decision_group(settings: Any) -> Group:
    layer = Requirement("DECISION_LAYER", "decision_layer", "The advisory layer would follow the default.")
    if settings.decision_layer == "dual_process":
        return Group(
            "Advisory layer (planner)",
            "A bounded planner for genuine trade-offs. It may reorder or opine, never authorize.",
            (
                layer,
                Requirement("PLANNER_BASE_URL", "planner_base_url", "No planner is called, so the layer falls back to the fast path.", kind="url"),
                Requirement("PLANNER_MODEL", "planner_model", "A planner URL without a model cannot be called."),
                Requirement("PLANNER_API_KEY", "planner_api_key", "The planner endpoint would be called without credentials.", kind="secret"),
            ),
        )
    return Group(
        "Advisory layer",
        "The deterministic policy decides. This layer may reorder a queue or explain, and can never authorize.",
        (layer,),
    )


def _worker_group(settings: Any) -> Group:
    return Group(
        "Worker and alerting",
        "What the unattended loop does, and where it shouts when something is wrong.",
        (
            Requirement("WORKER_INTERVAL_SECONDS", "worker_interval_seconds", "The loop would use the default interval.", kind="number"),
            Requirement("WORKER_AUTOPAY", "worker_autopay", "Paying unattended would follow the default, which is off.", kind="bool"),
            Requirement("WORKER_INTAKE", "worker_intake", "Discovery would follow the default, which is on.", kind="bool"),
            Requirement("WORKER_MAX_ACTIONS_PER_PASS", "worker_max_actions_per_pass", "A pass would use the default ceiling.", kind="number"),
            Requirement("ALERT_WEBHOOK_URL", "alert_webhook_url", "Alerts are still recorded and shown, but nobody is told.", kind="url", required=False),
        ),
    )


def _database_group(settings: Any) -> Group:
    return Group(
        "Storage",
        "Where the evidence, the payments and the audit chain live.",
        (
            Requirement("DATABASE_PATH", "database_path", "There would be nowhere to record anything.", kind="text"),
        ),
    )


def inventory(settings: Any) -> dict[str, Any]:
    """Every setting that matters, grouped, with what breaks without it and no secret values."""
    groups = [
        _database_group(settings),
        _access_group(settings),
        _accounting_group(settings),
        _settlement_group(settings),
        _screening_group(settings),
        _limits_group(settings),
        _decision_group(settings),
        _worker_group(settings),
    ]
    rendered = []
    missing: list[str] = []
    flagged: list[str] = []
    for group in groups:
        rows = _rows(group, settings)
        for requirement, row in zip(group.requirements, rows):
            if row["state"] == "missing" and requirement.required:
                missing.append(row["env"])
            elif row["state"] == "demo_default":
                flagged.append(row["env"])
        rendered.append(
            {
                "name": group.name,
                "summary": group.summary,
                "complete": not any(
                    row["state"] == "missing" and requirement.required
                    for requirement, row in zip(group.requirements, rows)
                ),
                "requirements": rows,
            }
        )
    return {
        "ready": not missing,
        "missing": missing,
        "demo_defaults": flagged,
        "groups": rendered,
    }


def live_checks(workflow: Any, settings: Any, *, rpc: Any | None = None) -> dict[str, Any]:
    """Probe what can only be confirmed against the world. Read-only; nothing is signed or sent."""
    from .verify_arc import RpcClient, provider_view, verify

    checks: list[dict[str, Any]] = []

    provider = workflow.payment_provider
    prober = getattr(provider, "verify_configuration", None)
    if prober is None:
        prober = getattr(provider, "assert_arc_testnet", None)
    if prober is None:
        checks.append(
            {
                "name": "payment provider",
                "ok": True,
                "detail": f"{settings.payment_provider} settles against the local database; there is nothing to reach.",
            }
        )
    else:
        try:
            prober()
            checks.append(
                {
                    "name": "payment provider",
                    "ok": True,
                    "detail": (
                        "the wallet and the guard answered, and the permit signer matches the guard's policy signer"
                        if settings.payment_provider == "circle"
                        else "the configured RPC is Arc Testnet"
                    ),
                }
            )
        except Exception as exc:
            checks.append({"name": "payment provider", "ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]})

    checks.append(_chain_check(settings, rpc=rpc, provider_view=provider_view, verify=verify))
    checks.append(_accounting_check(workflow, settings))

    return {"ok": all(check["ok"] for check in checks), "checks": checks}


def _chain_check(settings: Any, *, rpc: Any, provider_view: Any, verify: Any) -> dict[str, Any]:
    from .verify_arc import RpcClient

    try:
        view = provider_view(settings)
        # The caller can inject a client; a real one is built from the configured endpoint otherwise.
        rpc = rpc if rpc is not None else RpcClient(view.rpc_url)
        signer_address = (getattr(settings, "permit_signing_address", None) or "").strip() or None
        if signer_address is None and settings.permit_signing_private_key:
            from .security import EIP712PermitSigner

            signer_address = EIP712PermitSigner(settings.permit_signing_private_key).address
        ok, findings = verify(rpc, settings, signer_address=signer_address, view=view)
    except Exception as exc:
        return {"name": "arc testnet and guard", "ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}
    # ``verify`` answers a bigger question than this screen asks: it reports readiness for a live
    # payment, which also depends on the inventory above. Here the question is only whether the
    # chain and the guard answered and matched, so a refusal is what fails and a caveat is a note.
    failures = [line for line in findings if line.startswith("FAIL")]
    notes = [line for line in findings if line.startswith("TODO")]
    if failures:
        detail = "; ".join(failures)
    elif notes:
        detail = "; ".join(notes)
    else:
        detail = f"{len(findings)} chain and guard checks passed, nothing to report"
    return {"name": "arc testnet and guard", "ok": not failures, "detail": detail, "findings": findings}


def _accounting_check(workflow: Any, settings: Any) -> dict[str, Any]:
    connector = workflow.accounting
    if settings.accounting_provider != "frappe":
        return {"name": "accounting", "ok": True, "detail": "the mock connector reads local records; there is nothing to reach."}
    reader = getattr(connector, "list_documents", None)
    if reader is None:
        return {"name": "accounting", "ok": True, "detail": "this connector has no read to probe."}
    try:
        # One read that proves the URL, the credentials and the permissions together, and whose
        # failure the connector already classifies.
        rows = reader("Company", [], ["name"], limit=1)
    except Exception as exc:
        return {"name": "accounting", "ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}
    return {"name": "accounting", "ok": True, "detail": f"the ledger answered with {len(rows)} company record(s)"}
