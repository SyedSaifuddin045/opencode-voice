"""tts-agent CLI: daemon lifecycle, status, test, voices, benchmark, launchd.

Commands:
    run               foreground daemon (used by launchd / start)
    start             spawn detached daemon, wait for health
    stop              graceful shutdown (HTTP, then pid fallback)
    restart           stop + start
    status            JSON status (running:false when down)
    health            exit 0 only when daemon answers /health
    test [text]       speak text via daemon (or in-process fallback)
    voices            list Kokoro voices
    benchmark         load/warmup/latency/RTF/memory report
    install-launchd   install + load user LaunchAgent
    uninstall-launchd unload + remove LaunchAgent
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import Config, default_config_path, load_config

LOG = logging.getLogger("opencode_voice.cli")


def _base_url(cfg: Config) -> str:
    return f"http://{cfg.host}:{cfg.port}"


def _request(cfg: Config, method: str, path: str, payload: dict | None = None,
             timeout: float = 2.0) -> tuple[int, dict]:
    url = _base_url(cfg) + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return resp.status, (json.loads(body) if body else {})


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _read_pid(cfg: Config) -> int | None:
    try:
        pid = int(Path(cfg.pid_file).read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


# --------------------------------------------------------------- commands


def cmd_run(cfg: Config) -> int:
    """Foreground daemon: load model, warm up, serve forever."""
    from .playback import pick_player
    from .server import SpeechService, build_server
    from .tts import KokoroTTS

    state_dir = Path(cfg.pid_file).parent
    state_dir.mkdir(parents=True, exist_ok=True)

    existing = _read_pid(cfg)
    if existing and existing != os.getpid():
        print(f"daemon already running (pid {existing})", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    t_start = time.perf_counter()
    print("[start] TTS daemon starting", flush=True)

    tts = KokoroTTS(model_id=cfg.model, voice=cfg.voice, speed=cfg.speed)
    if cfg.enabled:
        tts.load()
        tts.warmup()
    print(f"[ready] model loaded, total startup "
          f"{time.perf_counter() - t_start:.2f}s", flush=True)

    player = pick_player(cfg.playback, volume=cfg.volume)
    print(f"[ready] playback backend: {type(player).__name__}", flush=True)

    service = SpeechService(cfg, tts, player)
    service.start()
    httpd, thread = build_server(cfg, service)

    Path(cfg.pid_file).write_text(str(os.getpid()))
    print(f"[serving] {_base_url(cfg)} (localhost only)", flush=True)

    if cfg.idle_shutdown_s > 0:
        def _idle_watch():
            while True:
                time.sleep(5)
                idle = time.time() - service.last_activity
                if idle >= cfg.idle_shutdown_s:
                    print(f"[idle] no requests for {idle:.0f}s; exiting",
                          flush=True)
                    os.kill(os.getpid(), signal.SIGTERM)
                    return

        threading.Thread(target=_idle_watch, name="idle-watch",
                         daemon=True).start()
        print(f"[idle] auto-exit after {cfg.idle_shutdown_s}s without requests",
              flush=True)

    def _terminate(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _terminate)
    try:
        while thread.is_alive():
            thread.join(timeout=1.0)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        print("[stop] shutting down", flush=True)
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
        service.shutdown()
        try:
            Path(cfg.pid_file).unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def cmd_start(cfg: Config) -> int:
    if _request_status(cfg) is not None:
        print("daemon already running")
        return 0
    log_path = Path(cfg.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    with open(log_path, "ab") as out:
        subprocess.Popen(
            [sys.executable, "-m", "opencode_voice.cli", "run"],
            stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True, env=env,
        )
    # wait for health
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            status, body = _request(cfg, "GET", "/health", timeout=1.0)
            if status == 200 and body.get("ok"):
                print(f"daemon started: {_base_url(cfg)}")
                return 0
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    print(f"daemon did not become healthy; check {log_path}", file=sys.stderr)
    return 1


def cmd_stop(cfg: Config) -> int:
    stopped = False
    try:
        _request(cfg, "POST", "/shutdown", {}, timeout=2.0)
        stopped = True
    except (urllib.error.URLError, OSError):
        pass
    pid = _read_pid(cfg)
    if pid:
        deadline = time.time() + 5
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                stopped = True
            except OSError:
                pass
    if stopped or pid:
        print("daemon stopped")
        return 0
    print("daemon not running")
    return 0


def cmd_restart(cfg: Config) -> int:
    cmd_stop(cfg)
    time.sleep(0.4)
    return cmd_start(cfg)


def _read_enabled_file(cfg: Config) -> bool:
    try:
        return Path(cfg.enabled_file).read_text().strip() != "0"
    except OSError:
        return True


def cmd_set_enabled(cfg: Config, value: bool) -> int:
    """on/off: mute or unmute speech without touching the loaded model."""
    try:
        status, body = _request(cfg, "POST", "/enabled",
                                {"enabled": value}, timeout=1.5)
        if status == 200:
            print("enabled" if body.get("enabled") else "disabled")
            return 0
    except (urllib.error.URLError, OSError):
        pass
    # daemon down: persist for next start
    Path(cfg.enabled_file).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.enabled_file).write_text("1" if value else "0")
    state = "enabled" if value else "disabled"
    print(f"daemon not running; speech {state} for next start")
    return 0


def cmd_toggle(cfg: Config) -> int:
    st = _request_status(cfg)
    if st is not None:
        return cmd_set_enabled(cfg, not st.get("enabled", True))
    return cmd_set_enabled(cfg, not _read_enabled_file(cfg))


def _request_status(cfg: Config) -> dict | None:
    try:
        status, body = _request(cfg, "GET", "/status", timeout=1.5)
        return body if status == 200 else None
    except (urllib.error.URLError, OSError):
        return None


def cmd_status(cfg: Config) -> int:
    body = _request_status(cfg)
    if body is None:
        body = {"running": False, "enabled": _read_enabled_file(cfg),
                "model_loaded": False, "speaking": False, "queue_length": 0}
    print(json.dumps(body, indent=2))
    return 0


def cmd_health(cfg: Config) -> int:
    try:
        status, body = _request(cfg, "GET", "/health", timeout=1.5)
    except (urllib.error.URLError, OSError):
        print("daemon not running / unreachable", file=sys.stderr)
        return 1
    if status == 200 and body.get("ok"):
        print("ok" + (" (model loaded)" if body.get("model_loaded") else
                      " (model loading)"))
        return 0
    print(f"unhealthy: {body}", file=sys.stderr)
    return 1


def cmd_test(cfg: Config, text: str) -> int:
    try:
        status, body = _request(cfg, "POST", "/speak", {"text": text}, timeout=3.0)
    except (urllib.error.URLError, OSError):
        print("daemon not running; synthesizing in-process "
              "(first run loads the model)...", flush=True)
        return _test_inprocess(cfg, text)
    if status == 202:
        print(f"queued {body.get('sentences', 0)} sentence(s)")
        return 0
    print(f"unexpected response: {status} {body}", file=sys.stderr)
    return 1


def _test_inprocess(cfg: Config, text: str) -> int:
    from .playback import pick_player
    from .server import SpeechService
    from .tts import KokoroTTS

    tts = KokoroTTS(model_id=cfg.model, voice=cfg.voice, speed=cfg.speed)
    tts.load()
    tts.warmup()
    player = pick_player(cfg.playback, volume=cfg.volume)
    service = SpeechService(cfg, tts, player)
    service.start()
    t0 = time.perf_counter()
    n = service.speak(text)
    # wait for synthesis + playback to fully drain
    while True:
        st = service.status()
        if not st["synth_pending"] and not st["queue_length"] and not st["speaking"]:
            # one extra tick so a just-popped sentence registers
            time.sleep(0.1)
            st = service.status()
            if not st["synth_pending"] and not st["queue_length"] and not st["speaking"]:
                break
        time.sleep(0.05)
    print(f"spoke {n} sentence(s) in {time.perf_counter() - t0:.2f}s "
          f"({type(player).__name__})")
    service.shutdown()
    return 0 if n else 1


def cmd_voices(cfg: Config) -> int:
    from .tts import VOICES

    for v in VOICES:
        print(v)
    return 0


def cmd_benchmark(cfg: Config) -> int:
    import resource

    from .tts import KokoroTTS

    report: dict = {"model": cfg.model, "voice": cfg.voice}
    tts = KokoroTTS(model_id=cfg.model, voice=cfg.voice, speed=cfg.speed)

    t0 = time.perf_counter()
    tts.load()
    report["model_load_s"] = round(time.perf_counter() - t0, 3)

    tts.warmup()
    report["warmup_s"] = round(tts.warmup_seconds or 0.0, 3)

    samples = [
        "I found the problem with the Docker configuration.",
        "The uploads directory was being deleted during initialization.",
        "I changed the code to create the directory instead. All tests passed.",
    ]
    # time-to-first-audio: synth one short sentence
    t0 = time.perf_counter()
    pcm, sr = tts.synth(samples[0])
    ttfb = time.perf_counter() - t0
    dur = len(pcm) / 4 / sr
    report["time_to_first_audio_s"] = round(ttfb, 3)
    report["audio_seconds"] = round(dur, 3)
    report["real_time_factor"] = round(ttfb / dur, 3) if dur else None

    # sustained throughput over all samples
    t0 = time.perf_counter()
    total_audio = 0.0
    for s in samples:
        pcm, sr = tts.synth(s)
        total_audio += len(pcm) / 4 / sr
    elapsed = time.perf_counter() - t0
    report["batch_audio_seconds"] = round(total_audio, 3)
    report["batch_synth_seconds"] = round(elapsed, 3)
    report["batch_rtf"] = round(elapsed / total_audio, 3) if total_audio else None

    import sys as _sys

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss: bytes on macOS, KiB on Linux
    report["peak_rss_mb"] = round(rss / (1024 ** 2), 1) if _sys.platform == "darwin" else round(rss / 1024, 1)
    print(json.dumps(report, indent=2))
    return 0


LAUNCHD_LABEL = "com.opencode.voice"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _launchd_plist_content() -> str:
    shim = Path.home() / ".local" / "bin" / "tts-agent"
    state = Path.home() / ".local" / "state" / "opencode-voice"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{shim}</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StandardOutPath</key>
  <string>{state}/launchd.log</string>
  <key>StandardErrorPath</key>
  <string>{state}/launchd.log</string>
  <key>WorkingDirectory</key>
  <string>{Path.home() / "Programming" / "opencode-voice"}</string>
</dict>
</plist>
"""


