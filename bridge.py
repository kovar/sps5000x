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
import datetime
import getpass
import glob
import os
import shutil
import signal
import sys

import websockets


TCP_PORT = 5025
WS_HOST = "localhost"
WS_PORT = 8769
TUI_ROWS = 12  # fixed terminal rows used by TUI

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

# SCPI measurement query patterns for InfluxDB/TUI tracking (uppercase for comparison)
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

# ─────────────────────────────────────────────────────────────────────────────
# TUI STATE
# ─────────────────────────────────────────────────────────────────────────────
_tui_active           = False
_tui_values           = {k: None for k in
                         ["ch1_v", "ch1_i", "ch2_v", "ch2_i", "ch3_v", "ch3_i"]}
_tui_client           = None          # connected client IP string or None
_tui_influx_desc      = "disabled"    # "disabled" or "enabled (name)"
_tui_transport_desc   = ""
_tui_input_buf        = ""
_tui_last_update      = ""
_tui_term_state       = None          # saved termios state for restore
_tui_loop             = None          # event loop reference set in tui_start()
_tui_send_func        = None          # async def(cmd: str) -> str, set in main()
_serial_lock          = None          # asyncio.Lock, initialized in main()
_tui_w                = 80            # current terminal width


# ─────────────────────────────────────────────────────────────────────────────
# TUI HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _tui_can_use():
    """Return True if terminal TUI is supported on this system."""
    if os.name != "posix":
        return False
    if not sys.stdout.isatty():
        return False
    try:
        import tty as _t, termios as _m  # noqa: F401
        return True
    except ImportError:
        return False


def _tui_box_line(content, row):
    """Write a │-bordered content line at the given 1-indexed row."""
    inner = _tui_w - 2
    padded = content[:inner].ljust(inner)
    sys.stdout.write(f"\033[{row};1H\u2502{padded}\u2502")


def _tui_cell_width():
    return max(12, (_tui_w - 2) // 6)


def _tui_labels_line():
    cell = _tui_cell_width()
    labels = ["CH1 Voltage", "CH1 Current",
              "CH2 Voltage", "CH2 Current",
              "CH3 Voltage", "CH3 Current"]
    return "".join(lbl.center(cell) for lbl in labels)


def _tui_values_line():
    cell = _tui_cell_width()
    parts = []
    for key, unit in [("ch1_v", "V"), ("ch1_i", "A"),
                      ("ch2_v", "V"), ("ch2_i", "A"),
                      ("ch3_v", "V"), ("ch3_i", "A")]:
        v = _tui_values[key]
        s = f"{v:.3f} {unit}" if v is not None else "---"
        parts.append(s.center(cell))
    return "".join(parts)


def _tui_position_cursor():
    """Move cursor to end of input on row 11: │ > {buf}│"""
    col = 5 + len(_tui_input_buf)  # 1=│  2=space  3=>  4=space  5+=input
    sys.stdout.write(f"\033[11;{col}H")


# ─────────────────────────────────────────────────────────────────────────────
# TUI LIFECYCLE
# ─────────────────────────────────────────────────────────────────────────────

def tui_start(transport_desc, influx_desc):
    """Initialize TUI: save terminal, setcbreak, hide cursor, draw frame."""
    global _tui_active, _tui_transport_desc, _tui_influx_desc
    global _tui_term_state, _tui_w, _tui_loop

    if not _tui_can_use():
        return
    cols, rows = shutil.get_terminal_size()
    if cols < 50 or rows < TUI_ROWS:
        return

    import tty, termios  # noqa: E401

    _tui_transport_desc = transport_desc
    _tui_influx_desc = influx_desc
    _tui_w = min(cols, 120)
    _tui_active = True

    fd = sys.stdin.fileno()
    _tui_term_state = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    sys.stdout.write("\033[?25l\033[2J")
    sys.stdout.flush()
    tui_draw()

    _tui_loop = asyncio.get_event_loop()
    _tui_loop.add_reader(fd, _tui_on_stdin)
    try:
        _tui_loop.add_signal_handler(signal.SIGWINCH,
                                     lambda: (tui_draw(), sys.stdout.flush()))
    except (OSError, NotImplementedError):
        pass


def tui_stop():
    """Restore terminal to original state and show cursor."""
    global _tui_active, _tui_term_state

    if not _tui_active:
        return
    _tui_active = False

    if _tui_loop is not None and not _tui_loop.is_closed():
        try:
            _tui_loop.remove_reader(sys.stdin.fileno())
        except Exception:
            pass
        try:
            _tui_loop.remove_signal_handler(signal.SIGWINCH)
        except Exception:
            pass

    if _tui_term_state is not None:
        try:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _tui_term_state)
        except Exception:
            pass

    sys.stdout.write(f"\033[?25h\033[{TUI_ROWS + 1};1H\033[J")
    sys.stdout.flush()


