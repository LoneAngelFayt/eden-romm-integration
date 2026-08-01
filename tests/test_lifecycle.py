"""Launch lifecycle and HTTP request hardening.

Two kill+start sequences running against one session is the failure mode these
guard: the loser's Eden gets reaped mid-boot by the winner's kill, and /status
ends up describing a game that is not the one on screen.
"""

import json
import time

import broker
from conftest import request, rom


class FakeProc:
    """Stands in for a Popen the monitor is watching."""

    def __init__(self, returncode=1):
        self.returncode = returncode

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode


# ── Request body hardening ────────────────────────────────────────────────────


def test_oversized_body_is_rejected_rather_than_truncated(client):
    """A body read capped at N bytes but parsed anyway would silently act on a
    truncated request; the cap has to be an error."""
    base, launched = client
    oversized = b'{"rom_path": "' + b"a" * (broker._BODY_MAX_BYTES + 1) + b'"}'
    code, body, _ = request(base, "/launch", "POST", raw=oversized)
    assert code == 413
    assert body["error"] == "request body too large"
    assert launched == []


def test_malformed_json_body_is_reported_as_such(client):
    base, launched = client
    code, body, _ = request(base, "/launch", "POST", raw=b"{not json")
    assert code == 400
    assert body["error"] == "body is not valid JSON"
    assert launched == []


def test_non_object_json_body_is_rejected(client):
    """`json.loads` happily returns a list; `.get` on it would be a 500."""
    base, launched = client
    code, body, _ = request(base, "/launch", "POST", raw=b'["rom_path"]')
    assert code == 400
    assert body["error"] == "body must be a JSON object"
    assert launched == []


def test_an_absent_body_is_treated_as_empty(client):
    """/mute with no body means 'toggle', so no body must parse as {}, not 400."""
    base, _launched = client
    code, body, _ = request(base, "/launch", "POST")
    assert code == 400
    assert body["error"] == "rom_path is required"


def test_a_query_string_does_not_hide_the_route(client, rom_root):
    """RomM appends cache-busting params; routing on self.path verbatim 404s."""
    base, _launched = client
    nsp = rom(rom_root, "switch/game.nsp")
    code, body, _ = request(
        base, "/launch?t=123", "POST", {"rom_path": str(nsp)}
    )
    assert code == 200
    assert body["rom_path"] == str(nsp)


# ── The launch claim ──────────────────────────────────────────────────────────


def test_launch_is_refused_while_another_launch_is_in_flight(client, rom_root):
    base, launched = client
    nsp = rom(rom_root, "switch/game.nsp")
    with broker._session_lock:
        broker._session["launch_in_progress"] = True
    code, body, _ = request(base, "/launch", "POST", {"rom_path": str(nsp)})
    assert code == 409
    assert body["error"] == "launch already in progress"
    assert launched == []


def test_soft_reset_is_refused_while_a_launch_is_in_flight(client):
    base, launched = client
    with broker._session_lock:
        broker._session["launch_in_progress"] = True
    code, body, _ = request(base, "/launch", "DELETE")
    assert code == 409
    assert launched == []


def test_save_and_exit_is_refused_while_a_launch_is_in_flight(client, rom_root, monkeypatch):
    """The kill in this handler is a lifecycle sequence like any other. Run it
    without the claim and a concurrent /launch has its freshly spawned Eden
    reaped, with the dashboard relaunch then skipped because that launch still
    holds the claim — nothing running, and nobody told."""
    base, launched = client
    killed = []
    monkeypatch.setattr(broker, "_kill_eden", lambda: killed.append(True))
    with broker._session_lock:
        broker._session["rom_path"] = str(rom(rom_root, "switch/game.nsp"))
        broker._session["launch_in_progress"] = True
    code, body, _ = request(base, "/save-and-exit", "POST", {"wait": True})
    assert code == 409
    assert body["error"] == "launch already in progress"
    assert killed == []
    assert launched == []


def test_save_and_exit_holds_the_claim_across_the_kill(client, rom_root, monkeypatch):
    """Claimed before the kill and released only once the dashboard is back, so
    no other launch can interleave with the two halves."""
    base, launched = client
    held = []
    monkeypatch.setattr(
        broker, "_kill_eden",
        lambda: held.append(broker._session["launch_in_progress"]),
    )
    with broker._session_lock:
        broker._session["rom_path"] = str(rom(rom_root, "switch/game.nsp"))
    assert request(base, "/save-and-exit", "POST", {"wait": True})[0] == 200
    assert held == [True]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not launched:
        time.sleep(0.02)
    assert launched == [None]
    with broker._session_lock:
        assert broker._session["launch_in_progress"] is False


def test_save_and_exit_does_not_revoke_the_token_when_it_is_refused(client, rom_root):
    """A 409 means nothing happened; revoking the stream token anyway would
    black out the session that is legitimately running."""
    base, _launched = client
    token = broker._issue_stream_token()
    with broker._session_lock:
        broker._session["rom_path"] = str(rom(rom_root, "switch/game.nsp"))
        broker._session["launch_in_progress"] = True
    assert request(base, "/save-and-exit", "POST", {"wait": True})[0] == 409
    assert broker._check_stream_token(token) is True


def test_a_rejected_launch_does_not_leave_the_claim_held(client, rom_root):
    """The claim is taken after validation, so a 422 must not wedge the broker."""
    base, _launched = client
    request(base, "/launch", "POST", {"rom_path": str(rom_root / "switch" / "gone.nsp")})
    with broker._session_lock:
        assert broker._session["launch_in_progress"] is False