def _launchctl(*args: str) -> int:
    return subprocess.call(["launchctl", *args])


def cmd_install_launchd(cfg: Config) -> int:
    plist = _launchd_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(_launchd_plist_content())
    Path(cfg.pid_file).parent.mkdir(parents=True, exist_ok=True)
    print(f"wrote {plist}")
    _launchctl("bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}")
    rc = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist))
    if rc != 0:
        print("bootstrap failed; trying kickstart", file=sys.stderr)
        rc = _launchctl("kickstart", "-k",
                        f"gui/{os.getuid()}/{LAUNCHD_LABEL}")
    print("launch agent installed" if rc == 0 else "launchctl error",
          file=sys.stderr)
    return 0 if rc == 0 else 1


def cmd_uninstall_launchd(cfg: Config) -> int:
    _launchctl("bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}")
    plist = _launchd_plist_path()
    if plist.exists():
        plist.unlink()
        print(f"removed {plist}")
    print("launch agent uninstalled")
    return 0


# ------------------------------------------------------------------ entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tts-agent",
        description="Local Kokoro/MLX text-to-speech daemon for OpenCode",
    )
    parser.add_argument("--config", default=None,
                        help=f"config file (default {default_config_path()})")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("restart")
    sub.add_parser("status")
    sub.add_parser("health")
    sub.add_parser("on")
    sub.add_parser("off")
    sub.add_parser("toggle")
    p_test = sub.add_parser("test")
    p_test.add_argument("text", nargs="?",
                        default="Hello, this is the local OpenCode voice.")
    sub.add_parser("voices")
    sub.add_parser("benchmark")
    sub.add_parser("install-launchd")
    sub.add_parser("uninstall-launchd")

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if not args.command:
        parser.print_help()
        return 2

    cfg = load_config(args.config)
    port_override = os.environ.get("OC_VOICE_PORT")
    if port_override:
        cfg.port = int(port_override)

    handlers = {
        "run": lambda: cmd_run(cfg),
        "start": lambda: cmd_start(cfg),
        "stop": lambda: cmd_stop(cfg),
        "restart": lambda: cmd_restart(cfg),
        "status": lambda: cmd_status(cfg),
        "health": lambda: cmd_health(cfg),
        "on": lambda: cmd_set_enabled(cfg, True),
        "off": lambda: cmd_set_enabled(cfg, False),
        "toggle": lambda: cmd_toggle(cfg),
        "test": lambda: cmd_test(cfg, args.text),
        "voices": lambda: cmd_voices(cfg),
        "benchmark": lambda: cmd_benchmark(cfg),
        "install-launchd": lambda: cmd_install_launchd(cfg),
        "uninstall-launchd": lambda: cmd_uninstall_launchd(cfg),
    }
    try:
        return handlers[args.command]()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
