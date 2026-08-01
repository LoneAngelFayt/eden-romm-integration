#!/usr/bin/with-contenv bash

# ── XDG runtime dir ───────────────────────────────────────────────────────────
XDG_RUNTIME_DIR="/config/.XDG"
mkdir -p "$XDG_RUNTIME_DIR"

# Clean up stale Wayland and X11 sockets so pixelflux/Xwayland always start on
# the default indices (wayland-1, :0).  Stale lock files on the host-mapped
# /config volume cause them to increment on relaunch, breaking the broker's
# hardcoded display expectations.
find "$XDG_RUNTIME_DIR" -name "wayland-*" -delete
rm -rf /tmp/.X11-unix/X* /tmp/.X*lock
echo "[broker-mod] Cleaned up stale display sockets."

# ── python3 + wmctrl availability ────────────────────────────────────────────
# Both are runtime requirements: broker.py is python3, and wmctrl/xdotool are
# used to drive Eden's window. apt-get failure here means broker.service will
# fail to start (or fail at runtime) so we exit non-zero to surface it
# immediately instead of letting the operator chase a confusing broker error.
_need_apt=0
command -v python3 &>/dev/null || _need_apt=1
command -v wmctrl  &>/dev/null || _need_apt=1
if [ "$_need_apt" = "1" ]; then
    echo "[broker-mod] Installing missing packages (python3, wmctrl)..."
    if ! apt-get update -qq; then
        echo "[broker-mod] FATAL: apt-get update failed — cannot install python3/wmctrl"
        exit 1
    fi
    if ! apt-get install -y -qq python3 wmctrl; then
        echo "[broker-mod] FATAL: apt-get install failed — broker cannot run without python3/wmctrl"
        exit 1
    fi
fi

# ── Disable labwc autostart ───────────────────────────────────────────────────
# Prevents eden from being launched a second time by the desktop session —
# the broker manages the process lifecycle directly.
AUTOSTART="/config/.config/labwc/autostart"
mkdir -p "$(dirname "$AUTOSTART")"
printf '# Disabled by eden-broker-mod\n' > "$AUTOSTART"
echo "[broker-mod] Disabled labwc autostart."

# ── Selkies input_handler.py patches ─────────────────────────────────────────
# Glob over the python version so patches survive base-image upgrades that bump
# e.g. python3.12 → python3.13. The linuxserver image only ever ships ONE
# python3.X under /lsiopy, but we explicitly count matches and refuse to guess
# if a future image starts shipping multiple — patching the wrong tree silently
# would leave gamepad EOF detection broken.
INPUT_HANDLER_MATCHES=$(compgen -G "/lsiopy/lib/python3.*/site-packages/selkies/input_handler.py" || true)
INPUT_HANDLER_COUNT=$(printf '%s\n' "$INPUT_HANDLER_MATCHES" | sed '/^$/d' | wc -l)
INPUT_HANDLER=$(printf '%s\n' "$INPUT_HANDLER_MATCHES" | sed '/^$/d' | head -1)

if [ "$INPUT_HANDLER_COUNT" -gt 1 ]; then
    echo "[broker-mod] ERROR: multiple selkies input_handler.py matches found:"
    printf '%s\n' "$INPUT_HANDLER_MATCHES" | sed 's/^/[broker-mod]   /'
    echo "[broker-mod]   Refusing to guess. Skipping patches; gamepad EOF detection may misbehave."
    INPUT_HANDLER=""
fi

if [ -z "$INPUT_HANDLER" ]; then
    echo "[broker-mod] ERROR: selkies input_handler.py not found — Python version glob matched nothing."
    echo "[broker-mod]   Expected: /lsiopy/lib/python3.*/site-packages/selkies/input_handler.py"
    echo "[broker-mod]   Selkies patches will be skipped. Check base image Python version."
elif [ -f "$INPUT_HANDLER" ]; then
    # Patch 1: Active EOF detection in the keep-alive loop.
    #
    # The phase-2 keep-alive loop in _handle_interposer_client is:
    #
    #   while self.running and not writer.is_closing():
    #       await asyncio.sleep(0.1)
    #
    # writer.is_closing() never flips on Unix sockets when the remote end
    # closes, so dead emulator connections accumulate indefinitely.
    #
    # The naive fix (adding `not reader.at_eof()` to the while condition)
    # fails because at_eof() returns `self._eof AND not self._buffer`.
    # If the interposer has buffered any data at exit, _buffer is non-empty
    # and at_eof() stays False forever.
    #
    # The real fix: replace asyncio.sleep(0.1) with a short-timeout read.
    # reader.read(1) returns b"" on EOF regardless of buffer state, so we
    # detect emulator disconnect within one 0.1 s tick.
    if grep -q "wait_for(reader.read(1)" "$INPUT_HANDLER"; then
        echo "[broker-mod] selkies input_handler.py EOF patch already applied."
    else
        if python3 - "$INPUT_HANDLER" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
