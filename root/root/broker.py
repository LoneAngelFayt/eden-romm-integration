#!/usr/bin/env python3
"""broker.py — launch Eden on demand and expose a small HTTP API."""

import calendar
import glob
import hmac
import io
import json
import logging
import os
import re
import secrets
import signal
import socket as _socket
import subprocess
import sys
import time
import zipfile
from collections.abc import Iterable
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from threading import Thread, Lock
from urllib.parse import parse_qs, urlparse

# ── Config ────────────────────────────────────────────────────────────────────

PORT             = int(os.environ.get("BROKER_PORT", "8000"))
SECRET           = os.environ.get("BROKER_SECRET", "")
ROM_ROOT         = Path(os.environ.get("ROM_ROOT", "/romm/library")).resolve()
FULLSCREEN_DELAY = float(os.environ.get("FULLSCREEN_DELAY", "3.0"))

# JSON request bodies are tiny (a rom_path, a mute flag); anything larger is
# rejected with 413 rather than silently truncated.
_BODY_MAX_BYTES = 64 * 1024

# Stream token lifetime. The TTL is idle time, not absolute: every admitted
# request slides it forward, so it only fires on a session nobody is watching.
# Without it a token minted by /launch stays valid until an explicit release,
# and a container that loses its RomM side (crash, network partition, a user
# who just closes the tab) leaves the gate open indefinitely.
STREAM_TOKEN_TTL   = float(os.environ.get("STREAM_TOKEN_TTL",   "43200.0"))
# How long the superseded token keeps working after a re-issue. Relaunching
# into an already-open tab means the browser is still replaying the old
# stream_sid cookie while RomM navigates the iframe to the new URL; without
# this window every one of those in-flight requests 403s and the stream client
# reports a dropped connection and retries in a loop. Each reconnect opens
# another gamepad client on the selkies sockets, and the controller ends up
# bound to a stale one — a live picture with a dead pad.
STREAM_TOKEN_GRACE = float(os.environ.get("STREAM_TOKEN_GRACE", "120.0"))

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


# Eden is launched through `sudo -u abc env K=V ...`, and sudo's default
# ENV passed to the Eden subprocess via sudo -u abc env.
# DISPLAY        — detected at runtime (Xwayland may land on :1 if /tmp lock
#                  files persist across container restarts on Podman).
# WAYLAND_DISPLAY — likewise detected from XDG_RUNTIME_DIR.
# LD_PRELOAD      — joystick interposer redirects /dev/input/* opens to selkies
#                   sockets; libudev.so.1.0.0-fake is intentionally excluded —
#                   it intercepts Mesa/DRI udev calls and causes a black screen.
# sudo's default env_reset drops everything the container was started with, so
# only the names spelled out on the `env` line survive the hop. Every renderer
# knob an operator sets in docker-compose — VK_DRIVER_FILES,
# __GLX_VENDOR_LIBRARY_NAME, MESA_VK_DEVICE_SELECT — was silently discarded
# before it could take effect. Forward the vendor namespaces wholesale rather
# than an exact list so a knob we haven't heard of still arrives.
_GPU_ENV_PREFIXES = (
    "NVIDIA_", "VK_", "MESA_", "LIBGL_", "GALLIUM_", "RADV_", "AMD_",
    "DRI_", "LIBVA_", "VDPAU_", "__GLX_", "__NV_", "__EGL_", "__VK_",
)
# XDG_DATA_DIRS is not a GPU knob, but the Vulkan loader searches it for
# icd.d/ — dropping it hides ICDs installed outside /usr/share. DRINODE is the
# linuxserver base image's render-node selector, which misses the DRI_ prefix.
_GPU_ENV_NAMES = ("XDG_DATA_DIRS", "DRINODE")


def _gpu_env() -> dict[str, str]:
    """Graphics-related variables inherited from the container environment.

    Empty values are skipped: `env VAR=` sets the variable to the empty string,
    which for the likes of LIBGL_ALWAYS_SOFTWARE reads as set-and-false to some
    consumers and set-and-true to others. DRI_NODE and DRINODE used to be
    forwarded unconditionally this way, putting `DRINODE=` on every launch even
    when the operator had never set it."""
    return {
        k: v for k, v in os.environ.items()
        if v and (k.startswith(_GPU_ENV_PREFIXES) or k in _GPU_ENV_NAMES)
    }


# Captured once: what goes into ENV below and what gets logged at startup have
# to be the same set, or the log answers a question nobody asked.
GPU_ENV = _gpu_env()

