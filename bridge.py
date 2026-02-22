#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "websockets",
#     "influxdb-client",
# ]
# ///
"""
bridge.py — WebSocket bridge for Siglent SPS5000X power supply.

Relays SCPI text commands between a WebSocket client and the SPS5000X
via Ethernet TCP (port 5025) or USB USBTMC. For InfluxDB logging, tracks
pending measurement queries and collects 6 responses (CH1/CH2/CH3
voltage/current) before writing a point.

Usage:
    uv run bridge.py

The web app connects to ws://localhost:8769 (default).
"""

import asyncio
import getpass
import glob
import sys

import websockets


TCP_PORT = 5025
WS_HOST = "localhost"
WS_PORT = 8769

# ─────────────────────────────────────────────────────────────────────────────
# USER CONFIGURATION
# Hard-code values here to skip the interactive prompts at startup.
# Leave a field as None to be prompted interactively.
# ─────────────────────────────────────────────────────────────────────────────
INFLUXDB_URL         = None   # e.g. "http://localhost:8086"
INFLUXDB_ORG         = None   # e.g. "my-org"
INFLUXDB_BUCKET      = None   # e.g. "sensors"
INFLUXDB_TOKEN       = None   # e.g. "my-token=="
INFLUXDB_MEASUREMENT = None   # e.g. "sps5000x_bench1"
# ─────────────────────────────────────────────────────────────────────────────

# SCPI measurement query patterns for InfluxDB tracking (uppercase for comparison)
MEAS_QUERIES = {
    "MEASURE:VOLTAGE? CH1": "ch1_v",
    "MEASURE:CURRENT? CH1": "ch1_i",
    "MEASURE:VOLTAGE? CH2": "ch2_v",
    "MEASURE:CURRENT? CH2": "ch2_i",
    "MEASURE:VOLTAGE? CH3": "ch3_v",
    "MEASURE:CURRENT? CH3": "ch3_i",
}

# InfluxDB state
_influx = None
_pending_fields = []  # FIFO of field name strings
_collected = {}       # accumulates fields until all 6 present


def select_transport():
    """Prompt user to select transport. Returns ('ethernet', ip) or ('usbtmc', None)."""
    print("Select transport:")
    print("  [1] Ethernet (TCP)")
    print("  [2] USB (USBTMC)")
    while True:
        try:
            choice = input("Choice: ").strip()
        except EOFError:
            sys.exit(1)
        if choice == "1":
            default_ip = "192.168.1.100"
            ip = input(f"IP address [{default_ip}]: ").strip() or default_ip
            return "ethernet", ip
        elif choice == "2":
            return "usbtmc", None
        print("  Enter 1 or 2")


