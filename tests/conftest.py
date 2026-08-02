"""Shared fixtures: the broker module under test, a ROM library, and a live
server with the emulator launch stubbed out.

The broker is a single stdlib module under root/root with no package around it,
so importing it means putting that directory on sys.path first.
"""

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "root" / "root"))
import broker  # noqa: E402


@pytest.fixture(autouse=True)
def clean_session():
    """Reset the module-level session between tests — broker.py keeps its state
    in globals, so one test's launch claim would otherwise 409 the next."""
    broker._rapid_exits = 0
    with broker._session_lock:
        broker._session["launch_in_progress"] = False
        broker._session["relaunch_abandoned"] = False
        broker._session["stream_token"] = None
        broker._session["stream_expires"] = 0.0
        broker._session["stream_prev_token"] = None
        broker._session["stream_prev_expires"] = 0.0
        broker._session["is_managed"] = False
        broker._session["process"] = None
        broker._session["rom_path"] = None
        broker._session["rom_name"] = "Dashboard"
        broker._session["save_baseline"] = None
    yield
    broker._rapid_exits = 0


@pytest.fixture
def rom_root(tmp_path, monkeypatch):
    root = tmp_path / "library"
    (root / "switch").mkdir(parents=True)
    monkeypatch.setattr(broker, "ROM_ROOT", root.resolve())
    return root.resolve()


def rom(root, rel):
    """Create a dummy ROM file at `rel` under `root`."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"cartridge")
    return p


@pytest.fixture
def real_launch(monkeypatch):
    """Stub only the system boundary, so the real `_launch_eden` runs.

    Lifecycle tests must exercise the actual function: a stub standing in for
    `_launch_eden` cannot show what that function does to shared counters and
    claims, which is exactly where lifecycle bugs live. Yields the list of
    rom_paths that reached the process-spawn boundary.
    """
    spawned = []
    monkeypatch.setattr(broker, "_kill_eden", lambda: None)
    monkeypatch.setattr(broker, "_drain_gamepad_sockets", lambda: None)
    monkeypatch.setattr(broker, "_patch_ini", lambda: None)
    monkeypatch.setattr(broker, "_wait_for_no_eden", lambda timeout=3.0: True)
    monkeypatch.setattr(broker, "_launch_eden_internal", spawned.append)
    monkeypatch.setattr(broker.time, "sleep", lambda s: None)
    return spawned


@pytest.fixture
def client(monkeypatch):
    """A live broker server with the emulator launch stubbed out.

    Yields (base_url, launched) where `launched` collects each rom_path the
    broker asked to boot. This stub answers "did the handler route and validate
    correctly" only — it replaces `_launch_eden` wholesale, so it can say
    nothing about what the real launch does to counters or claims. Use the
    `real_launch` fixture for anything that turns on lifecycle behaviour.
    """
    monkeypatch.setattr(broker, "SECRET", "")
    launched = []

    def _fake_launch(rom_path, release_claim=False):
        launched.append(rom_path)
        # The real _launch_eden releases the claim it was handed; the stub has
        # to as well, or every later launch in the same test 409s.
        if release_claim:
            with broker._session_lock:
                broker._session["launch_in_progress"] = False

    monkeypatch.setattr(broker, "_launch_eden", _fake_launch)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), broker.BrokerHandler)
    # shutdown() waits for the next poll tick; the 0.5 s default would add half a
    # second of teardown to every test using this fixture.
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02},
                     daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", launched
    finally:
        srv.shutdown()
        srv.server_close()


def request(base, path, method="GET", body=None, headers=None, raw=None):
    """Issue a request and return (status, parsed_json, response_headers).

    `body` is JSON-encoded; `raw` sends bytes verbatim (for malformed-body
    tests). Error responses come back like any other rather than raising.
    """
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, _decode(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read()), dict(exc.headers)


def _decode(payload):
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return payload
