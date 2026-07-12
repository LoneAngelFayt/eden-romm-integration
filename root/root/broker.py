#!/usr/bin/env python3
"""broker.py — launch Eden on demand and expose a small HTTP API."""

import glob
import hmac
import io
import json
import logging
import os
import signal
import socket as _socket
import subprocess
import sys
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
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


def _build_sdl_defaults_for_player(idx: int) -> dict[str, str]:
    """Generate the standard SDL pad mapping for a single player slot.

    Each Switch port (player) is independent in qt-config.ini under keys
    `player_<idx>_*`, with `port:<idx>` inside the SDL engine descriptor.
    We seed all eight slots so multi-controller users do not get keyboard
    fallbacks on players 1-7.
    """
    return {
        f"player_{idx}_button_a":      f'"engine:sdl,port:{idx},guid:{_SDL_GUID},button:1"',
        f"player_{idx}_button_b":      f'"engine:sdl,port:{idx},guid:{_SDL_GUID},button:0"',
        f"player_{idx}_button_x":      f'"engine:sdl,port:{idx},guid:{_SDL_GUID},button:3"',
        f"player_{idx}_button_y":      f'"engine:sdl,port:{idx},guid:{_SDL_GUID},button:2"',
        f"player_{idx}_button_lstick": f'"engine:sdl,port:{idx},guid:{_SDL_GUID},button:9"',
        f"player_{idx}_button_rstick": f'"engine:sdl,port:{idx},guid:{_SDL_GUID},button:10"',
        f"player_{idx}_button_l":      f'"engine:sdl,port:{idx},guid:{_SDL_GUID},button:4"',
        f"player_{idx}_button_r":      f'"engine:sdl,port:{idx},guid:{_SDL_GUID},button:5"',
        f"player_{idx}_button_zl":     f'"engine:sdl,port:{idx},guid:{_SDL_GUID},axis:2,threshold:0.500000,invert:+"',
        f"player_{idx}_button_zr":     f'"engine:sdl,port:{idx},guid:{_SDL_GUID},axis:5,threshold:0.500000,invert:+"',
        f"player_{idx}_button_plus":   f'"engine:sdl,port:{idx},guid:{_SDL_GUID},button:7"',
        f"player_{idx}_button_minus":  f'"engine:sdl,port:{idx},guid:{_SDL_GUID},button:6"',
        f"player_{idx}_button_dleft":  f'"engine:sdl,port:{idx},guid:{_SDL_GUID},hat:0,direction:left"',
        f"player_{idx}_button_dup":    f'"engine:sdl,port:{idx},guid:{_SDL_GUID},hat:0,direction:up"',
        f"player_{idx}_button_dright": f'"engine:sdl,port:{idx},guid:{_SDL_GUID},hat:0,direction:right"',
        f"player_{idx}_button_ddown":  f'"engine:sdl,port:{idx},guid:{_SDL_GUID},hat:0,direction:down"',
        f"player_{idx}_button_home":   f'"engine:sdl,port:{idx},guid:{_SDL_GUID},button:8"',
        f"player_{idx}_lstick":        f'"engine:sdl,port:{idx},guid:{_SDL_GUID},axis_x:0,axis_y:1,offset_x:-0.000000,offset_y:0.000000,invert_x:+,invert_y:+,deadzone:0.150000"',
        f"player_{idx}_rstick":        f'"engine:sdl,port:{idx},guid:{_SDL_GUID},axis_x:3,axis_y:4,offset_x:-0.000000,offset_y:0.000000,invert_x:+,invert_y:+,deadzone:0.150000"',
    }


# All 8 Switch player slots get SDL defaults. Selkies presents up to 4 virtual
# pads in practice, but seeding all 8 keeps the config consistent with Eden's
# expectations and leaves room for future multi-pad streaming.
PLAYER_SDL_DEFAULTS: dict[str, str] = {
    k: v
    for idx in range(8)
    for k, v in _build_sdl_defaults_for_player(idx).items()
}