def find_usbtmc():
    """Find a USBTMC device. Returns path or exits."""
    devs = sorted(glob.glob("/dev/usbtmc*"))
    if not devs:
        print("No USBTMC devices found at /dev/usbtmc*.")
        print("Ensure the SPS5000X is connected via USB and the driver is loaded.")
        sys.exit(1)
    if len(devs) == 1:
        print(f"Found USBTMC device: {devs[0]}")
        return devs[0]
    print("Multiple USBTMC devices found:\n")
    for i, d in enumerate(devs, 1):
        print(f"  [{i}]  {d}")
    print()
    while True:
        try:
            choice = input(f"Type a number [1-{len(devs)}] and press Enter: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(devs):
                return devs[idx]
        except (ValueError, EOFError):
            pass
        print(f"  Please enter a number between 1 and {len(devs)}")


def setup_influxdb():
    """Interactively configure InfluxDB logging. Returns config dict or None."""
    global _influx

    # Use pre-configured values if all USER CONFIGURATION fields are set
    if all([INFLUXDB_URL, INFLUXDB_ORG, INFLUXDB_BUCKET, INFLUXDB_TOKEN, INFLUXDB_MEASUREMENT]):
        url = INFLUXDB_URL
        org = INFLUXDB_ORG
        bucket = INFLUXDB_BUCKET
        token = INFLUXDB_TOKEN
        measurement = INFLUXDB_MEASUREMENT
        print(f"\nUsing pre-configured InfluxDB: {org}/{bucket}/{measurement}")
    else:
        try:
            answer = input("\nEnable InfluxDB logging? [y/N]: ").strip().lower()
        except EOFError:
            return None
        if answer != "y":
            return None

        print("\n── InfluxDB Setup ──────────────────────────────────")
        url = input("URL [http://localhost:8086]: ").strip() or "http://localhost:8086"
        org = input("Organization: ").strip()
        bucket = input("Bucket: ").strip()
        print("API Token")
        print("  (Find yours at: InfluxDB UI → Load Data → API Tokens)")
        token = getpass.getpass("  Token: ")
        measurement = input("Measurement name: ").strip()
        print("  Use snake_case, e.g. sps5000x_bench1")

        if not all([org, bucket, token, measurement]):
            print("Missing required fields — InfluxDB logging disabled.")
            return None

    from influxdb_client import InfluxDBClient

    print("\nTesting connection... ", end="", flush=True)
    client = InfluxDBClient(url=url, token=token, org=org)
    try:
        health = client.health()
        if health.status != "pass":
            print(f"✗ ({health.message})")
            client.close()
            return None
    except Exception as e:
        print(f"✗ ({e})")
        client.close()
        return None
    print("✓")

    write_api = client.write_api()
    _influx = {
        "client": client,
        "write_api": write_api,
        "bucket": bucket,
        "org": org,
        "measurement": measurement,
    }
    print(f"InfluxDB logging enabled → {org}/{bucket}/{measurement}\n")
    return _influx


def close_influxdb():
    """Flush pending writes and close the InfluxDB client."""
    global _influx
    if _influx:
        print("Flushing InfluxDB...", end=" ", flush=True)
        try:
            _influx["write_api"].close()
            _influx["client"].close()
        except Exception:
            pass
        print("done.")
        _influx = None


def track_query(cmd):
    """If cmd is a voltage/current measurement query, record the expected field name."""
    cmd_upper = cmd.strip().upper()
    for pattern, field_name in MEAS_QUERIES.items():
        if cmd_upper == pattern:
            _pending_fields.append(field_name)
            return


def track_response(line):
    """Match a response to a pending measurement query and collect for InfluxDB."""
    global _collected
    if not _influx or not _pending_fields:
        return

    field_name = _pending_fields.pop(0)
    try:
        value = float(line.strip())
    except ValueError:
        return

    _collected[field_name] = value

    # When all 6 fields are present, write the point
    required = {"ch1_v", "ch1_i", "ch2_v", "ch2_i", "ch3_v", "ch3_i"}
    if required.issubset(_collected.keys()):
        write_influx_point(_collected)
        _collected = {}


def write_influx_point(fields):
    """Write a complete measurement point to InfluxDB."""
    if not _influx:
        return

    from influxdb_client import Point

    point = Point(_influx["measurement"])
    for name, value in fields.items():
        point = point.field(name, value)

    try:
        _influx["write_api"].write(
            bucket=_influx["bucket"],
            org=_influx["org"],
            record=point,
        )
    except Exception as e:
        print(f"  InfluxDB write error: {e}")


async def handler_tcp(ws, reader, writer):
    """WebSocket ↔ Ethernet TCP handler (natively async)."""
    peer = getattr(ws, "remote_address", None)
    print(f"  Client connected: {peer}")
    try:
        async for message in ws:
            cmd = message.strip()
            if not cmd:
                continue
            try:
                writer.write((cmd + "\n").encode("ascii"))
                await writer.drain()
            except OSError as e:
                print(f"\n  TCP write error: {e}")
                break
            print(f"  → Sent to PSU: {cmd}")
            track_query(cmd)
            if "?" in cmd:
                try:
                    raw = await reader.readline()
                except OSError as e:
                    print(f"\n  TCP read error: {e}")
                    break
                line = raw.decode("ascii", errors="replace").strip()
                if line:
                    try:
                        await ws.send(line)
                    except websockets.ConnectionClosed:
                        break
                    track_response(line)
    except websockets.ConnectionClosed:
        pass
    finally:
        print(f"  Client disconnected: {peer}")


async def handler_usbtmc(ws, f):
    """WebSocket ↔ USBTMC handler (run_in_executor for blocking I/O)."""
    peer = getattr(ws, "remote_address", None)
    print(f"  Client connected: {peer}")
    loop = asyncio.get_event_loop()
    try:
        async for message in ws:
            cmd = message.strip()
            if not cmd:
                continue
            try:
                await loop.run_in_executor(None, f.write, (cmd + "\n").encode("ascii"))
            except OSError as e:
                print(f"\n  USBTMC write error: {e}")
                break
            print(f"  → Sent to PSU: {cmd}")
            track_query(cmd)
            if "?" in cmd:
                try:
                    raw = await loop.run_in_executor(None, f.read, 4096)
                except OSError as e:
                    print(f"\n  USBTMC read error: {e}")
                    break
                line = raw.decode("ascii", errors="replace").strip()
                if line:
                    try:
                        await ws.send(line)
                    except websockets.ConnectionClosed:
                        break
                    track_response(line)
    except websockets.ConnectionClosed:
        pass
    finally:
        print(f"  Client disconnected: {peer}")


async def main():
    transport, config = select_transport()

    if transport == "ethernet":
        ip = config
        print(f"\nConnecting to {ip}:{TCP_PORT}...", end=" ", flush=True)
        try:
            reader, writer = await asyncio.open_connection(ip, TCP_PORT)
        except (ConnectionRefusedError, OSError) as e:
            print(f"✗\nFailed to connect to {ip}:{TCP_PORT}: {e}")
            sys.exit(1)
        print("✓")
        setup_influxdb()
        print(f"Starting WebSocket server on ws://{WS_HOST}:{WS_PORT}")
        print("Web app can now connect via the Bridge button.\n")
        async with websockets.serve(
            lambda ws: handler_tcp(ws, reader, writer), WS_HOST, WS_PORT
        ):
            await asyncio.Future()

    else:  # usbtmc
        path = find_usbtmc()
        print(f"Opening USBTMC device: {path}")
        try:
            f = open(path, "r+b", buffering=0)
        except PermissionError:
            print(f"Permission denied: {path}")
            print("Add a udev rule to grant access:")
            print("  echo 'SUBSYSTEM==\"usbmisc\", KERNEL==\"usbtmc*\", ATTRS{idVendor}==\"f4ec\", MODE=\"0666\"' \\")
            print("    | sudo tee /etc/udev/rules.d/99-siglent-sps5000x.rules")
            print("  sudo udevadm control --reload-rules && sudo udevadm trigger")
            sys.exit(1)
        setup_influxdb()
        print(f"Starting WebSocket server on ws://{WS_HOST}:{WS_PORT}")
        print("Web app can now connect via the Bridge button.\n")
        async with websockets.serve(
            lambda ws: handler_usbtmc(ws, f), WS_HOST, WS_PORT
        ):
            await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        close_influxdb()
        print("\nBridge stopped.")
