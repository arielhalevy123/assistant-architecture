"""Voice-note transcription for the assistant.

Wraps whisper.cpp so voice messages arriving on any listener become plain text on
`stream.jsonl`, exactly as if the owner had typed them. This is what makes the
"send a voice note to the assistant" flow actually work end-to-end — before this, the file was
saved and the summary field just said "[voice]", so the assistant had no idea what was said.

WHY WHISPER.CPP AND NOT THE OPENAI API
--------------------------------------
the design principle is that state stays local and secrets stay minimal. The Whisper
API is faster and cheaper per call, but adds a key the assistant does not otherwise need, and
routes every voice note the owner ever sends through an external service. whisper.cpp
runs on the local Metal GPU, needs no key, and is fast enough for a personal-scale
inbox (a 20-second voice note transcribes in under two seconds on M-series silicon).

If the binary or model is missing the transcript field is left None and the record
still lands on the stream — so a broken transcription install degrades gracefully
into the pre-transcription behaviour rather than dropping the message entirely.

MODEL CHOICE
------------
`small` (multilingual, ~466 MB) is the smallest model that reliably transcribes
Hebrew. `base` produces gibberish on Hebrew often enough that using it here would
be worse than no transcript at all. If quality is still not good enough, upgrade
to `medium` (~1.5 GB); no code change is required, just re-point ASSISTANT_WHISPER_MODEL.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

# The binary is installed by `brew install whisper-cpp`. The homebrew CLI is named
# `whisper-cli` on newer versions; older installs shipped `whisper-cpp`. Try both.
_CANDIDATES = ("whisper-cli", "whisper-cpp", "main")
WHISPER_BIN = os.environ.get("ASSISTANT_WHISPER_BIN") or next(
    (b for b in _CANDIDATES if shutil.which(b)), None
)

# Model file. Kept alongside the assistant's other runtime assets so a fresh install is one
# scripted download rather than a fold-of-a-README ceremony.
MODEL_DIR = Path.home() / ".assistant" / "models"
MODEL_NAME = os.environ.get("ASSISTANT_WHISPER_MODEL", "ggml-small.bin")
MODEL_PATH = Path(os.environ.get("ASSISTANT_WHISPER_MODEL_PATH", str(MODEL_DIR / MODEL_NAME)))

# Telegram voice notes arrive as OGG/Opus. whisper.cpp reads WAV only, so we transcode
# once through ffmpeg. Mono 16 kHz is what whisper.cpp expects and rejecting anything
# else would just make callers guess the shape.
FFMPEG_BIN = os.environ.get("ASSISTANT_FFMPEG", "ffmpeg")


class TranscribeUnavailable(Exception):
    """Raised when the environment is not set up for transcription. Callers should
    log and continue rather than treat this as a hard error — a missing model is not
    a reason to drop the owner's voice message."""


def _ensure_ready() -> None:
    if not WHISPER_BIN:
        raise TranscribeUnavailable(
            "whisper.cpp binary not found; install with `brew install whisper-cpp` "
            "or set ASSISTANT_WHISPER_BIN"
        )
    if not MODEL_PATH.exists():
        raise TranscribeUnavailable(
            f"whisper model not found at {MODEL_PATH}; run scripts/fetch_whisper_model.sh "
            "or set ASSISTANT_WHISPER_MODEL_PATH"
        )
    if not shutil.which(FFMPEG_BIN):
        raise TranscribeUnavailable(f"ffmpeg not on PATH (looked for {FFMPEG_BIN!r})")


def _to_wav(src: Path, dst: Path) -> None:
    """Transcode any audio ffmpeg can read into 16 kHz mono WAV.

    -ac 1 -ar 16000 mirrors whisper.cpp's expected input; anything else forces the
    binary into a resample path that changes accuracy in subtle ways. -y overwrites
    a leftover temp file from a previous crash rather than failing the whole batch.
    """
    subprocess.run(
        [FFMPEG_BIN, "-y", "-loglevel", "error", "-i", str(src),
         "-ac", "1", "-ar", "16000", str(dst)],
        check=True, capture_output=True, timeout=60,
    )


def transcribe(audio_path: Path, language: str | None = "he") -> dict:
    """Transcribe one audio file and return {'text': str, 'language': str, ...}.

    `language` is a hint, not a lock: whisper.cpp will still detect if the hint is
    wrong. Default is Hebrew because the owner speaks Hebrew; passing None asks
    whisper to auto-detect, which is a shade slower and no more accurate on his voice.
    """
    _ensure_ready()
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)

    started = time.monotonic()
    tmpdir = Path(tempfile.mkdtemp(prefix="ted_stt_"))
    try:
        wav = tmpdir / "audio.wav"
        _to_wav(audio_path, wav)

        out_prefix = tmpdir / "out"
        cmd = [
            WHISPER_BIN,
            "-m", str(MODEL_PATH),
            "-f", str(wav),
            "-of", str(out_prefix),
            "-oj",                          # emit JSON alongside the plain text
            "-nt",                          # suppress timestamps in the plain output
        ]
        if language:
            cmd += ["-l", language]

        # -pp is "print progress"; the assistant never wants stdout chatter, and the JSON side
        # file is the only thing we read. Timeout is generous: a 60s voice note
        # transcribes in a few seconds on M-series, but a cold model load costs 1-2 s.
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(f"whisper.cpp failed rc={r.returncode}: "
                               f"{r.stderr[:400].decode('utf-8', 'replace')}")

        # Prefer the JSON side-file: it carries the detected language, which we want
        # to log so a mis-hint is visible rather than silent.
        json_path = out_prefix.with_suffix(".json")
        if json_path.exists():
            data = json.loads(json_path.read_text())
            segments = data.get("transcription") or []
            text = " ".join(seg.get("text", "").strip() for seg in segments).strip()
            detected = data.get("result", {}).get("language") or language
        else:
            text = (out_prefix.with_suffix(".txt").read_text().strip()
                    if out_prefix.with_suffix(".txt").exists() else "")
            detected = language

        return {
            "text": text,
            "language": detected,
            "duration_s": round(time.monotonic() - started, 2),
            "model": MODEL_PATH.name,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def transcribe_or_none(audio_path: Path, language: str | None = "he") -> dict | None:
    """Best-effort wrapper: never raises. Logs and returns None on any failure.

    Used from the listener hot path, where a broken transcribe install must not
    make the assistant drop the voice note — the file is still saved, the record still lands
    on stream.jsonl, downstream can see the transcript is absent and act accordingly.
    """
    try:
        return transcribe(Path(audio_path), language=language)
    except TranscribeUnavailable as e:
        logging.warning("transcribe unavailable: %s", e)
        return None
    except Exception as e:                       # noqa: BLE001
        logging.warning("transcribe failed for %s: %s", audio_path, e)
        return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: transcribe.py <audio_file> [language]", file=sys.stderr)
        sys.exit(2)
    out = transcribe(Path(sys.argv[1]), language=sys.argv[2] if len(sys.argv) > 2 else "he")
    print(json.dumps(out, ensure_ascii=False, indent=2))