ENV = {
    # Inherited GPU vars come first so the computed entries below always win:
    # DISPLAY and LD_PRELOAD are derived from live container state and must not
    # be shadowed by a stale value from the container environment.
    **GPU_ENV,
    "DISPLAY":            _detect_display(),
    "WAYLAND_DISPLAY":    _detect_wayland_display(),
    "XDG_RUNTIME_DIR":    XDG_RUNTIME_DIR,
    "PULSE_RUNTIME_PATH": "/defaults",
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

# Stream gate enforcement. "token" enforces the nginx auth_request gate: a
# request reaches the desktop only if it carries the session token that
# POST /launch mints. "off" admits everything.
#
# The default is "off" because RomM has no way to send that token yet.
# rommapp/romm#3211 is merged and is what people are running, and its claim
# response hands the browser the operator's configured host with nothing
# appended; the half that carries the token through to the iframe URL is
# rommapp/romm#3856, still open. Enforcing against a client that cannot
# possibly comply refuses the document, every asset and the WebSocket upgrade
# alike, which is a total lockout rather than a gate. Set STREAM_GATE=token
# once #3856 ships, and see the README's Security section for what running
# with it off exposes.
#
# Declared here rather than beside STREAM_TOKEN_GRACE with the other stream
# constants because resolving the value can warn, and `log` does not exist
# that early in the module.
STREAM_GATE_MODES   = ("off", "token")
STREAM_GATE_DEFAULT = "off"


def _resolve_stream_gate(raw: str) -> str:
    """Normalize a STREAM_GATE value to one of STREAM_GATE_MODES.

    An unrecognized value resolves to "off" rather than to the current
    default. That is on purpose and does not track the default: a typo must
    fail toward a reachable stream, never toward one nobody can open.
    """
    mode = (raw or STREAM_GATE_DEFAULT).strip().lower()
    if mode not in STREAM_GATE_MODES:
        log.warning(
            "STREAM_GATE=%r is not one of %s, falling back to 'off', "
            "so the stream gate will not be enforced",
            raw,
            ", ".join(STREAM_GATE_MODES),
        )
        return "off"
    return mode


STREAM_GATE = _resolve_stream_gate(os.environ.get("STREAM_GATE", STREAM_GATE_DEFAULT))


def _log_stream_gate_mode() -> None:
    """Announce stream gate enforcement at startup, in both directions.

    The permissive case names what is exposed and how to close it, because an
    operator should never have to deduce that the desktop is open. The
    enforcing case says so too, so "is the gate actually on" is answerable
    from `docker logs pcsx2` alone.
    """
    if STREAM_GATE == "token":
        log.info(
            "Stream gate enforced: the desktop admits only requests carrying "
            "the stream token that POST /launch mints"
        )
        return
    log.warning(
        "STREAM_GATE=off, the stream gate is NOT enforced: anyone who can reach "
        "port 3000 or 3001 gets the interactive desktop and the ROM library at "
        "/files, with no credential. This is the default while RomM has no way "
        "to send the token (rommapp/romm#3856). Set STREAM_GATE=token to close it."
    )




# Report the forwarded GPU environment at startup. Renderer complaints almost
# always begin with "my env vars aren't taking effect", and this line answers
# that question from the broker log without a shell in the container.
_forwarded_gpu = sorted(GPU_ENV)
if _forwarded_gpu:
    log.info("Forwarding GPU environment to Eden: %s", ", ".join(_forwarded_gpu))
else:
    log.info(
        "No GPU environment variables found to forward. If the renderer falls back to "
        "llvmpipe, run `vulkaninfo --summary` in the container: NVIDIA absent means the "
        "ICD was never injected (check NVIDIA_DRIVER_CAPABILITIES includes 'graphics'); "
        "NVIDIA present means the failure is at surface creation instead."
    )

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
    deterministic archive (identical content zips to identical bytes).

    Dot-prefixed path components are excluded: a restore writes each member
    through a `.<name>.tmp` staging file in the same directory, and a GET that
    overlaps one must not sweep that half-written temp into the archive."""
    files: list[Path] = []
    for sub in SAVE_SYNC_SUBTREES:
        base = root / sub
        if not base.is_dir():
            continue
        files.extend(
            p
            for p in sorted(base.rglob("*"))
            if p.is_file()
            and not p.is_symlink()
            and not any(part.startswith(".") for part in p.relative_to(base).parts)
        )
    return files


def _read_file_stable(
    p: Path, retries: int = 4, settle: float = 0.5
) -> tuple[bytes, float] | None:
    """Read `p` only when its size/mtime are identical before and after the
    read, so a save Eden is mid-writing to NAND is never shipped torn. Returns
    (contents, mtime), or None when the file stays unstable through every retry
    or cannot be read."""
    for attempt in range(retries):
        try:
            st_before = p.stat()
            data = p.read_bytes()
            st_after = p.stat()
        except OSError as exc:
            log.warning("save-file: could not read %s — %s", p, exc)
            return None
        if (st_before.st_size, st_before.st_mtime_ns) == (
            st_after.st_size,
            st_after.st_mtime_ns,
        ):
            return data, st_after.st_mtime
        if attempt < retries - 1:
            time.sleep(settle)
    log.warning("save-file: %s still being written — skipped this pull", p)
    return None


class NoSaveArchive(Exception):
    """There is no archive to serve, and why. Carries the HTTP status because
    the reason for having nothing is what picks it, and that knowledge belongs
    at the point the reason is discovered rather than in a union the caller has
    to re-decode."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _build_save_archive(baseline: float) -> tuple[bytes, int]:
    """Zip every save file modified since the last game launch.

    Returns (zip_bytes, skipped), where `skipped` counts files left out because
    they were unreadable or still being written. Raises NoSaveArchive when there
    is nothing to hand back: no data dir yet, no file changed since `baseline`,
    the changed set over the size limit, or every changed file mid-write — that
    last one refused rather than served as an empty archive the caller would
    record as a clean sync. Member paths are relative to the data dir so a later
    PUT restores them regardless of which candidate dir is live."""
    root = _save_data_root()
    if root is None:
        log.debug("save-file: no Eden data dir found")
        raise NoSaveArchive(404, "no save changes since last launch")
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
        raise NoSaveArchive(404, "no save changes since last launch")
    if total > SAVE_FILE_MAX_BYTES:
        log.warning("save-file: changed saves exceed size limit (%d bytes)", total)
        raise NoSaveArchive(413, f"changed saves exceed size limit ({total} bytes)")
    buf = io.BytesIO()
    skipped = 0
    wrote = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in changed:
            result = _read_file_stable(p)
            if result is None:
                skipped += 1
                continue
            data, mtime = result
            # UTC, matched by calendar.timegm on extract — a TZ difference
            # between the GET and PUT containers must not shift mtimes and
            # silently break the newer-file guard.
            info = zipfile.ZipInfo(
                p.relative_to(root).as_posix(),
                date_time=time.gmtime(mtime)[:6],
            )
            zf.writestr(info, data, zipfile.ZIP_DEFLATED)
            wrote += 1
    if wrote == 0:
        raise NoSaveArchive(503, "save files are still being written; retry shortly")
    return buf.getvalue(), skipped


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


def _extract_save_archive(content: bytes) -> tuple[int, int, int] | str:
    """Restore a pulled save archive into the data dir.

    Returns (written, skipped, failed), or an error string for a bad archive.
    Existing files newer than their archive member are skipped so a restore can
    never roll back saves made since the archive was taken. A per-file write
    failure doesn't abort the restore — remaining members still land, the
    failure is counted, and the handler reports it; the mtime guard makes a
    retry of the same archive idempotent."""
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

        written = skipped = failed = 0
        for info in infos:
            target = root / PurePosixPath(info.filename)
            # timegm, matching the gmtime stamp _build_save_archive writes.
            # time.mktime would read the entry as local time, so a GET and PUT
            # in containers with different TZ would shift every mtime by the
            # offset and silently misfire the newer-file guard below.
            mtime = calendar.timegm(info.date_time)
            tmp = None
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
                log.warning("save-file: could not restore %s — %s", info.filename, exc)
                failed += 1
                # The staging file is dot-prefixed, which _iter_save_files
                # deliberately hides from every later pull — so nothing else
                # would ever sweep it. A disk-full restore must not leave one
                # invisible partial file per member behind.
                if tmp is not None:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                continue
            written += 1
    return (written, skipped, failed)

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
    # Claim shared by every kill+relaunch path so two lifecycle sequences can
    # never interleave over one session.
    "launch_in_progress": False,
    # Set when the crash-loop limiter gives up. /status exposes it so a pooled
    # fleet can tell "idle, waiting for a user" from "broker surrendered".
    "relaunch_abandoned": False,
    # Wall-clock stamp of the last GAME launch (not dashboard relaunches).
    # GET /save-file only ships files modified at or after this point; it
    # survives game exit so RomM can still pull after the session ends.
    "save_baseline": None,
    # Random per-session token gating the stream proxy on port 3001. Minted on
    # /launch, swapped for a cookie by the browser, cleared on release. The
    # expiry is a monotonic deadline refreshed on every admitted request; the
    # prev_* pair holds the token a re-issue replaced, valid for a short grace
    # window so an already-open tab is not cut off mid-relaunch.
    "stream_token":        None,
    "stream_expires":      0.0,
    "stream_prev_token":   None,
    "stream_prev_expires": 0.0,
}

