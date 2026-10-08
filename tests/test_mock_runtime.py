"""Mock API/worker processes share a demo signer, never real signing authority."""
from arc_payables.api import create_app
from arc_payables.runtime import build_workflow
from arc_payables.security import EIP712PermitSigner
from arc_payables.seed import seed_demo
from arc_payables.settings import Settings


def _settings(tmp_path):
    return Settings(
        _env_file=None,
        database_path=tmp_path / "demo.sqlite3",
        payment_provider="mock",
        accounting_provider="mock",
        screening_provider="fixture",
        decision_layer="heuristics",
    )


def test_mock_audit_chain_survives_api_worker_and_api_restart(tmp_path):
    settings = _settings(tmp_path)
    api_workflow = create_app(settings=settings).state.workflow
    invoice_id, _ = seed_demo(api_workflow.store)
    api_workflow.evaluate(invoice_id)
    assert api_workflow.store.verify_audit_chain()["ok"]

    worker_workflow = build_workflow(settings)
    report = worker_workflow.store.verify_audit_chain()
    assert report["ok"], report
    assert worker_workflow.signer.address == api_workflow.signer.address
    worker_workflow.evaluate(invoice_id)
    assert api_workflow.store.verify_audit_chain()["ok"]

    restarted = create_app(settings=settings).state.workflow
    report = restarted.store.verify_audit_chain()
    assert report["ok"], report
    assert report["signed"] > 0
    assert report["signature_anchor"] == "configured_signer"


def test_mock_provider_and_workflow_honor_an_explicit_signer(tmp_path):
    signer = EIP712PermitSigner("0x" + "02".zfill(64))
    workflow = build_workflow(_settings(tmp_path), signer=signer)
    assert workflow.signer is signer
    assert workflow.payment_provider.signer is signer
    invoice_id, _ = seed_demo(workflow.store)
    workflow.evaluate(invoice_id)
    workflow.submit_payment(invoice_id)
    assert workflow.payment_provider.submission_calls == 1
    assert workflow.store.verify_audit_chain()["ok"]
