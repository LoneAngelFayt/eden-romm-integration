"""The stream token gate.

RomM's own auth never sits on the container's 3001 socket, so without this gate
anyone who learns the address gets an interactive desktop with the ROM library
mounted. nginx sends every 3001 request to /verify as an auth_request
subrequest; the broker admits only requests carrying the live session token.
"""

import broker
from conftest import request, rom


def _decision(uri="/", cookie=None):
    return broker._verify_stream_decision(uri, cookie)


# ── The verify decision ───────────────────────────────────────────────────────


def test_a_request_with_no_token_is_rejected():
    assert _decision("/index.html") == (403, None)


def test_a_request_is_rejected_when_no_session_is_live():
    """After DELETE /launch there is no token, so nothing may be admitted —
    including a browser still holding a cookie from the previous session."""
    broker._clear_stream_token()
    assert _decision("/", "stream_sid=anything")[0] == 403


def test_a_wrong_token_is_rejected():
    broker._issue_stream_token()
    assert _decision("/?stream_token=guessed")[0] == 403


def test_the_query_token_is_admitted_and_bootstraps_a_cookie():
    """The iframe URL carries the token once; every later asset request and the
    WebSocket upgrade ride the cookie instead, so it must be set on that hit."""
    token = broker._issue_stream_token()
    status, set_cookie = _decision(f"/?stream_token={token}")
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
    _status, set_cookie = _decision(f"/?stream_token={token}")
    assert "Secure" in set_cookie
    assert "SameSite=None" in set_cookie
    assert "Partitioned" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=/" in set_cookie


def test_a_cookie_authed_request_is_admitted_without_resetting_the_cookie():
    token = broker._issue_stream_token()
    assert _decision("/socket", f"stream_sid={token}") == (200, None)


def test_the_query_token_wins_over_a_stale_cookie():
    """A browser reusing a container after a new launch holds the old cookie;
    the fresh URL token has to be what decides."""
    token = broker._issue_stream_token()
    status, set_cookie = _decision(
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
    code, _body, _headers = request(base, "/verify", headers={"X-Original-URI": "/"})
    assert code == 403


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


def test_each_launch_invalidates_the_previous_session_token(client, rom_root):
    """A token handed to one player must not keep working after the next launch."""
    base, _launched = client
    nsp = rom(rom_root, "switch/game.nsp")
    first = request(base, "/launch", "POST", {"rom_path": str(nsp)})[1]["stream_token"]
    second = request(base, "/launch", "POST", {"rom_path": str(nsp)})[1]["stream_token"]
    assert first != second
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