# ─────────────────────────────────────────────────────────────────────────────
# TUI DRAWING
# ─────────────────────────────────────────────────────────────────────────────

def tui_draw():
    """Full TUI redraw — used on startup and terminal resize."""
    global _tui_w

    if not _tui_active:
        return

    cols, _ = shutil.get_terminal_size()
    _tui_w = min(cols, 120)
    w = _tui_w
    inner = w - 2

    # Row 1: top border with title
    title = f" SPS5000X Bridge  ws://{WS_HOST}:{WS_PORT}  [{_tui_transport_desc}] "
    fill = max(0, w - 2 - len(title) - 1)
    top = ("\u250c\u2500" + title + "\u2500" * fill + "\u2510")[:w]
    sys.stdout.write(f"\033[1;1H{top}")

    # Row 2: blank
    _tui_box_line("", 2)

    # Row 3: channel labels
    _tui_box_line(_tui_labels_line(), 3)

    # Row 4: blank
    _tui_box_line("", 4)

    # Row 5: measurement values
    _tui_box_line(_tui_values_line(), 5)

    # Row 6: blank
    _tui_box_line("", 6)

    # Row 7: InfluxDB + client status
    influx_str = f"InfluxDB: {_tui_influx_desc}"
    client_str = ("Client: connected (" + _tui_client + ")"
                  if _tui_client else "Client: disconnected")
    gap = max(2, inner - 4 - len(influx_str) - len(client_str))
    _tui_box_line(f"  {influx_str}{' ' * gap}{client_str}", 7)

    # Row 8: blank
    _tui_box_line("", 8)

    # Row 9: last update time
    _tui_box_line(f"  Updated: {_tui_last_update or '--:--:--'}", 9)

    # Row 10: SCPI input divider
    div_title = " SCPI Command "
    div_fill = max(0, w - 2 - len(div_title) - 1)
    div = ("\u251c\u2500" + div_title + "\u2500" * div_fill + "\u2524")[:w]
    sys.stdout.write(f"\033[10;1H{div}")

    # Row 11: input line
    _tui_box_line(f" > {_tui_input_buf}", 11)

    # Row 12: bottom border
    bot = ("\u2514" + "\u2500" * (w - 2) + "\u2518")[:w]
    sys.stdout.write(f"\033[12;1H{bot}")

    _tui_position_cursor()
    sys.stdout.flush()


def tui_update_values():
    """Rewrite rows 5 and 9 with current measurement values and timestamp."""
    global _tui_last_update

    if not _tui_active:
        return

    _tui_last_update = datetime.datetime.now().strftime("%H:%M:%S")
    inner = _tui_w - 2

    content5 = _tui_values_line()
    sys.stdout.write(f"\033[5;1H\u2502{content5[:inner].ljust(inner)}\u2502")

    content9 = f"  Updated: {_tui_last_update}"
    sys.stdout.write(f"\033[9;1H\u2502{content9[:inner].ljust(inner)}\u2502")

    _tui_position_cursor()
    sys.stdout.flush()