# Eden (Nintendo Switch) does not support emulator-level save states.
# The Switch's own save system is used instead — games save to NAND via the
# normal in-game save menu.  The /save-state and /load-state endpoints return
# 501 Not Implemented; /save-and-exit simply kills the game and returns to the
# dashboard.

XDG_RUNTIME_DIR = "/config/.XDG"
X11_SOCKET_DIR  = "/tmp/.X11-unix"


def _live_x_sockets() -> list[Path]:
    """Return X11 sockets that have a listening peer, newest first.
    A bare /tmp/.X11-unix/X<N> file with no Xwayland behind it is a stale
    lock that must be skipped or the broker will hand Eden a dead display."""
    candidates: list[tuple[float, Path]] = []
    try:
        for entry in os.listdir(X11_SOCKET_DIR):
            if not (entry.startswith("X") and entry[1:].isdigit()):
                continue
            p = Path(X11_SOCKET_DIR) / entry
            try:
                st = p.stat()
                with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                    s.settimeout(0.2)
                    s.connect(str(p))
                candidates.append((st.st_mtime, p))
            except OSError:
                continue
    except OSError:
        pass
    candidates.sort(reverse=True)
    return [p for _, p in candidates]


def _detect_display(default: str = ":0") -> str:
    """Return the live X display, preferring the most recently created socket.
    Falls back to $DISPLAY then `default` if no live socket can be probed."""
    live = _live_x_sockets()
    if live:
        return f":{live[0].name[1:]}"
    return os.environ.get("DISPLAY", default)