# ── Stream token ──────────────────────────────────────────────────────────────
# The browser-facing stream on port 3001 is otherwise open to anyone who can
# reach the port. Each /launch mints a token bound to that session; nginx sends
# every 3001 request to /verify as an auth_request subrequest, and the broker
# admits only requests carrying the live token.


def _issue_stream_token() -> str:
    """Mint a fresh stream token and bind it to the current session.

    The token being replaced is demoted rather than dropped: it stays usable
    for STREAM_TOKEN_GRACE seconds so requests already in flight from an open
    tab still land. See STREAM_TOKEN_GRACE for why that matters.
    """
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _session_lock:
        previous = _session["stream_token"]
        if previous:
            _session["stream_prev_token"] = previous
            _session["stream_prev_expires"] = now + STREAM_TOKEN_GRACE
        _session["stream_token"] = token
        _session["stream_expires"] = now + STREAM_TOKEN_TTL
    return token


def _check_stream_token(token: str) -> str | None:
    """Judge token against the live session token, then the superseded one.

    Returns None when the token is good, otherwise a short reason for the log.
    A hit on the live token slides its expiry forward: the TTL exists to close
    an abandoned session, not to interrupt someone who is still playing.
    """
    if not token:
        return "no stream token in the request"
    now = time.monotonic()
    with _session_lock:
        current = _session["stream_token"]
        if not current:
            return "no stream session is open"
        if hmac.compare_digest(token, current):
            if now >= _session["stream_expires"]:
                return "stream token expired after %.0fs idle" % STREAM_TOKEN_TTL
            _session["stream_expires"] = now + STREAM_TOKEN_TTL
            return None
        previous = _session["stream_prev_token"]
        if previous and hmac.compare_digest(token, previous):
            if now < _session["stream_prev_expires"]:
                return None
            _session["stream_prev_token"] = None
            _session["stream_prev_expires"] = 0.0
            return "stream token superseded by a newer launch"
    return "stream token does not match the open session"


