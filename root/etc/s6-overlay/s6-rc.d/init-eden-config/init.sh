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
_need_apt=0
command -v python3 &>/dev/null || _need_apt=1
command -v wmctrl  &>/dev/null || _need_apt=1
if [ "$_need_apt" = "1" ]; then
    echo "[broker-mod] Installing missing packages (python3, wmctrl)..."
    apt-get update -qq && apt-get install -y -qq python3 wmctrl \
        || echo "[broker-mod] ERROR: apt-get install failed"
fi

# ── sudoers permission ────────────────────────────────────────────────────────
# sudo requires mode 0440; the Docker COPY sets 0644.
chmod 0440 /etc/sudoers.d/broker
echo "[broker-mod] sudoers rule set."

# ── Disable labwc autostart ───────────────────────────────────────────────────
# Prevents eden from being launched a second time by the desktop session —
# the broker manages the process lifecycle directly.
AUTOSTART="/config/.config/labwc/autostart"
mkdir -p "$(dirname "$AUTOSTART")"
printf '# Disabled by eden-broker-mod\n' > "$AUTOSTART"
echo "[broker-mod] Disabled labwc autostart."

# ── Selkies input_handler.py patches ─────────────────────────────────────────
# Glob over the python version so patches survive base-image upgrades that bump
# e.g. python3.12 → python3.13.
INPUT_HANDLER=$(compgen -G "/lsiopy/lib/python3.*/site-packages/selkies/input_handler.py" | head -1)
INPUT_HANDLER="${INPUT_HANDLER:-/lsiopy/lib/python3.13/site-packages/selkies/input_handler.py}"

if [ -f "$INPUT_HANDLER" ]; then
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
