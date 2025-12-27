import json
import os
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.json"
EXAMPLE_CONFIG_PATH = APP_DIR / "config.example.json"


def load_config() -> dict:
    if DEFAULT_CONFIG_PATH.exists():
        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    with open(EXAMPLE_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()
ALSA_DEVICE = CFG.get("alsa_device", "hw:2,0")
MUSIC_ROOT = Path(CFG.get("music_root", "/mnt/music")).resolve()
LOG_FILE = CFG.get("log_file", "/var/log/pi-audio-endpoint.log")
BIND_HOST = CFG.get("bind_host", "0.0.0.0")
BIND_PORT = int(CFG.get("bind_port", 8099))


def log(line: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"{ts} {line}\n"
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg)
    except Exception:
        pass


def safe_resolve_path(p: str) -> Path:
    raw = Path(p)
    resolved = (raw if raw.is_absolute() else (MUSIC_ROOT / raw)).resolve()

    try:
        resolved.relative_to(MUSIC_ROOT)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Path is outside music_root: {MUSIC_ROOT}")

    if not resolved.exists():
        raise HTTPException(status_code=404, detail="File does not exist")

    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    return resolved


@dataclass
class PlayerState:
    status: Literal["stopped", "playing", "error"] = "stopped"
    current_file: Optional[str] = None
    started_utc: Optional[float] = None
    pid: Optional[int] = None
    last_error: Optional[str] = None
    last_exit_code: Optional[int] = None


STATE = PlayerState()
STATE_LOCK = threading.Lock()
PROC: Optional[subprocess.Popen] = None


class PlayRequest(BaseModel):
    path: str = Field(..., description="Relative to music_root or absolute under music_root")
    kind: Literal["auto", "flac", "wav", "pcm"] = "auto"

    pcm_format: Optional[Literal["S32_LE", "S24_LE", "S16_LE"]] = None
    pcm_rate: Optional[int] = None
    pcm_channels: Optional[int] = None


app = FastAPI(title="Pi Audio Endpoint", version="1.0.0")


def stop_playback_locked(reason: str) -> None:
    global PROC
    if PROC and PROC.poll() is None:
        try:
            log(f"STOP reason={reason} pid={PROC.pid}")
            os.killpg(os.getpgid(PROC.pid), signal.SIGTERM)
            try:
                PROC.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(PROC.pid), signal.SIGKILL)
        except Exception as e:
            log(f"STOP error={e!r}")

    PROC = None
    STATE.status = "stopped"
    STATE.current_file = None
    STATE.started_utc = None
    STATE.pid = None


def spawn_player(cmd: str, display_file: str) -> None:
    global PROC
    log(f"PLAY cmd={cmd}")
    PROC = subprocess.Popen(
        ["/bin/sh", "-lc", cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
        text=True,
        bufsize=1,
    )

    with STATE_LOCK:
        STATE.status = "playing"
        STATE.current_file = display_file
        STATE.started_utc = time.time()
        STATE.pid = PROC.pid
        STATE.last_error = None
        STATE.last_exit_code = None

    assert PROC.stdout is not None
    for line in PROC.stdout:
        log(f"OUT {line.rstrip()}")

    rc = PROC.wait()
    with STATE_LOCK:
        STATE.last_exit_code = rc
        if rc == 0:
            log("EXIT ok")
            stop_playback_locked("natural_end")
        else:
            log(f"EXIT rc={rc}")
            STATE.status = "error"
            STATE.last_error = f"Player exited with code {rc}"
            PROC = None
            STATE.pid = None


def build_command(req: PlayRequest, file_path: Path) -> str:
    ext = file_path.suffix.lower()

    kind = req.kind
    if kind == "auto":
        if ext == ".flac":
            kind = "flac"
        elif ext in [".wav", ".wave"]:
            kind = "wav"
        elif ext in [".pcm", ".raw"]:
            kind = "pcm"
        else:
            raise HTTPException(status_code=400, detail=f"Unknown file type: {ext}")

    f = shlex.quote(str(file_path))

    if kind == "flac":
        # Decode FLAC to WAV (PCM S32_LE) so aplay reads rate/channels from the header.
        # Fixes packed-24-bit playback and prevents 8000 Hz slow playback.
        return (
            f"/usr/bin/ffmpeg -loglevel error -i {f} -f wav -c:a pcm_s32le -ac 2 - | "
            f"/usr/bin/aplay -D {shlex.quote(ALSA_DEVICE)}"
        )

    if kind == "wav":
        return f"/usr/bin/aplay -D {shlex.quote(ALSA_DEVICE)} {f}"

    if kind == "pcm":
        if not req.pcm_format or not req.pcm_rate or not req.pcm_channels:
            raise HTTPException(
                status_code=400,
                detail="For kind=pcm, pcm_format, pcm_rate and pcm_channels are required",
            )
        return (
            f"/usr/bin/aplay -D {shlex.quote(ALSA_DEVICE)} "
            f"-f {shlex.quote(req.pcm_format)} "
            f"-r {int(req.pcm_rate)} "
            f"-c {int(req.pcm_channels)} {f}"
        )

    raise HTTPException(status_code=400, detail="Unsupported kind")


@app.get("/status")
def status():
    with STATE_LOCK:
        return asdict(STATE)


@app.post("/stop")
def stop():
    with STATE_LOCK:
        stop_playback_locked("api_stop")
        return {"ok": True}


@app.post("/play")
def play(req: PlayRequest):
    file_path = safe_resolve_path(req.path)

    with STATE_LOCK:
        stop_playback_locked("preempt_for_new_play")

    cmd = build_command(req, file_path)
    t = threading.Thread(target=spawn_player, args=(cmd, str(file_path)), daemon=True)
    t.start()

    return {"ok": True, "playing": str(file_path), "device": ALSA_DEVICE}


def main():
    import uvicorn
    log(f"START bind={BIND_HOST}:{BIND_PORT} device={ALSA_DEVICE} root={MUSIC_ROOT}")
    uvicorn.run("app:app", host=BIND_HOST, port=BIND_PORT, log_level="warning")


if __name__ == "__main__":
    main()