"""The stream token gate.

RomM's own auth never sits on the container's 3001 socket, so without this gate
anyone who learns the address gets an interactive desktop with the ROM library
mounted. nginx sends every 3001 request to /verify as an auth_request
subrequest; the broker admits only requests carrying the live session token.

The token is not a bare secret but a small lifecycle: it ages out after an idle
TTL, and a re-issue demotes its predecessor for a grace window instead of
killing it outright. Both exist because the alternative is a 403 loop — see
test_relaunching_does_not_cut_off_a_tab_that_is_still_open.
"""

import time

import broker
from conftest import request, rom


class _RunningProc:
    """Stands in for the Popen /status checks to decide a session is live."""

    def poll(self):
        return None


def _decision(uri="/", cookie=None):
    return broker._verify_stream_decision(uri, cookie)


def _age_out(field="stream_expires"):
    """Push a deadline into the past, standing in for the wait it represents.

    Reaching into the session beats sleeping for a real TTL and beats faking
    the clock: the deadline is the whole of what the code reads, so moving it
    exercises the same branch a genuine timeout would.
    """
    with broker._session_lock:
        broker._session[field] = time.monotonic() - 1


# ── The verify decision ───────────────────────────────────────────────────────


def test_a_request_with_no_token_is_rejected():
    status, set_cookie, reason = _decision("/index.html")
    assert (status, set_cookie) == (403, None)
    assert reason


def test_a_request_is_rejected_when_no_session_is_live():
    """After DELETE /launch there is no token, so nothing may be admitted —
    including a browser still holding a cookie from the previous session."""
    broker._clear_stream_token()
    assert _decision("/", "stream_sid=anything")[0] == 403


def test_a_wrong_token_is_rejected():
    broker._issue_stream_token()
    assert _decision("/?stream_token=guessed")[0] == 403


def test_every_refusal_carries_a_reason():
    """The gate's 403s reach the container log. A refusal that does not say why
    leaves an operator staring at a blank iframe with nothing to go on."""
    assert _decision("/")[2] == "no stream token in the request"
    broker._clear_stream_token()
    assert _decision("/", "stream_sid=x")[2] == "no stream session is open"
    broker._issue_stream_token()
    assert _decision("/?stream_token=wrong")[2] == (
        "stream token does not match the open session"
    )


def test_the_query_token_is_admitted_and_bootstraps_a_cookie():
    """The iframe URL carries the token once; every later asset request and the
    WebSocket upgrade ride the cookie instead, so it must be set on that hit."""
    token = broker._issue_stream_token()
    status, set_cookie, _reason = _decision(f"/?stream_token={token}")
    assert status == 200
    assert set_cookie is not None
    # Split by hand rather than parsing with SimpleCookie: `Partitioned` only
    # entered the stdlib's attribute table in 3.14, and an older parser drops
    # the whole morsel when it meets an attribute it does not recognise. What
    # matters is the header the browser receives, so assert on that directly.
    name, _, value = set_cookie.split(";", 1)[0].partition("=")
    assert (name, value) == ("stream_sid", token)


def test_the_bootstrap_cookie_survives_a_cross_site_iframe():
    """The stream is third-party to RomM's origin. Without Secure, SameSite=None
    and Partitioned the browser drops or partitions the cookie away and every
    asset after the first request 403s."""
    token = broker._issue_stream_token()
    set_cookie = _decision(f"/?stream_token={token}")[1]
    assert "Secure" in set_cookie
    assert "SameSite=None" in set_cookie
    assert "Partitioned" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=/" in set_cookie


def test_a_cookie_authed_request_is_admitted_without_resetting_the_cookie():
    token = broker._issue_stream_token()
    assert _decision("/socket", f"stream_sid={token}") == (200, None, None)


def test_the_query_token_wins_over_a_stale_cookie():
    """A browser reusing a container after a new launch holds the old cookie;
    the fresh URL token has to be what decides."""
    token = broker._issue_stream_token()
    status, set_cookie, _reason = _decision(
        f"/?stream_token={token}", "stream_sid=from-the-last-session"
    )
    assert status == 200
    assert token in set_cookie


