"""Prove the alerting path end to end, on demand.

An alerting configuration is easy to believe in and hard to check: the loop records alerts whether
or not anyone receives them, and a typo in a webhook URL looks exactly like a quiet week. This
command sends one clearly-labelled test alert through the same sink the worker uses, so delivery is
demonstrated rather than assumed.

It is a diagnostic, not a monitoring system: it proves the path works *now*, at the moment an
operator runs it. Proving it keeps working is the drill documented in docs/operations.md.
"""

from __future__ import annotations

import argparse
import json
import sys

from .alerting import WARNING, Alert, WebhookSink


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one test alert to the configured webhook")
    parser.add_argument("--url", default=None, help="Override ALERT_WEBHOOK_URL for this check")
    parser.add_argument("--timeout", type=float, default=10.0, help="Delivery timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Print the payload without sending it")
    args = parser.parse_args(argv)

    from .settings import get_settings

    settings = get_settings()
    url = args.url or settings.alert_webhook_url
    alert = Alert(
        code="delivery_test",
        severity=WARNING,
        summary="test alert: alert delivery is working",
        detail={
            "source": "arc-payables-alert-test",
            "note": (
                "Sent on request to verify that alerts reach this destination. If this arrived, "
                "the delivery path works; it does not prove every alert is configured correctly."
            ),
        },
    )

    if not url:
        print(
            "FAIL  no alert destination is configured (ALERT_WEBHOOK_URL is empty), so nothing could "
            "be delivered. Alerts are still recorded on each worker pass, but no person is told.",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        sink = WebhookSink(url, min_interval_seconds=0, timeout=args.timeout)
        print(json.dumps(alert.to_dict(), indent=2, sort_keys=True))
        print(f"(dry run: nothing was sent to {sink.url})", file=sys.stderr)
        return 0

    # min_interval_seconds=0: a deliberate check must send even if the worker sent recently.
    sink = WebhookSink(url, min_interval_seconds=0, timeout=args.timeout)
    failures = sink.send([alert])
    if failures:
        reason = failures[0].get("error", "unknown error")
        print(f"FAIL  the test alert was not delivered: {reason}", file=sys.stderr)
        print(
            "      Alerts are still recorded on each pass, so the worker is not silently broken; "
            "what is broken is the path to a person.",
            file=sys.stderr,
        )
        return 1
    print(f"Test alert delivered to {url}. A worker pass would deliver real alerts the same way.")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
