# SPS5000X Power Supply Web App

Browser-based interface for the Siglent SPS5000X series programmable DC power supply. Monitors up to 3 channels live, plots voltage and current over time, and exports CSV recordings.

## Features

- Live 3-channel readout (voltage, current, power, CV/CC mode)
- Real-time dual charts (voltage + current stacked)
- Per-channel set voltage and current limits
- Global output ON/OFF control
- Statistics (min/max/avg) for all 6 measurements
- CSV export with timestamped readings
- Demo mode for testing without hardware
- Dark/light theme

## Connecting

The SPS5000X communicates over Ethernet TCP (port 5025) or USB USBTMC — not virtual serial. A Python bridge translates between the browser (WebSocket) and the instrument.

**Start the bridge:**
```bash
uv run bridge.py
```

Select transport at the prompt:
- **[1] Ethernet** — enter the IP shown on the instrument display
- **[2] USB** — auto-detects `/dev/usbtmc*` on Linux

**Open the app:**
```bash
uv run serve.py
```
Then click **Bridge** in the toolbar.

## Requirements

- Python 3.10+ with [uv](https://github.com/astral-sh/uv)
- Any modern browser (Chrome, Firefox, Safari, Edge)
- Siglent SPS5000X power supply on your network or connected via USB

## Project Structure

```
index.html      Web UI
css/styles.css  Styles (purple theme, 3-column layout)
js/             ES modules (main, protocol, websocket, connection, chart, recorder, stats, ui)
bridge.py       Python WebSocket↔TCP/USBTMC bridge with optional InfluxDB logging
serve.py        Local HTTP server for development
```