def test_other_cookies_alongside_the_session_do_not_confuse_the_gate():
    token = broker._issue_stream_token()
    cookie = f"theme=dark; stream_sid={token}; lang=en"
    assert _decision("/", cookie)[0] == 200


def test_unrelated_query_parameters_do_not_admit_a_request():
    broker._issue_stream_token()
    assert _decision("/?token=x&sid=y")[0] == 403


# ── The idle TTL ──────────────────────────────────────────────────────────────


def test_an_idle_token_expires():
    """A container that loses its RomM side — crash, network partition, a user
    who just closes the tab — must not leave the gate open forever."""
    token = broker._issue_stream_token()
    _age_out()
    status, _set_cookie, reason = _decision("/", f"stream_sid={token}")
    assert status == 403
    assert "expired" in reason


def test_an_admitted_request_slides_the_expiry_forward():
    """The TTL is idle time, not a session cap: someone still playing must never
    be cut off mid-game."""
    token = broker._issue_stream_token()
    with broker._session_lock:
        broker._session["stream_expires"] = time.monotonic() + 1
    assert _decision("/", f"stream_sid={token}")[0] == 200
    with broker._session_lock:
        remaining = broker._session["stream_expires"] - time.monotonic()
    assert remaining > 1


def test_a_rejected_request_does_not_slide_the_expiry_forward():
    """Otherwise a stranger guessing at the port could hold an abandoned
    session's gate open indefinitely."""
    broker._issue_stream_token()
    with broker._session_lock:
        before = broker._session["stream_expires"]
    assert _decision("/?stream_token=guessed")[0] == 403
    with broker._session_lock:
        assert broker._session["stream_expires"] == before


def test_status_does_not_hand_back_an_expired_token(client, rom_root):
    """RomM reads /status to re-attach a reconnecting client. Handing it a dead
    token would only send it into the 403 loop the TTL exists to end."""
    base, _launched = client
    nsp = rom(rom_root, "switch/game.nsp")
    request(base, "/launch", "POST", {"rom_path": str(nsp)})
    # /status only reports a token for a live session, and the client fixture
    # stubs the launch out — so stand a running process up by hand.
    with broker._session_lock:
        broker._session["process"] = _RunningProc()
        broker._session["rom_path"] = str(nsp)
    assert request(base, "/status")[1]["stream_token"] is not None
    _age_out()
    assert request(base, "/status")[1]["stream_token"] is None


# ── The re-issue grace window ─────────────────────────────────────────────────


def test_relaunching_does_not_cut_off_a_tab_that_is_still_open():
    """The regression this window exists for. RomM navigates the iframe to the
    new URL, but the open tab keeps replaying the old stream_sid until it does.
    Dropping the old token instantly 403s every one of those in-flight
    requests; the client reads that as a dropped connection and reconnects in a
    loop, and each reconnect strands another gamepad client on the selkies
    sockets until the controller is bound to a dead one."""
    first = broker._issue_stream_token()
    second = broker._issue_stream_token()
    assert first != second
    assert _decision("/socket", f"stream_sid={first}")[0] == 200
    assert _decision("/socket", f"stream_sid={second}")[0] == 200


def test_the_superseded_token_stops_working_once_the_window_closes():
    """The grace window is a handover, not a second key: a token handed to one
    player must not outlive it."""
    first = broker._issue_stream_token()
    broker._issue_stream_token()
    _age_out("stream_prev_expires")
    status, _set_cookie, reason = _decision("/", f"stream_sid={first}")
    assert status == 403
    assert "superseded" in reason


def test_a_tab_on_the_old_token_is_moved_onto_the_new_one():
    """Surviving the grace window means being re-cookied inside it. The query
    token is the current one while the cookie is still the superseded one, and
    that request is the browser's one chance to be moved across."""
    broker._issue_stream_token()
    current = broker._issue_stream_token()
    status, set_cookie, _reason = _decision(
        f"/?stream_token={current}", "stream_sid=the-superseded-one"
    )
    assert status == 200
    assert current in set_cookie


