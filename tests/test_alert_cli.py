"""Alert delivery verification: proving a person is actually told.

The worker's alerting already has tests for the sink's behaviour. These tests cover the operator
command that answers the question those tests cannot: "is the destination configured today
actually reachable, with these settings?" A misconfigured URL is indistinguishable from a quiet
week until someone checks, so the command must report failure honestly rather than exit zero.
"""

from __future__ import annotations

import json

import pytest

from arc_payables.alert_cli import main
from arc_payables.alerting import WebhookSink


def test_no_destination_is_reported_rather_than_silently_passing(monkeypatch, capsys):
    monkeypatch.setattr("arc_payables.settings.get_settings", lambda: _settings(None))
    assert main([]) == 2
    captured = capsys.readouterr()
    assert "no alert destination is configured" in captured.err
    # The distinction matters to an operator: recording without delivery is not delivery.
    assert "no person is told" in captured.err


def test_a_delivered_alert_reports_success(monkeypatch, capsys):
    monkeypatch.setattr("arc_payables.settings.get_settings", lambda: _settings("https://alerts.invalid/hook"))
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(WebhookSink, "_http_post", lambda self, url, payload: sent.append((url, payload)))
    assert main([]) == 0
    assert sent and sent[0][0] == "https://alerts.invalid/hook"
    assert sent[0][1]["alerts"][0]["code"] == "delivery_test"
    assert "delivered" in capsys.readouterr().out


def test_a_failed_delivery_is_reported_and_does_not_claim_success(monkeypatch, capsys):
    monkeypatch.setattr("arc_payables.settings.get_settings", lambda: _settings("https://alerts.invalid/hook"))

    def explode(self, url, payload):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(WebhookSink, "_http_post", explode)
    assert main([]) == 1
    captured = capsys.readouterr()
    assert "was not delivered" in captured.err
    assert "connection refused" in captured.err
    # It must also say what is still working, so the operator does not chase the wrong fault.
    assert "still recorded" in captured.err


def test_a_dry_run_never_sends(monkeypatch, capsys):
    monkeypatch.setattr("arc_payables.settings.get_settings", lambda: _settings("https://alerts.invalid/hook"))
    sent: list[object] = []
    monkeypatch.setattr(WebhookSink, "_http_post", lambda self, url, payload: sent.append(payload))
    assert main(["--dry-run"]) == 0
    assert sent == []
    payload = json.loads(capsys.readouterr().out.split("\n(")[0])
    assert payload["detail"]["source"] == "arc-payables-alert-test"


def test_an_explicit_url_overrides_the_configuration(monkeypatch, capsys):
    monkeypatch.setattr("arc_payables.settings.get_settings", lambda: _settings("https://ignored.invalid"))
    sent: list[str] = []
    monkeypatch.setattr(WebhookSink, "_http_post", lambda self, url, payload: sent.append(url))
    assert main(["--url", "https://override.invalid/hook"]) == 0
    assert sent == ["https://override.invalid/hook"]


def _settings(webhook: str | None):
    from arc_payables.settings import Settings

    return Settings(_env_file=None, alert_webhook_url=webhook)
