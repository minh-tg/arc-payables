"""The recording captions: readable, ordered, inside 2:40 and the same words as the narration."""

from __future__ import annotations

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
SRT = DOCS / "demo-recording.srt"
SCRIPT = DOCS / "demo-recording.md"

TIMESTAMP = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})")
MAX_LINE = 42
MAX_SPEED = 17.0
MIN_SECONDS = 1.2
MAX_SECONDS = 6.0
MAX_END = 160.0
MIN_GAP = 0.1
LABEL_TEXTS = {
    "[Mock data. Simulated payment, no funds move.]",
    "[Saved result from an earlier run.]",
    "[Scripted fake model, not a live model.]",
    "[Testnet. One 0.01 test-USDC payment.]",
}


def _seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def parse_srt(text: str) -> list[dict]:
    cues = []
    for index, block in enumerate(text.strip().split("\n\n"), 1):
        lines = block.split("\n")
        assert lines[0] == str(index), f"cue {index} is numbered {lines[0]!r}"
        match = TIMESTAMP.fullmatch(lines[1])
        assert match, f"cue {index} has a malformed timestamp {lines[1]!r}"
        start = _seconds(*match.groups()[:4])
        end = _seconds(*match.groups()[4:])
        cues.append({"index": index, "start": start, "end": end, "lines": lines[2:], "text": " ".join(lines[2:])})
    return cues


def narration_from_script(text: str) -> str:
    says = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3 or not (cells[2].startswith('"') and cells[2].endswith('"')):
            continue
        says.append(cells[2][1:-1])
    return " ".join(says)


def read_all() -> tuple[list[dict], str]:
    raw = SRT.read_bytes()
    assert b"\r" not in raw, "the SRT must use LF line endings"
    cues = parse_srt(raw.decode("utf-8"))
    return cues, narration_from_script(SCRIPT.read_text())


def test_srt_exists_and_parses_to_ordered_cues():
    cues, _narration = read_all()
    assert cues, "the SRT has no cues"
    previous_end = -1.0
    for cue in cues:
        assert cue["start"] >= previous_end + MIN_GAP, f"cue {cue['index']} overlaps or follows too closely"
        previous_end = cue["end"]


def test_every_cue_fits_two_lines_of_forty_two_characters():
    cues, _narration = read_all()
    for cue in cues:
        assert len(cue["lines"]) <= 2, f"cue {cue['index']} has {len(cue['lines'])} lines"
        for line in cue["lines"]:
            assert len(line) <= MAX_LINE, f"cue {cue['index']} line is {len(line)} characters"


def test_every_cue_is_on_screen_between_1_2_and_6_0_seconds():
    cues, _narration = read_all()
    for cue in cues:
        duration = cue["end"] - cue["start"]
        assert MIN_SECONDS <= duration <= MAX_SECONDS, f"cue {cue['index']} lasts {duration:.2f}s"


def test_reading_speed_stays_under_seventeen_characters_per_second():
    cues, _narration = read_all()
    for cue in cues:
        speed = len(cue["text"]) / (cue["end"] - cue["start"])
        assert speed <= MAX_SPEED, f"cue {cue['index']} reads at {speed:.1f} characters per second"


def test_the_last_cue_ends_at_or_before_two_forty():
    cues, _narration = read_all()
    assert cues[-1]["end"] <= MAX_END, f"the captions end at {cues[-1]['end']:.3f}s"


def test_mock_labels_are_separate_cues_of_2_5_to_4_seconds():
    cues, _narration = read_all()
    labels = [cue for cue in cues if cue["text"].startswith("[") and cue["text"].endswith("]")]
    assert {cue["text"] for cue in labels} == LABEL_TEXTS
    for cue in labels:
        duration = cue["end"] - cue["start"]
        assert 2.5 <= duration <= 4.0, f"label cue {cue['index']} lasts {duration:.2f}s"


def test_caption_text_is_the_narration_verbatim():
    cues, narration = read_all()
    spoken = " ".join(cue["text"] for cue in cues if not cue["text"].startswith("["))
    assert " ".join(spoken.split()) == " ".join(narration.split())


def test_captions_use_digits_and_carry_no_tags_or_secrets():
    cues, _narration = read_all()
    text = "\n".join(cue["text"] for cue in cues)
    assert "33 " in text and "150 " in text and "400 USDC" in text
    for word in ("thirty-three", "one hundred fifty", "four hundred"):
        assert word not in text
    assert "<" not in text and ">" not in text
    assert not re.search(r"0x[0-9a-fA-F]{8,}", text), "a hash or address is in the captions"
