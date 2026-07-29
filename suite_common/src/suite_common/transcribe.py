"""Subprocess adapter for local Whisper speech-to-text.

Forensic exams routinely involve audio evidence -- recorded
interviews, intercepted calls, voicemail extractions -- and examiners
narrate findings. The suite integrates the ``whisper`` CLI (from the
silvance/whisper.py fork of OpenAI Whisper) the same way it
integrates Ollama: as an OPTIONAL local tool detected at runtime,
never as a hard dependency. PyTorch would roughly triple the
air-gapped bundle; shelling out keeps the suite lean and keeps the
transcription engine independently upgradeable.

Availability: ``find_whisper_cli()`` returns the executable path or
``None``. Callers surface a friendly "install it with pip install
silvance-whisper" message when absent -- see
:func:`whisper_install_hint`.

Everything runs fully offline once the model weights are downloaded
(first run per model caches them), matching the air-gapped design.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Model used when the caller doesn't specify one. ``base`` is the
#: sweet spot for CPU-only forensic workstations: ~74 MB of weights,
#: near-real-time on modern hardware, adequate accuracy for
#: English-language evidence review. Callers pass ``model=`` for
#: higher-accuracy runs (``small`` / ``medium`` / ``turbo``).
DEFAULT_WHISPER_MODEL = "base"

#: Hard ceiling on a single transcription run. Long-form evidence
#: audio (multi-hour interview WAVs) on CPU can be slow; an hour of
#: wall-clock covers everything reasonable while guaranteeing a hung
#: ffmpeg or a swap-thrashing model load can't wedge the worker
#: thread forever.
DEFAULT_TIMEOUT_S = 3600.0

#: Tail of stderr preserved in error messages. Whisper's stderr is
#: dominated by progress bars; the trailing lines carry the actual
#: failure ("No such file", CUDA errors, ffmpeg complaints).
_STDERR_TAIL_CHARS = 800


class TranscriptionError(Exception):
    """Base class for transcription failures."""


class WhisperNotAvailableError(TranscriptionError):
    """The whisper CLI isn't installed / on PATH."""