def _detect_wayland_display(default: str = "wayland-1") -> str:
    """Return the most recent wayland-* socket name in $XDG_RUNTIME_DIR.
    Falls back to $WAYLAND_DISPLAY then `default`."""
    runtime = Path(XDG_RUNTIME_DIR)
    try:
        socks = sorted(
            (p for p in runtime.iterdir() if p.name.startswith("wayland-") and p.is_socket()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if socks:
            return socks[0].name
    except OSError:
        pass
    return os.environ.get("WAYLAND_DISPLAY", default)


def _wait_for_x_display(timeout: float = 30.0) -> str | None:
    """Block until at least one live X socket appears, returning the display.
    Returns None on timeout. Used at startup so the broker doesn't race the
    desktop on slow hosts."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        live = _live_x_sockets()
        if live:
            return f":{live[0].name[1:]}"
        time.sleep(0.25)
    return None


# ENV passed to the Eden subprocess via sudo -u abc env.
# DISPLAY        — detected at runtime (Xwayland may land on :1 if /tmp lock
#                  files persist across container restarts on Podman).
# WAYLAND_DISPLAY — likewise detected from XDG_RUNTIME_DIR.
# LD_PRELOAD      — joystick interposer redirects /dev/input/* opens to selkies
#                   sockets; libudev.so.1.0.0-fake is intentionally excluded —
#                   it intercepts Mesa/DRI udev calls and causes a black screen.
ENV = {
    "DISPLAY":            _detect_display(),
    "WAYLAND_DISPLAY":    _detect_wayland_display(),
    "XDG_RUNTIME_DIR":    XDG_RUNTIME_DIR,
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

# Eden's stdout/stderr is captured to this file so renderer/Vulkan/Qt errors
# are visible after the fact, regardless of broker log level. /config is the
# only host-mapped writable path we can rely on.
EDEN_LOG_PATH = Path(os.environ.get("EDEN_LOG_PATH", "/config/eden.log"))

# UID/GID to chown the log file to; defaults to the linuxserver-image abc
# user but respects PUID/PGID overrides.
_EDEN_LOG_UID = int(os.environ.get("PUID", "1000"))
_EDEN_LOG_GID = int(os.environ.get("PGID", "1000"))

# ── In-game save sync ─────────────────────────────────────────────────────────
# Eden has no save states, so RomM syncs the Switch's own NAND save data
# instead: GET /save-file zips every save file modified since the last game
# launch, PUT /save-file restores a previously pulled archive before launch.
# Per-title dirs live under <data>/nand/user/save/0000000000000000/<uuid>/<tid>.
#
# The data dir is probed because the linuxserver image has shipped different
# dir names across versions (same reasoning as the qt-config.ini candidates).
_SAVE_DATA_ROOTS = (
    Path("/config/.local/share/eden"),
    Path("/config/.local/share/Eden"),
    Path("/config/.local/share/yuzu"),
)
# Archive members must live under one of these root-relative subtrees; PUT
# rejects anything else so a crafted zip cannot touch configs or keys.
SAVE_SYNC_SUBTREES = ("nand/user/save",)
SAVE_FILE_MAX_BYTES = 256 * 1024 * 1024
# Zip stores mtimes at 2 s DOS resolution; the slack keeps the newer-file
# guard from skipping files over rounding alone.
_SAVE_MTIME_SLACK = 2.0


def _save_data_root() -> Path | None:
    """Return Eden's data dir, honouring a SAVE_DATA_ROOT override."""
    env = os.environ.get("SAVE_DATA_ROOT")
    if env:
        return Path(env)
    for c in _SAVE_DATA_ROOTS:
        if c.is_dir():
            return c
    return None


def _iter_save_files(root: Path) -> list[Path]:
    """Every regular file under the allowed save subtrees, sorted for a
    deterministic archive (identical content zips to identical bytes)."""
    files: list[Path] = []
    for sub in SAVE_SYNC_SUBTREES:
        base = root / sub
        if not base.is_dir():
            continue
        files.extend(
            p for p in sorted(base.rglob("*")) if p.is_file() and not p.is_symlink()
        )
    return files


def _build_save_archive(baseline: float) -> bytes | None:
    """Zip every save file modified since the last game launch.

    Returns None when there is nothing to sync — no data dir yet, or no file
    changed since `baseline`. Member paths are relative to the data dir so a
    later PUT restores them regardless of which candidate dir is live."""
    root = _save_data_root()
    if root is None:
        log.debug("save-file: no Eden data dir found")
        return None
    changed: list[Path] = []
    total = 0
    for p in _iter_save_files(root):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime >= baseline:
            changed.append(p)
            total += st.st_size
    if not changed:
        return None
    if total > SAVE_FILE_MAX_BYTES:
        log.warning("save-file: changed saves exceed size limit (%d bytes)", total)
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in changed:
            try:
                zf.write(p, p.relative_to(root).as_posix())
            except OSError as exc:
                log.warning("save-file: could not read %s — %s", p, exc)
    return buf.getvalue()


def _mkdirs_owned(path: Path) -> None:
    """mkdir -p with abc ownership on every directory this call creates, so
    Eden (running as abc) can keep writing saves inside them later."""
    missing: list[Path] = []
    cur = path
    while not cur.exists():
        missing.append(cur)
        cur = cur.parent
    path.mkdir(parents=True, exist_ok=True)
    for d in reversed(missing):
        try:
            os.chown(d, _EDEN_LOG_UID, _EDEN_LOG_GID)
        except OSError:
            pass


def _extract_save_archive(content: bytes) -> tuple[int, int] | str:
    """Restore a pulled save archive into the data dir.

    Returns (written, skipped) on success, or an error string for a bad
    archive. Existing files newer than their archive member are skipped so a
    restore can never roll back saves made since the archive was taken."""
    root = _save_data_root() or _SAVE_DATA_ROOTS[0]
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return "body is not a zip archive"
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > SAVE_FILE_MAX_BYTES:
            return "archive exceeds size limit when extracted"
        for info in infos:
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                return f"archive member escapes save dir: {info.filename}"
            if not any(
                member.as_posix().startswith(sub + "/") for sub in SAVE_SYNC_SUBTREES
            ):
                return f"archive member outside save subtrees: {info.filename}"

        written = skipped = 0
        for info in infos:
            target = root / PurePosixPath(info.filename)
            mtime = time.mktime(info.date_time + (0, 0, -1))
            try:
                if (
                    target.exists()
                    and target.stat().st_mtime > mtime + _SAVE_MTIME_SLACK
                ):
                    skipped += 1
                    continue
                _mkdirs_owned(target.parent)
                tmp = target.parent / f".{target.name}.tmp"
                tmp.write_bytes(zf.read(info))
                os.chown(tmp, _EDEN_LOG_UID, _EDEN_LOG_GID)
                os.replace(tmp, target)
                os.utime(target, (mtime, mtime))
            except OSError as exc:
                return f"could not write {info.filename}: {exc}"
            written += 1
    return (written, skipped)

# ── Session state ─────────────────────────────────────────────────────────────

_session_lock = Lock()
_session: dict = {
    "process":    None,
    "rom_path":   None,
    "rom_name":   None,
    "started_at": None,
    "is_managed": False,
    # Monotonic launch counter used as a session identity token. String
    # timestamps with one-second resolution can collide across rapid
    # relaunches; a counter makes session-change detection unambiguous.
    "launch_id":  0,
    # Wall-clock stamp of the last GAME launch (not dashboard relaunches).
    # GET /save-file only ships files modified at or after this point; it
    # survives game exit so RomM can still pull after the session ends.
    "save_baseline": None,
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
    Three locations are checked because the linuxserver image has shipped
    different config-dir conventions across versions:
      - "Eden"  — current capitalised dir name used by recent releases.
      - "eden"  — older lowercase dir from early Eden builds.
      - "yuzu"  — Eden is a yuzu fork; configs migrated from yuzu are
                  occasionally still found at the original yuzu path on
                  long-lived /config volumes.
    First match wins. If none exists yet, we log and skip — Eden will create
    the file on first launch and the next broker cycle will patch it.
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
                    # Qt's QSettings INI dialect serialises some keys with a
                    # backslash suffix like `confirmClose\default=true`, where
                    # `\default=` distinguishes user values from compiled-in
                    # defaults. We accept all three forms (`key\`, `key=`,
                    # `key =`) so existing files are patched in place
                    # regardless of which form Qt happened to write.
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

        # Insert missing keys under their existing section header; only create
        # the section if the file doesn't have it at all. Blindly appending a
        # second [section] block at EOF works for QSettings but accumulates
        # duplicate headers over time.
        for section, keys in target.items():
            missing = {k: v for k, v in keys.items() if k not in applied[section]}
            if not missing:
                continue
            header_idx = next(
                (i for i, l in enumerate(new_lines) if l.strip() == f"[{section}]"),
                None,
            )
            add_lines = [f"{k}={v}" for k, v in missing.items()]
            if header_idx is None:
                new_lines.append(f"[{section}]")
                new_lines.extend(add_lines)
            else:
                new_lines[header_idx + 1:header_idx + 1] = add_lines
            for k in missing:
                log.warning("_patch_ini: [%s] %s not found — inserted", section, k)

        tmp = ini_path.with_suffix(".tmp")
        tmp.write_text("\n".join(new_lines) + "\n")
        tmp.replace(ini_path)
        log.info("_patch_ini: qt-config.ini patched")
        _seed_controller_config(ini_path)
    except OSError as exc:
        log.error("_patch_ini: filesystem error patching %s: %s — broker settings NOT applied", ini_path, exc)
    except Exception:
        log.exception("_patch_ini: unexpected failure — broker settings NOT applied")


def _seed_controller_config(ini_path: Path) -> None:
    """Replace non-SDL player input mappings with SDL defaults for all 8 slots.

    The linuxserver/eden container seeds /config/.config/eden/qt-config.ini
    from /defaults/qt-config.ini only when the file does not yet exist.  If
    the volume config was created by an older image version it will have
    keyboard-style engine mappings (engine:keyboard for buttons, or
    engine:analog_from_button for analog sticks).  This function repairs
    those on every launch so controller input always works for any player slot.
    """
    try:
        text = ini_path.read_text()
    except OSError as exc:
        log.error("_seed_controller_config: cannot read %s: %s", ini_path, exc)
        return

    non_sdl = ("engine:keyboard", "engine:analog_from_button")
    if not any(e in text for e in non_sdl):
        log.debug("_seed_controller_config: already SDL engine, skipping")
        return

    lines = text.splitlines()
    new_lines = []
    replaced = 0
    for line in lines:
        stripped = line.strip()
        seeded = False
        for key, sdl_val in PLAYER_SDL_DEFAULTS.items():
            if stripped.startswith(f"{key}=") and "engine:sdl" not in stripped:
                new_lines.append(f"{key}={sdl_val}")
                replaced += 1
                seeded = True
                break
        if not seeded:
            new_lines.append(line)

    if replaced:
        try:
            tmp = ini_path.with_suffix(".tmp")
            tmp.write_text("\n".join(new_lines) + "\n")
            tmp.replace(ini_path)
        except OSError as exc:
            log.error("_seed_controller_config: failed to write %s: %s", ini_path, exc)
            return
        log.info("_seed_controller_config: replaced %d non-SDL mapping(s) with SDL defaults", replaced)


# ── xdotool helpers ───────────────────────────────────────────────────────────

_XDOTOOL_ENV = {
    "DISPLAY":    ENV["DISPLAY"],
    "XAUTHORITY": "/config/.Xauthority",
}


def _xdotool_find_window() -> str | None:
    """Return the first visible Eden window ID, or None if no window is mapped.

    Eden may briefly create multiple windows during startup (splash, main);
    the first match is sufficient for fullscreen toggling, which targets the
    focused window via xdotool's default key behaviour.
    """
    try:
        result = subprocess.run(
            ["sudo", "-u", "abc", "env",
             *[f"{k}={v}" for k, v in _XDOTOOL_ENV.items()],
             "xdotool", "search", "--onlyvisible", "--classname", "eden"],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        log.warning("_xdotool_find_window: xdotool search timed out (X server slow or hung)")
        return None
    except OSError as exc:
        log.warning("_xdotool_find_window: failed to invoke xdotool: %s", exc)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip().splitlines()[0]


def _trigger_fullscreen(launch_id: int) -> None:
    """Wait FULLSCREEN_DELAY seconds then send F11 to enter fullscreen.

    Activates the Eden window first, then sends F11 without --window so
    Eden's Qt event loop handles the toggle natively (same path as pressing
    F11 through the browser stream), avoiding the X11 input grab that
    targeting a window ID causes. An untargeted key goes to the focused
    window, so activation guarantees that window is Eden.

    Only called for game launches — dashboard runs windowed intentionally.
    Aborts (with a debug log) if a newer launch supersedes this one before
    the delay expires; the launch_id token cannot collide across sessions.
    """
    time.sleep(FULLSCREEN_DELAY)

    with _session_lock:
        if _session["launch_id"] != launch_id:
            log.debug("_trigger_fullscreen: launch %d superseded by %d, skipping",
                      launch_id, _session["launch_id"])
            return

    window_id = _xdotool_find_window()
    if not window_id:
        log.warning("_trigger_fullscreen: no Eden window found after %.1fs", FULLSCREEN_DELAY)
        return

    try:
        result = subprocess.run(
            ["sudo", "-u", "abc", "env",
             *[f"{k}={v}" for k, v in _XDOTOOL_ENV.items()],
             "xdotool", "windowactivate", "--sync", window_id, "key", "F11"],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        log.warning("_trigger_fullscreen: xdotool timed out sending F11")
        return
    except OSError as exc:
        log.warning("_trigger_fullscreen: xdotool failed to run: %s", exc)
        return
    if result.returncode == 0:
        log.info("_trigger_fullscreen: F11 sent")
    else:
        log.warning("_trigger_fullscreen: xdotool failed: %s", result.stderr.strip())


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
            try:
                proc.wait(timeout=10)
                log.debug("_kill_eden: process killed with SIGKILL")
            except subprocess.TimeoutExpired:
                log.error("Eden did not exit after SIGKILL — giving up")
    except ProcessLookupError:
        log.debug("_kill_eden: process already gone")


def _drain_gamepad_sockets():
    """Send EOF to each selkies gamepad socket before launching a new session.

    The selkies input_handler has two phases per connection:
      1. Sends config payload, awaits a 1-byte arch specifier from the client.
      2. Keep-alive loop: while self.running and not writer.is_closing().

    Connecting and immediately sending SHUT_WR causes readexactly(1) in phase 1
    to raise IncompleteReadError — the handler exits and removes itself from the
    active client list without ever entering phase 2.

    Phase-2 handlers exit via the reader.at_eof() patch applied in init.sh.

    Socket files that refuse connection are stale and are unlinked.
    """
    paths = sorted(
        glob.glob("/tmp/selkies_js*.sock") + glob.glob("/tmp/selkies_event*.sock")
    )
    if not paths:
        log.debug("Socket drain: no gamepad sockets found.")
        return

    drained = 0
    removed = 0
    for path in paths:
        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                s.connect(path)
                s.shutdown(_socket.SHUT_WR)
            drained += 1
        except OSError:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass

    log.debug(
        "Socket drain: sent EOF to %d socket(s), removed %d dead file(s) (of %d total).",
        drained, removed, len(paths),
    )


def _launch_eden_internal(rom_path):
    """Launch /usr/bin/eden as abc via sudo+env.

    Eden's stdout/stderr is redirected directly to EDEN_LOG_PATH (append mode,
    unbuffered, fd-level) instead of through a Python reader thread. This
    avoids the failure mode where the reader thread dies and Eden silently
    blocks on a full 64 KB pipe buffer; the kernel writes straight to disk.
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

    # Open the log file in append mode so we keep history across launches.
    # If we cannot open it (e.g., read-only /config), fall back to DEVNULL —
    # Eden still launches, just without captured output.
    log_fh = None
    try:
        log_fh = open(EDEN_LOG_PATH, "ab", buffering=0)
        try:
            os.chown(EDEN_LOG_PATH, _EDEN_LOG_UID, _EDEN_LOG_GID)
        except (OSError, PermissionError):
            pass
        log_fh.write(
            f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} launch (rom={rom_path or 'dashboard'}) ===\n".encode()
        )
        log_fh.flush()
    except OSError as exc:
        log.warning(
            "Cannot open %s for Eden output capture (%s); continuing without capture.",
            EDEN_LOG_PATH, exc,
        )

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh if log_fh else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
            preexec_fn=os.setpgrp,
        )
    except OSError as exc:
        log.error("_launch_eden_internal: failed to launch Eden: %s", exc)
        if log_fh:
            log_fh.close()
        with _session_lock:
            _session["process"] = None
            _session["is_managed"] = False
        return
    finally:
        # Popen dup'd the fd; close our handle so it isn't leaked.
        if log_fh:
            log_fh.close()

    with _session_lock:
        _session["process"] = proc
        _session["is_managed"] = True
    log.info("Eden launched (PID %d)", proc.pid)
    Thread(target=_monitor_process, args=(proc, time.monotonic()), daemon=True).start()


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


def _wait_for_no_eden(timeout: float = 3.0) -> bool:
    """Block until no `eden` process is running, up to `timeout` seconds.
    Returns True if the process is gone, False on timeout. Replaces a fixed
    sleep that papered over residual processes after _kill_eden."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(["pgrep", "-x", "eden"], capture_output=True)
        if result.returncode != 0:  # pgrep exits 1 when no process matches
            return True
        time.sleep(0.1)
    return False


def _launch_eden(rom_path):
    """Top-level launch: kill any running Eden, clean sockets, patch ini, launch."""
    _kill_eden()
    _drain_gamepad_sockets()
    _patch_ini()
    if not _wait_for_no_eden():
        log.warning("eden still running after kill+drain; relaunching anyway")

    with _session_lock:
        _session["launch_id"] += 1
        launch_id = _session["launch_id"]
        _session["rom_path"] = rom_path
        _session["rom_name"] = Path(rom_path).stem if rom_path else "Dashboard"
        # Only stamp a session start when an actual ROM is being played; the
        # dashboard is an idle state and should not appear in /status.
        _session["started_at"] = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if rom_path else None
        )
        # Dashboard relaunches keep the previous baseline so an end-of-session
        # save pull still sees the files the last game wrote.
        if rom_path:
            _session["save_baseline"] = time.time()
    _launch_eden_internal(rom_path)
    if rom_path:
        Thread(target=_trigger_fullscreen, args=(launch_id,), daemon=True).start()


# ── PulseAudio helpers ────────────────────────────────────────────────────────

_PACTL_CMD = [
    "sudo", "-u", "abc", "env",
    "PULSE_RUNTIME_PATH=/defaults",
    "HOME=/config",
    "USER=abc",
]


def _pactl(*args: str) -> subprocess.CompletedProcess:
    """Run pactl as abc so it connects to abc's PulseAudio instance.

    A hung or missing pactl is reported as a non-zero CompletedProcess (rather
    than raising) so the /volume and /mute handlers return a 500 instead of
    dropping the connection with an unhandled exception."""
    cmd = _PACTL_CMD + ["pactl"] + list(args)
    log.debug("_pactl: cmd=%s", " ".join(cmd))
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        log.error("pactl timed out: %s", " ".join(args))
        return subprocess.CompletedProcess(cmd, 124, "", "pactl timed out")
    except OSError as exc:
        log.error("pactl failed to run: %s", exc)
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


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
        self.end_headers()
        self.wfile.write(payload)
        log.debug("HTTP response: %d %s", code, body)

    def _read_body(self) -> dict:
        try:
            length = max(0, min(int(self.headers.get("Content-Length", 0)), 64 * 1024))
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
        elif not self._check_secret():
            # /health stays open for container healthchecks; all other GETs
            # require the shared secret, matching POST/DELETE.
            self._send_json(403, {"error": "forbidden"})
        elif self.path == "/status":
            with _session_lock:
                active = (
                    _session["process"] is not None
                    and _session["process"].poll() is None
                    and _session["rom_path"] is not None
                )
                rom_path   = _session["rom_path"]   if active else None
                rom_name   = _session["rom_name"]   if active else None
                started_at = _session["started_at"] if active else None
            self._send_json(200, {
                "active":     active,
                "rom_path":   rom_path,
                "rom_name":   rom_name,
                "started_at": started_at,
            })
        elif self.path == "/save-file":
            with _session_lock:
                baseline = _session["save_baseline"]
                rom_name = _session["rom_name"]
            if baseline is None:
                self._send_json(404, {"error": "no game has been launched"})
                return
            archive = _build_save_archive(baseline)
            if archive is None:
                self._send_json(404, {"error": "no save changes since last launch"})
                return
            # Header values must be latin-1; ROM stems can be anything.
            safe_name = "".join(
                c for c in (rom_name or "eden") if c.isascii() and c.isprintable()
            ).strip() or "eden"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(archive)))
            self.send_header("X-Save-Filename", f"{safe_name}.saves.zip")
            self.end_headers()
            self.wfile.write(archive)
            log.info("save-file: served archive (%d bytes)", len(archive))
        else:
            self._send_json(404, {"error": "not found"})

    def do_PUT(self):
        log.debug("HTTP PUT %s", self.path)
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return
        if self.path != "/save-file":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"error": "missing or empty body"})
            return
        if length > SAVE_FILE_MAX_BYTES:
            self._send_json(413, {"error": "archive too large"})
            return
        content = self.rfile.read(length)
        result = _extract_save_archive(content)
        if isinstance(result, str):
            self._send_json(400, {"error": result})
            return
        written, skipped = result
        log.info("save-file: restored archive — %d written, %d skipped", written, skipped)
        self._send_json(200, {"status": "ok", "written": written, "skipped": skipped})

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
                # _launch_eden kills any running instance first, so no separate
                # _kill_eden is needed here.
                Thread(target=_launch_eden, args=(None,), daemon=True).start()
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


