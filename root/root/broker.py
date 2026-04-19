#!/usr/bin/env python3
"""broker.py — launch Eden on demand and expose a small HTTP API."""

import glob
import hmac
import json
import logging
import os
import signal
import socket as _socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread, Lock

# ── Config ────────────────────────────────────────────────────────────────────

PORT             = int(os.environ.get("BROKER_PORT", "8000"))
SECRET           = os.environ.get("BROKER_SECRET", "")
ROM_ROOT         = Path(os.environ.get("ROM_ROOT", "/romm/library")).resolve()
FULLSCREEN_DELAY = float(os.environ.get("FULLSCREEN_DELAY", "3.0"))

# SDL controller mappings for the selkies virtual "Microsoft X-Box 360 pad".
# GUID 000000004d6963726f736f6674205800 is the name-based SDL GUID Eden assigns
# to this device.  These values are sourced from /defaults/qt-config.ini
# shipped with the linuxserver/eden image.  The broker seeds them into the live
# config whenever it detects keyboard engine mappings (which are the container
# defaults when the volume config pre-dates the SDL defaults being added).
_SDL_GUID = "000000004d6963726f736f6674205800"
PLAYER_0_SDL_DEFAULTS: dict[str, str] = {
    "player_0_button_a":        f'"engine:sdl,port:0,guid:{_SDL_GUID},button:1"',
    "player_0_button_b":        f'"engine:sdl,port:0,guid:{_SDL_GUID},button:0"',
    "player_0_button_x":        f'"engine:sdl,port:0,guid:{_SDL_GUID},button:3"',
    "player_0_button_y":        f'"engine:sdl,port:0,guid:{_SDL_GUID},button:2"',
    "player_0_button_lstick":   f'"engine:sdl,port:0,guid:{_SDL_GUID},button:9"',
    "player_0_button_rstick":   f'"engine:sdl,port:0,guid:{_SDL_GUID},button:10"',
    "player_0_button_l":        f'"engine:sdl,port:0,guid:{_SDL_GUID},button:4"',
    "player_0_button_r":        f'"engine:sdl,port:0,guid:{_SDL_GUID},button:5"',
    "player_0_button_zl":       f'"engine:sdl,port:0,guid:{_SDL_GUID},axis:2,threshold:0.500000,invert:+"',
    "player_0_button_zr":       f'"engine:sdl,port:0,guid:{_SDL_GUID},axis:5,threshold:0.500000,invert:+"',
    "player_0_button_plus":     f'"engine:sdl,port:0,guid:{_SDL_GUID},button:7"',
    "player_0_button_minus":    f'"engine:sdl,port:0,guid:{_SDL_GUID},button:6"',
    "player_0_button_dleft":    f'"engine:sdl,port:0,guid:{_SDL_GUID},hat:0,direction:left"',
    "player_0_button_dup":      f'"engine:sdl,port:0,guid:{_SDL_GUID},hat:0,direction:up"',
    "player_0_button_dright":   f'"engine:sdl,port:0,guid:{_SDL_GUID},hat:0,direction:right"',
    "player_0_button_ddown":    f'"engine:sdl,port:0,guid:{_SDL_GUID},hat:0,direction:down"',
    "player_0_button_home":     f'"engine:sdl,port:0,guid:{_SDL_GUID},button:8"',
    "player_0_lstick":          f'"engine:sdl,port:0,guid:{_SDL_GUID},axis_x:0,axis_y:1,offset_x:-0.000000,offset_y:0.000000,invert_x:+,invert_y:+,deadzone:0.150000"',
    "player_0_rstick":          f'"engine:sdl,port:0,guid:{_SDL_GUID},axis_x:3,axis_y:4,offset_x:-0.000000,offset_y:0.000000,invert_x:+,invert_y:+,deadzone:0.150000"',
}

# Eden (Nintendo Switch) does not support emulator-level save states.
# The Switch's own save system is used instead — games save to NAND via the
# normal in-game save menu.  The /save-state and /load-state endpoints return
# 501 Not Implemented; /save-and-exit simply kills the game and returns to the
# dashboard.

