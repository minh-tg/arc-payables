from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest

from tameion.currency import USDCOnlyConverter
from tameion.domain import DecisionAction, ScreeningStatus, SupplierRecord, WorkflowState
from tameion.mock_adapters import MockAccountingConnector, MockPaymentProvider
from tameion.policy import DeterministicPolicy
from tameion.screening import (
    ENTITY_QUERY_KEY,
    WALLET_QUERY_KEY,
    FixtureScreeningProvider,
    OpenSanctionsScreener,
    ScreeningResult,
    UnavailableScreeningProvider,
    build_screening_provider,
)
from tameion.seed import APPROVED_WALLET, seed_demo
from tameion.service import APWorkflow, WorkflowError
from tameion.settings import Settings
from tameion.store import SQLiteEvidenceStore

SUPPLIER = SupplierRecord(
    id="SUP-ACME-001",
    name="Acme Industrial Supply (Demo)",
    approved_wallet=APPROVED_WALLET,
    wallet_verified=True,
    wallet_version="v1",
    erp_supplier_id="SUP-ACME-001",
)


def _settings(**overrides) -> Settings:
    values = {"_env_file": None, "opensanctions_api_key": "test-opensanctions-key"}
    values.update(overrides)
    return Settings(**values)


def _screener(payload, *, status_code: int = 200, settings=None, record=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(status_code, json=payload)

    return OpenSanctionsScreener(
        settings or _settings(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _results(*rows) -> dict:
    return {"responses": {ENTITY_QUERY_KEY: {"status": 200, "results": list(rows), "total": {"value": len(rows)}}}, "limit": 5}


def _row(**overrides) -> dict:
    row = {
        "id": "NK-test",
        "caption": "Test Entity",
        "schema": "Company",
        "datasets": ["us_ofac_sdn"],
        "target": False,
        "score": 0.9,
        "match": True,
        "properties": {"topics": ["sanction"]},
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------------------
# Request shape and classification
# --------------------------------------------------------------------------------------


def test_screening_sends_apikey_header_and_screens_both_entity_and_wallet():
    requests: list[httpx.Request] = []
    screener = _screener({"responses": {}}, record=requests)
    screener.screen(SUPPLIER, APPROVED_WALLET)

    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/match/default"
    assert request.headers["authorization"] == "ApiKey test-opensanctions-key"
    body = json.loads(request.content)
    assert body["queries"][ENTITY_QUERY_KEY]["schema"] == "Company"
    assert body["queries"][ENTITY_QUERY_KEY]["properties"]["name"] == [SUPPLIER.name]
    assert body["queries"][WALLET_QUERY_KEY]["schema"] == "CryptoWallet"
    assert body["queries"][WALLET_QUERY_KEY]["properties"]["publicKey"] == [APPROVED_WALLET]
    assert body["limit"] == 5


def test_screening_reports_clear_when_nothing_matches():
    result = _screener({"responses": {}}).screen(SUPPLIER, APPROVED_WALLET)
    assert result.status == ScreeningStatus.CLEAR
    assert result.clear and result.matches == ()
    assert result.provider == "opensanctions"


def test_screening_flags_a_risk_topic_match_regardless_of_the_target_flag():
    flagged = _screener(_results(_row(target=False, score=0.91))).screen(SUPPLIER, APPROVED_WALLET)
    assert flagged.status == ScreeningStatus.FLAGGED
    assert flagged.strongest and flagged.strongest.topics == ("sanction",)
    assert "Sanctions/risk screening matched" in flagged.reason

    target_flagged = _screener(_results(_row(target=True))).screen(SUPPLIER, APPROVED_WALLET)
    assert target_flagged.status == ScreeningStatus.FLAGGED


def test_screening_treats_a_matched_non_risk_entity_as_ambiguous():
    result = _screener(_results(_row(target=False, properties={"topics": ["role.pep"]}))).screen(SUPPLIER, APPROVED_WALLET)
    assert result.status == ScreeningStatus.INCONCLUSIVE
    assert "Ambiguous screening match" in result.reason


def test_screening_treats_a_high_scoring_non_match_as_ambiguous():
    result = _screener(_results(_row(match=False, score=0.72, properties={}))).screen(SUPPLIER, APPROVED_WALLET)
    assert result.status == ScreeningStatus.INCONCLUSIVE
    assert "at or above the review threshold" in result.reason


def test_screening_ignores_results_below_the_review_threshold():
    result = _screener(_results(_row(match=False, score=0.31, properties={}))).screen(SUPPLIER, APPROVED_WALLET)
    assert result.status == ScreeningStatus.CLEAR
    assert result.response_hash and result.response_hash.startswith("0x")


def test_screening_threshold_is_configurable():
    screener = _screener(_results(_row(match=False, score=0.45, properties={})), settings=_settings(opensanctions_review_threshold=0.4))
    assert screener.screen(SUPPLIER, APPROVED_WALLET).status == ScreeningStatus.INCONCLUSIVE


# --------------------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------------------


def test_screening_fails_closed_without_an_api_key():
    result = OpenSanctionsScreener(_settings(opensanctions_api_key=None)).screen(SUPPLIER, APPROVED_WALLET)
    assert result.status == ScreeningStatus.UNAVAILABLE
    assert "OPENSANCTIONS_API_KEY is not configured" in result.reason


@pytest.mark.parametrize(
    ("payload", "status_code", "expected"),
    [
        ({"detail": "boom"}, 500, "HTTP 500"),
        ({"detail": "unauthorized"}, 401, "HTTP 401"),
        ({"unexpected": True}, 200, "unrecognised response body"),
        ({"responses": {ENTITY_QUERY_KEY: {"status": 422, "results": []}}}, 200, "rejected"),
    ],
)
def test_screening_fails_closed_on_provider_problems(payload, status_code, expected):
    result = _screener(payload, status_code=status_code).screen(SUPPLIER, APPROVED_WALLET)
    assert result.status == ScreeningStatus.UNAVAILABLE
    assert expected in result.reason


def test_screening_fails_closed_on_a_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route", request=request)

    screener = OpenSanctionsScreener(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    result = screener.screen(SUPPLIER, APPROVED_WALLET)
    assert result.status == ScreeningStatus.UNAVAILABLE
    assert "request failed" in result.reason


def test_unavailable_provider_always_fails_closed():
    result = UnavailableScreeningProvider().screen(SUPPLIER, APPROVED_WALLET)
    assert result.status == ScreeningStatus.UNAVAILABLE


def test_provider_factory_selects_by_configuration():
    with TemporaryDirectory() as tmp:
        store = SQLiteEvidenceStore(Path(tmp) / "s.sqlite3")
        store.initialize()
        assert isinstance(build_screening_provider(_settings(screening_provider="opensanctions"), store), OpenSanctionsScreener)
        assert isinstance(build_screening_provider(_settings(screening_provider="unavailable"), store), UnavailableScreeningProvider)
        assert isinstance(build_screening_provider(_settings(screening_provider="fixture"), store), FixtureScreeningProvider)


# --------------------------------------------------------------------------------------
# Policy integration
# --------------------------------------------------------------------------------------


class _StubScreener:
    name = "stub"

    def __init__(self, result: ScreeningResult):
        self.result = result

    def screen(self, supplier, wallet):
        return self.result


def _workflow(tmp: str, screening: ScreeningResult, **settings_overrides):
    settings = Settings(_env_file=None, database_path=Path(tmp) / "p.sqlite3", **settings_overrides)
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    legitimate_id, _ = seed_demo(store)
    accounting = MockAccountingConnector(store)
    payment = MockPaymentProvider(store)
    workflow = APWorkflow(
        store,
        accounting,
        payment,
        payment.signer,
        DeterministicPolicy(settings, USDCOnlyConverter("USD")),
        settings,
        screener=_StubScreener(screening),
    )
    return settings, store, payment, workflow, legitimate_id


def _result(status: ScreeningStatus, reason: str = "test result") -> ScreeningResult:
    from tameion.screening import ScreeningMatch

    matches = ()
    if status == ScreeningStatus.FLAGGED:
        matches = (ScreeningMatch("supplier_entity", "NK-1", "Blocked Entity", "Company", 0.95, True, True, ("us_ofac_sdn",), ("sanction",)),)
    elif status == ScreeningStatus.INCONCLUSIVE:
        matches = (ScreeningMatch("supplier_entity", "NK-2", "Possible Entity", "Company", 0.8, True, False, ("peps",), ("role.pep",)),)
    return ScreeningResult(status=status, provider="stub", subject=SUPPLIER.name, wallet=APPROVED_WALLET, matches=matches, reason=reason)


def test_clear_screening_allows_payment():
    with TemporaryDirectory() as tmp:
        _settings_, store, payment, workflow, invoice_id = _workflow(tmp, _result(ScreeningStatus.CLEAR))
        assert workflow.evaluate(invoice_id)["decision"]["action"] == DecisionAction.PAY_NOW.value
        assert workflow.submit_payment(invoice_id)["state"] == WorkflowState.ERP_RECORDED.value


def test_flagged_screening_blocks_payment_by_default():
    with TemporaryDirectory() as tmp:
        settings, store, payment, workflow, invoice_id = _workflow(
            tmp, _result(ScreeningStatus.FLAGGED), approval_token="tok"
        )
        evaluated = workflow.evaluate(invoice_id)
        assert evaluated["decision"]["action"] == DecisionAction.ESCALATE.value
        check = next(item for item in evaluated["decision"]["policy_checks"] if item["code"] == "screening_flagged")
        assert check["requires_human"] and not check["overridable"]
        with pytest.raises(WorkflowError) as error:
            workflow.approve(
                invoice_id,
                {"reviewer": "ap", "approved": True, "note": "cleared out of band", "acknowledged_checks": ["screening_flagged"]},
            )
        assert error.value.code == "invalid_review_scope"
        with pytest.raises(WorkflowError):
            workflow.submit_payment(invoice_id)
        assert payment.submission_calls == 0


def test_flagged_screening_can_be_configured_as_reviewable_but_still_needs_a_human():
    with TemporaryDirectory() as tmp:
        settings, store, payment, workflow, invoice_id = _workflow(
            tmp, _result(ScreeningStatus.FLAGGED), approval_token="tok", screening_positive_match_policy="review"
        )
        evaluated = workflow.evaluate(invoice_id)
        check = next(item for item in evaluated["decision"]["policy_checks"] if item["code"] == "screening_flagged")
        assert check["requires_human"] and check["overridable"]
        assert evaluated["decision"]["action"] == DecisionAction.ESCALATE.value
        approved = workflow.approve(
            invoice_id,
            {"reviewer": "ap", "approved": True, "note": "verified false positive", "acknowledged_checks": ["screening_flagged"]},
        )
        assert approved["decision"]["action"] == DecisionAction.PAY_NOW.value


def test_ambiguous_and_unavailable_screening_require_review():
    for status in (ScreeningStatus.INCONCLUSIVE, ScreeningStatus.UNAVAILABLE):
        with TemporaryDirectory() as tmp:
            settings, store, payment, workflow, invoice_id = _workflow(tmp, _result(status))
            evaluated = workflow.evaluate(invoice_id)
            assert evaluated["decision"]["action"] == DecisionAction.ESCALATE.value
            expected_code = "screening_ambiguous" if status == ScreeningStatus.INCONCLUSIVE else "screening_unavailable"
            check = next(item for item in evaluated["decision"]["policy_checks"] if item["code"] == expected_code)
            assert check["requires_human"] and check["overridable"]
            with pytest.raises(WorkflowError):
                workflow.submit_payment(invoice_id)
            assert payment.submission_calls == 0


def test_screening_result_is_recorded_as_evidence_and_binds_the_hash():
    with TemporaryDirectory() as tmp:
        settings, store, payment, workflow, invoice_id = _workflow(tmp, _result(ScreeningStatus.CLEAR, "clear per stub"))
        first = workflow.evaluate(invoice_id)
        assert first["decision"]["evidence_hash"]
        snapshot = store.get_evidence_snapshot(invoice_id)
        assert snapshot["screening"]["provider"] == "stub"
        assert snapshot["screening"]["status"] == "CLEAR"
        assert snapshot["screening"]["reason"] == "clear per stub"

    with TemporaryDirectory() as tmp:
        settings, store, payment, workflow, invoice_id = _workflow(
            tmp, _result(ScreeningStatus.INCONCLUSIVE, "possible match")
        )
        second = workflow.evaluate(invoice_id)
        assert second["decision"]["evidence_hash"] != first["decision"]["evidence_hash"]


def test_screening_provider_exception_fails_closed():
    class BrokenScreener:
        name = "broken"

        def screen(self, supplier, wallet):
            raise RuntimeError("provider exploded")

    with TemporaryDirectory() as tmp:
        settings = Settings(_env_file=None, database_path=Path(tmp) / "b.sqlite3")
        store = SQLiteEvidenceStore(settings.database_path)
        store.initialize()
        legitimate_id, _ = seed_demo(store)
        accounting = MockAccountingConnector(store)
        payment = MockPaymentProvider(store)
        workflow = APWorkflow(
            store, accounting, payment, payment.signer,
            DeterministicPolicy(settings, USDCOnlyConverter("USD")), settings, screener=BrokenScreener(),
        )
        evaluated = workflow.evaluate(legitimate_id)
        assert evaluated["decision"]["action"] == DecisionAction.ESCALATE.value
        check = next(item for item in evaluated["decision"]["policy_checks"] if item["code"] == "screening_unavailable")
        assert "raised an error" in check["detail"]