def tui_update_client(peer, connected):
    """Update the client connection status display."""
    global _tui_client

    if connected:
        _tui_client = peer[0] if isinstance(peer, tuple) else str(peer)
    else:
        _tui_client = None

    if not _tui_active:
        if connected:
            print(f"  Client connected: {peer}")
        else:
            print(f"  Client disconnected: {peer}")
        return

    inner = _tui_w - 2
    influx_str = f"InfluxDB: {_tui_influx_desc}"
    client_str = ("Client: connected (" + _tui_client + ")"
                  if _tui_client else "Client: disconnected")
    gap = max(2, inner - 4 - len(influx_str) - len(client_str))
    status = f"  {influx_str}{' ' * gap}{client_str}"
    sys.stdout.write(f"\033[7;1H\u2502{status[:inner].ljust(inner)}\u2502")
    _tui_position_cursor()
    sys.stdout.flush()


def tui_redraw_input():
    """Rewrite the input line (row 11)."""
    if not _tui_active:
        return
    inner = _tui_w - 2
    content = f" > {_tui_input_buf}"
    sys.stdout.write(f"\033[11;1H\u2502{content[:inner].ljust(inner)}\u2502")
    _tui_position_cursor()
    sys.stdout.flush()


def _tui_show_response(resp):
    """Display a SCPI response in row 9 (replaces 'Updated' until next poll cycle)."""
    if not _tui_active:
        return
    inner = _tui_w - 2
    content = f"  Response: {resp}"
    sys.stdout.write(f"\033[9;1H\u2502{content[:inner].ljust(inner)}\u2502")
    _tui_position_cursor()
    sys.stdout.flush()


# ─────────────────────────────────────────────────────────────────────────────
# TUI INPUT
# ─────────────────────────────────────────────────────────────────────────────

def _tui_on_stdin():
    """Sync add_reader callback: char-by-char line editor."""
    global _tui_input_buf

    try:
        ch = sys.stdin.read(1)
    except Exception:
        return
    if not ch:
        return

    if ch in ("\r", "\n"):
        cmd = _tui_input_buf.strip()
        _tui_input_buf = ""
        tui_redraw_input()
        if cmd:
            asyncio.ensure_future(_tui_dispatch_command(cmd))
    elif ch in ("\x7f", "\x08"):  # backspace / DEL
        _tui_input_buf = _tui_input_buf[:-1]
        tui_redraw_input()
    elif ch == "\x15":  # Ctrl-U: clear line
        _tui_input_buf = ""
        tui_redraw_input()
    elif "\x20" <= ch < "\x7f":  # printable ASCII
        _tui_input_buf += ch
        tui_redraw_input()


async def _tui_dispatch_command(cmd):
    """Send a TUI-entered SCPI command to the device and display the response."""
    if _tui_send_func is None:
        return
    try:
        resp = await _tui_send_func(cmd)
    except Exception as e:
        resp = f"(error: {e})"
    if resp:
        _tui_show_response(resp)


# ─────────────────────────────────────────────────────────────────────────────
# DEVICE DETECTION
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# INFLUXDB
# ─────────────────────────────────────────────────────────────────────────────

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

    from influxdb_client.client.write_api import SYNCHRONOUS
    write_api = client.write_api(write_options=SYNCHRONOUS)
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


# ─────────────────────────────────────────────────────────────────────────────
# MEASUREMENT TRACKING
# ─────────────────────────────────────────────────────────────────────────────

def track_query(cmd):
    """If cmd is a voltage/current measurement query, record the expected field name."""
    cmd_upper = cmd.strip().upper()
    for pattern, field_name in MEAS_QUERIES.items():
        if cmd_upper == pattern:
            _pending_fields.append(field_name)
            return


