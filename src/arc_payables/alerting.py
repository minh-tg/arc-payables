"""Alerts: what the loop tells a human, and how it says it.

The worker is built to keep going, which is exactly why a failure can go unnoticed: the loop is fine
and the work is not. An alert is the opposite of that, and it is deliberately small. A fixed set of
conditions, evaluated from the same snapshot the metrics are rendered from, so the two can never
disagree about what is wrong.

Nothing here invents a destination. With no webhook configured the alerts are still worked out and
stored on the pass, which is what `/metrics` and the console read. A configured destination gets one
POST per distinct alert per interval, because an alert that repeats every thirty seconds is an alert
nobody reads.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .domain import utcnow

SOURCE = "arc-payables"

WARNING = "warning"
CRITICAL = "critical"


@dataclass(frozen=True)
class Alert:
    code: str
    severity: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "summary": self.summary, "detail": self.detail}


def evaluate_alerts(
    *,
    outcome: str,
    consecutive_failures: int,
    snapshot: dict[str, Any],
    settings: Any,
) -> list[Alert]:
    """Decide what is worth telling someone. Pure: same inputs, same alerts."""
    alerts: list[Alert] = []
    threshold = int(getattr(settings, "alert_after_consecutive_failures", 3) or 3)

    if outcome != "ok" and consecutive_failures >= threshold:
        alerts.append(
            Alert(
                "worker_failing",
                CRITICAL,
                f"{consecutive_failures} worker passes in a row have not finished cleanly",
                {"consecutive_failures": consecutive_failures, "outcome": outcome},
            )
        )

    # The audit chain outranks everything else here: if it does not verify, the record of what
    # happened can no longer be trusted, and nothing downstream of it can be trusted either.
    if snapshot.get("audit_chain_ok") == 0:
        alerts.append(
            Alert(
                "audit_chain_broken",
                CRITICAL,
                "the audit chain no longer verifies",
                {"entries": snapshot.get("audit_entries")},
            )
        )

    headroom = snapshot.get("reserve_headroom_usdc")
    if headroom is not None and headroom < 0:
        alerts.append(
            Alert(
                "reserve_breached",
                CRITICAL,
                f"the treasury is {abs(headroom):.6f} USDC below the configured reserve floor",
                {"headroom_usdc": headroom, "reserve_usdc": snapshot.get("reserve_usdc")},
            )
        )

    unconfirmed = int(snapshot.get("payments_uncertain") or 0)
    if unconfirmed:
        alerts.append(
            Alert(
                "settlements_unconfirmed",
                WARNING,
                f"{unconfirmed} settlement(s) have not been confirmed",
                {"count": unconfirmed},
            )
        )

    unrecorded = int(snapshot.get("payments_awaiting_ledger") or 0)
    if unrecorded:
        alerts.append(
            Alert(
                "payments_not_in_ledger",
                WARNING,
                f"{unrecorded} confirmed payment(s) are not in the ledger",
                {"count": unrecorded},
            )
        )

    return alerts


class WebhookSink:
    """Posts alerts as JSON to one URL, at most once per distinct alert per interval.

    Failures are returned rather than raised: an unreachable alerting endpoint must not stop the
    loop, and it must not be silently swallowed either, so the caller records what happened.
    """

    def __init__(
        self,
        url: str,
        *,
        min_interval_seconds: int = 3600,
        timeout: float = 5.0,
        poster: Callable[[str, dict], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utcnow,
    ):
        self.url = url
        self.min_interval_seconds = max(0, int(min_interval_seconds))
        self.timeout = timeout
        self._poster = poster or self._http_post
        self._clock = clock
        self._now = now
        self._sent: dict[str, float] = {}

    def send(self, alerts: list[Alert]) -> list[dict[str, Any]]:
        """Send what is due, and report what could not be sent."""
        due: list[Alert] = []
        for alert in alerts:
            last = self._sent.get(alert.code)
            if last is not None and (self._clock() - last) < self.min_interval_seconds:
                continue
            due.append(alert)
        if not due:
            return []

        payload = {
            "source": SOURCE,
            "generated_at": self._now().isoformat(),
            "alerts": [alert.to_dict() for alert in due],
        }
        try:
            self._poster(self.url, payload)
        except Exception as exc:
            return [{"code": alert.code, "error": f"{type(exc).__name__}: {exc}"[:200]} for alert in due]
        for alert in due:
            self._sent[alert.code] = self._clock()
        return []

    def _http_post(self, url: str, payload: dict) -> None:  # pragma: no cover - exercised by hand
        import httpx

        response = httpx.post(url, json=payload, timeout=self.timeout)
        response.raise_for_status()


def log_alerts(alerts: list[Alert], stream=None) -> None:
    """One line per alert on stderr, so a foreground run shows them too."""
    stream = stream or sys.stderr
    for alert in alerts:
        print(f"alert [{alert.severity}] {alert.code}: {alert.summary}", file=stream, flush=True)


def payload_preview(alerts: list[Alert]) -> str:
    """What would be posted, for a dry run or a log line."""
    return json.dumps({"source": SOURCE, "alerts": [alert.to_dict() for alert in alerts]})
