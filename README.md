# eden-romm-integration-mod

A [linuxserver Docker mod](https://docs.linuxserver.io/general/container-customization/#docker-mods) for [linuxserver/eden](https://github.com/linuxserver/docker-eden) that adds an HTTP broker for [RomM](https://github.com/rommapp/romm) streaming integration.

Enables RomM to launch Nintendo Switch games in a remote streaming session, with full controller support, volume/mute control, and automatic fullscreen on launch.

## Features

- Launch Switch ROMs on demand from RomM
- Return to the Eden game library (dashboard) when done
- Automatic fullscreen on game launch (xdotool F11)
- Volume and mute control via PulseAudio
- Controller support via selkies joystick interposer (SDL engine mappings auto-seeded)
- Dashboard auto-launches on broker start so the stream always shows something
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
      - DOCKER_MODS=ghcr.io/YOUR_USERNAME/eden-romm-integration-mod:latest
      - BROKER_PORT=8000
      - BROKER_SECRET=your-secret-here
      - ROM_ROOT=/romm/library
      - FULLSCREEN_DELAY=3.0
      # - BROKER_LOG_LEVEL=INFO
    volumes:
      - ./config:/config
      - /path/to/romm/library:/romm/library:ro
    ports:
      - 3000:3000   # selkies WebRTC stream
      - 8000:8000   # broker API (internal — proxy behind RomM, do not expose)
    restart: unless-stopped
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BROKER_PORT` | `8000` | Port the broker HTTP API listens on |
| `BROKER_SECRET` | *(unset)* | Shared secret for broker API auth. Set this. All POST/DELETE endpoints require `X-Broker-Secret` header when set. |
| `ROM_ROOT` | `/romm/library` | Absolute path to the RomM library root. ROM paths in API requests must be under this directory. |
| `FULLSCREEN_DELAY` | `3.0` | Seconds to wait after game launch before sending F11 to enter fullscreen. Increase if Eden is slow to load on your hardware. |
| `BROKER_LOG_LEVEL` | `INFO` | Log level for the broker (`DEBUG`, `INFO`, `WARNING`, `ERROR`). `DEBUG` also logs Eden stdout. |

## Broker API

All write endpoints require `X-Broker-Secret: <secret>` when `BROKER_SECRET` is set.

### `GET /health`
Returns `{"status": "ok"}`. Always 200.

### `GET /status`
Returns the current session state.

```json
{
  "active": true,
  "rom_path": "/romm/library/switch/game.nsp",
  "rom_name": "game",
  "started_at": "2026-04-19T14:00:00Z"
}
```

### `POST /launch`
Launch a ROM. Eden is killed, sockets drained, ini patched, then the ROM is launched. Fullscreen is triggered after `FULLSCREEN_DELAY` seconds.

```json
{ "rom_path": "/romm/library/switch/game.nsp" }
```

Returns `{"status": "launching", "rom_path": "..."}`.

### `DELETE /launch`
Stop the current game and return to the Eden dashboard.

### `POST /save-and-exit`
Eden does not support save states. This endpoint kills the game and returns to the dashboard without saving. The Switch's in-game save system handles persistence.

```json
{ "wait": true }
```

- `wait: true` (default) — blocks until Eden is killed, then returns
- `wait: false` — fires kill in background, returns immediately (use for navigation away)

Returns `{"status": "ok", "saved": false}`.

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

**Impact:** At ~4 zombies per launch and a default fd limit of ~1024, controllers will break after roughly 250 game launches without a container restart.

**Workaround:** Restart the container weekly (or on demand before hitting the limit). Add a weekly cron job or Docker healthcheck restart policy.

## RomM Integration

This mod is designed to work with the RomM `feature/eden-streaming` branch. The RomM frontend:

- Shows a "Stream" button on Switch ROMs
- Calls `POST /api/streaming/sessions` with `platform=switch` and `rom_path`
- Proxies to this broker's `/launch` endpoint
- Hides save/load state controls for the `switch` platform (`maxSlots: 0`)
- Sends `POST /api/streaming/sessions/switch/save-and-exit` when leaving the player

## Building

Images are published to GHCR automatically on merge to `main` via semantic release. Tags: `latest`, `vX.Y.Z`, `vX.Y`.

```bash
docker build -t eden-romm-integration-mod .
```