def _clear_stream_token() -> None:
    """Drop the stream token so the gate rejects everything until next launch."""
    with _session_lock:
        _session["stream_token"] = None
        _session["stream_expires"] = 0.0
        _session["stream_prev_token"] = None
        _session["stream_prev_expires"] = 0.0


def _live_stream_token() -> str | None:
    """The session token if it is still inside its TTL, else None.

    /status hands this to RomM so a reconnecting client can re-attach without
    a relaunch. An expired token would only send it into the 403 loop the TTL
    is there to end, so it is reported as absent.
    """
    with _session_lock:
        if _session["stream_token"] and time.monotonic() < _session["stream_expires"]:
            return _session["stream_token"]
    return None


def _extract_stream_token(query: str, cookie_header: str | None) -> str | None:
    """Read the stream token: query stream_token wins, else the stream_sid cookie."""
    qs = parse_qs(query)
    if qs.get("stream_token"):
        return qs["stream_token"][0]
    if cookie_header:
        jar = SimpleCookie()
        jar.load(cookie_header)
        if "stream_sid" in jar:
            return jar["stream_sid"].value
    return None


def _stream_cookie_value(token: str) -> str:
    """Set-Cookie value for the stream session. SameSite=None, Secure, and
    Partitioned are required: the iframe is cross-site to RomM, so the cookie is
    third-party and browsers partition or drop it without these attributes."""
    return (
        f"stream_sid={token}; HttpOnly; Secure; "
        "SameSite=None; Partitioned; Path=/"
    )


def _verify_stream_decision(
    original_uri: str, cookie_header: str | None
) -> tuple[int, str | None, str | None]:
    """Decide an nginx auth_request subrequest for the stream gate.

    Returns (status, set_cookie, reason). 200 admits the request, 403 rejects
    it and carries the reason so the refusal is legible in the container log.
    When the token arrives in the query (the first iframe load), the caller
    gets a Set-Cookie so later requests carry stream_sid and the token drops
    out of the URL. A cookie-authed request that is already good gets no
    Set-Cookie back, so nginx does not rewrite it.
    Under STREAM_GATE=off none of that runs and every request is admitted.
    """
    if STREAM_GATE == "off":
        # The switch is off: admit without reading the token. No Set-Cookie
        # either, because there is no gate for a cookie to satisfy later and
        # nginx would only rewrite the response for nothing.
        return 200, None, None
    query = urlparse(original_uri).query
    token = _extract_stream_token(query, cookie_header)
    reason = _check_stream_token(token or "")
    if reason:
        return 403, None, reason
    if "stream_token" in parse_qs(query):
        # Re-cookie on the query bootstrap, and also when the query token is
        # the current one but the cookie still holds the superseded token:
        # the browser must be moved onto the new value before the grace
        # window closes, or the tab drops out the moment it does.
        return 200, _stream_cookie_value(token), None
    return 200, None, None


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