class TranscriptionFailedError(TranscriptionError):
    """The CLI ran but exited non-zero or produced no usable output."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TranscriptSegment:
    """One timed segment of the transcript."""

    start_s: float
    end_s: float
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Transcription:
    """Result of one whisper run over one media file."""

    source_path: Path
    model: str
    language: str
    text: str
    segments: list[TranscriptSegment] = field(default_factory=list)

    def as_plain_text(self) -> str:
        """Render for saving to a ``.txt`` deliverable.

        Header lines carry the provenance an exhibit needs (source
        file, model, detected language); the body is the segment list
        with ``[MM:SS]`` offsets when segments are available, else the
        flat text.
        """
        lines = [
            f"Source: {self.source_path.name}",
            f"Transcribed with: whisper ({self.model} model), local/offline",
            f"Detected language: {self.language or 'unknown'}",
            "",
        ]
        if self.segments:
            for seg in self.segments:
                lines.append(f"[{_mmss(seg.start_s)}] {seg.text.strip()}")
        else:
            lines.append(self.text.strip())
        return "\n".join(lines) + "\n"


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= 3600:
        return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return f"{total // 60:02d}:{total % 60:02d}"


def find_whisper_cli() -> str | None:
    """Locate the whisper CLI, or ``None`` when transcription is unavailable.

    PATH lookup only -- the CLI arrives via ``pip install`` into the
    same environment (or any PATH-visible install). Windows resolves
    the pip-generated ``whisper.exe`` shim through the same call.
    """
    return shutil.which("whisper")


def whisper_install_hint() -> str:
    """Operator-facing text for the not-installed case."""
    return (
        "Audio transcription requires the local Whisper engine, which "
        "is not installed on this machine.\n\n"
        "Install it into the suite's Python environment:\n"
        "    pip install git+https://github.com/silvance/whisper.py.git\n\n"
        "plus ffmpeg on PATH. Everything runs locally -- no audio "
        "leaves this machine. Model weights download on first use, so "
        "run one transcription while online before going air-gapped."
    )


def transcribe_file(
    media_path: Path,
    *,
    model: str = DEFAULT_WHISPER_MODEL,
    cli_path: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Transcription:
    """Transcribe ``media_path`` via the whisper CLI. Blocking.

    Callers on a GUI thread must wrap this in a worker (Inscription
    uses the same QThread pattern as the LLM rewrite). Raises
    :class:`WhisperNotAvailableError` when the CLI is absent and
    :class:`TranscriptionFailedError` on any run failure -- both carry
    operator-appropriate messages.

    The CLI writes its JSON into a private temp directory (never next
    to the evidence file: writing artifacts beside evidence is bad
    practice and the source directory may be read-only, e.g. a mounted
    image).
    """
    cli = cli_path or find_whisper_cli()
    if cli is None:
        raise WhisperNotAvailableError(whisper_install_hint())
    if not media_path.is_file():
        msg = f"Audio file not found: {media_path}"
        raise TranscriptionFailedError(msg)

    with tempfile.TemporaryDirectory(prefix="inscription-whisper-") as tmp:
        cmd = [
            cli,
            str(media_path),
            "--model",
            model,
            "--output_format",
            "json",
            "--output_dir",
            tmp,
            # CPU-only workstations warn-and-fall-back without this;
            # passing it explicitly keeps stderr clean and behaviour
            # deterministic across machines.
            "--fp16",
            "False",
        ]
        creationflags = 0
        if sys.platform == "win32":  # pragma: no cover - Windows-only flag
            creationflags = subprocess.CREATE_NO_WINDOW
        logger.info("Transcribing %s with whisper %s model", media_path.name, model)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            msg = (
                f"Transcription of {media_path.name} exceeded "
                f"{int(timeout_s // 60)} minutes and was stopped. Try a "
                "smaller model, or split the audio."
            )
            raise TranscriptionFailedError(msg) from exc
        except OSError as exc:
            msg = f"Could not run the whisper CLI ({cli}): {exc}"
            raise TranscriptionFailedError(msg) from exc

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-_STDERR_TAIL_CHARS:]
            msg = (
                f"Whisper exited with code {proc.returncode} for "
                f"{media_path.name}.\n\n{tail}"
            )
            raise TranscriptionFailedError(msg)

        return _load_result(Path(tmp), media_path=media_path, model=model)


def _load_result(tmp: Path, *, media_path: Path, model: str) -> Transcription:
    """Parse whisper's JSON output from the temp directory.

    The CLI names the file after the input's stem. Defensive about
    shape: a fork/version that changes field names degrades to the
    flat text rather than failing the whole run.
    """
    json_path = tmp / f"{media_path.stem}.json"
    if not json_path.is_file():
        # Some versions sanitise stems; take any .json that appeared.
        candidates = sorted(tmp.glob("*.json"))
        if not candidates:
            msg = (
                f"Whisper reported success but wrote no JSON output for "
                f"{media_path.name}."
            )
            raise TranscriptionFailedError(msg)
        json_path = candidates[0]
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"Could not parse whisper output: {exc}"
        raise TranscriptionFailedError(msg) from exc

    text = str(raw.get("text", "")).strip()
    language = str(raw.get("language", ""))
    segments: list[TranscriptSegment] = []
    raw_segments = raw.get("segments")
    if isinstance(raw_segments, list):
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            try:
                segments.append(
                    TranscriptSegment(
                        start_s=float(item.get("start", 0.0)),
                        end_s=float(item.get("end", 0.0)),
                        text=str(item.get("text", "")),
                    )
                )
            except (TypeError, ValueError):
                continue
    if not text and not segments:
        msg = f"Whisper produced an empty transcript for {media_path.name}."
        raise TranscriptionFailedError(msg)
    return Transcription(
        source_path=media_path,
        model=model,
        language=language,
        text=text,
        segments=segments,
    )
