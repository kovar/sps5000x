# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Web application for communicating with Siglent SPS5000X programmable DC power supplies using SCPI over Ethernet TCP or USB USBTMC. Reads voltage, current, and CV/CC mode for up to 3 channels, displays live readings with statistics, plots measurements in real-time, and exports to CSV.

## Architecture

```
index.html              → HTML shell (links CSS + JS modules, CDN scripts)
css/styles.css          → All styles, CSS custom properties for dark/light theming (purple accent)
js/
  main.js               → Entry point: imports modules, wires DOM events
  websocket.js          → WebSocketTransport (connects to bridge.py)
  connection.js         → ConnectionManager: WebSocket-only transport, uniform event interface
  chart-manager.js      → DualChartManager wrapping Chart.js with 3 datasets (CH1/CH2/CH3)
  recorder.js           → Recorder with Blob-based CSV export (6 data columns)
  stats.js              → StatsTracker (Welford's algorithm for live statistics)
  protocol.js           → ScpiQueue, SCPI constants, requestReading (9 queries per cycle)
  ui.js                 → 3-channel readout, CV/CC badges, button states, stats display

bridge.py               → WebSocket ↔ TCP/USBTMC bridge (no pyserial needed)
.github/workflows/static.yml → GitHub Pages deployment (deploys on push to main)
```

No build step. No npm. ES modules loaded via `<script type="module">`. Chart.js + date adapter loaded from CDN with pinned versions.

## Transport Layer

WebSocket-only in the browser — connects to `bridge.py`. No Web Serial support (the SPS5000X uses Ethernet TCP or USB USBTMC, not virtual serial).

`bridge.py` supports two backends selected at startup:
- **Ethernet TCP** — `asyncio.open_connection(ip, 5025)` — natively async
- **USB USBTMC** — `/dev/usbtmc*` — `run_in_executor` for blocking file I/O

## SCPI Protocol Commands

| Command | Purpose |
|---------|---------|
| `*IDN?` | Device identity string |
| `MEASure:VOLTage? CH1` | Actual output voltage |
| `MEASure:CURRent? CH1` | Actual output current |
| `MEASure:RUN:MODE? CH1` | CV or CC mode |
| `:SOURce:VOLTage:SET CH1,<v>` | Set voltage |
| `:SOURce:CURRent:SET CH1,<a>` | Set current limit |
| `OUTPut {ON\|OFF}` | Global output toggle |

Replace `CH1` with `CH2` or `CH3` for other channels. Commands are case-insensitive, terminated with `\n`. Responses terminated with `\n`.

## Poll Cycle

9 queries per tick (V, I, mode for CH1/CH2/CH3):
```
MEASure:VOLTage? CH1  → ch1.voltage
MEASure:CURRent? CH1  → ch1.current
MEASure:RUN:MODE? CH1 → ch1.mode (CV/CC)
MEASure:VOLTage? CH2  → ch2.voltage
MEASure:CURRent? CH2  → ch2.current
MEASure:RUN:MODE? CH2 → ch2.mode
MEASure:VOLTage? CH3  → ch3.voltage
MEASure:CURRent? CH3  → ch3.current
MEASure:RUN:MODE? CH3 → ch3.mode
```

Power (W) = V × I calculated in JS — no extra query.

## Deployment

The site is deployed to GitHub Pages automatically on push to `main` via `.github/workflows/static.yml`.

## Running

**Web UI (local development):**
```bash
uv run serve.py     # starts http://localhost:8000 and opens browser
```
Do NOT open `index.html` directly — ES modules require HTTP, not `file://`.

**WebSocket Bridge:**
```bash
uv run bridge.py
```
Prompts for transport selection at startup:
- **[1] Ethernet** — enter IP address (shown on device display) → connects TCP port 5025
- **[2] USB** — auto-detects `/dev/usbtmc*` (Linux USBTMC class driver)

Dependencies (`websockets`, `influxdb-client`) are declared inline via PEP 723 — `uv` installs them automatically. No `pyserial` needed.

**Linux USBTMC permissions:**
The SPS5000X presents as `/dev/usbtmc0`. To grant access without `sudo`:
```bash
echo 'SUBSYSTEM=="usbmisc", KERNEL=="usbtmc*", ATTRS{idVendor}=="f4ec", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-siglent-sps5000x.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

**Optional InfluxDB logging:**
After transport selection, the bridge prompts `Enable InfluxDB logging? [y/N]`. If enabled, writes a single point per poll cycle with 6 fields: `ch1_v`, `ch1_i`, `ch2_v`, `ch2_i`, `ch3_v`, `ch3_i`.
