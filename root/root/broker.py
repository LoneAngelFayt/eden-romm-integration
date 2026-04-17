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

PORT       = int(os.environ.get("BROKER_PORT", "8000"))
SECRET     = os.environ.get("BROKER_SECRET", "")
ROM_ROOT   = Path(os.environ.get("ROM_ROOT", "/romm/library")).resolve()
SAVE_SLOT  = int(os.environ.get("SAVE_SLOT", "1"))      # default slot for save-and-exit (1–8)
SSTATE_WAIT = float(os.environ.get("SSTATE_WAIT", "3.0"))  # seconds to wait after save key

# Configurable xdotool key strings — verify against the running container.
# Set BROKER_LOG_LEVEL=DEBUG and check Eden's Emulation → Hotkeys menu,
# then override these defaults in your compose file.
SAVE_STATE_KEY = os.environ.get("SAVE_STATE_KEY", "")   # e.g. "ctrl+F5"
LOAD_STATE_KEY = os.environ.get("LOAD_STATE_KEY", "")   # e.g. "F5"
# Comma-separated list of per-slot selection keys, one per slot (slots 1–8).
# e.g. "ctrl+F1,ctrl+F2,ctrl+F3,ctrl+F4,ctrl+F5,ctrl+F6,ctrl+F7,ctrl+F8"
_slot_keys_raw = os.environ.get("SLOT_KEYS", "")
SLOT_KEYS: list[str] = [k.strip() for k in _slot_keys_raw.split(",") if k.strip()]

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
    "process":          None,
    "rom_path":         None,
    "rom_name":         None,
    "started_at":       None,
    "is_managed":       False,
    "save_in_progress": False,
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
    # Actual key names are verified during first-run testing; update if needed.
    target = {
        "UI": {
            "confirmClose": "false",
            "fullscreen": "true",
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
                        # Qt INI uses "key\default=val" or "key=val" format
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


def _kill_eden():
    """Kill the managed eden process group. Releases lock before waiting."""
    with _session_lock:
        _session["is_managed"] = False
        proc = _session["process"]
        _session["process"] = None

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
    """Read Eden stdout/stderr line-by-line and emit as [eden] DEBUG log entries.

    Keeps the broker log self-contained — no separate journald/syslog needed.
    Only active at DEBUG level; at INFO the subprocess output is discarded.
    """
    try:
        for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line:
                log.debug("[eden] %s", line)
    except Exception as exc:
        log.debug("_log_eden_output: reader exited: %s", exc)


def _launch_eden_internal(rom_path):
    """Launch /usr/bin/eden as abc via sudo+env.

    No --batch equivalent: the Qt event loop must stay alive for xdotool
    input delivery and SDL gamepad polling to work.
    """
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
    """On unexpected exit, relaunch the dashboard if the session is still managed.

    Backs off for 5 s if Eden died almost immediately (< 5 s) to avoid a
    tight crash loop. Normal kills (via _kill_eden) set is_managed=False first,
    so this watcher is a no-op for intentional stops.
    """
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
    time.sleep(2)  # let killed process and sockets settle before re-launching
    with _session_lock:
        _session["rom_path"] = rom_path
        _session["rom_name"] = Path(rom_path).stem if rom_path else "Dashboard"
        _session["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _launch_eden_internal(rom_path)

# ── xdotool helpers ───────────────────────────────────────────────────────────

# Minimal env for xdotool: needs DISPLAY and HOME for X11 auth.
_XDOTOOL_ENV = {
    "DISPLAY":         ":0",
    "HOME":            "/config",
    "USER":            "abc",
    "XDG_RUNTIME_DIR": ENV["XDG_RUNTIME_DIR"],
}


def _xdotool_find_window() -> str | None:
    """Return the X11 window ID for eden, or None if not found.

    Searches by PID first (more precise); falls back to classname search
    in case pgrep returns multiple pids or the window is not yet mapped.
    """
    try:
        pids = subprocess.check_output(
            ["pgrep", "-x", "eden"], text=True
        ).split()
        log.debug("_xdotool_find_window: pgrep found pids=%s", pids)
    except subprocess.CalledProcessError:
        log.error("_xdotool_find_window: eden process not found via pgrep")
        return None

    xdo_base = (
        ["sudo", "-u", "abc", "env"]
        + [f"{k}={v}" for k, v in _XDOTOOL_ENV.items()]
        + ["xdotool"]
    )

    for pid in pids:
        cmd = xdo_base + ["search", "--onlyvisible", "--pid", pid]
        log.debug("_xdotool_find_window: trying cmd=%s", " ".join(cmd))
        try:
            out = subprocess.check_output(cmd, text=True, timeout=5)
            ids = out.strip().split()
            log.debug("_xdotool_find_window: pid=%s window ids=%s", pid, ids)
            if ids:
                wid = ids[-1]
                log.debug("_xdotool_find_window: selected window %s for pid %s", wid, pid)
                return wid
        except subprocess.CalledProcessError as exc:
            log.debug("_xdotool_find_window: pid=%s search returned non-zero: %s", pid, exc)
        except Exception as exc:
            log.debug("_xdotool_find_window: pid=%s search failed: %s", pid, exc)

    # Fallback: search by classname
    cmd = xdo_base + ["search", "--onlyvisible", "--classname", "eden"]
    log.debug("_xdotool_find_window: classname fallback cmd=%s", " ".join(cmd))
    try:
        out = subprocess.check_output(cmd, text=True, timeout=5)
        ids = out.strip().split()
        if ids:
            wid = ids[-1]
            log.debug("_xdotool_find_window: found window %s via classname", wid)
            return wid
        log.debug("_xdotool_find_window: classname search returned no windows")
    except subprocess.CalledProcessError as exc:
        log.debug("_xdotool_find_window: classname search returned non-zero: %s", exc)
    except Exception as exc:
        log.debug("_xdotool_find_window: classname search failed: %s", exc)

    log.error("_xdotool_find_window: Eden window not found")
    return None


def _xdotool_key(wid: str, key: str) -> bool:
    """Send a single key to the Eden window. Returns False on any error."""
    cmd = (
        ["sudo", "-u", "abc", "env"]
        + [f"{k}={v}" for k, v in _XDOTOOL_ENV.items()]
        + ["xdotool", "key", "--window", wid, key]
    )
    log.debug("_xdotool_key: cmd=%s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, timeout=5, capture_output=True, text=True)
        if result.returncode == 0:
            log.debug("_xdotool_key: key=%r delivered to window %s", key, wid)
            return True
        log.error(
            "_xdotool_key: key=%r failed (rc=%d) stdout=%r stderr=%r",
            key, result.returncode, result.stdout, result.stderr,
        )
        return False
    except Exception as exc:
        log.error("_xdotool_key: key=%r exception: %s", key, exc)
        return False


def _xdotool_save_state(slot: int) -> bool:
    """Save emulator state to slot (1–8) via configured xdotool hotkeys.

    If SLOT_KEYS is configured and has an entry for this slot, sends the
    slot-selection key first. Then sends SAVE_STATE_KEY. Waits SSTATE_WAIT
    seconds for the write to complete.

    Returns False if SAVE_STATE_KEY is not configured or key delivery fails.
    """
    if not SAVE_STATE_KEY:
        log.error(
            "_xdotool_save_state: SAVE_STATE_KEY is not set — "
            "set it via the SAVE_STATE_KEY env var (check Eden's hotkey settings)"
        )
        return False

    wid = _xdotool_find_window()
    if wid is None:
        return False

    # Send slot-selection key if configured for this slot.
    effective_slot = slot if 1 <= slot <= 8 else 1
    if SLOT_KEYS and effective_slot <= len(SLOT_KEYS):
        slot_key = SLOT_KEYS[effective_slot - 1]
        log.debug("_xdotool_save_state: sending slot key %r for slot %d", slot_key, effective_slot)
        if not _xdotool_key(wid, slot_key):
            return False
        time.sleep(0.1)  # brief pause so Eden registers the slot change

    log.debug("_xdotool_save_state: sending save key %r (slot %d)", SAVE_STATE_KEY, effective_slot)
    if not _xdotool_key(wid, SAVE_STATE_KEY):
        return False

    log.info(
        "_xdotool_save_state: save key sent (slot %d, window %s) — waiting %.1fs",
        effective_slot, wid, SSTATE_WAIT,
    )
    time.sleep(SSTATE_WAIT)
    return True


def _xdotool_load_state(slot: int) -> bool:
    """Load emulator state from slot (1–8) via configured xdotool hotkeys.

    Sends the slot-selection key (if SLOT_KEYS is configured) then LOAD_STATE_KEY.
    Returns False if LOAD_STATE_KEY is not configured or key delivery fails.
    """
    if not LOAD_STATE_KEY:
        log.error(
            "_xdotool_load_state: LOAD_STATE_KEY is not set — "
            "set it via the LOAD_STATE_KEY env var (check Eden's hotkey settings)"
        )
        return False

    wid = _xdotool_find_window()
    if wid is None:
        return False

    effective_slot = slot if 1 <= slot <= 8 else 1
    if SLOT_KEYS and effective_slot <= len(SLOT_KEYS):
        slot_key = SLOT_KEYS[effective_slot - 1]
        log.debug("_xdotool_load_state: sending slot key %r for slot %d", slot_key, effective_slot)
        if not _xdotool_key(wid, slot_key):
            return False
        time.sleep(0.1)

    log.debug("_xdotool_load_state: sending load key %r (slot %d)", LOAD_STATE_KEY, effective_slot)
    if not _xdotool_key(wid, LOAD_STATE_KEY):
        return False

    log.info(
        "_xdotool_load_state: load key sent (slot %d, window %s)",
        effective_slot, wid,
    )
    return True


def _save_and_exit(slot: int) -> bool:
    """Save emulator state then kill Eden. Returns True if save key was delivered."""
    ok = _xdotool_save_state(slot)
    _kill_eden()
    return ok


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
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _session["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                _session["save_in_progress"] = True
            body = self._read_body()
            slot = body.get("slot", SAVE_SLOT)
            if not isinstance(slot, int) or not (1 <= slot <= 8):
                with _session_lock:
                    _session["save_in_progress"] = False
                self._send_json(400, {"error": "slot must be 1–8"})
                return
            wait = body.get("wait", True)
            if wait:
                try:
                    ok = _save_and_exit(slot)
                finally:
                    with _session_lock:
                        _session["save_in_progress"] = False
                if not ok:
                    log.warning("save-and-exit: save key failed (slot %d) — killed anyway", slot)
                self._send_json(200, {"status": "ok", "saved": ok, "slot": slot})
                Thread(target=_launch_eden, args=(None,), daemon=True).start()
            else:
                def _bg(s):
                    try:
                        ok = _save_and_exit(s)
                    finally:
                        with _session_lock:
                            _session["save_in_progress"] = False
                    if not ok:
                        log.warning("save-and-exit: save key failed (slot %d) — killed anyway", s)
                    _launch_eden(None)
                Thread(target=_bg, args=(slot,), daemon=True).start()
                self._send_json(200, {"status": "queued", "slot": slot})
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

        if self.path == "/save-state":
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _session["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                _session["save_in_progress"] = True
            body = self._read_body()
            slot = body.get("slot", 1)
            if not isinstance(slot, int) or not (1 <= slot <= 8):
                with _session_lock:
                    _session["save_in_progress"] = False
                self._send_json(400, {"error": "slot must be 1–8"})
                return

            def _bg_save(s):
                try:
                    ok = _xdotool_save_state(s)
                finally:
                    with _session_lock:
                        _session["save_in_progress"] = False
                if not ok:
                    log.warning("save-state: key delivery failed for slot %d", s)

            Thread(target=_bg_save, args=(slot,), daemon=True).start()
            self._send_json(200, {"status": "saving", "slot": slot})
            return

        if self.path == "/load-state":
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
            body = self._read_body()
            slot = body.get("slot", 1)
            if not isinstance(slot, int) or not (1 <= slot <= 8):
                self._send_json(400, {"error": "slot must be 1–8"})
                return
            ok = _xdotool_load_state(slot)
            self._send_json(
                200 if ok else 500,
                {"status": "ok" if ok else "error", "loaded": ok, "slot": slot},
            )
            return

        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
            return

        with _session_lock:
            if _session["save_in_progress"]:
                self._send_json(409, {"error": "save in progress"})
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

    # Log startup config at DEBUG so failures are diagnosable without code changes.
    redacted_env = {k: ("***" if k == "BROKER_SECRET" else v) for k, v in ENV.items()}
    log.debug("Startup ENV: %s", redacted_env)
    log.debug(
        "Hotkeys — SAVE_STATE_KEY=%r  LOAD_STATE_KEY=%r  SLOT_KEYS=%r",
        SAVE_STATE_KEY, LOAD_STATE_KEY, SLOT_KEYS,
    )

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
