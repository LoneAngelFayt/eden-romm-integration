#!/usr/bin/with-contenv bash

# Package installation, split out of init-eden-config because it is slow and
# networked. init-eden-config now gates the whole service stack (nginx, xorg and
# selkies all wait on it via init-services), so anything that can spend a minute
# on apt-get has to live somewhere that only the broker waits for — otherwise a
# slow mirror holds up the stream itself.
#
# Both are runtime requirements of the broker service: broker.py is python3, and
# xdotool drives Eden's window for the F11 fullscreen toggle. The current base
# image ships both, so this normally installs nothing at all — it is here so a
# base image that drops one still gives a clear failure instead of a confusing
# broker error at runtime.
#
# wmctrl is deliberately NOT in this list. It was added when the fullscreen
# toggle briefly went through wmctrl; that was reverted to xdotool and nothing
# has referenced wmctrl since, but the check stayed behind. Because the base
# image has never shipped wmctrl, that stale check fired on every single
# container start and dragged a full apt-get update+install in front of the
# selkies and nginx patches — which is how those patches came to lose their
# race with the services that read the files they rewrite.
_pkgs=()
command -v python3 &>/dev/null || _pkgs+=(python3)
command -v xdotool &>/dev/null || _pkgs+=(xdotool)
if [ ${#_pkgs[@]} -gt 0 ]; then
    echo "[broker-mod] Installing missing packages: ${_pkgs[*]}"
    if ! apt-get update -qq; then
        echo "[broker-mod] FATAL: apt-get update failed — cannot install ${_pkgs[*]}"
        exit 1
    fi
    if ! apt-get install -y -qq "${_pkgs[@]}"; then
        echo "[broker-mod] FATAL: apt-get install failed — broker cannot run without ${_pkgs[*]}"
        exit 1
    fi
fi