text = p.read_text()
# Handle both the original loop and any previously applied at_eof() variant.
variants = [
    '            while self.running and not writer.is_closing():\n                await asyncio.sleep(0.1) ',
    '            while self.running and not writer.is_closing():\n                await asyncio.sleep(0.1)',
    '            while self.running and not writer.is_closing() and not reader.at_eof():\n                await asyncio.sleep(0.1) ',
    '            while self.running and not writer.is_closing() and not reader.at_eof():\n                await asyncio.sleep(0.1)',
]
new_loop = (
    '            while self.running and not writer.is_closing():\n'
    '                try:\n'
    '                    _bdata = await asyncio.wait_for(reader.read(1), timeout=0.1)\n'
    '                    if not _bdata:\n'
    '                        break\n'
    '                except asyncio.TimeoutError:\n'
    '                    pass\n'
    '                except Exception:\n'
    '                    break'
)
for old in variants:
    if old in text:
        p.write_text(text.replace(old, new_loop, 1))
        sys.exit(0)
sys.exit(1)
PYEOF
        then
            echo "[broker-mod] Patched selkies input_handler.py keep-alive loop (active EOF detection)."
        else
            echo "[broker-mod] ERROR: python patch failed on input_handler.py keep-alive loop"
        fi
    fi

    # Patch 2: Silence the selkies_gamepad logger.
    # It emits ~80 INFO lines per launch cycle; demote to WARNING.
    # Uses python3 for the insertion because sed \n behaviour is not portable
    # across GNU/BSD sed and can silently produce a literal '\n' in the file.
    if grep -q "setLevel(logging.WARNING)" "$INPUT_HANDLER"; then
        echo "[broker-mod] selkies_gamepad log-level patch already applied."
    else
        if python3 - "$INPUT_HANDLER" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
old = 'logger_selkies_gamepad = logging.getLogger("selkies_gamepad")'
new = old + '\nlogger_selkies_gamepad.setLevel(logging.WARNING)'
text = p.read_text()
if old in text:
    p.write_text(text.replace(old, new, 1))
    sys.exit(0)
sys.exit(1)
PYEOF
        then
            echo "[broker-mod] Patched selkies_gamepad log level to WARNING."
        else
            echo "[broker-mod] ERROR: python patch failed setting selkies_gamepad log level"
        fi
    fi
else
    echo "[broker-mod] WARNING: selkies input_handler.py not found at $INPUT_HANDLER"
fi

# ── Input device name diagnostic (DEBUG only) ────────────────────────────────
# Log the kernel sysfs names for the selkies virtual joystick devices so we can
# verify the SDL device name that Eden/Qt will see for controller mapping.
if [ "${BROKER_LOG_LEVEL,,}" = "debug" ]; then
    echo "[broker-mod] Input device names (for SDL controller mapping):"
    for node in js0 js1 js2 js3; do
        name_file="/sys/class/input/${node}/device/name"
        if [ -f "$name_file" ]; then
            echo "[broker-mod]   /dev/input/${node}: $(cat "$name_file")"
        else
            echo "[broker-mod]   /dev/input/${node}: sysfs name not found"
        fi
    done
fi

# ── nginx stream gate ────────────────────────────────────────────────────────
# Gate the browser-facing stream with nginx auth_request. The 3001 SSL vhost is
# the host RomM loads in the iframe; without this, anyone who learns the address
# gets an interactive desktop with the ROM library mounted, since RomM's auth
# never sits on this socket. auth_request sends every 3001 request to the
# broker's /verify, which checks the session-bound stream token RomM appends to
# the iframe URL and, on the first (query-token) hit, hands back a stream_sid
# cookie that carries every later asset and the WebSocket upgrade. Anchored on
# ssl_certificate_key, which appears only in the 3001 server block, so the plain
# 3000 vhost is untouched. The broker exempts /verify from its shared secret
# because nginx cannot forward that secret and the stream token is the credential.
#
# Two base-image behaviours dictate the shape of this:
#
#   1. init-nginx re-copies /defaults/default.conf over the live vhost on every
#      container start, and s6 gives us no ordering edge to it: init-eden-config
#      hangs off init-config, init-nginx off init-selkies — separate branches
#      under init-os-end, brought up concurrently. Patching only the live file is
#      a coin flip, and losing the race means that cp silently wipes the gate and
#      the stream comes up unauthenticated. So the template is the primary
#      target; the live file is patched too, for the case where the cp already
#      happened. Writing via a temp file and mv means a concurrent cp reads
#      either the old template or the new one, never a half-written one.
#
#   2. When PASSWORD is set, init-nginx runs `sed -i 's/#//g'` over the config to
#      uncomment its auth_basic lines. That strips every '#' in the file, so a
#      comment injected here would become a bare invalid directive and stop nginx
#      from starting. Nothing written into the config may contain a '#' — hence
#      the idempotency marker is the _stream_auth location name, not a comment.
#
# Set-Cookie is added with `always` so it survives the 101 on the WebSocket
# upgrade. It is set at server level, which the stream's own locations inherit
# because they declare no add_header of their own.
NGINX_TEMPLATE="/defaults/default.conf"
NGINX_SITE="/etc/nginx/sites-available/default"
# Must track broker.py's BROKER_PORT — the gate proxies to the broker's /verify,
# and pointing it at the wrong port turns every stream request into a 500.
#
# This value is interpolated straight into a proxy_pass directive, so a typo
# would write nginx a config it refuses to parse — and on a fresh container the
# live vhost does not exist yet, so _validate_nginx below never runs to catch
# it. A non-numeric port would stop the broker too (broker.py int()s it), but
# there the blast radius is one service; here it is nginx, and with nginx down
# there is no stream at all. Fall back to the default and say so.
_broker_port="${BROKER_PORT:-8000}"
_port_fault=""
case "$_broker_port" in
    ''|*[!0-9]*) _port_fault="not a number" ;;
    # The length test comes first and short-circuits: `[ -lt ]` errors out on
    # anything wider than a machine integer, and an errored test is a false one,
    # which would let the bad value straight through.
    *) [ "${#_broker_port}" -gt 5 ] || [ "$_broker_port" -lt 1 ] || [ "$_broker_port" -gt 65535 ] \
        && _port_fault="out of range" ;;