def test_releasing_the_session_revokes_the_superseded_token_too():
    """DELETE /launch must not leave a usable key behind in the grace slot."""
    first = broker._issue_stream_token()
    broker._issue_stream_token()
    broker._clear_stream_token()
    assert _decision("/", f"stream_sid={first}")[0] == 403


# ── Over HTTP, the way nginx calls it ─────────────────────────────────────────


def test_verify_does_not_require_the_broker_secret(client, monkeypatch):
    """nginx cannot forward the shared secret on an auth_request subrequest —
    the stream token is the credential on this endpoint."""
    base, _launched = client
    monkeypatch.setattr(broker, "SECRET", "s3cret")
    token = broker._issue_stream_token()
    code, _body, headers = request(
        base, "/verify", headers={"X-Original-URI": f"/?stream_token={token}"}
    )
    assert code == 200
    assert token in headers["Set-Cookie"]


def test_verify_over_http_rejects_a_request_with_no_token(client, monkeypatch):
    base, _launched = client
    monkeypatch.setattr(broker, "SECRET", "s3cret")
    broker._issue_stream_token()
    code, body, _headers = request(base, "/verify", headers={"X-Original-URI": "/"})
    assert code == 403
    assert body["error"]


def test_status_still_requires_the_broker_secret(client, monkeypatch):
    """Only /health and /verify are exempt; the gate must not have opened GET."""
    base, _launched = client
    monkeypatch.setattr(broker, "SECRET", "s3cret")
    assert request(base, "/status")[0] == 403
    assert request(base, "/status", headers={"X-Broker-Secret": "s3cret"})[0] == 200


# ── Token lifecycle ───────────────────────────────────────────────────────────


def test_launch_mints_a_token_that_the_gate_accepts(client, rom_root):
    base, _launched = client
    nsp = rom(rom_root, "switch/game.nsp")
    code, body, _ = request(base, "/launch", "POST", {"rom_path": str(nsp)})
    assert code == 200
    assert _decision(f"/?stream_token={body['stream_token']}")[0] == 200


def test_each_launch_supersedes_the_previous_session_token(client, rom_root):
    """A token handed to one player must not keep working indefinitely after
    the next launch — only for the handover window."""
    base, _launched = client
    nsp = rom(rom_root, "switch/game.nsp")
    first = request(base, "/launch", "POST", {"rom_path": str(nsp)})[1]["stream_token"]
    second = request(base, "/launch", "POST", {"rom_path": str(nsp)})[1]["stream_token"]
    assert first != second
    _age_out("stream_prev_expires")
    assert _decision(f"/?stream_token={first}")[0] == 403
    assert _decision(f"/?stream_token={second}")[0] == 200


def test_soft_reset_revokes_the_token(client, rom_root):
    """DELETE /launch releases the session; a discovered host must stop working."""
    base, _launched = client
    nsp = rom(rom_root, "switch/game.nsp")
    token = request(base, "/launch", "POST", {"rom_path": str(nsp)})[1]["stream_token"]
    assert request(base, "/launch", "DELETE")[0] == 200
    assert _decision(f"/?stream_token={token}")[0] == 403


def test_save_and_exit_revokes_the_token(client, rom_root):
    base, _launched = client
    nsp = rom(rom_root, "switch/game.nsp")
    token = request(base, "/launch", "POST", {"rom_path": str(nsp)})[1]["stream_token"]
    with broker._session_lock:
        broker._session["rom_path"] = str(nsp)
    assert request(base, "/save-and-exit", "POST", {"wait": False})[0] == 200
    assert _decision(f"/?stream_token={token}")[0] == 403


def test_tokens_are_long_enough_to_resist_guessing():
    token = broker._issue_stream_token()
    assert len(token) >= 32
    assert token != broker._issue_stream_token()