# Formats Eden can boot, best first: a folder holding several candidates picks
# by this order, so a cartridge dump beats the eShop package beside it and a
# real title beats a homebrew .nro dropped in the same folder.
ROM_EXTENSIONS = (
    ".xci", ".nsp", ".nca", ".nro", ".nso", ".kip", ".elf",
)

# Where to look for the title below a game folder. The folder itself first,
# then one level down for the subfolders some sets use. Nothing deeper: a
# launch must not pay for a full walk of a large set, and anything further
# down is extras, not the game.
_ROM_SEARCH_GLOBS = ("*", "*/*")

# "Disc 1", "(Disc 2)", "CD1", "Disk_3" in a folder or file name. The leading
# boundary keeps it off words that merely end in the letters, so "abcd2.iso" is
# not read as disc 2.
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)


def _disc_number(rel: Path) -> int:
    """Disc number named anywhere in `rel`, or 1 when nothing names one.

    Unmarked files count as disc 1 so that a single-disc game ranks level with
    the first disc of a set, and so a false positive can only ever mean "first".
    """
    match = _DISC_RE.search(str(rel))
    if match is None:
        return 1
    return max(1, int(match.group(1)))


def _resolve_rom_file(path: Path) -> Path | None:
    """Return the file Eden should boot for `path`, or None if there isn't one.

    RomM addresses a folder-organized game by its folder: `Rom.full_path` is
    `fs_path/fs_name`, and for a multi-file ROM `fs_name` is the directory,
    not the title inside it. So /launch regularly receives something like
    `.../roms/switch/Metroid Dread` for a library laid out one game per folder.
    A path that is already a file passes straight through.

    A folder holding a base title alongside its updates and DLC is ambiguous:
    they share an extension, so the name ordering below decides, and there is
    no reliable way to tell a base .nsp from an update .nsp by filename. Keep
    updates out of the game folder, or in a subfolder, to boot the base title.
    """
    if path.is_file():
        return path
    if not path.is_dir():
        return None
    # Every level is collected before anything is ranked. Taking the first level
    # that merely yields a match would let an extras file with a bootable
    # extension beat the real game one level down.
    candidates: list[Path] = []
    for pattern in _ROM_SEARCH_GLOBS:
        try:
            candidates.extend(path.glob(pattern))
        except OSError:
            # Libraries are routinely NFS mounts, so a stalled or vanished
            # share surfaces here as an OSError mid-walk. Report it as "no
            # bootable file" rather than 500-ing the launch.
            return None
    return _pick_rom_file(candidates, path)


