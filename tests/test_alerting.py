"""Alerts: what the loop is willing to interrupt somebody about.

The conditions are pure, so they are asserted directly. The sink is asserted on the two things that
make alerting usable in practice: it does not repeat itself, and it never takes the loop down with it.
"""

from __future__ import annotations

from arc_payables.alerting import WebhookSink, evaluate_alerts, payload_preview


class _Settings:
    def __init__(self, **overrides):
        self.alert_after_consecutive_failures = overrides.get("alert_after_consecutive_failures", 3)
        self.alert_min_interval_seconds = overrides.get("alert_min_interval_seconds", 3600)


def _snapshot(**overrides) -> dict:
    base = {
        "audit_chain_ok": 1,
        "audit_entries": 10,
        "reserve_headroom_usdc": 100.0,
        "reserve_usdc": 2000.0,
        "payments_uncertain": 0,
        "payments_awaiting_ledger": 0,
    }
    base.update(overrides)
    return base


def _codes(alerts) -> set[str]:
    return {alert.code for alert in alerts}


def test_a_healthy_pass_raises_nothing():
    assert evaluate_alerts(outcome="ok", consecutive_failures=0, snapshot=_snapshot(), settings=_Settings()) == []


def test_one_degarded_pass_is_not_an_alert():
    alerts = evaluate_alerts(outcome="degraded", consecutive_failures=1, snapshot=_snapshot(), settings=_Settings())
    assert "worker_failing" not in _codes(alerts)


def test_a_run_of_bad_passes_is_an_alert():
    alerts = evaluate_alerts(outcome="degraded", consecutive_failures=3, snapshot=_snapshot(), settings=_Settings())
    failing = next(alert for alert in alerts if alert.code == "worker_failing")
    assert failing.severity == "critical"
    assert "3" in failing.summary
    assert failing.detail["consecutive_failures"] == 3


def test_a_broken_audit_chain_outranks_everything():
    alerts = evaluate_alerts(
        outcome="ok",
        consecutive_failures=0,
        snapshot=_snapshot(audit_chain_ok=0, reserve_headroom_usdc=-5, payments_uncertain=2),
        settings=_Settings(),
    )
    assert _codes(alerts) == {"audit_chain_broken", "reserve_breached", "settlements_unconfirmed"}
    assert next(a for a in alerts if a.code == "audit_chain_broken").severity == "critical"


def test_a_breached_reserve_is_reported_with_the_number():
    alerts = evaluate_alerts(
        outcome="ok", consecutive_failures=0, snapshot=_snapshot(reserve_headroom_usdc=-12.5), settings=_Settings()
    )
    breach = next(alert for alert in alerts if alert.code == "reserve_breached")
    assert "12.5" in breach.summary
    assert breach.detail["headroom_usdc"] == -12.5


def test_money_that_left_but_is_not_recorded_is_reported():
    alerts = evaluate_alerts(
        outcome="ok", consecutive_failures=0, snapshot=_snapshot(payments_awaiting_ledger=2), settings=_Settings()
    )
    assert _codes(alerts) == {"payments_not_in_ledger"}
    assert alerts[0].severity == "warning"


def test_an_unreadable_treasury_does_not_invent_a_breach():
    """A provider that cannot be read is reported by the probe metric, not as a reserve breach."""
    alerts = evaluate_alerts(
        outcome="ok", consecutive_failures=0, snapshot=_snapshot(reserve_headroom_usdc=None), settings=_Settings()
    )
    assert alerts == []


def test_the_sink_sends_once_and_then_stays_quiet():
    sent = []
    clock = [1000.0]
    sink = WebhookSink(
        "https://alerts.example.test/hook",
        min_interval_seconds=600,
        poster=lambda url, payload: sent.append((url, payload)),
        clock=lambda: clock[0],
    )
    alerts = evaluate_alerts(
        outcome="ok", consecutive_failures=0, snapshot=_snapshot(reserve_headroom_usdc=-1), settings=_Settings()
    )
    assert sink.send(alerts) == []
    assert len(sent) == 1
    assert sent[0][1]["source"] == "arc-payables"
    assert sent[0][1]["alerts"][0]["code"] == "reserve_breached"

    # The same alert again inside the interval is dropped, so a scrape every 30 seconds does not
    # become a page every 30 seconds.
    assert sink.send(alerts) == []
    assert len(sent) == 1

    clock[0] += 601.0
    assert sink.send(alerts) == []
    assert len(sent) == 2


def test_the_sink_never_takes_the_loop_down():
    def explode(url, payload):
        raise RuntimeError("the alerting endpoint is down")

    sink = WebhookSink("https://alerts.example.test/hook", poster=explode)
    alerts = evaluate_alerts(
        outcome="ok", consecutive_failures=0, snapshot=_snapshot(audit_chain_ok=0), settings=_Settings()
    )
    failures = sink.send(alerts)
    assert len(failures) == 1
    assert failures[0]["code"] == "audit_chain_broken"
    assert "the alerting endpoint is down" in failures[0]["error"]
    # A failed delivery is not recorded as sent, so the next pass tries again.
    assert sink.send(alerts)[0]["code"] == "audit_chain_broken"


def test_the_preview_says_what_would_be_posted():
    alerts = evaluate_alerts(
        outcome="ok", consecutive_failures=0, snapshot=_snapshot(payments_uncertain=1), settings=_Settings()
    )
    assert '"code": "settlements_unconfirmed"' in payload_preview(alerts)