def test_the_real_launch_releases_the_claim_it_was_handed(real_launch):
    """Exercises `_launch_eden` itself, not a stub that reimplements the
    release — the contract lives in that function's `finally`."""
    assert broker._claim_launch() is True
    broker._launch_eden("/roms/game.nsp", release_claim=True)
    assert real_launch == ["/roms/game.nsp"]
    with broker._session_lock:
        assert broker._session["launch_in_progress"] is False


def test_the_real_launch_keeps_a_claim_it_was_not_handed(real_launch):
    """The monitor claims for itself and releases in its own `finally`; a
    `_launch_eden` that cleared the flag regardless would wipe that claim."""
    assert broker._claim_launch() is True
    broker._launch_eden(None)
    with broker._session_lock:
        assert broker._session["launch_in_progress"] is True


def test_the_claim_is_released_after_a_launch_so_the_next_one_is_accepted(
    client, rom_root
):
    base, launched = client
    nsp = rom(rom_root, "switch/game.nsp")
    assert request(base, "/launch", "POST", {"rom_path": str(nsp)})[0] == 200
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not launched:
        time.sleep(0.02)
    assert request(base, "/launch", "POST", {"rom_path": str(nsp)})[0] == 200


def test_claim_launch_is_exclusive():
    assert broker._claim_launch() is True
    assert broker._claim_launch() is False


# ── The crash-loop limiter ────────────────────────────────────────────────────


# These drive the real `_launch_eden` via the `real_launch` fixture. Stubbing
# `_launch_eden` out here would hide the failure mode this feature actually had:
# a counter reset living inside the launch, wiping the count on every automatic
# relaunch so the threshold was never reached.


def _watch(proc, ran_for=0.0):
    """Run the exit monitor for `proc` as if the session lasted `ran_for`."""
    with broker._session_lock:
        broker._session["is_managed"] = True
        broker._session["process"] = proc
    broker._monitor_process(proc, time.monotonic() - ran_for)


def test_an_unexpected_exit_relaunches_the_dashboard(real_launch):
    _watch(FakeProc())
    assert real_launch == [None]
    with broker._session_lock:
        assert broker._session["relaunch_abandoned"] is False


def test_repeated_instant_exits_stop_the_relaunch_loop(real_launch):
    """A container with a broken Vulkan ICD would otherwise respawn Eden
    forever, burning CPU and flooding the log."""
    proc = FakeProc()
    for _ in range(broker._CRASH_LOOP_LIMIT + 2):
        _watch(proc)
    # It relaunched up to the limit and then stopped, however many exits follow.
    assert len(real_launch) == broker._CRASH_LOOP_LIMIT - 1
    with broker._session_lock:
        assert broker._session["relaunch_abandoned"] is True


def test_an_automatic_relaunch_does_not_clear_the_crash_count(real_launch):
    """The relaunch the monitor performs must leave the counter alone — this is
    the bug that made the limiter unreachable in production."""
    proc = FakeProc()
    _watch(proc)
    assert real_launch == [None]
    assert broker._rapid_exits == 1


def test_a_long_running_session_resets_the_crash_counter(real_launch):
    """Only *consecutive* instant exits count — a session that ran for a while
    and then quit is a normal exit, not evidence of a crash loop."""
    proc = FakeProc()
    for _ in range(broker._CRASH_LOOP_LIMIT - 1):
        _watch(proc)
    assert broker._rapid_exits == broker._CRASH_LOOP_LIMIT - 1
    _watch(proc, ran_for=600)
    assert broker._rapid_exits == 0


def test_a_deliberate_kill_does_not_count_toward_the_limit(real_launch):
    """/save-and-exit clears is_managed before killing; those exits must not
    push an otherwise healthy container toward relaunch_abandoned."""
    proc = FakeProc()
    for _ in range(broker._CRASH_LOOP_LIMIT + 2):
        with broker._session_lock:
            broker._session["is_managed"] = False
            broker._session["process"] = proc
        broker._monitor_process(proc, time.monotonic())
    assert broker._rapid_exits == 0
    assert real_launch == []
    with broker._session_lock:
        assert broker._session["relaunch_abandoned"] is False


def test_an_explicit_launch_clears_a_crash_loop_surrender(client, rom_root, real_launch):
    """The documented recovery path: fix the fault, POST /launch, and the
    container stops being one strike from surrender."""
    base, _launched = client
    nsp = rom(rom_root, "switch/game.nsp")
    proc = FakeProc()
    for _ in range(broker._CRASH_LOOP_LIMIT):
        _watch(proc)
    assert broker._rapid_exits >= broker._CRASH_LOOP_LIMIT
    assert request(base, "/launch", "POST", {"rom_path": str(nsp)})[0] == 200
    assert broker._rapid_exits == 0


def test_status_reports_that_relaunch_was_abandoned(client):
    base, _launched = client
    with broker._session_lock:
        broker._session["relaunch_abandoned"] = True
    code, body, _ = request(base, "/status")
    assert code == 200
    # Distinguishes a dead container from an idle dashboard.
    assert body["relaunch_abandoned"] is True


def test_status_is_json_and_reports_an_idle_session(client):
    base, _launched = client
    code, body, _ = request(base, "/status")
    assert code == 200
    assert body["active"] is False
    assert json.dumps(body)  # serializable — no stray objects leaked in
