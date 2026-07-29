"""Transcription adapter: driven against a fake whisper CLI.

The real whisper CLI needs PyTorch + model weights; these tests
substitute a small Python script that mimics its contract (JSON file
named after the input stem, written into --output_dir) so the
adapter's parsing, error handling, and timeout paths are covered
deterministically and offline.
"""

from __future__ import annotations

import stat
import sys
from typing import TYPE_CHECKING

import pytest

from suite_common.transcribe import (
    Transcription,
    TranscriptionFailedError,
    TranscriptSegment,
    WhisperNotAvailableError,
    transcribe_file,
)

if TYPE_CHECKING:
    from pathlib import Path


def _fake_cli(tmp_path: Path, body: str) -> str:
    """Write an executable fake-whisper script and return its path."""
    script = tmp_path / "fake-whisper"
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


_HAPPY_CLI = """
import json, sys
from pathlib import Path
args = sys.argv[1:]
media = Path(args[0])
out_dir = Path(args[args.index("--output_dir") + 1])
payload = {
    "text": " Hash verified. SHA-256 matches the acquisition value.",
    "language": "en",
    "segments": [
        {"start": 0.0, "end": 2.5, "text": " Hash verified."},
        {"start": 2.5, "end": 6.0, "text": " SHA-256 matches the acquisition value."},
    ],
}
(out_dir / f"{media.stem}.json").write_text(json.dumps(payload), encoding="utf-8")
"""


def test_transcribe_happy_path_parses_segments(tmp_path: Path) -> None:
    media = tmp_path / "interview.wav"
    media.write_bytes(b"RIFF-fake")
    cli = _fake_cli(tmp_path, _HAPPY_CLI)

    result = transcribe_file(media, cli_path=cli)

    assert isinstance(result, Transcription)
    assert result.language == "en"
    assert "SHA-256 matches" in result.text
    assert len(result.segments) == 2
    assert isinstance(result.segments[0], TranscriptSegment)
    assert result.segments[0].start_s == 0.0


def test_as_plain_text_carries_provenance_and_timestamps(tmp_path: Path) -> None:
    """The saved .txt is a deliverable: it must name its source, the
    model, and the language, and carry per-segment offsets."""
    media = tmp_path / "call-recording.mp3"
    media.write_bytes(b"ID3-fake")
    cli = _fake_cli(tmp_path, _HAPPY_CLI)

    rendered = transcribe_file(media, cli_path=cli, model="base").as_plain_text()

    assert "Source: call-recording.mp3" in rendered
    assert "base model" in rendered
    assert "Detected language: en" in rendered
    assert "[00:00] Hash verified." in rendered
    assert "[00:02] SHA-256 matches" in rendered


def test_missing_cli_raises_not_available_with_install_hint(tmp_path: Path) -> None:
    media = tmp_path / "a.wav"
    media.write_bytes(b"x")
    import suite_common.transcribe as mod

    original = mod.find_whisper_cli
    mod.find_whisper_cli = lambda: None  # type: ignore[assignment]
    try:
        with pytest.raises(WhisperNotAvailableError) as excinfo:
            transcribe_file(media)
    finally:
        mod.find_whisper_cli = original  # type: ignore[assignment]
    assert "pip install" in str(excinfo.value)


def test_missing_media_file_fails_before_running_cli(tmp_path: Path) -> None:
    cli = _fake_cli(tmp_path, _HAPPY_CLI)
    with pytest.raises(TranscriptionFailedError):
        transcribe_file(tmp_path / "gone.wav", cli_path=cli)


def test_nonzero_exit_surfaces_stderr_tail(tmp_path: Path) -> None:
    media = tmp_path / "bad.wav"
    media.write_bytes(b"x")
    cli = _fake_cli(
        tmp_path,
        'import sys; print("ffmpeg: invalid data found", file=sys.stderr); sys.exit(1)',
    )
    with pytest.raises(TranscriptionFailedError) as excinfo:
        transcribe_file(media, cli_path=cli)
    assert "ffmpeg: invalid data found" in str(excinfo.value)


def test_success_without_json_output_is_a_failure(tmp_path: Path) -> None:
    """Exit 0 but no JSON on disk must not masquerade as an empty
    transcript -- it's a contract violation worth surfacing."""
    media = tmp_path / "quiet.wav"
    media.write_bytes(b"x")
    cli = _fake_cli(tmp_path, "pass")
    with pytest.raises(TranscriptionFailedError) as excinfo:
        transcribe_file(media, cli_path=cli)
    assert "no JSON output" in str(excinfo.value)


def test_empty_transcript_is_a_failure(tmp_path: Path) -> None:
    media = tmp_path / "silence.wav"
    media.write_bytes(b"x")
    cli = _fake_cli(
        tmp_path,
        """
import json, sys
from pathlib import Path
out_dir = Path(sys.argv[sys.argv.index("--output_dir") + 1])
(out_dir / "silence.json").write_text(json.dumps({"text": "", "segments": []}))
""",
    )
    with pytest.raises(TranscriptionFailedError) as excinfo:
        transcribe_file(media, cli_path=cli)
    assert "empty transcript" in str(excinfo.value)


def test_timeout_kills_the_run_with_guidance(tmp_path: Path) -> None:
    media = tmp_path / "long.wav"
    media.write_bytes(b"x")
    cli = _fake_cli(tmp_path, "import time; time.sleep(30)")
    with pytest.raises(TranscriptionFailedError) as excinfo:
        transcribe_file(media, cli_path=cli, timeout_s=0.5)
    assert "smaller model" in str(excinfo.value)