# ENV passed to the Eden subprocess via sudo -u abc env.
# DISPLAY=:0      — Xwayland under labwc (pixelflux compositor chain)
# WAYLAND_DISPLAY — pixelflux creates wayland-1 in XDG_RUNTIME_DIR
# LD_PRELOAD      — joystick interposer redirects /dev/input/* opens to selkies
#                   sockets; libudev.so.1.0.0-fake is intentionally excluded —
#                   it intercepts Mesa/DRI udev calls and causes a black screen.
ENV = {
    "DISPLAY":            ":0",
    "WAYLAND_DISPLAY":    "wayland-1",
    "XDG_RUNTIME_DIR":    "/config/.XDG",
    "PULSE_RUNTIME_PATH": "/defaults",
    "DRI_NODE":           os.environ.get("DRI_NODE", ""),
    "DRINODE":            os.environ.get("DRINODE", ""),
    "HOME":               "/config",
    "USER":               "abc",
    "LD_PRELOAD":         "/usr/lib/selkies_joystick_interposer.so",
}

logging.basicConfig(
    level=getattr(logging, os.environ.get("BROKER_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [broker] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("broker")

# ── Session state ─────────────────────────────────────────────────────────────

_session_lock = Lock()
_session: dict = {
    "process":    None,
    "rom_path":   None,
    "rom_name":   None,
    "started_at": None,
    "is_managed": False,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_rom_path(raw: str) -> Path | None:
    """Resolve raw to an absolute path and confirm it lives under ROM_ROOT."""
    try:
        p = Path(raw).resolve()
    except (ValueError, OSError):
        return None
    if not p.is_relative_to(ROM_ROOT):
        return None
    return p


def _patch_ini():
    """Patch Eden's qt-config.ini to set required broker defaults.

    The path is discovered at runtime because Eden writes it on first launch.
    We look for the ini under /config/.config/ using common Eden/yuzu paths.
    If the file does not exist yet, we log a warning and skip — the broker
    calls this before every launch so it will be patched on the next cycle.
    """
    candidates = [
        Path("/config/.config/Eden/qt-config.ini"),
        Path("/config/.config/eden/qt-config.ini"),
        Path("/config/.config/yuzu/qt-config.ini"),
    ]
    ini_path: Path | None = None
    for c in candidates:
        if c.exists():
            ini_path = c
            log.debug("_patch_ini: found qt-config.ini at %s", ini_path)
            break

    if ini_path is None:
        log.warning(
            "_patch_ini: qt-config.ini not found in %s — skipping (Eden has not run yet?)",
            [str(c) for c in candidates],
        )
        return

    # Keys to patch: section → {key: value}.
    target = {
        "UI": {
            "confirmClose": "false",
            "fullscreen":   "true",
        },
    }

    try:
        lines = ini_path.read_text().splitlines()
        current_section: str | None = None
        applied: dict[str, set] = {s: set() for s in target}
        new_lines = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                current_section = stripped[1:-1]
                new_lines.append(line)
                continue

            if current_section in target:
                for key, val in target[current_section].items():
                    if stripped.startswith(f"{key}\\") or stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                        old = line
                        if "\\default=" in stripped:
                            new_line = f"{key}\\default={val}"
                        else:
                            new_line = f"{key}={val}"
                        new_lines.append(new_line)
                        applied[current_section].add(key)
                        log.debug("_patch_ini: [%s] %s: %r → %r", current_section, key, old.strip(), new_line)
                        break
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        # Append any keys not found in the existing file.
        for section, keys in target.items():
            missing = {k: v for k, v in keys.items() if k not in applied[section]}
            if missing:
                new_lines.append(f"[{section}]")
                for k, v in missing.items():
                    new_lines.append(f"{k}={v}")
                    log.warning("_patch_ini: [%s] %s not found — appended", section, k)

        tmp = ini_path.with_suffix(".tmp")
        tmp.write_text("\n".join(new_lines) + "\n")
        tmp.replace(ini_path)
        log.info("_patch_ini: qt-config.ini patched")
    except Exception as exc:
        log.error("_patch_ini: failed: %s", exc)

    _seed_controller_config(ini_path)


def _seed_controller_config(ini_path: Path) -> None:
    """Replace keyboard engine mappings for player 0 with SDL defaults.

    The linuxserver/eden container seeds /config/.config/eden/qt-config.ini
    from /defaults/qt-config.ini only when the file does not yet exist.  If
    the volume config was created by an older image version it will have
    keyboard engine mappings.  This function repairs those on every launch so
    controller input always works without manual UI configuration.
    """
    try:
        text = ini_path.read_text()
    except Exception as exc:
        log.error("_seed_controller_config: cannot read %s: %s", ini_path, exc)
        return

    if "engine:keyboard" not in text:
        log.debug("_seed_controller_config: already SDL engine, skipping")
        return

    lines = text.splitlines()
    new_lines = []
    replaced = 0
    for line in lines:
        stripped = line.strip()
        seeded = False
        for key, sdl_val in PLAYER_0_SDL_DEFAULTS.items():
            if stripped.startswith(f"{key}=") and "engine:keyboard" in stripped:
                new_lines.append(f"{key}={sdl_val}")
                replaced += 1
                seeded = True
                break
        if not seeded:
            new_lines.append(line)

    if replaced:
        tmp = ini_path.with_suffix(".tmp")
        tmp.write_text("\n".join(new_lines) + "\n")
        tmp.replace(ini_path)
        log.info("_seed_controller_config: replaced %d keyboard mapping(s) with SDL defaults", replaced)


# ── xdotool helpers ───────────────────────────────────────────────────────────

_XDOTOOL_ENV = {
    "DISPLAY":    ":0",
    "XAUTHORITY": "/config/.Xauthority",
}


def _xdotool_find_window() -> list[str]:
    """Return window IDs for all visible Eden windows."""
    result = subprocess.run(
        ["sudo", "-u", "abc", "env",
         *[f"{k}={v}" for k, v in _XDOTOOL_ENV.items()],
         "xdotool", "search", "--onlyvisible", "--classname", "eden"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return result.stdout.strip().splitlines()


def _trigger_fullscreen(launch_session_time: str) -> None:
    """Wait FULLSCREEN_DELAY seconds, then press F11 to enter true fullscreen.

    Aborts silently if the session has changed (different game launched or
    game was stopped) before the delay expires.
    """
    time.sleep(FULLSCREEN_DELAY)

    with _session_lock:
        if _session["started_at"] != launch_session_time:
            log.debug("_trigger_fullscreen: session changed, skipping F11")
            return

    ids = _xdotool_find_window()
    if not ids:
        log.warning("_trigger_fullscreen: no Eden window found after %.1fs", FULLSCREEN_DELAY)
        return

    win_id = ids[-1]
    # Use wmctrl to set _NET_WM_STATE_FULLSCREEN via the window manager.
    # This avoids X11 key injection which can trigger input grabs and break
    # selkies gamepad/keyboard delivery.
    win_id_hex = hex(int(win_id))
    result = subprocess.run(
        ["sudo", "-u", "abc", "env",
         *[f"{k}={v}" for k, v in _XDOTOOL_ENV.items()],
         "wmctrl", "-i", "-r", win_id_hex, "-b", "add,fullscreen"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode == 0:
        log.info("Fullscreen set via wmctrl (window %s)", win_id_hex)
    else:
        log.warning("_trigger_fullscreen: wmctrl failed: %s", result.stderr.strip())


def _kill_eden():
    """Kill the managed eden process group. Releases lock before waiting."""
    with _session_lock:
        _session["is_managed"] = False
        proc = _session["process"]
        _session["process"] = None
        _session["rom_path"] = None
        _session["rom_name"] = None
        _session["started_at"] = None

    if proc is None or proc.poll() is not None:
        log.debug("_kill_eden: no running process to kill")
        return

    log.info("Stopping Eden (PID %d)...", proc.pid)
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        log.debug("_kill_eden: SIGTERM sent to pgid %d", pgid)
        try:
            proc.wait(timeout=5)
            log.debug("_kill_eden: process exited cleanly after SIGTERM")
        except subprocess.TimeoutExpired:
            log.warning("Eden did not exit after SIGTERM — sending SIGKILL")
            os.killpg(pgid, signal.SIGKILL)
            proc.wait()
            log.debug("_kill_eden: process killed with SIGKILL")
    except ProcessLookupError:
        log.debug("_kill_eden: process already gone")


def _cleanup_stale_sockets():
    """Remove only unreachable selkies socket files.

    Does NOT send EOF — sending EOF disconnects the browser gamepad client,
    which breaks input for the new Eden instance. The interposer reconnects to
    existing live sockets automatically; we only clean up orphaned files.
    """
    paths = sorted(
        glob.glob("/tmp/selkies_js*.sock") + glob.glob("/tmp/selkies_event*.sock")
    )
    if not paths:
        log.debug("Socket cleanup: no gamepad sockets found.")
        return

    removed = 0
    for path in paths:
        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                s.connect(path)
            log.debug("Socket cleanup: %s is alive, leaving it.", path)
        except OSError:
            try:
                os.unlink(path)
                removed += 1
                log.debug("Socket cleanup: removed stale socket %s", path)
            except OSError:
                pass

    log.debug(
        "Socket cleanup: removed %d stale socket(s) of %d total.",
        removed, len(paths),
    )


def _log_eden_output(proc):
    """Read Eden stdout/stderr line-by-line and emit as [eden] DEBUG log entries."""
    try:
        for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line:
                log.debug("[eden] %s", line)
    except Exception as exc:
        log.debug("_log_eden_output: reader exited: %s", exc)


def _launch_eden_internal(rom_path):
    """Launch /usr/bin/eden as abc via sudo+env."""
    cmd = [
        "sudo", "-u", "abc", "env",
        *[f"{k}={v}" for k, v in ENV.items()],
        "/usr/bin/eden",
    ]
    if rom_path:
        cmd.append(str(rom_path))

    log.info("Launching Eden (rom=%s)", rom_path or "dashboard")
    log.debug("_launch_eden_internal: cmd=%s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setpgrp,
        )
    except Exception as exc:
        log.error("_launch_eden_internal: failed to launch Eden: %s", exc)
        with _session_lock:
            _session["process"] = None
            _session["is_managed"] = False
        return

    with _session_lock:
        _session["process"] = proc
        _session["is_managed"] = True
    log.info("Eden launched (PID %d)", proc.pid)
    Thread(target=_monitor_process, args=(proc, time.monotonic()), daemon=True).start()
    Thread(target=_log_eden_output, args=(proc,), daemon=True).start()


def _monitor_process(proc, start_time):
    """On unexpected exit, relaunch the dashboard if the session is still managed."""
    proc.wait()
    exit_code = proc.returncode
    duration = time.monotonic() - start_time
    log.debug(
        "_monitor_process: Eden exited (code=%s, duration=%.1fs)",
        exit_code, duration,
    )

    with _session_lock:
        should_relaunch = _session["is_managed"] and _session["process"] is proc

    if not should_relaunch:
        log.debug("_monitor_process: managed=False or proc replaced — not relaunching")
        return

    wait_time = 5 if duration < 5 else 1
    log.info(
        "Eden exited after %.1fs (code=%s) — relaunching dashboard in %ds",
        duration, exit_code, wait_time,
    )
    time.sleep(wait_time)

    with _session_lock:
        if not _session["is_managed"]:
            log.debug("_monitor_process: managed cleared during sleep — aborting relaunch")
            return

    _launch_eden(None)


def _launch_eden(rom_path):
    """Top-level launch: kill any running Eden, clean sockets, patch ini, launch."""
    _kill_eden()
    _cleanup_stale_sockets()
    _patch_ini()
    time.sleep(2)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _session_lock:
        _session["rom_path"] = rom_path
        _session["rom_name"] = Path(rom_path).stem if rom_path else "Dashboard"
        _session["started_at"] = started_at
    _launch_eden_internal(rom_path)
    if rom_path:
        Thread(target=_trigger_fullscreen, args=(started_at,), daemon=True).start()


# ── PulseAudio helpers ────────────────────────────────────────────────────────

_PACTL_CMD = [
    "sudo", "-u", "abc", "env",
    "PULSE_RUNTIME_PATH=/defaults",
    "HOME=/config",
    "USER=abc",
]


def _pactl(*args: str) -> subprocess.CompletedProcess:
    """Run pactl as abc so it connects to abc's PulseAudio instance."""
    cmd = _PACTL_CMD + ["pactl"] + list(args)
    log.debug("_pactl: cmd=%s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=5)


def _pactl_get_mute() -> bool | None:
    """Return current mute state as bool, or None on error."""
    result = _pactl("get-sink-mute", "@DEFAULT_SINK@")
    if result.returncode != 0:
        log.error("_pactl_get_mute: pactl failed: %s", result.stderr.strip())
        return None
    return result.stdout.strip().endswith("yes")


def _cleanup_sockets():
    """Restart selkies to flush all stale gamepad connections.
    s6-overlay brings selkies back automatically within a few seconds."""
    log.info("Socket cleanup: restarting selkies...")
    result = subprocess.run(["pkill", "-15", "-f", "selkies"], capture_output=True)
    if result.returncode == 0:
        log.info("Socket cleanup: selkies stopped, s6 will restart it shortly.")
    else:
        log.warning("Socket cleanup: selkies not found or already stopped.")


# ── HTTP handler ──────────────────────────────────────────────────────────────

class BrokerHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)

    def _check_secret(self) -> bool:
        if not SECRET:
            return True
        return hmac.compare_digest(
            self.headers.get("X-Broker-Secret", ""),
            SECRET,
        )

    def _send_json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)
        log.debug("HTTP response: %d %s", code, body)

    def _read_body(self) -> dict:
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 64 * 1024)
        except ValueError:
            length = 0
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        log.debug("HTTP GET %s", self.path)
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/status":
            with _session_lock:
                active = (
                    _session["process"] is not None
                    and _session["process"].poll() is None
                    and _session["rom_path"] is not None
                )
                snap = dict(_session) if active else {}
            self._send_json(200, {
                "active":     active,
                "rom_path":   snap.get("rom_path")   if active else None,
                "rom_name":   snap.get("rom_name")   if active else None,
                "started_at": snap.get("started_at") if active else None,
            })
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        log.debug("HTTP POST %s", self.path)
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return

        if self.path == "/cleanup":
            Thread(target=_cleanup_sockets, daemon=True).start()
            self._send_json(200, {"status": "cleanup started"})
            return

        if self.path == "/save-and-exit":
            # Eden does not support save states — just exit the game.
            # The Switch's own in-game save system handles persistence.
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
            body = self._read_body()
            wait = body.get("wait", True)
            log.info("save-and-exit: exiting game (no save state support)")
            if wait:
                _kill_eden()
                self._send_json(200, {"status": "ok", "saved": False})
                Thread(target=_launch_eden, args=(None,), daemon=True).start()
            else:
                def _bg():
                    _kill_eden()
                    _launch_eden(None)
                Thread(target=_bg, daemon=True).start()
                self._send_json(200, {"status": "queued", "saved": False})
            return

        if self.path == "/save-state":
            self._send_json(501, {"error": "save states are not supported by Eden"})
            return

        if self.path == "/load-state":
            self._send_json(501, {"error": "save states are not supported by Eden"})
            return

        if self.path == "/volume":
            body = self._read_body()
            level = body.get("level")
            if not isinstance(level, int) or not (0 <= level <= 100):
                self._send_json(400, {"error": "level must be an integer 0–100"})
                return
            result = _pactl("set-sink-volume", "@DEFAULT_SINK@", f"{level}%")
            if result.returncode != 0:
                self._send_json(500, {"error": "pactl failed", "detail": result.stderr.strip()})
                return
            log.info("Volume set to %d%%", level)
            self._send_json(200, {"status": "ok", "level": level})
            return

        if self.path == "/mute":
            body = self._read_body()
            if "mute" in body:
                mute_arg = "1" if body["mute"] else "0"
            else:
                mute_arg = "toggle"
            result = _pactl("set-sink-mute", "@DEFAULT_SINK@", mute_arg)
            if result.returncode != 0:
                self._send_json(500, {"error": "pactl failed", "detail": result.stderr.strip()})
                return
            mute_state = _pactl_get_mute()
            log.info("Mute %s", "on" if mute_state else "off")
            self._send_json(200, {"status": "ok", "mute": mute_state})
            return

        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
            return

        body = self._read_body()
        raw_path = body.get("rom_path", "").strip()

        if not raw_path:
            self._send_json(400, {"error": "rom_path is required"})
            return

        rom_path = _validate_rom_path(raw_path)
        if rom_path is None:
            self._send_json(400, {
                "error": "rom_path must be within ROM_ROOT",
                "rom_root": str(ROM_ROOT),
            })
            return
        if not rom_path.exists():
            self._send_json(422, {"error": "rom_path does not exist", "path": str(rom_path)})
            return

        Thread(target=_launch_eden, args=(str(rom_path),), daemon=True).start()
        self._send_json(200, {"status": "launching", "rom_path": str(rom_path)})

    def do_DELETE(self):
        log.debug("HTTP DELETE %s", self.path)
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return
        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
            return

        Thread(target=_launch_eden, args=(None,), daemon=True).start()
        log.info("Soft reset: returning to dashboard")
        self._send_json(200, {"status": "resetting"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Broker-Secret")
        self.end_headers()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("Broker starting — waiting 5s for desktop to initialise...")
    if not SECRET:
        log.warning("BROKER_SECRET is not set — all POST/DELETE endpoints are unauthenticated")

    log.debug("Startup ENV: %s", {k: ("***" if k == "BROKER_SECRET" else v) for k, v in ENV.items()})

    time.sleep(5)

    # Kill any stale Eden instance left from a previous broker run.
    result = subprocess.run(["pkill", "-9", "-x", "eden"], capture_output=True)
    if result.returncode == 0:
        log.info("Killed stale eden instance(s) on startup.")
        time.sleep(2)

    _patch_ini()

    # Auto-launch the Eden game library so the stream shows something useful
    # while no game is running.
    Thread(target=_launch_eden, args=(None,), daemon=True).start()

    server = HTTPServer(("0.0.0.0", PORT), BrokerHandler)
    log.info("Eden broker listening on port %d", PORT)
    if SECRET:
        log.info("Shared secret auth enabled")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