def _pick_rom_file(candidates: Iterable[Path], base: Path) -> Path | None:
    """Best bootable file among `candidates`, all of them somewhere under `base`.

    Ranked by disc number, then format, then depth, then name:

      * disc first, so a set starts on disc 1 whatever format the later discs
        are in. Comparing the numbers also keeps 'Disc 2' ahead of 'Disc 10',
        which sorting the names as text does not.
      * format next, because among candidates for the same disc it decides
        which title to boot: a cartridge dump beats the eShop package beside
        it, and both beat a homebrew .nro.
      * then depth, so the title sitting in the game folder wins over one
        buried in an extras or updates subfolder.
    """
    ranked: list[tuple[int, int, int, str, Path]] = []
    for p in candidates:
        if p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext not in ROM_EXTENSIONS:
            continue
        try:
            if not p.is_file():
                continue
            # A symlink in the folder must not walk the launch out of
            # ROM_ROOT: _validate_rom_path only vetted the folder itself.
            real = p.resolve()
            rel = p.relative_to(base)
        except (OSError, ValueError):
            continue
        if not real.is_relative_to(ROM_ROOT):
            continue
        ranked.append(
            (_disc_number(rel), ROM_EXTENSIONS.index(ext), len(rel.parts),
             p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


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
                (i for i, line in enumerate(new_lines) if line.strip() == f"[{section}]"),
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
            # New session ⇒ own process group, so killpg is clean. Unlike
            # preexec_fn (unsafe with threads — it can deadlock between fork and
            # exec in this ThreadingHTTPServer process), this is thread-safe.
            start_new_session=True,
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
        # An instance is up again — whatever the limiter concluded is stale.
        _session["relaunch_abandoned"] = False
    log.info("Eden launched (PID %d)", proc.pid)
    Thread(target=_monitor_process, args=(proc, time.monotonic()), daemon=True).start()


# Consecutive sub-5s exits before the monitor stops relaunching. An Eden that
# dies instantly every time (missing lib, dead display, bad Vulkan ICD) must not
# respawn forever; an explicit POST /launch resets the counter so recovery is
# manual and deliberate.
_CRASH_LOOP_LIMIT = 3
_rapid_exits = 0  # guarded by _session_lock


def _reset_crash_counter() -> None:
    """Forget the crash history. Called only from the request handlers: an
    operator asking for a launch is the deliberate act that earns a container a
    clean slate. The monitor's automatic relaunch must never call this, or the
    count resets on every crash and the limiter never trips."""
    global _rapid_exits
    with _session_lock:
        _rapid_exits = 0


def _monitor_process(proc, start_time):
    """On unexpected exit, relaunch the dashboard if the session is still managed."""
    global _rapid_exits
    proc.wait()
    exit_code = proc.returncode
    duration = time.monotonic() - start_time
    log.debug(
        "_monitor_process: Eden exited (code=%s, duration=%.1fs)",
        exit_code, duration,
    )

    rapid = 0
    with _session_lock:
        should_relaunch = _session["is_managed"] and _session["process"] is proc
        # Only unexpected exits count toward the crash-loop limit — a deliberate
        # kill (/save-and-exit, DELETE /launch) cleared is_managed and must not
        # push the counter toward a false trip.
        if should_relaunch:
            if duration < 5:
                _rapid_exits += 1
            else:
                _rapid_exits = 0
            rapid = _rapid_exits

    if not should_relaunch:
        log.debug("_monitor_process: managed=False or proc replaced — not relaunching")
        return

    if rapid >= _CRASH_LOOP_LIMIT:
        log.error(
            "Eden exited within 5s %d times in a row — giving up on relaunch. "
            "Fix the underlying failure, then POST /launch to recover.",
            rapid,
        )
        with _session_lock:
            _session["relaunch_abandoned"] = True
        return

    wait_time = 5 if duration < 5 else 1
    log.info(
        "Eden exited after %.1fs (code=%s) — relaunching dashboard in %ds",
        duration, exit_code, wait_time,
    )
    time.sleep(wait_time)

    # The crash relaunch is a lifecycle sequence like any other: it must hold
    # the launch claim, or a concurrent /launch interleaves with it and the
    # monitor stomps the new game's session state with Dashboard.
    if not _claim_launch():
        log.info("Crash relaunch skipped — a launch is already in progress")
        return
    try:
        with _session_lock:
            # Re-check under the claim: _kill_eden ended the session, or a
            # /launch that completed before we claimed installed a new process
            # (in which case `process` is no longer OUR dead proc).
            if not _session["is_managed"] or _session["process"] is not proc:
                log.debug("_monitor_process: superseded under the claim — aborting relaunch")
                return
        _launch_eden(None)
    finally:
        with _session_lock:
            _session["launch_in_progress"] = False


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


def _claim_launch() -> bool:
    """Atomically claim launch_in_progress. Every kill+relaunch path must hold
    this claim so two lifecycle sequences can never interleave."""
    with _session_lock:
        if _session["launch_in_progress"]:
            return False
        _session["launch_in_progress"] = True
        return True


def _launch_eden(rom_path, release_claim=False):
    """Top-level launch: kill any running Eden, clean sockets, patch ini, launch.

    Deliberately does NOT touch the crash-loop counter: the monitor's own
    relaunch comes through here too, so resetting it here would clear the count
    on every crash and the limiter could never reach its threshold. Only the
    request handlers reset it, via _reset_crash_counter."""
    try:
        _kill_eden()
        _drain_gamepad_sockets()
        _patch_ini()
        if not _wait_for_no_eden():
            # _kill_eden already reaped the managed process group, so any
            # survivor is an unmanaged stray. Strays sit on top of the new game
            # window, steal xdotool targeting for the F11 fullscreen toggle, and
            # hold the GPU and audio device — reap them.
            log.warning("Stray eden still running after kill+drain — sending SIGKILL")
            subprocess.run(["pkill", "-9", "-x", "eden"], capture_output=True)
            if not _wait_for_no_eden():
                log.error("Stray eden survived SIGKILL; new instance may misbehave")

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
    finally:
        # Only the caller that claimed launch_in_progress may release it —
        # clearing it unconditionally would wipe a concurrent claim and reopen
        # the TOCTOU.
        if release_claim:
            with _session_lock:
                _session["launch_in_progress"] = False


def _relaunch_dashboard():
    """Dashboard relaunch that respects the launch claim. If a /launch is
    already in flight, skip: that launch's kill+start sequence supersedes the
    dashboard anyway, and interleaving the two corrupts both."""
    if not _claim_launch():
        log.info("Dashboard relaunch skipped — a launch is already in progress")
        return
    _reset_crash_counter()
    _launch_eden(None, release_claim=True)


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

    def _verify_stream(self) -> None:
        # nginx auth_request subrequest for the stream gate. nginx forwards the
        # real request URI (carrying the stream_token query on first load) via
        # X-Original-URI and the browser Cookie header; the broker returns 200
        # to admit or 403 to reject, and a Set-Cookie on the query bootstrap.
        status, set_cookie, reason = _verify_stream_decision(
            self.headers.get("X-Original-URI", ""),
            self.headers.get("Cookie"),
        )
        headers = {"Set-Cookie": set_cookie} if set_cookie else None
        body = {"ok": status == 200}
        if status != 200:
            body["error"] = reason or "stream request rejected"
            # Response bodies are logged at DEBUG, and a stream that will not
            # load is exactly the moment an operator needs the reason without
            # first raising the log level.
            log.info("Stream gate refused a request: %s", body["error"])
        self._send_json(status, body, headers)

    def _send_json(self, code: int, body: dict, headers: dict | None = None) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)
        log.debug("HTTP response: %d %s", code, body)

    def _read_body(self) -> dict | None:
        """Parse the JSON request body. Returns {} for an absent body. Sends the
        error response itself and returns None when the body is oversized or not
        a JSON object — callers must bail out on None."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > _BODY_MAX_BYTES:
            self._send_json(413, {"error": "request body too large"})
            return None
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "body is not valid JSON"})
            return None
        if not isinstance(body, dict):
            self._send_json(400, {"error": "body must be a JSON object"})
            return None
        return body

    def _get_save_file(self):
        with _session_lock:
            baseline = _session["save_baseline"]
            rom_name = _session["rom_name"]
        if baseline is None:
            self._send_json(404, {"error": "no game has been launched"})
            return
        try:
            archive, unstable = _build_save_archive(baseline)
        except NoSaveArchive as exc:
            self._send_json(exc.status, {"error": exc.message})
            return
        # Header values must be latin-1; ROM stems can be anything.
        safe_name = "".join(
            c for c in (rom_name or "eden") if c.isascii() and c.isprintable()
        ).strip() or "eden"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(archive)))
        self.send_header("X-Save-Filename", f"{safe_name}.saves.zip")
        if unstable:
            # Files skipped mid-write; the caller can tell this pull was partial.
            self.send_header("X-Save-Skipped-Unstable", str(unstable))
        self.end_headers()
        self.wfile.write(archive)
        log.info(
            "save-file: served archive (%d bytes, %d unstable skipped)",
            len(archive), unstable,
        )

    def _put_save_file(self):
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
        if len(content) != length:
            self._send_json(400, {"error": "truncated request body"})
            return
        result = _extract_save_archive(content)
        if isinstance(result, str):
            self._send_json(400, {"error": result})
            return
        written, skipped, failed = result
        if failed:
            log.warning(
                "save-file: restore incomplete — %d written, %d skipped, %d failed",
                written, skipped, failed,
            )
            self._send_json(500, {
                "error": "some archive members could not be written",
                "written": written, "skipped": skipped, "failed": failed,
            })
            return
        log.info("save-file: restored archive — %d written, %d skipped", written, skipped)
        self._send_json(200, {"status": "ok", "written": written, "skipped": skipped})

    def do_GET(self):
        log.debug("HTTP GET %s", self.path)
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok"})
        elif path == "/verify":
            self._verify_stream()
        elif not self._check_secret():
            # /health and /verify stay open; /health for container healthchecks,
            # /verify because the stream token is itself the credential (nginx
            # auth_request cannot forward the broker secret). All other GETs
            # require the shared secret, matching POST/DELETE.
            self._send_json(403, {"error": "forbidden"})
        elif path == "/status":
            with _session_lock:
                active = (
                    _session["process"] is not None
                    and _session["process"].poll() is None
                    and _session["rom_path"] is not None
                )
                rom_path     = _session["rom_path"]     if active else None
                rom_name     = _session["rom_name"]     if active else None
                started_at   = _session["started_at"]   if active else None
                abandoned    = _session["relaunch_abandoned"]
            # Outside the lock: _live_stream_token takes it too, and this one
            # is not reentrant.
            stream_token = _live_stream_token() if active else None
            self._send_json(200, {
                "active":     active,
                "rom_path":   rom_path,
                "rom_name":   rom_name,
                "started_at": started_at,
                # True once the crash-loop limiter gave up: nothing is running
                # and nothing will restart it without an explicit POST /launch.
                # Distinguishes a dead container from an idle dashboard.
                "relaunch_abandoned": abandoned,
                "stream_token": stream_token,
            })
        elif path == "/save-file":
            self._get_save_file()
        else:
            self._send_json(404, {"error": "not found"})

    def do_PUT(self):
        log.debug("HTTP PUT %s", self.path)
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return
        if urlparse(self.path).path != "/save-file":
            self._send_json(404, {"error": "not found"})
            return
        self._put_save_file()

    def do_POST(self):
        log.debug("HTTP POST %s", self.path)
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return
        path = urlparse(self.path).path

        if path == "/cleanup":
            Thread(target=_cleanup_sockets, daemon=True).start()
            self._send_json(200, {"status": "cleanup started"})
            return

        if path == "/save-and-exit":
            # Eden does not support save states — just exit the game.
            # The Switch's own in-game save system handles persistence.
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
            body = self._read_body()
            if body is None:
                return
            wait = body.get("wait", True)
            # Same claim as POST and DELETE /launch. This is a kill+start
            # sequence like any other: without the claim, a /launch running
            # concurrently has its freshly spawned Eden reaped by the kill
            # below, and the dashboard relaunch that should follow is then
            # skipped because that launch still holds the claim.
            if not _claim_launch():
                self._send_json(409, {"error": "launch already in progress"})
                return
            log.info("save-and-exit: exiting game (no save state support)")
            # Save-and-exit releases the session, so the stream token must die
            # with it: a discovered host is otherwise still usable.
            _clear_stream_token()
            _reset_crash_counter()
            if wait:
                _kill_eden()
                self._send_json(200, {"status": "ok", "saved": False})
                # release_claim=True: this handler claimed, so the relaunch it
                # spawns is what releases, once the dashboard is back up.
                Thread(target=_launch_eden, args=(None, True), daemon=True).start()
            else:
                # Clear visible session state synchronously so that callers
                # polling /status immediately after this response observe "no
                # game running" instead of a stale rom_path. The background
                # thread still runs the actual kill+relaunch.
                with _session_lock:
                    _session["rom_path"] = None
                    _session["rom_name"] = "Dashboard"
                    _session["started_at"] = None
                # _launch_eden kills any running instance first, so no separate
                # _kill_eden is needed here.
                Thread(target=_launch_eden, args=(None, True), daemon=True).start()
                self._send_json(200, {"status": "queued", "saved": False})
            return

        if path == "/save-state":
            self._send_json(501, {"error": "save states are not supported by Eden"})
            return

        if path == "/load-state":
            self._send_json(501, {"error": "save states are not supported by Eden"})
            return

        if path == "/volume":
            body = self._read_body()
            if body is None:
                return
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

        if path == "/mute":
            body = self._read_body()
            if body is None:
                return
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

        if path != "/launch":
            self._send_json(404, {"error": "not found"})
            return

        body = self._read_body()
        if body is None:
            return
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

        # A folder-organized game arrives as its folder, which Eden cannot
        # boot; find the title inside it.
        rom_file = _resolve_rom_file(rom_path)
        if rom_file is None:
            self._send_json(422, {
                "error": "no bootable ROM file found under rom_path",
                "path": str(rom_path),
                "extensions": list(ROM_EXTENSIONS),
            })
            return
        if rom_file != rom_path:
            log.info("Resolved ROM folder %s to %s", rom_path, rom_file)
        rom_path = rom_file

        # Claim before spawning: two concurrent launches would otherwise run two
        # kill+start sequences against one session, and the loser's Eden would
        # be reaped mid-boot by the winner's kill.
        if not _claim_launch():
            self._send_json(409, {"error": "launch already in progress"})
            return

        # An operator asking for a game is the deliberate act that clears a
        # crash-loop surrender; see _reset_crash_counter.
        _reset_crash_counter()
        stream_token = _issue_stream_token()
        Thread(target=_launch_eden, args=(str(rom_path), True), daemon=True).start()
        self._send_json(200, {
            "status": "launching",
            "rom_path": str(rom_path),
            "stream_token": stream_token,
        })

    def do_DELETE(self):
        log.debug("HTTP DELETE %s", self.path)
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return
        if urlparse(self.path).path != "/launch":
            self._send_json(404, {"error": "not found"})
            return

        # Same claim as POST /launch — a soft reset interleaved with a live
        # launch would run two kill+start sequences against one session.
        if not _claim_launch():
            self._send_json(409, {"error": "launch already in progress"})
            return
        # DELETE /launch releases the session, so the stream token must die with
        # it: a discovered host is otherwise still usable.
        _clear_stream_token()
        _reset_crash_counter()
        Thread(target=_launch_eden, args=(None, True), daemon=True).start()
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
    # while no game is running. Goes through the claim like every other
    # lifecycle path, so a /launch arriving during startup cannot interleave.
    Thread(target=_relaunch_dashboard, daemon=True).start()

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