esac
if [ -n "$_port_fault" ]; then
    echo "[broker-mod] WARNING: BROKER_PORT='${_broker_port}' is ${_port_fault}; stream gate will use 8000."
    _broker_port=8000
fi

# Writes the gated config to stdout; exits non-zero if the anchor is missing.
_inject_stream_gate() {
    awk -v port="$_broker_port" '
      { print }
      /ssl_certificate_key/ && !injected {
        print "  auth_request /_stream_auth;"
        print "  auth_request_set $stream_set_cookie $upstream_http_set_cookie;"
        print "  add_header Set-Cookie $stream_set_cookie always;"
        print "  location = /_stream_auth {"
        print "    internal;"
        print "    auth_request off;"
        print "    proxy_pass http://127.0.0.1:" port "/verify;"
        print "    proxy_pass_request_body off;"
        print "    proxy_set_header Content-Length \"\";"
        print "    proxy_set_header X-Original-URI $request_uri;"
        print "  }"
        injected = 1
      }
      END { exit !injected }
    ' "$1"
}

_patch_stream_gate() {
    local target="$1"
    if grep -q "_stream_auth" "$target"; then
        # Already gated — but /defaults lives in the image layer and survives a
        # `docker restart`, so an operator who changed BROKER_PORT would
        # otherwise keep the old port forever. Re-point it every time.
        sed -i \
            "s|proxy_pass http://127.0.0.1:[0-9]*/verify;|proxy_pass http://127.0.0.1:${_broker_port}/verify;|" \
            "$target"
        echo "[broker-mod] nginx stream gate already present in $target (broker port ${_broker_port})."
        return 0
    fi
    if ! _inject_stream_gate "$target" > "$target.tmp"; then
        rm -f "$target.tmp"
        echo "[broker-mod] ERROR: no ssl_certificate_key anchor in $target (base image may have changed)."
        return 1
    fi
    mv "$target.tmp" "$target"
    echo "[broker-mod] Applied nginx stream gate to $target (broker port ${_broker_port})."
}

# nginx -t exits non-zero whether the config is malformed or the SSL cert simply
# does not exist yet — init-nginx generates that cert and may not have run. The
# messages differ, though, and nginx aborts on the first [emerg]: if the only
# complaint is the certificate, everything before it — including the injection —
# parsed clean. Anything else is a config this mod broke.
_validate_nginx() {
    local out rc
    out="$(nginx -t 2>&1)"
    rc=$?
    if [ "$rc" = "0" ] || echo "$out" | grep -q "cannot load certificate"; then
        return 0
    fi
    echo "[broker-mod] ERROR: nginx rejects the config after the stream gate injection:"
    echo "$out" | sed 's/^/[broker-mod]   /'
    return 1
}

_gate_applied=0
if [ -f "$NGINX_TEMPLATE" ]; then
    _patch_stream_gate "$NGINX_TEMPLATE" && _gate_applied=1
else
    echo "[broker-mod] WARNING: nginx template not found at $NGINX_TEMPLATE"
fi
if [ -f "$NGINX_SITE" ]; then
    # Only the live config is testable: the template still holds init-nginx's
    # unsubstituted placeholders (SUBFOLDER, CWS), which are not valid nginx.
    _patch_stream_gate "$NGINX_SITE" && _gate_applied=1 && _validate_nginx
fi
if [ "$_gate_applied" = "0" ]; then
    echo "[broker-mod] ERROR: stream gate applied to no nginx config — the 3001 stream will be UNAUTHENTICATED."
fi
