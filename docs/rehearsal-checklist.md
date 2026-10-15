# Rehearsal checklist

Run this top to bottom once before recording, then again immediately before the take. It matches
the timeline in [demo-recording.md](demo-recording.md) and the cues in
[demo-recording.srt](demo-recording.srt).

## Before the machine is on camera

- [ ] `git status` is clean and the intended revision is the one you push.
- [ ] The full suite is green: `uv run pytest -q` and `forge build && forge test`.
- [ ] `uv run python script/decision-eval.py` ends `PASS` and `uv run python script/adversarial-corpus.py` ends `PASS`.
- [ ] `bash script/manual-readiness-demo.sh` serves on `http://127.0.0.1:8097/console/` and the Overview loads.
- [ ] The browser is a private window with no other tabs, extensions, bookmarks bar or autocomplete visible.
- [ ] Terminal scrollback is cleared, the prompt shows no key path, and no `.env`, key file or secret is on screen or in history.
- [ ] The audit-anchor and paymaster environment variables are unset unless the segment needs them.
- [ ] The hosted console URL is in a tab **only if you have deployed it**. If not, plan to cut the 1:27 row and do not type the URL.
- [ ] The settlement explorer page opens from `docs/submission-evidence.md` and the transaction link resolves signed out.

## Labels, every time they appear

- [ ] The console header reads `Hosted demo · simulated payments & ledger` wherever the console is on screen.
- [ ] The caption `[Mock data. Simulated payment, no funds move.]` is burned in or the header label is visible.
- [ ] The caption `[Scripted fake model, not a live model.]` is burned in before the eval and corpus output.
- [ ] The caption `[Saved result from an earlier run.]` is burned in before `show-planner-evidence.sh`.
- [ ] The caption `[Testnet. One 0.01 test-USDC payment.]` is burned in over the explorer.
- [ ] Nothing on screen says Circle or ERPNext activity is live. Nothing says the model approved or paid.
- [ ] No full hash, private key, API key or session cookie is visible. Shortened hashes only.

## Commands, in order

- [ ] `bash script/show-planner-evidence.sh` (about one second, reads a saved file).
- [ ] `uv run python script/decision-eval.py` (39 cases, scripted fake model, no key).
- [ ] `uv run python script/adversarial-corpus.py` (250 variants, scripted fake model, about 30 seconds).
- [ ] Console: **Load demo invoices**, open the suspicious invoice, **Evaluate**, expand the failed check.
- [ ] Console: open the clean invoice, **Evaluate**, **Review payment**, **Cancel**, reopen, **Confirm**, **Verify audit**.
- [ ] Run each command once before the take so the shell has no cold-start delay on camera.

## Audio and captions

- [ ] The voice-over reads the `Say` column exactly; no sentence is added, cut or reworded.
- [ ] `docs/demo-recording.srt` is loaded by the recorder, or burned in, and matches the voice.
- [ ] `uv run pytest -q tests/test_captions.py` is green after any narration edit.
- [ ] Microphone level is checked, the room is quiet, and notifications are off (focus mode on).
- [ ] Listen to the first 20 seconds back: the hook is audible and the mock label is on screen.

## Timed rehearsal

- [ ] Read the narration aloud against the timeline three times; the last cue lands at or before 2:40.
- [ ] Time each row with a stopwatch. The hosted row is 7 seconds and is the first to cut if you run long.
- [ ] Confirm the last caption ends at or before `00:02:40,000` and no cue is longer than 6.0 seconds.
- [ ] Re-record any segment where the screen action does not match the sentence, rather than narrating over it.

## After the take

- [ ] Watch the whole video once with the sound off and once with the captions on.
- [ ] Check every visible number against its command output: 39 labelled cases, 250 variants, 0 with policy on, 250 with policy off, four payables and 400 USDC.
- [ ] Check that no segment claims adoption, customers or a live Circle notification.
- [ ] Export at the platform's bitrate, upload, and test the link signed out.