def track_response(line):
    """Match a response to the oldest pending query; update TUI and InfluxDB."""
    global _collected

    if not _pending_fields:
        return

    field_name = _pending_fields.pop(0)
    try:
        value = float(line.strip())
    except ValueError:
        return

    # Always update TUI values regardless of InfluxDB state
    _tui_values[field_name] = value
    if all(v is not None for v in _tui_values.values()):
        tui_update_values()

    # InfluxDB: accumulate until all 6 fields collected
    if _influx:
        _collected[field_name] = value
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
        if not _tui_active:
            print(f"  InfluxDB write error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# TRANSPORT HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def handler_tcp(ws, reader, writer):
    """WebSocket ↔ Ethernet TCP handler (natively async)."""
    peer = getattr(ws, "remote_address", None)
    tui_update_client(peer, True)
    try:
        async for message in ws:
            cmd = message.strip()
            if not cmd:
                continue
            async with _serial_lock:
                try:
                    writer.write((cmd + "\n").encode("ascii"))
                    await writer.drain()
                except OSError as e:
                    if not _tui_active:
                        print(f"\n  TCP write error: {e}")
                    break
                track_query(cmd)
                if "?" in cmd:
                    try:
                        raw = await reader.readline()
                    except OSError as e:
                        if not _tui_active:
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
        tui_update_client(peer, False)


async def handler_usbtmc(ws, f):
    """WebSocket ↔ USBTMC handler (run_in_executor for blocking I/O)."""
    peer = getattr(ws, "remote_address", None)
    tui_update_client(peer, True)
    loop = asyncio.get_event_loop()
    try:
        async for message in ws:
            cmd = message.strip()
            if not cmd:
                continue
            async with _serial_lock:
                try:
                    await loop.run_in_executor(None, f.write, (cmd + "\n").encode("ascii"))
                except OSError as e:
                    if not _tui_active:
                        print(f"\n  USBTMC write error: {e}")
                    break
                track_query(cmd)
                if "?" in cmd:
                    try:
                        raw = await loop.run_in_executor(None, f.read, 4096)
                    except OSError as e:
                        if not _tui_active:
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
        tui_update_client(peer, False)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    global _serial_lock, _tui_send_func

    _serial_lock = asyncio.Lock()

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
        influx_cfg = setup_influxdb()
        influx_desc = (f"enabled ({influx_cfg['measurement']})"
                       if influx_cfg else "disabled")

        async def _tcp_send(cmd):
            async with _serial_lock:
                try:
                    writer.write((cmd + "\n").encode("ascii"))
                    await writer.drain()
                except OSError as e:
                    return f"(TCP error: {e})"
                track_query(cmd)
                if "?" in cmd:
                    try:
                        raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
                        resp = raw.decode("ascii", errors="replace").strip()
                        track_response(resp)
                        return resp
                    except asyncio.TimeoutError:
                        return "(timeout)"
            return ""

        _tui_send_func = _tcp_send

        print(f"Starting WebSocket server on ws://{WS_HOST}:{WS_PORT}")
        print("Web app can now connect via the Bridge button.\n")
        tui_start(f"ethernet: {ip}:{TCP_PORT}", influx_desc)
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
        influx_cfg = setup_influxdb()
        influx_desc = (f"enabled ({influx_cfg['measurement']})"
                       if influx_cfg else "disabled")
        loop = asyncio.get_event_loop()

        async def _usbtmc_send(cmd):
            async with _serial_lock:
                try:
                    await loop.run_in_executor(None, f.write, (cmd + "\n").encode("ascii"))
                except OSError as e:
                    return f"(USBTMC error: {e})"
                track_query(cmd)
                if "?" in cmd:
                    try:
                        raw = await asyncio.wait_for(
                            loop.run_in_executor(None, f.read, 4096), timeout=2.0
                        )
                        resp = raw.decode("ascii", errors="replace").strip()
                        track_response(resp)
                        return resp
                    except asyncio.TimeoutError:
                        return "(timeout)"
            return ""

        _tui_send_func = _usbtmc_send

        print(f"Starting WebSocket server on ws://{WS_HOST}:{WS_PORT}")
        print("Web app can now connect via the Bridge button.\n")
        tui_start(f"usbtmc: {path}", influx_desc)
        async with websockets.serve(
            lambda ws: handler_usbtmc(ws, f), WS_HOST, WS_PORT
        ):
            await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        tui_stop()
        close_influxdb()
        print("\nBridge stopped.")
