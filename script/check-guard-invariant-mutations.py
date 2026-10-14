#!/usr/bin/env python3
"""Prove the guard harness detects broken controls, using disposable copies only."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = "test/PaymentGuard.invariant.t.sol"
GUARD = "src/PaymentGuard.sol"


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError(f"Mutation anchor must match exactly once: {old!r}")
    return source.replace(old, new, 1)


def run_forge(project: Path, match: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["forge", "test", "--root", str(project), "--match-contract", "PaymentGuardInvariantTest",
         "--match-test", match, "--fuzz-seed", "0x42"],
        cwd=project, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, check=False,
    )


def main() -> None:
    # Exact failure messages distinguish caught defects from compilation/tooling failures.
    mutations = [
        ("pause", GUARD, "if (paused) revert Paused();", "",
         "invariant_nothing_settles_while_paused", "a payment settled while paused"),
        ("per-payment cap", GUARD,
         "if (perPaymentCap != 0 && permit.amount > perPaymentCap) revert PerPaymentCapExceeded();", "",
         "invariant_no_single_payment_exceeded_the_per_payment_cap", "a payment exceeded the per-payment cap"),
        ("epoch cap", GUARD,
         "if (epochCap != 0 && epochTotal > epochCap) revert EpochCapExceeded();", "",
         "test_handler_reaches_recipient_and_epoch_cap_boundaries", "epoch cap allowed one extra unit"),
        ("recipient cap", GUARD,
         "if (recipientEpochCap != 0 && recipientTotal > recipientEpochCap) {", "if (false) {",
         "test_handler_reaches_recipient_and_epoch_cap_boundaries", "recipient cap allowed one extra unit"),
        ("target selection", HARNESS, "targets[0] = address(handler);", "targets[0] = address(token);",
         "invariant_", "fuzzer did not exercise the handler"),
    ]
    with tempfile.TemporaryDirectory(prefix="arc-guard-mutations-") as temporary:
        project = Path(temporary)
        for relative in ("foundry.toml", GUARD, HARNESS, "test/PaymentGuard.t.sol"):
            destination = project / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        baseline = run_forge(project, "invariant_|test_handler_")
        if baseline.returncode != 0:
            raise RuntimeError(f"Baseline failed; mutation results would be meaningless:\n{baseline.stdout}")
        print("PASS baseline: unmodified harness", flush=True)
        for name, relative, old, new, match, failure in mutations:
            path = project / relative
            original = path.read_text()
            mutated = replace_once(original, old, new)
            # Remove the explicit selector registration too, so a lost target cannot still be
            # reached through a second registration mechanism.
            if name == "target selection":
                mutated = replace_once(mutated, "targets[0] = FuzzSelector(address(handler), selectors);",
                                       "targets[0] = FuzzSelector(address(token), new bytes4[](0));")
            try:
                path.write_text(mutated)
                result = run_forge(project, match)
                if result.returncode == 0 or "[FAIL" not in result.stdout or failure not in result.stdout:
                    raise RuntimeError(f"Mutation {name!r} was not caught by the expected check:\n{result.stdout}")
                print(f"PASS detected broken {name}: {failure}", flush=True)
            finally:
                path.write_text(original)
    print("All mutations detected. Repository contracts and live systems were never modified.")


if __name__ == "__main__":
    main()