# ── Main ──────────────────────────────────────────────────────────────────────

def _graceful_shutdown(server: HTTPServer, signum: int) -> None:
    """Stop the HTTP listener, then SIGTERM Eden so in-game saves get a
    chance to flush to NAND. Triggered on SIGTERM/SIGINT — the bare default
    `KeyboardInterrupt` handler does not cover SIGTERM from systemd."""
    log.info("Received signal %d — beginning graceful shutdown", signum)
    Thread(target=server.shutdown, daemon=True).start()
    _kill_eden()
    log.info("Shutdown complete")


def main():
    log.info("Broker starting — waiting for desktop X display...")
    if not SECRET:
        log.warning("BROKER_SECRET is not set — all POST/DELETE endpoints are unauthenticated")

    log.debug("Startup ENV: %s", {k: ("***" if k == "BROKER_SECRET" else v) for k, v in ENV.items()})

    display = _wait_for_x_display(timeout=30.0)
    if display is None:
        log.error("No live X display appeared within 30s; Eden will likely fail to launch")
    else:
        # Update ENV in case the live display differs from the one detected at
        # module load (Xwayland may not have come up yet then).
        ENV["DISPLAY"] = display
        _XDOTOOL_ENV["DISPLAY"] = display
        log.info("Desktop ready on DISPLAY=%s", display)

    # Kill any stale Eden instance left from a previous broker run.
    # SIGTERM first (allows in-game saves to flush to NAND), SIGKILL only if
    # the process refuses to exit. We poll for the process actually being gone
    # so the rest of startup doesn't race a half-dead Eden.
    result = subprocess.run(["pkill", "-15", "-x", "eden"], capture_output=True)
    if result.returncode == 0:
        log.info("Sent SIGTERM to stale eden instance(s) on startup.")
        if not _wait_for_no_eden(timeout=3.0):
            log.warning("Stale eden did not exit on SIGTERM — escalating to SIGKILL")
            subprocess.run(["pkill", "-9", "-x", "eden"], capture_output=True)
            if not _wait_for_no_eden(timeout=2.0):
                log.warning("Stale eden still running after SIGKILL — OS may be slow")

    _patch_ini()

    # Auto-launch the Eden game library so the stream shows something useful
    # while no game is running.
    Thread(target=_launch_eden, args=(None,), daemon=True).start()

    # ThreadingHTTPServer: /save-and-exit with wait=true kills Eden inline (up
    # to ~5s on a stubborn process); a single-threaded server would stall
    # /health and /status for the duration. Session state is lock-protected.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), BrokerHandler)
    log.info("Eden broker listening on port %d", PORT)
    if SECRET:
        log.info("Shared secret auth enabled")

    # Install a single handler for both SIGTERM (systemd stop) and SIGINT
    # (Ctrl-C). serve_forever()'s default KeyboardInterrupt path only covers
    # the latter; without an explicit handler, SIGTERM kills the broker mid-
    # write and any in-game NAND save the user just triggered is lost.
    def _handle(signum, _frame):
        _graceful_shutdown(server, signum)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
