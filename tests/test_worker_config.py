"""Worker CLI overrides environment configuration without making autopay implicit."""

from __future__ import annotations

import sys

import pytest

from arc_payables import runtime, settings as settings_module, worker
from arc_payables.settings import Settings
from arc_payables.worker import _autopay_enabled, _build_parser


@pytest.mark.parametrize(
    ("arguments", "configured", "expected"),
    [
        ([], False, False),
        ([], True, True),
        (["--autopay"], False, True),
        (["--no-autopay"], True, False),
    ],
)
def test_worker_autopay_setting_is_default_and_cli_can_override(arguments, configured, expected):
    args = _build_parser().parse_args(arguments)
    assert _autopay_enabled(args.autopay, configured) is expected


def test_worker_entrypoint_builds_workflow_without_importing_http_app(monkeypatch):
    settings = Settings(_env_file=None, worker_autopay=True)
    workflow = type("Workflow", (), {"settings": settings})()
    captured = {}

    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime, "build_workflow", lambda configured: workflow)

    def run_pass(_workflow, **kwargs):
        captured.update(kwargs)
        return type("Report", (), {"outcome": "ok", "summary_line": lambda self: "pass ok"})()

    monkeypatch.setattr(worker, "run_pass", run_pass)
    monkeypatch.setattr(sys, "argv", ["arc-payables-worker", "--once", "--no-rescreen"])
    # Importing api constructs a FastAPI app at module load. The worker entrypoint must not need it.
    monkeypatch.setitem(sys.modules, "arc_payables.api", None)

    with pytest.raises(SystemExit) as stopped:
        worker.main()

    assert stopped.value.code == 0
    assert captured["autopay"] is True
