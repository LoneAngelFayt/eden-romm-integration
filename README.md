# eden-romm-integration-mod

A [linuxserver Docker mod](https://docs.linuxserver.io/general/container-customization/#docker-mods) for [linuxserver/eden](https://github.com/linuxserver/docker-eden) that adds an HTTP broker for [RomM](https://github.com/rommapp/romm) streaming integration.

Launch Switch games from the RomM web UI and stream them in the browser. Controller input works via selkies, volume is adjustable, and games open fullscreen automatically.

## Migrating to webstation (v2)

This per-emulator broker mod is deprecated in favor of [docker-webstation](https://github.com/linuxserver/docker-webstation) running [romm-broker](https://github.com/romm-streaming/romm-broker). It keeps working today, but it won't get new features, and RomM will eventually drop support for the per-emulator broker shape entirely.

Why: one container serving every platform RomM streams from, running one broker implementation, instead of one container (and one broker fork) per emulator, each drifting slightly.

Before, a dedicated Eden container in `config.yml`:

```yaml
streaming:
  containers:
    - platform: switch
      host: https://192.168.1.53:3001
      broker_host: http://192.168.1.53:8000
      label: Eden
```

After, Switch is one entry in a webstation container's `platforms:` map (no `memory_card_sync`, Switch has no memory card):

```yaml
streaming:
  containers:
    - host: https://192.168.1.56:3010
      protocol: webstation
      subfolder: /streaming
      library_path: /romm
      label: Emulation station
      platforms:
        switch:
          emulator: eden
          label: Eden
```

See RomM's `docs/STREAMING_MIGRATION.md` for the full guide:
[https://github.com/rommapp/romm/blob/master/docs/STREAMING_MIGRATION.md](https://github.com/rommapp/romm/blob/master/docs/STREAMING_MIGRATION.md)

## Features

- Launch Switch ROMs on demand from RomM
- Return to the Eden game library (dashboard) when done
- Automatic fullscreen on game launch (xdotool F11)
- Volume and mute control via PulseAudio
- Controller support via selkies joystick interposer (SDL engine mappings auto-seeded)
- Dashboard auto-launches on broker start so the stream always shows something
- In-game NAND save sync with the RomM library (`GET`/`PUT /save-file`)
- Stream gated by a per-session token, so the 3001 port is not an open desktop — provided `BROKER_SECRET` is set (see below)
- Save state UI hidden in the RomM player (Switch has no emulator-level save state support)

## Usage

Add the mod to your `linuxserver/eden` container via the `DOCKER_MODS` environment variable.

### Docker Compose example

```yaml
services:
  eden:
    image: lscr.io/linuxserver/eden:latest
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/New_York
      - DOCKER_MODS=ghcr.io/loneangelfayt/eden-romm-integration-mod:latest
      - BROKER_PORT=8000
      - BROKER_SECRET=your-secret-here
      - ROM_ROOT=/romm/library
      - FULLSCREEN_DELAY=3.0
      # - BROKER_LOG_LEVEL=INFO
    volumes:
      - ./config:/config
      - /path/to/romm/library:/romm/library:ro
    ports:
      - 3001:3001   # selkies https WebRTC stream
      - 8000:8000   # broker API (internal — proxy behind RomM, do not expose)
    restart: unless-stopped
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BROKER_PORT` | `8000` | Port the broker HTTP API listens on. The nginx stream gate is pointed at the same port; a non-numeric or out-of-range value is logged and the gate falls back to `8000`. |
| `BROKER_SECRET` | *(unset)* | Shared secret for broker API auth. **Set this.** All POST/DELETE endpoints require the `X-Broker-Secret` header when it is set — and accept anyone when it is not, which also defeats the [stream gate](#stream-gate), since `POST /launch` is what mints the stream token. |
| `ROM_ROOT` | `/romm/library` | Absolute path to the RomM library root. ROM paths in API requests must be under this directory. |
| `FULLSCREEN_DELAY` | `3.0` | Seconds to wait after game launch before sending F11 to enter fullscreen. Increase if Eden is slow to load on your hardware. |
| `BROKER_LOG_LEVEL` | `INFO` | Log level for the broker (`DEBUG`, `INFO`, `WARNING`, `ERROR`). `DEBUG` also logs Eden stdout. |
| `STREAM_TOKEN_TTL` | `43200.0` | Seconds of **idle** time before a stream token expires. Every admitted request slides it forward, so it only fires on a session nobody is watching — it closes the gate on a container that lost its RomM side rather than capping a play session. |
| `STREAM_TOKEN_GRACE` | `120.0` | Seconds a superseded token keeps working after a re-issue. See [stream gate](#stream-gate); shortening this to `0` restores the old cut-off-instantly behaviour and the 403 loop that came with it. |

## Broker API

Every endpoint requires `X-Broker-Secret: <secret>` when `BROKER_SECRET` is set,
except `/health` (container healthchecks) and `/verify` (nginx cannot forward
the secret on a subrequest; the stream token is the credential there).

Request bodies must be a JSON object and are capped at 64 KB — a larger body is
rejected with `413` rather than truncated, and malformed JSON returns `400`
instead of being silently treated as empty.

### `GET /health`
Returns `{"status": "ok"}`. Always 200.

### `GET /status`
Returns the current session state.

```json
{
  "active": true,
  "rom_path": "/romm/library/switch/game.nsp",
  "rom_name": "game",
  "started_at": "2026-04-19T14:00:00Z",
  "relaunch_abandoned": false,
  "stream_token": "…"
}
```

`relaunch_abandoned` goes `true` once the crash-loop limiter has given up (see
[Crash-loop limiter](#crash-loop-limiter)); it distinguishes a container that is
broken from one that is merely idle at the dashboard. `stream_token` is the live
token for the current session, or `null` when no game is running.

### `POST /launch`
Launch a ROM. Eden is killed, sockets drained, ini patched, then the ROM is launched. Fullscreen is triggered after `FULLSCREEN_DELAY` seconds.

```json
{ "rom_path": "/romm/library/switch/game.nsp" }
```

Returns `{"status": "launching", "rom_path": "...", "stream_token": "..."}`.

`stream_token` is a fresh single-session token for the stream gate; RomM appends
it to the iframe URL. Launching again mints a new one and invalidates the old.

Returns `409` if another launch is already in flight — two kill+start sequences
against one session would otherwise reap each other's Eden mid-boot.

`rom_path` must exist and be under `ROM_ROOT`. It may be either a file or a
**directory**, for libraries laid out one game per folder
(`roms/switch/Metroid Dread/Metroid Dread.nsp`). RomM addresses such a game by
its folder, because `Rom.full_path` is `fs_path/fs_name` and for a multi-file
ROM `fs_name` is the directory, so the broker looks inside for the title, in the
folder itself and one level down. Everything found across both levels is ranked
together by format (`.xci`, `.nsp`, `.nca`, `.nro`, `.nso`, `.kip`, `.elf`),
then depth, then name. So a cartridge dump wins over an eShop package, and a
real title wins over a homebrew `.nro` beside it or a level above it. Dot-files
are skipped, and a symlink pointing outside `ROM_ROOT` is never chosen. The
resolved file is what `/status` and the response body report.

Ranking also reads a disc number off the name when one is there, which the
Switch has no use for. It is kept because every broker shares one resolution
algorithm, and an unmarked name counts as disc 1, so on this platform the rule
never changes an outcome.

A folder holding a base title alongside its updates and DLC is ambiguous: they
share an extension, so name ordering decides, and no filename rule reliably
tells a base `.nsp` from an update `.nsp`. Keep updates out of the game folder,
or in a subfolder, to boot the base title.

A directory with nothing bootable inside returns `422` with the accepted
extensions in an `extensions` field, which is a different message from the
`422` for a path that does not exist at all.

### `DELETE /launch`
Stop the current game and return to the Eden dashboard. Revokes the stream
token. Returns `409` if a launch is already in flight.

### `GET /save-file`
Download the in-game NAND saves written since the current game was launched, as
a zip. Members are paths relative to Eden's data root, under `nand/user/save`.
Member timestamps are stamped in UTC so a pull and a push from containers in
different timezones agree on which copy is newer.

- `200` — `application/zip`, with `X-Save-Filename` and, when some files were
  skipped because Eden was mid-write, `X-Save-Skipped-Unstable: <count>`
- `404` — no game has been launched, or nothing changed since it was
- `413` — the changed set exceeds 256 MB
- `503` — every changed file was mid-write; retry shortly. (Serving an empty
  archive here would let the caller record a clean sync it never got.)

### `PUT /save-file`
Restore a zip previously fetched from `GET /save-file`. A member is skipped when
the local file is more than 2 s newer, so a stale archive never rolls back
progress the player made since. Members escaping the save directory or falling
outside `nand/user/save` are rejected outright.

Returns `{"status": "ok", "written": n, "skipped": n}`, or `500` with the same
counts plus `failed` when some members could not be written — the restore
applies every member it can rather than abandoning the rest half-applied.

### `GET /verify`
The nginx `auth_request` endpoint for the stream gate — not called directly. See
[Stream gate](#stream-gate).

### `POST /save-and-exit`
Eden does not support save states. This endpoint kills the game and returns to the dashboard without saving. The Switch's in-game save system handles persistence.

```json
{ "wait": true }
```

- `wait: true` (default) — blocks until Eden is killed, then returns
- `wait: false` — fires kill in background, returns immediately (use for navigation away)

Returns `{"status": "ok", "saved": false}`, or `409` with
`{"error": "launch already in progress"}` if a launch is in flight — this is a
kill+start sequence and takes the same claim as `POST`/`DELETE /launch`. Also
revokes the stream token.

### `POST /save-state` / `POST /load-state`
Returns `501 Not Implemented`. Eden has no emulator-level save state support.

### `POST /volume`
Set PulseAudio sink volume (0–100).

```json
{ "level": 75 }
```

### `POST /mute`
Toggle or set mute. Omit `mute` to toggle.

```json
{ "mute": true }
```

Returns `{"status": "ok", "mute": true}`.

### `POST /cleanup`
> **Warning:** This calls `pkill selkies`, which kills the WebRTC streaming session. Do not use while a user is connected. Intended only for maintenance when no one is streaming.

Stops the selkies process. s6-overlay restarts it automatically.

## Architecture

```
RomM backend
    │ POST /api/streaming/sessions (platform=switch, rom_path=...)
    ▼
RomM → broker (HTTP, port 8000)
    │
    ├── POST /launch  →  _launch_eden(rom_path)
    │       ├── _kill_eden()              kill current Eden process
    │       ├── _drain_gamepad_sockets()  send EOF to selkies phase-1 sockets
    │       ├── _patch_ini()              set fullscreen=true, confirmClose=false
    │       │   └── _seed_controller_config()  replace keyboard→SDL mappings
    │       ├── time.sleep(2)             wait for selkies to settle
    │       ├── _launch_eden_internal()   spawn Eden via sudo -u abc env ...
    │       └── Thread → _trigger_fullscreen()   xdotool key F11 after FULLSCREEN_DELAY
    │
    └── Eden process ←─ LD_PRELOAD selkies_joystick_interposer.so
                           │
                        selkies (WebRTC) ←─ browser (RomM player)
```

### Init ordering

The mod adds two s6 oneshots to the base image, and the split between them is
load-bearing.

`init-eden-config` does the patching: the nginx stream gate, the selkies
`input_handler.py` rewrites, the Eden INI seeding. Every one of those edits a
file that a base-image service reads at startup, so all of it has to be finished
before any of those services start. That is what
`init-services/dependencies.d/init-eden-config` buys — every `svc-*` longrun in
the base image (`svc-nginx`, `svc-selkies`, `svc-xorg`, `svc-de` and the rest)
already depends on `init-services`, so one edge in front of `init-services` puts
the whole stack behind the patches.

Without that edge the patches still run and still report success, but selkies
has already imported `input_handler.py` by the time they land. The EOF-detection
patch is then inert: dead gamepad clients are never reaped off the
`/tmp/selkies_js*.sock` sockets, and the controller ends up bound to one of them.
Video and keyboard keep working because each reconnect gets a fresh WebRTC
session — this is the "live picture, dead pad" failure.

`init-eden-deps` exists because of that edge, not in spite of it. Package
installation is slow and networked, and anything slow inside `init-eden-config`
now holds up the entire service stack, including the stream itself. So the
`apt-get` work lives in its own oneshot that only `svc-broker` waits on. It
normally installs nothing — the base image ships both `python3` and `xdotool` —
and is there so an image that drops one fails loudly instead of confusing the
broker at runtime.

Because the two oneshots run concurrently, `init-eden-config` cannot assume
`init-eden-deps` has finished. It resolves an interpreter itself (PATH,
`/lsiopy/bin`, `/usr/bin`) and skips the selkies patches with an explicit error
if it finds none, rather than failing silently.

### Stream gate

RomM's own authentication never sits on the container's 3001 socket, so without
a gate anyone who learns the address gets an interactive desktop with the ROM
library mounted. `init.sh` injects an nginx `auth_request` into the 3001 SSL
vhost (anchored on `ssl_certificate_key`, which appears only in that server
block, so the plain 3000 vhost is untouched). Every request is sent to the
broker's `/verify` as a subrequest, carrying the original URI and the browser's
cookies.

`POST /launch` mints the session token and returns it; RomM appends it to the
iframe URL. On that first request the broker admits it and hands back a
`stream_sid` cookie, so the token drops out of the URL and every later asset and
the WebSocket upgrade ride the cookie instead. The cookie is
`HttpOnly; Secure; SameSite=None; Partitioned` — the stream is cross-site to
RomM's origin, and without those attributes the browser drops or partitions it
away and everything after the first request 403s.

The token dies with the session: `DELETE /launch` and `POST /save-and-exit`
revoke it outright, and it expires on its own after `STREAM_TOKEN_TTL` seconds
of idleness so an abandoned session stops holding the gate open.

A new launch **supersedes** the previous token rather than dropping it: the old
one stays valid for `STREAM_TOKEN_GRACE` seconds. Relaunching into an
already-open tab means the browser is still replaying the old `stream_sid`
cookie while RomM navigates the iframe to the new URL. Without that window every
one of those in-flight requests 403s, the stream client reads it as a dropped
connection and reconnects in a loop, and each reconnect strands another gamepad
client on the selkies sockets — the symptom being a live picture with a dead
controller. A request arriving with the current token in the query but the
superseded one in its cookie is re-cookied, moving the tab across before the
window closes.

**The gate is only as good as `BROKER_SECRET`.** `POST /launch` is what mints the
token, and with no secret set the broker accepts that call from anyone — so
anyone who can reach the broker port can mint a valid token and walk through the
gate. Set the secret, and keep port 8000 off the network the way the compose
example does. The broker logs a warning at startup when it is unset.

The injection targets `/defaults/default.conf`, the template the base image's
`init-nginx` copies over the live vhost on every start, as well as the live
`/etc/nginx/sites-available/default`. Patching both is deliberate: the template
covers any start where the copy has not happened yet, and the live file covers
the one where it has. Which of the two is load-bearing depends on the base
image's dependency graph, and that graph is not ours to pin.

What *is* pinned is that nothing reads either file before the patch runs. Every
long-running service in the base image — `svc-nginx` and `svc-selkies` included
— depends on `init-services`, and `init-services` now depends on
`init-eden-config` (see `init-services/dependencies.d/init-eden-config`). See
[init ordering](#init-ordering) for why that edge matters more than it looks.

Nothing injected into the config may contain a `#`. When `PASSWORD` is set,
`init-nginx` runs `sed -i 's/#//g'` over the whole file to uncomment its
`auth_basic` lines, which would turn any comment into a bare invalid directive
and stop nginx from starting.

The gate proxies to the broker on `BROKER_PORT`, and re-points an existing
injection on every start — `/defaults` lives in the image layer and survives a
`docker restart`, so changing the port would otherwise leave the gate aimed at a
dead socket and 500 every stream request. Once the live vhost exists, `nginx -t`
runs against it: a missing SSL certificate is ignored (`init-nginx` generates it
and may not have run yet), anything else is logged as an error with nginx's own
message.

The injection is idempotent and logs an error if the anchor ever disappears from
the base image. If it does, the gate is simply absent — the stream keeps working
but is no longer protected, so watch for that line in the container log.

### Crash-loop limiter

The broker relaunches the dashboard whenever Eden exits unexpectedly. If Eden
dies within 5 s three times in a row — a missing library, a dead display, a bad
Vulkan ICD — it stops relaunching and sets `relaunch_abandoned` in `/status`
rather than respawning forever. Fix the underlying failure, then `POST /launch`
to recover; a launch that ran for more than 5 s resets the counter, and a
deliberate kill (`/save-and-exit`, `DELETE /launch`) never counts toward it.

Every kill+relaunch path — including this one — takes a single launch claim, so
two lifecycle sequences can never interleave and leave `/status` describing a
game that is not the one on screen.

### GPU environment

`sudo`'s `env_reset` drops everything not explicitly passed, so the broker
forwards the container's GPU-related variables through to Eden by name:
`NVIDIA_*`, `VK_*`, `MESA_*`, `LIBGL_*`, `GALLIUM_*`, `RADV_*`, `AMD_*`, `DRI_*`,
`LIBVA_*`, `VDPAU_*`, `__GLX_*`, `__NV_*`, `__EGL_*`, `__VK_*`, plus
`XDG_DATA_DIRS` and `DRINODE`. The forwarded names are logged at startup; if the
list comes back empty on a machine that should have a GPU, that log line is the
first place to look for a session that fell back to llvmpipe.

### Display chain

Eden runs on Xwayland (`:0`) inside a pixelflux compositor session (`WAYLAND_DISPLAY=wayland-1`). The selkies process captures the display and streams it over WebRTC. Stale Wayland/X11 sockets are cleaned up at container start by `init.sh` to ensure display indices stay predictable across restarts.

### Controller support

The selkies joystick interposer (`LD_PRELOAD`) redirects Eden's `/dev/input/*` opens to Unix sockets managed by selkies, which proxies gamepad input from the browser. Eden's qt-config.ini is seeded with SDL engine mappings for the selkies virtual Xbox 360 controller on every launch (GUID `000000004d6963726f736f6674205800`).

`libudev.so.1.0.0-fake` is **not** included in `LD_PRELOAD` — it intercepts Mesa/DRI udev calls and causes a black screen with Eden.

### selkies input_handler patches

`init.sh` applies two patches to the selkies `input_handler.py` at container start:

1. **Active EOF detection** — replaces the phase-2 keep-alive `asyncio.sleep(0.1)` loop body with `asyncio.wait_for(reader.read(1), timeout=0.1)`. This detects emulator disconnect within one 0.1s tick when the remote closes the connection. The naive `reader.at_eof()` check fails because `at_eof()` returns `False` when the reader buffer is non-empty.

2. **Log silencing** — demotes `selkies_gamepad` logger from INFO to WARNING. The INFO level emits ~80 lines per launch cycle.

Both patches are idempotent and survive base-image upgrades that change the Python version (the path is discovered via glob).

## Known Limitations

### Save states

Nintendo Switch games do not support emulator-level save states in Eden. Games save to the emulated NAND via the normal in-game save menu. The `/save-state` and `/load-state` endpoints return 501; the RomM player UI hides all save/load controls for the `switch` platform.

### Evdev zombie socket accumulation

Each game launch+exit cycle leaves ~4 dead Unix socket connections in the selkies process (`ss -x | grep selkies_event`). The selkies asyncio event loop does not reliably clean up phase-2 connections from killed Eden instances despite the `wait_for(reader.read(1))` patch. The selkies `finally` block calls `writer.close()` correctly but scheduling is not guaranteed under load.

At ~4 zombies per launch with a default fd limit of ~1024, controllers will stop working after roughly 250 launches without a restart. Restart the container weekly (or before you hit the limit) — a cron job or Docker healthcheck restart policy both work.

Treat the rate above as unverified against current builds. It was measured while the `init-services` ordering edge was missing (see [init ordering](#init-ordering)), so selkies had imported `input_handler.py` before the patch landed and the patch was not actually running. Re-measure with `ss -x | grep selkies_event` before assuming the same restart cadence is still needed.

## RomM Integration

This mod is designed to work with the RomM `feature/eden-streaming` branch. The RomM frontend:

- Shows a "Stream" button on Switch ROMs
- Calls `POST /api/streaming/sessions` with `platform=switch` and `rom_path`
- Proxies to this broker's `/launch` endpoint
- Hides save/load state controls for the `switch` platform (`maxSlots: 0`)
- Sends `POST /api/streaming/sessions/switch/save-and-exit` when leaving the player
- Appends the `stream_token` from the launch response to the iframe URL — this
  is platform-agnostic in RomM's `streaming.py`, so the gate needs no RomM change
- Syncs in-game NAND saves to the library via `GET`/`PUT /save-file`

## Building

Images are published to GHCR automatically on merge to `main` via semantic release. Tags: `latest`, `vX.Y.Z`, `vX.Y`.

```bash
docker build -t eden-romm-integration-mod .
```
