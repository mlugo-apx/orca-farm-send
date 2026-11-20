"""Farm Send core: printer protocols and job logic. Pure standard library, no OrcaSlicer imports,
so it can be tested outside the slicer against real printers."""
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import struct
import threading
import time
import urllib.parse
import uuid

try:
    import fcntl
except ImportError:   # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:   # everything else
    msvcrt = None

CHUNK = 1024 * 1024
KEEP_PARTS_WAIT = 900.0   # seconds a start may take (leveling, heat-up) before the skip commands get through
KEEP_PARTS_POLL = 1.0
CC2_UPLOAD_TRIES = 5      # per piece, when the printer says busy (HTTP 429) or drops the connection
CC2_UPLOAD_PAUSE = 3.0    # seconds before the first retry; grows with each one. The printer needed about 6 s.
KIND_CC2 = "elegoo_cc2"
KIND_MOONRAKER = "moonraker"
KINDS = {KIND_CC2: "Elegoo Centauri Carbon 2 (ElegooLink)", KIND_MOONRAKER: "Moonraker / Klipper (Snapmaker U1)"}


class FarmError(Exception):
    """An error whose message is safe and useful to show to the user."""


# --------------------------------------------------------------------------------------
# G-code metadata
# --------------------------------------------------------------------------------------

_META_KEYS = {
    "printer_model": "printer_model",
    "printer_settings_id": "printer_preset",
    "filament_settings_id": "filament_preset",
    "print_settings_id": "process_preset",
    "filament_type": "filament_type",
    "nozzle_diameter": "nozzle_diameter",
    "total filament used [g]": "filament_g",
    "filament used [g]": "filament_g",
    "total filament weight [g]": "filament_g",
    "estimated printing time (normal mode)": "time_text",
    "model printing time": "time_text",
    "total estimated time": "time_text",
    "post_process": "post_process",
    "total layer number": "layers",
    "total layers count": "layers",
}


def _parse_duration(text):
    total = 0
    found = False
    for value, unit in re.findall(r"(\d+)\s*([dhms])", text or ""):
        total += int(value) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
        found = True
    return total if found else None


def _sum_numbers(text):
    values = re.findall(r"-?\d+(?:\.\d+)?", text or "")
    return round(sum(float(v) for v in values), 2) if values else None


def parse_gcode_meta(path, head_bytes=256 * 1024, tail_bytes=768 * 1024):
    """Read the slicer comments at the head and tail of a G-code file.

    OrcaSlicer writes the estimates in the header block and the full config dump at the end, so
    only those two regions are read; a 150 MB file costs the same as a 1 MB one."""
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        head = handle.read(min(size, head_bytes))
        tail = b""
        if size > head_bytes:
            handle.seek(max(head_bytes, size - tail_bytes))
            tail = handle.read()
    text = (head + b"\n" + tail).decode("utf-8", errors="replace")
    meta = {"size": size}
    for line in text.splitlines():
        if not line.startswith(";"):
            continue
        body = line[1:].strip()
        # The header packs two estimates on one line: "model printing time: 1h; total estimated time: 2h"
        parts = [body]
        if "total estimated time" in body and ";" in body:
            parts = [p.strip() for p in body.split(";")]
        for part in parts:
            sep = "=" if "=" in part and (":" not in part or part.index("=") < part.index(":")) else ":"
            if sep not in part:
                continue
            key, _, value = part.partition(sep)
            key = key.strip().lower()
            field = _META_KEYS.get(key)
            if field is None:
                continue
            value = value.strip().strip('"')
            if field == "time_text":
                # Prefer the total estimate over the model-only time when both exist.
                if key == "model printing time" and "time_text" in meta:
                    continue
                meta["time_text"] = value
            elif field == "filament_g":
                number = _sum_numbers(value)
                if number is not None and (key.startswith("total") or "filament_g" not in meta):
                    meta["filament_g"] = number
            elif field == "layers":
                digits = re.findall(r"\d+", value)
                if digits:
                    meta["layers"] = int(digits[0])
            else:
                meta[field] = value.split(";")[0].strip().strip('"') if field != "printer_model" else value
    meta["time_s"] = _parse_duration(meta.get("time_text"))
    meta["filaments"] = _filaments_used(text)
    return meta


def _config_list(text, key):
    match = re.search(r"^; %s = (.*)$" % re.escape(key), text, re.M)
    if not match:
        return []
    return [item.strip().strip('"') for item in match.group(1).strip().split(";")]


def _filaments_used(text):
    """Which filament slots the G-code actually extrudes with: [{slot, grams, color, preset, type}].

    Slot 1 is the printer's first toolhead. A plate assigned to the wrong slot prints in the wrong
    color, and nothing else in the slice summary shows it."""
    match = re.search(r"^; filament used \[g\] = (.*)$", text, re.M)
    if not match:
        return []
    grams = [float(value) if re.match(r"^-?\d+(\.\d+)?$", value.strip()) else 0.0 for value in match.group(1).split(",")]
    colors, presets, types = (_config_list(text, key) for key in ("filament_colour", "filament_settings_id", "filament_type"))
    used = []
    for index, amount in enumerate(grams):
        if amount > 0:
            used.append({"slot": index + 1, "grams": round(amount, 2),
                         "color": colors[index] if index < len(colors) else "",
                         "preset": presets[index] if index < len(presets) else "",
                         "type": types[index] if index < len(types) else ""})
    return used


def normalize_model(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def model_key(name):
    """Stable key for a printer type: 'Elegoo Centauri Carbon 2' and 'CentauriCarbon2' share one."""
    key = normalize_model(name)
    for vendor in ("elegoo", "snapmaker"):
        if key.startswith(vendor) and len(key) > len(vendor):
            key = key[len(vendor):]
    return key


def model_matches(gcode_model, printer_model):
    """True when G-code sliced for gcode_model may be sent to a printer of printer_model.

    Compared after dropping case, spaces and punctuation, and an "elegoo"/"snapmaker" vendor
    prefix, because the printers report e.g. "Centauri Carbon 2" or "CentauriCarbon2" while the
    presets say "Elegoo Centauri Carbon 2"."""
    a, b = normalize_model(gcode_model), normalize_model(printer_model)
    if not a or not b:
        return False
    for vendor in ("elegoo", "snapmaker"):
        if a.startswith(vendor):
            a = a[len(vendor):]
        if b.startswith(vendor):
            b = b[len(vendor):]
    return a == b


def safe_filename(name):
    name = os.path.basename(name or "").strip()
    # ASCII only: the Centauri Carbon 2 takes the name in a raw HTTP header, and the same name is
    # later sent back to start the print, so it must survive both trips byte for byte.
    name = re.sub(r"[^A-Za-z0-9_.+\- ()\[\]]", "_", name)
    name = re.sub(r"\s+", "_", name).strip("._")
    return name or "plate"


MODEL_SUFFIXES = (".gcode.3mf", ".gcode", ".3mf", ".stl", ".step", ".stp", ".obj", ".amf")


def strip_model_suffix(name):
    """'bracket.stl' -> 'bracket': object names carry the extension of the file they came from."""
    name = (name or "").strip()
    for suffix in MODEL_SUFFIXES:
        if name.lower().endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def job_filename(base, when=None, ext=".gcode"):
    """base + '_YYYYMMDD-HHMMSS' + ext, so two sends of the same plate never overwrite each other."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
    stem = safe_filename(strip_model_suffix(base))
    return "%s_%s%s" % (stem[:80], stamp, ext)


def file_md5(path):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def host_of(address):
    """Accept '192.168.0.5', '192.168.0.5:7125' or 'http://192.168.0.5/' and return host[:port]."""
    address = (address or "").strip()
    if "://" in address:
        address = urllib.parse.urlsplit(address).netloc
    return address.strip("/")


# --------------------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------------------

def _split_url(url):
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.hostname, parts.port or 80, path


def http_request(url, method="GET", data=None, headers=None, timeout=6):
    """One plain-HTTP request through http.client. urllib is avoided on purpose: OrcaSlicer's
    plugin audit keys a urllib grant on the full URL, while a connection is keyed on the host."""
    host, port, path = _split_url(url)
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        send_headers = {"Accept": "application/json"}
        send_headers.update(headers or {})
        if data is not None:
            send_headers.setdefault("Content-Length", str(len(data)))
        connection.request(method, path, body=data, headers=send_headers)
        response = connection.getresponse()
        return response.status, response.read()
    except PermissionError:
        raise
    except (socket.timeout, TimeoutError):
        raise FarmError("Cannot reach printer: timed out")
    except (OSError, http.client.HTTPException) as error:
        raise FarmError("Cannot reach printer: %s" % (getattr(error, "strerror", None) or error))
    finally:
        connection.close()


def http_json(url, method="GET", data=None, headers=None, timeout=6):
    status, raw = http_request(url, method, data, headers, timeout)
    if status in (401, 403):
        raise FarmError("Access denied (HTTP %d). Check the access code or API key." % status)
    if status >= 300:
        detail = raw[:200].decode("utf-8", errors="replace").strip()
        if detail.startswith("<"):
            detail = ""
        try:
            detail = json.loads(raw.decode("utf-8", errors="replace")).get("error", {}).get("message", detail)
        except (ValueError, AttributeError):
            pass
        raise FarmError("HTTP %d from printer%s" % (status, (": " + str(detail)) if detail else ""))
    try:
        value = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
        return value if isinstance(value, dict) else {}
    except ValueError:
        raise FarmError("Printer sent an unreadable reply (HTTP %d)" % status)


# --------------------------------------------------------------------------------------
# Minimal websocket JSON-RPC call (RFC 6455, text frames): some Moonraker methods are not
# offered over HTTP at all
# --------------------------------------------------------------------------------------

MAX_REPLY_BYTES = 4 * 1024 * 1024    # no printer reply this plugin reads is anywhere near this
MAX_WS_FRAMES = 2000                 # unrelated notifications tolerated while waiting for one reply


class RpcError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


def websocket_rpc(address, method, params, timeout=20, path="/websocket"):
    """Open a websocket, make one JSON-RPC call, return its result, close."""
    host, _, port = address.partition(":")
    request_id = 4242
    try:
        sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
    except PermissionError:
        raise
    except (socket.timeout, TimeoutError):
        raise FarmError("Cannot reach printer: timed out")
    except OSError as error:
        raise FarmError("Cannot reach printer: %s" % (error.strerror or error))
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, host, key)).encode("ascii"))
        deadline = time.monotonic() + timeout
        buffer = b""

        def fill(count):
            nonlocal buffer
            while len(buffer) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout()
                sock.settimeout(remaining)
                chunk = sock.recv(65536)
                if not chunk:
                    raise FarmError("Printer closed the connection")
                buffer += chunk

        while b"\r\n\r\n" not in buffer:
            if len(buffer) > 65536:
                raise FarmError("Printer sent an oversized reply")
            fill(len(buffer) + 1)
        head, _, buffer = buffer.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0] + b" ":
            raise RpcError(-32601, "Printer does not offer a websocket API")
        payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode("utf-8")
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            frame = bytes([0x81, 0x80 | size])
        elif size < 65536:
            frame = bytes([0x81, 0x80 | 126]) + struct.pack("!H", size)
        else:
            frame = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", size)
        sock.sendall(frame + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)))
        message = bytearray()
        for _ in range(MAX_WS_FRAMES):
            fill(2)
            final, opcode, length = buffer[0] & 0x80, buffer[0] & 0x0F, buffer[1] & 0x7F
            offset = 2
            if length == 126:
                fill(4)
                length, offset = struct.unpack("!H", buffer[2:4])[0], 4
            elif length == 127:
                fill(10)
                length, offset = struct.unpack("!Q", buffer[2:10])[0], 10
            # The length comes from the peer. Refuse before reading, so a hostile or broken device
            # cannot make the plugin buffer gigabytes inside OrcaSlicer.
            if length > MAX_REPLY_BYTES or len(message) + length > MAX_REPLY_BYTES:
                raise FarmError("Printer sent an oversized reply")
            fill(offset + length)
            data, buffer = buffer[offset:offset + length], buffer[offset + length:]
            if opcode == 8:
                raise FarmError("Printer closed the connection")
            if opcode not in (0, 1):
                continue        # ping/pong/binary: nothing this one-shot call needs
            message += data
            if not final:
                continue
            try:
                reply = json.loads(bytes(message).decode("utf-8", errors="replace"))
            except ValueError:
                reply = None
            message = bytearray()
            if not isinstance(reply, dict) or reply.get("id") != request_id:
                continue        # status notifications the printer pushes to every client
            if "error" in reply:
                error = reply["error"] if isinstance(reply["error"], dict) else {}
                raise RpcError(error.get("code", -1), str(error.get("message", "printer error")))
            return reply.get("result")
        raise FarmError("Printer did not answer the request")
    except PermissionError:
        raise
    except (socket.timeout, TimeoutError):
        raise FarmError("Printer did not answer in time")
    except (FarmError, RpcError):
        raise
    except OSError as error:
        raise FarmError("Lost connection to printer: %s" % (error.strerror or error))
    finally:
        try:
            sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------------------
# Minimal MQTT 3.1.1 client (QoS 0/1, no TLS): all the CC2 needs
# --------------------------------------------------------------------------------------

def _mqtt_str(text):
    raw = text.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


def _mqtt_len(length):
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        out.append(byte | (0x80 if length else 0))
        if not length:
            return bytes(out)


class MiniMqtt:
    def __init__(self, host, port=1883, username=None, password=None, client_id=None, timeout=6):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.client_id = client_id or ("fs" + uuid.uuid4().hex[:8])
        self.timeout = timeout
        self.sock = None
        self._packet_id = 0
        self._buffer = b""

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except PermissionError:
            raise
        except (socket.timeout, TimeoutError):
            raise FarmError("Cannot reach printer: timed out")
        except OSError as error:
            raise FarmError("Cannot reach printer: %s" % (error.strerror or error))
        flags = 0x02
        payload = _mqtt_str(self.client_id)
        if self.username is not None:
            flags |= 0x80
            payload += _mqtt_str(self.username)
        if self.password is not None:
            flags |= 0x40
            payload += _mqtt_str(self.password)
        body = _mqtt_str("MQTT") + bytes([4, flags]) + struct.pack("!H", 60) + payload
        self._send(0x10, body)
        try:
            kind, data = self._read_packet(self.timeout)
        except (socket.timeout, TimeoutError):
            raise FarmError("Printer did not answer the MQTT connection")
        except PermissionError:
            raise
        except OSError as error:
            raise FarmError("Lost connection to printer: %s" % (error.strerror or error))
        if kind != 0x20 or len(data) < 2:
            raise FarmError("Printer refused the MQTT connection")
        if data[1] != 0:
            if data[1] in (4, 5):
                raise FarmError("Access denied. Check the access code.")
            raise FarmError("Printer refused the MQTT connection (code %d)" % data[1])

    def close(self):
        if self.sock is not None:
            try:
                self.sock.sendall(b"\xe0\x00")
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _next_id(self):
        self._packet_id = self._packet_id % 65535 + 1
        return self._packet_id

    def _send(self, header, body):
        try:
            self.sock.sendall(bytes([header]) + _mqtt_len(len(body)) + body)
        except OSError as error:
            raise FarmError("Lost connection to printer: %s" % error)

    def subscribe(self, topic):
        self._send(0x82, struct.pack("!H", self._next_id()) + _mqtt_str(topic) + b"\x01")

    def publish(self, topic, payload):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._send(0x32, _mqtt_str(topic) + struct.pack("!H", self._next_id()) + payload)

    def _recv_exact(self, count, deadline):
        while len(self._buffer) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout()
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise FarmError("Printer closed the connection")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def _read_packet(self, timeout):
        deadline = time.monotonic() + timeout
        header = self._recv_exact(1, deadline)[0]
        length, shift = 0, 0
        while True:
            byte = self._recv_exact(1, deadline)[0]
            length |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        if length > MAX_REPLY_BYTES:
            raise FarmError("Printer sent an oversized reply")
        return header & 0xF0, (self._recv_exact(length, deadline) if length else b"")

    def read_message(self, timeout):
        """Return (topic, payload_bytes) for the next PUBLISH, or None on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                # Re-read the fixed header byte ourselves: QoS lives in its low nibble.
                first = self._recv_exact(1, time.monotonic() + remaining)[0]
            except socket.timeout:
                return None
            except OSError as error:
                raise FarmError("Lost connection to printer: %s" % error)
            hard_deadline = time.monotonic() + self.timeout
            try:
                length, shift = 0, 0
                while True:
                    byte = self._recv_exact(1, hard_deadline)[0]
                    length |= (byte & 0x7F) << shift
                    if not byte & 0x80:
                        break
                    shift += 7
                if length > MAX_REPLY_BYTES:
                    raise FarmError("Printer sent an oversized reply")
                data = self._recv_exact(length, hard_deadline) if length else b""
            except socket.timeout:
                raise FarmError("Printer stopped responding")
            except OSError as error:
                raise FarmError("Lost connection to printer: %s" % error)
            if first & 0xF0 != 0x30:
                continue
            qos = (first >> 1) & 0x03
            topic_len = struct.unpack("!H", data[:2])[0]
            topic = data[2:2 + topic_len].decode("utf-8", errors="replace")
            offset = 2 + topic_len
            if qos:
                packet_id = data[offset:offset + 2]
                offset += 2
                if qos == 1:
                    self._send(0x40, packet_id)
            return topic, data[offset:]


# --------------------------------------------------------------------------------------
# Elegoo Centauri Carbon 2
# --------------------------------------------------------------------------------------

CC2_STATUS = {
    -1: "offline", 0: "busy", 1: "idle", 2: "printing", 3: "busy", 4: "busy", 5: "busy", 6: "busy", 7: "busy",
    8: "busy", 9: "busy", 10: "busy", 11: "busy", 12: "busy", 13: "busy", 14: "error", 15: "busy", 16: "busy",
    17: "busy",
}
CC2_STATUS_TEXT = {
    0: "Initializing", 1: "Idle", 2: "Printing", 3: "Loading filament", 4: "Unloading filament",
    5: "Auto leveling", 6: "PID calibration", 7: "Resonance test", 8: "Self check", 9: "Upgrading",
    10: "Homing", 11: "Receiving file", 12: "Building time-lapse", 13: "Extruding", 14: "Emergency stop",
    15: "Power-loss resume", 16: "Upgrading component", 17: "Foreign object detection",
}
CC2_GET_BASIC_INFO = 1002
CC2_START_PRINT = 1020
CC2_GET_FILE_LIST = 1044
CC2_GET_FILE_DETAIL = 1046
CC2_DELETE_FILE = 1047
CC2_DELETE_BATCH = 10      # files per delete request
CC2_MAX_LISTED = 5000      # stop paging a list that never ends
CC2_MANY_FILES = 250       # uploads began failing (error 9002) on printers holding 355 and 516 files
CC2_ERR_BED_MESH_MISSING = 1026
CC2_ERRORS = {
    109: "filament runout", 1000: "access code rejected", 1003: "invalid parameter", 1004: "the printer could not write the file",
    1009: "the printer is busy", 1014: "the printer rejected the G-code file as invalid", 1021: "the file is not on the printer",
    1026: "bed mesh data is missing", 1036: "a printer module upgrade is not finished",
    9000: "upload offset mismatch", 9001: "the printer could not open the file for writing", 9002: "the printer could not write the file",
    9004: "checksum mismatch after upload", 9007: "the printer's upload folder is missing", 9010: "the printer could not replace a file of the same name",
}


class ElegooCC2:
    kind = KIND_CC2

    def __init__(self, address, access_code="", timeout=6, mqtt_port=1883):
        self.address = host_of(address)
        self.host = self.address.split(":")[0]
        self.token = access_code or "123456"
        self.timeout = timeout
        self.mqtt_port = int(mqtt_port or 1883)
        self._sn = None

    # -- HTTP -------------------------------------------------------------------------
    def system_info(self):
        url = "http://%s/system/info?X-Token=%s" % (self.address, urllib.parse.quote(self.token))
        reply = http_json(url, headers={"X-Token": self.token, "Accept": "application/json"}, timeout=self.timeout)
        if reply.get("error_code", -1) != 0:
            raise FarmError("Printer returned an error: %s (%s)" % (reply.get("message", "unknown"), reply.get("error_code")))
        info = reply.get("system_info") or {}
        if not info.get("sn"):
            raise FarmError("Printer did not report a serial number")
        self._sn = info["sn"]
        return info

    def serial(self):
        if not self._sn:
            self.system_info()
        return self._sn

    def identify(self):
        info = self.system_info()
        return {"model": info.get("machine_model", ""), "name": info.get("hostname", ""), "serial": info.get("sn", ""),
                "firmware": (info.get("software_version") or {}).get("ota_version", "")}

    def upload(self, path, filename, progress=None, cancelled=None):
        """Send the file in 1 MB pieces over ONE kept-open connection, as OrcaSlicer's own ElegooLink
        client does. The printer answers HTTP 429 once a few connections arrive within seconds, so a
        connection per piece fails on any plate larger than about 3 MB (seen on a 23 MB plate; the
        same plate took 6 s over one connection). A 429 or a dropped connection is retried on a new
        connection after a pause, resending the same piece."""
        filename = safe_filename(filename)
        size = os.path.getsize(path)
        if size <= 0:
            raise FarmError("The G-code file is empty")
        md5 = file_md5(path)
        host, port, target = _split_url("http://%s/upload" % self.address)
        connection, sent = None, 0
        try:
            with open(path, "rb") as handle:
                while sent < size:
                    chunk = handle.read(CHUNK)
                    if not chunk:
                        raise FarmError("The G-code file changed while uploading")
                    headers = {
                        "Accept": "application/json",
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": "bytes %d-%d/%d" % (sent, sent + len(chunk) - 1, size),
                        "X-File-Name": filename,   # raw, exactly as OrcaSlicer's own ElegooLink client sends it
                        "X-File-MD5": md5,
                        "X-Token": self.token,
                    }
                    for attempt in range(CC2_UPLOAD_TRIES):
                        if cancelled is not None and cancelled():
                            raise FarmError("Upload cancelled")
                        status, raw, problem = 0, b"", ""
                        try:
                            if connection is None:
                                connection = http.client.HTTPConnection(host, port, timeout=180)
                            connection.request("PUT", target, body=chunk, headers=headers)
                            response = connection.getresponse()
                            status, raw = response.status, response.read()
                        except PermissionError:
                            raise
                        except (socket.timeout, TimeoutError):
                            problem = "timed out"
                        except (OSError, http.client.HTTPException) as error:
                            problem = str(getattr(error, "strerror", None) or error)
                        if status == 200:
                            break
                        if status in (401, 403):
                            raise FarmError("Access denied (HTTP %d). Check the access code or API key." % status)
                        # Busy or dropped: start over on a fresh connection after a growing pause.
                        if connection is not None:
                            connection.close()
                            connection = None
                        if attempt == CC2_UPLOAD_TRIES - 1 or (status and status != 429):
                            raise FarmError("Upload failed: %s" % (("HTTP %d from printer" % status) if status else
                                                                   ("cannot reach printer: " + problem)))
                        time.sleep(CC2_UPLOAD_PAUSE * (attempt + 1))
                    try:
                        reply = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
                    except ValueError:
                        reply = {}
                    _cc2_check(reply if isinstance(reply, dict) and "error_code" in reply else {"error_code": -1}, "Upload failed")
                    sent += len(chunk)
                    if progress is not None:
                        progress(sent, size)
        finally:
            if connection is not None:
                connection.close()
        return filename

    # -- MQTT -------------------------------------------------------------------------
    def _session(self):
        return _CC2Session(self.host, self.serial(), self.token, self.timeout, self.mqtt_port)

    def status(self):
        with self._session() as session:
            return normalize_cc2_status(session.request(CC2_GET_BASIC_INFO, {}))

    def raw_status(self):
        with self._session() as session:
            return session.request(CC2_GET_BASIC_INFO, {})

    def list_files(self, limit=50):
        with self._session() as session:
            reply = session.request(CC2_GET_FILE_LIST, {"storage_media": "local", "offset": 0, "limit": limit}, timeout=15)
        _cc2_check(reply, "Could not list files")
        return [entry.get("filename", "") for entry in reply.get("file_list") or []]

    def list_stored(self):
        """Every stored file as {name, modified, size}. The printer hands out 100 per request."""
        files = []
        with self._session() as session:
            while len(files) < CC2_MAX_LISTED:
                reply = session.request(CC2_GET_FILE_LIST, {"storage_media": "local", "offset": len(files), "limit": 100}, timeout=15)
                _cc2_check(reply, "Could not list files")
                page = [entry for entry in reply.get("file_list") or [] if isinstance(entry, dict)]
                files += [{"name": str(entry.get("filename") or ""), "modified": float(entry.get("create_time") or 0),
                           "size": int(entry.get("size") or 0)}
                          for entry in page if entry.get("type", "file") == "file" and entry.get("filename")]
                total = reply.get("total")
                if not page or len(page) < 100 or (isinstance(total, int) and len(files) >= total):
                    break
        return files

    def delete_files(self, names, progress=None, cancelled=None):
        """Delete stored files in one session, a few per request. Returns how many requests of
        names were sent; the printer answers "ok" even for a file it did not delete, so the caller
        lists again to see what is really gone."""
        names, done = list(names), 0
        with self._session() as session:
            for start in range(0, len(names), CC2_DELETE_BATCH):
                if cancelled is not None and cancelled():
                    break
                batch = names[start:start + CC2_DELETE_BATCH]
                reply = session.request(CC2_DELETE_FILE, {"storage_media": "local", "file_path": batch}, timeout=20)
                _cc2_check(reply, "Could not delete files")
                done += len(batch)
                if progress is not None:
                    progress(done, len(names))
        return done

    def delete_file(self, filename):
        with self._session() as session:
            # The printer takes a list of paths; a bare string is rejected as "invalid parameter".
            reply = session.request(CC2_DELETE_FILE, {"storage_media": "local", "file_path": [filename]})
        _cc2_check(reply, "Could not delete file")

    def start_print(self, filename, options=None):
        options = options or {}
        config = {
            "delay_video": bool(options.get("timelapse", False)),
            "printer_check": bool(options.get("bed_leveling", True)),
            "print_layout": options.get("plate", "A"),
        }
        params = {"filename": filename, "storage_media": "local", "config": config}
        with self._session() as session:
            reply = session.request(CC2_START_PRINT, params, timeout=20)
            if reply.get("error_code") == CC2_ERR_BED_MESH_MISSING:
                config["bedlevel_force"] = True
                reply = session.request(CC2_START_PRINT, params, timeout=20)
        _cc2_check(reply, "Printer refused to start the print")


def _cc2_check(reply, what):
    code = reply.get("error_code", 0)
    if code not in (0, None):
        reason = CC2_ERRORS.get(code) or str(reply.get("message") or "")
        raise FarmError("%s: %s" % (what, reason) if reason else "%s (printer error %s)" % (what, code))


_CC2_CLIENT_IDS = {}
_CC2_CLIENT_LOCK = threading.Lock()
_CC2_SERIAL_LOCKS = {}
_CC2_SHARED_DIR = None


def set_shared_dir(path):
    """Folder that every OrcaSlicer window of this user shares (the plugin's storage folder).

    With it set, all windows use the same client id for a printer and take turns through a lock
    file, so together they occupy one of the printer's few client slots instead of one each."""
    global _CC2_SHARED_DIR
    with _CC2_CLIENT_LOCK:
        if path != _CC2_SHARED_DIR:
            _CC2_SHARED_DIR = path
            _CC2_CLIENT_IDS.clear()


def write_private(path, text, exclusive=False):
    """Write a small file readable only by the owner, created that way in one step.

    Only calls that name the file are used. OrcaSlicer's permission check asks the user about any
    file call it cannot show a path for, which rules out os.fdopen() and os.fchmod() on a bare
    descriptor: both raised an "allow this operation with no visible target" prompt. So the file
    is created with its final permissions by os.open(), written with os.write(), and a leftover
    from an earlier run is removed first instead of having its permissions changed. O_EXCL makes
    the create fail if anything, including a planted symlink, is already at the name."""
    if not exclusive:
        try:
            os.unlink(path)
        except OSError:
            pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        data = text.encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)


def _cc2_install_token(folder):
    """A random token kept in the shared folder, so two computers never pick the same client id."""
    path = os.path.join(folder, "cc2_client_token")
    for _ in range(3):   # read; or replace a damaged file, create, then read back
        try:
            with open(path, "r", encoding="ascii") as handle:
                token = handle.read().strip()
            if re.fullmatch(r"[0-9a-f]{32}", token):
                return token
        except (OSError, ValueError):
            pass
        try:
            # O_EXCL: when two windows start together, one creates the file and the other reads it.
            write_private(path, uuid.uuid4().hex, exclusive=True)
        except FileExistsError:
            try:   # present but unreadable or damaged: replace it
                if os.path.getsize(path) != 32:
                    os.unlink(path)
            except OSError:
                pass
        except OSError:
            return None
    return None


def _cc2_client_id(serial):
    """One client id per printer: shared by every window when a shared folder is set, otherwise
    for the life of this process.

    The printer keeps a registration slot per client id for about a minute after the client goes
    away and has only a few slots. A fresh id on every status poll fills them all within a minute
    and locks out ElegooSlicer and the printer's own web page; a stable id re-uses one slot."""
    with _CC2_CLIENT_LOCK:
        if serial not in _CC2_CLIENT_IDS:
            token = _cc2_install_token(_CC2_SHARED_DIR) if _CC2_SHARED_DIR else None
            tail = hashlib.sha256(("%s/%s" % (token, serial)).encode("utf-8")).hexdigest() if token else uuid.uuid4().hex
            # Same shape as the vendor web client: "0" + tag + hex, 10 characters.
            _CC2_CLIENT_IDS[serial] = ("0fs" + tail)[:10]
            _CC2_SERIAL_LOCKS.setdefault(serial, threading.Lock())
        return _CC2_CLIENT_IDS[serial]


class _FileLock:
    """Lock between processes on one computer. Released by the system if the process dies."""

    def __init__(self, path):
        self.path, self._handle = path, None

    def acquire(self, timeout):
        if fcntl is None and msvcrt is None:
            return True
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            return True   # cannot lock here: carry on unshared instead of failing every request
        deadline = time.monotonic() + timeout
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                self._handle = fd
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.05)

    def release(self):
        fd, self._handle = self._handle, None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            os.close(fd)


def _cc2_file_lock(serial):
    if not _CC2_SHARED_DIR:
        return None
    name = "cc2_%s.lock" % hashlib.sha256(serial.encode("utf-8")).hexdigest()[:16]
    return _FileLock(os.path.join(_CC2_SHARED_DIR, name))


class _CC2Session:
    """One short-lived registered MQTT session. The printer allows only a few clients, so every
    operation connects, registers, talks and disconnects instead of holding a slot open."""

    def __init__(self, host, serial, token, timeout, port=1883):
        self.serial, self.timeout = serial, timeout
        self.client_id = _cc2_client_id(serial)
        self.mqtt = MiniMqtt(host, port, "elegoo", token, client_id=self.client_id, timeout=timeout)
        self._request_id = 0

    def __enter__(self):
        self._serial_lock = _CC2_SERIAL_LOCKS[self.serial]
        if not self._serial_lock.acquire(timeout=30):
            raise FarmError("The printer is busy answering another request")
        # Other OrcaSlicer windows use the same client id, so only one may talk at a time.
        self._file_lock = _cc2_file_lock(self.serial)
        if self._file_lock is not None and not self._file_lock.acquire(timeout=30):
            self._serial_lock.release()
            raise FarmError("The printer is busy answering another OrcaSlicer window")
        try:
            self.mqtt.connect()
        except BaseException:
            self.mqtt.close()   # the socket may be open even though the MQTT handshake failed
            self._release()
            raise
        try:
            base = "elegoo/%s" % self.serial
            self.mqtt.subscribe("%s/%s/register_response" % (base, self.client_id))
            self.mqtt.subscribe("%s/%s/api_response" % (base, self.client_id))
            self.mqtt.publish("%s/api_register" % base, {"client_id": self.client_id, "request_id": self.client_id})
            deadline = time.monotonic() + self.timeout
            while True:
                message = self.mqtt.read_message(max(0.05, deadline - time.monotonic()))
                if message is None:
                    raise FarmError("Printer did not answer the registration request")
                topic, payload = message
                if not topic.endswith("/register_response"):
                    continue
                reply = _loads(payload)
                error = str(reply.get("error", ""))
                if error in ("ok", "already registered"):
                    return self
                if error == "too many clients":
                    raise FarmError("The printer has too many connected apps. Close ElegooSlicer or another client and retry.")
                raise FarmError("Printer rejected the registration: %s" % (error or "unknown reason"))
        except BaseException:
            self.mqtt.close()
            self._release()
            raise

    def _release(self):
        try:
            if self._file_lock is not None:
                self._file_lock.release()
        finally:
            self._serial_lock.release()

    def __exit__(self, *exc):
        try:
            self.mqtt.close()
        finally:
            self._release()
        return False

    def request(self, method, params, timeout=None):
        timeout = timeout or self.timeout
        self._request_id += 1
        request_id = self._request_id
        topic = "elegoo/%s/%s/api_request" % (self.serial, self.client_id)
        self.mqtt.publish(topic, {"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FarmError("Printer did not answer in time")
            message = self.mqtt.read_message(remaining)
            if message is None:
                raise FarmError("Printer did not answer in time")
            if not message[0].endswith("/api_response"):
                continue
            reply = _loads(message[1])
            if reply.get("id") == request_id:
                result = reply.get("result")
                return result if isinstance(result, dict) else {}


def _loads(payload):
    try:
        value = json.loads(payload.decode("utf-8", errors="replace"))
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def normalize_cc2_status(info):
    machine = info.get("machine_status") or {}
    job = info.get("print_status") or {}
    code = machine.get("status", 0)
    state = CC2_STATUS.get(code, "busy")
    out = {
        "state": state,
        "state_text": CC2_STATUS_TEXT.get(code, "Busy"),
        "filename": job.get("filename") or "",
        "progress": None,
        "remaining_s": None,
        "nozzle": _temp(info.get("extruder")),
        "bed": _temp(info.get("heater_bed")),
        "errors": list(machine.get("exception_status") or []),
    }
    if state == "printing":
        progress = machine.get("progress")
        if isinstance(progress, (int, float)):
            out["progress"] = max(0.0, min(100.0, float(progress)))
        remaining = job.get("remaining_time_sec")
        if isinstance(remaining, (int, float)):
            out["remaining_s"] = int(remaining)
        sub = job.get("state") or ""
        if str(sub).lower() in ("paused", "pausing"):
            out["state"], out["state_text"] = "paused", "Paused"
    # An idle printer keeps showing 100% after a print that ran to the end, and 0 before any print.
    progress = machine.get("progress")
    out["last_result"] = "complete" if state == "idle" and isinstance(progress, (int, float)) and progress >= 100 else ""
    if out["errors"] and state == "idle":
        out["state_text"] = "Idle (warning %s)" % ", ".join(str(e) for e in out["errors"])
    return out


WATCHED_STATES = ("idle", "printing", "paused", "error")


def status_event(before, now, name):
    """What changed on a printer between two status reads that is worth telling the user.

    before: the last status seen in a watched state, or None on the first read. Returns
    (level, text) or None. "Offline" and "busy" reads are skipped by the caller, so a print that
    ends while the network blips is still reported from the last "printing" read."""
    if not before or now.get("state") not in WATCHED_STATES:
        return None
    was, state = before.get("state"), now.get("state")
    job = before.get("filename") or now.get("filename") or "its print"
    if state == "error" and was != "error":
        return "error", "%s stopped with an error: %s" % (name, now.get("state_text") or "Error")
    if state == "paused" and was == "printing":
        return "error", "%s paused %s" % (name, job)
    if state == "idle" and was in ("printing", "paused"):
        result = now.get("last_result") or ""
        progress = before.get("progress")
        if result == "cancelled":
            return "info", "%s: %s was cancelled" % (name, job)
        if result == "complete" or (isinstance(progress, (int, float)) and progress >= 99):
            return "info", "%s finished %s" % (name, job)
        # No result reported (Centauri Carbon 2) and not near the end: do not claim it finished.
        return "info", "%s stopped printing %s at %d%%" % (name, job, progress or 0)
    if state in ("printing", "paused"):
        fresh = [code for code in now.get("errors") or [] if code not in (before.get("errors") or [])]
        if fresh:
            return "error", "%s reports a problem while printing (code %s)" % (name, ", ".join(str(code) for code in fresh))
    return None


def _temp(block):
    if not isinstance(block, dict):
        return None
    value = block.get("temperature")
    if not isinstance(value, (int, float)):
        return None
    target = block.get("target")
    return {"current": round(float(value), 1), "target": round(float(target), 1) if isinstance(target, (int, float)) else 0.0}


# --------------------------------------------------------------------------------------
# Moonraker (Snapmaker U1 and other Klipper printers)
# --------------------------------------------------------------------------------------

class Moonraker:
    kind = KIND_MOONRAKER

    def __init__(self, address, api_key="", timeout=6):
        self.address = host_of(address)
        self.api_key = api_key or ""
        self.timeout = timeout

    def _headers(self, extra=None):
        headers = dict(extra or {})
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    def _get(self, path, timeout=None):
        return http_json("http://%s%s" % (self.address, path), headers=self._headers(), timeout=timeout or self.timeout)

    def _post(self, path, timeout=None):
        return http_json("http://%s%s" % (self.address, path), method="POST", data=b"", headers=self._headers(),
                         timeout=timeout or self.timeout)

    def identify(self):
        info = (self._get("/printer/info").get("result") or {})
        model = ""
        try:
            objects = self._get("/printer/objects/query?configfile=settings").get("result", {})
            settings = objects.get("status", {}).get("configfile", {}).get("settings", {})
            if any(key.startswith("extruder") for key in settings) and "extruder3" in settings:
                model = "Snapmaker U1"
        except FarmError:
            pass
        try:
            machine = self._get("/machine/system_info").get("result", {}).get("system_info", {})
            product = (machine.get("product_info") or {}).get("machine_type") or ""
            if product:
                model = product
        except FarmError:
            pass
        return {"model": model, "name": info.get("hostname", ""), "serial": "", "firmware": info.get("software_version", "")}

    def status(self):
        reply = self._get("/printer/objects/query?print_stats&virtual_sdcard&heater_bed&display_status&toolhead&extruder"
                          "&extruder1&extruder2&extruder3&print_task_config")
        status = reply.get("result", {}).get("status", {})
        filename = (status.get("print_stats") or {}).get("filename") or ""
        active = (status.get("print_stats") or {}).get("state") in ("printing", "paused")
        return normalize_moonraker_status(status, self._klippy_state, self._estimated_time(filename) if active else None)

    def _estimated_time(self, filename):
        """The slicer's time estimate for a file, asked once per file and remembered."""
        if not filename:
            return None
        key = (self.address, filename)
        if key not in _MOONRAKER_ESTIMATES:
            try:
                reply = self._get("/server/files/metadata?filename=%s" % urllib.parse.quote(filename))
                value = (reply.get("result") or {}).get("estimated_time")
            except FarmError:
                return None   # not remembered: ask again on the next poll
            if len(_MOONRAKER_ESTIMATES) > 64:
                _MOONRAKER_ESTIMATES.clear()
            _MOONRAKER_ESTIMATES[key] = float(value) if isinstance(value, (int, float)) and value > 0 else None
        return _MOONRAKER_ESTIMATES[key]

    def _klippy_state(self):
        return (self._get("/printer/info").get("result") or {})

    def list_files(self, limit=None):
        reply = self._get("/server/files/list?root=gcodes", timeout=15)
        return [entry.get("path", "") for entry in reply.get("result") or []]

    def list_stored(self):
        """Every stored G-code file as {name, modified, size}."""
        reply = self._get("/server/files/list?root=gcodes", timeout=20)
        return [{"name": str(entry.get("path") or ""), "modified": float(entry.get("modified") or 0), "size": int(entry.get("size") or 0)}
                for entry in reply.get("result") or []
                if isinstance(entry, dict) and str(entry.get("path") or "").lower().endswith((".gcode", ".g", ".gco"))]

    def delete_files(self, names, progress=None, cancelled=None):
        names, done = list(names), 0
        for name in names:
            if cancelled is not None and cancelled():
                break
            self.delete_file(name)
            done += 1
            if progress is not None:
                progress(done, len(names))
        return done

    def delete_file(self, filename):
        http_json("http://%s/server/files/gcodes/%s" % (self.address, urllib.parse.quote(filename)), method="DELETE",
                  headers=self._headers(), timeout=self.timeout)

    def upload(self, path, filename, progress=None, cancelled=None):
        size = os.path.getsize(path)
        if size <= 0:
            raise FarmError("The G-code file is empty")
        boundary = "----farmsend" + uuid.uuid4().hex
        head = ("--%s\r\nContent-Disposition: form-data; name=\"root\"\r\n\r\ngcodes\r\n"
                "--%s\r\nContent-Disposition: form-data; name=\"print\"\r\n\r\nfalse\r\n"
                "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n" % (boundary, boundary, boundary, filename.replace('"', "_"))
                ).encode("utf-8")
        tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        total = len(head) + size + len(tail)
        host, _, port = self.address.partition(":")
        connection = http.client.HTTPConnection(host, int(port or 80), timeout=60)
        try:
            connection.putrequest("POST", "/server/files/upload")
            connection.putheader("Content-Type", "multipart/form-data; boundary=%s" % boundary)
            connection.putheader("Content-Length", str(total))
            if self.api_key:
                connection.putheader("X-Api-Key", self.api_key)
            connection.endheaders()
            connection.send(head)
            sent = 0
            with open(path, "rb") as handle:
                while True:
                    if cancelled is not None and cancelled():
                        raise FarmError("Upload cancelled")
                    chunk = handle.read(256 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    if progress is not None:
                        progress(sent, size)
            if sent != size:
                raise FarmError("The G-code file changed while uploading")
            connection.send(tail)
            connection.sock.settimeout(180)
            response = connection.getresponse()
            body = response.read()
        except (FarmError, PermissionError):
            raise
        except ConnectionRefusedError:
            raise FarmError("Cannot reach printer: connection refused")
        except (socket.timeout, TimeoutError):
            raise FarmError("Upload failed: the printer stopped responding")
        except (OSError, http.client.HTTPException) as error:
            raise FarmError("Upload failed: %s" % (getattr(error, "strerror", None) or error))
        finally:
            connection.close()
        if response.status in (401, 403):
            raise FarmError("Access denied (HTTP %d). Check the API key." % response.status)
        if response.status >= 300:
            raise FarmError("Upload failed: HTTP %d %s" % (response.status, body[:200].decode("utf-8", errors="replace")))
        try:
            item = json.loads(body.decode("utf-8", errors="replace")).get("result", {}).get("item", {})
            return item.get("path") or filename
        except ValueError:
            return filename

    def start_print(self, filename, options=None):
        """Start a file already on the printer.

        Snapmaker firmware decides bed leveling, flow calibration and time-lapse from options that
        travel WITH the start request (server.files.start_local_print, websocket only). A plain
        Moonraker start carries none, and the printer then silently skips auto bed leveling. So the
        Snapmaker call is tried first; a printer that does not know it gets the standard start."""
        options = options or {}
        table = toolhead_map_table(options.get("toolhead_map"))
        remapped = any(slot != tool for slot, tool in table)
        params = {"type": "gcode", "path": filename, "options": {
            "map_table": table,
            "bed_level": 1 if options.get("bed_leveling", True) else 0,
            "flow_calibrate": 1 if options.get("flow_calibrate", False) else 0,
            "time_lapse_camera": 1 if options.get("timelapse", False) else 0,
        }}
        try:
            result = websocket_rpc(self.address, "server.files.start_local_print", params, timeout=30)
        except RpcError as error:
            text = str(error).lower()
            if error.code == -32601 or "not found" in text or "unknown method" in text:
                if remapped:
                    # A plain start cannot carry the mapping: it would print from the sliced toolhead.
                    raise FarmError("Not started: this printer cannot print from a different toolhead than the file was sliced for.")
                self._post("/printer/print/start?filename=%s" % urllib.parse.quote(filename), timeout=30)
                return "standard"
            raise FarmError("Printer refused to start the print: %s" % error)
        if isinstance(result, dict) and str(result.get("state", "")).lower() not in ("success", "ok", ""):
            raise FarmError("Printer refused to start the print: %s" % (result.get("message") or result.get("state")))
        return "snapmaker"

    def cancel_print(self):
        self._post("/printer/print/cancel", timeout=30)

    def keep_parts(self, keep, cancelled=None, wait=None):
        """Right after a start: print only the first `keep` labelled parts of the plate and skip the rest.

        The printer lists the plate's parts a moment after the file starts. The skip commands then
        wait in the printer's queue until its first heat-up pause ends, and take effect before
        anything is extruded (seen on a U1: 26 of 28 skipped, 0 mm used). Returns (kept, total).
        Raises FarmError when the skips cannot be confirmed; the caller must then stop the print."""
        wait = KEEP_PARTS_WAIT if wait is None else wait
        deadline = time.monotonic() + wait
        query = "/printer/objects/query?exclude_object&print_stats=state"
        names = []
        while not names:
            if cancelled is not None and cancelled():
                raise FarmError("cancelled")
            status = (self._get(query).get("result") or {}).get("status") or {}
            names = [str(o.get("name")) for o in (status.get("exclude_object") or {}).get("objects") or [] if o.get("name")]
            if names:
                break
            if (status.get("print_stats") or {}).get("state") not in ("printing", "paused") and time.monotonic() > deadline - wait + 20:
                raise FarmError("the printer is not printing")
            if time.monotonic() > deadline:
                raise FarmError("the printer did not list the parts on this plate")
            time.sleep(KEEP_PARTS_POLL)
        total = len(names)
        if not 1 <= keep <= total:
            raise FarmError("the plate has %d parts" % total)
        skip = names[keep:]
        for start in range(0, len(skip), 20):          # a few at a time keeps each request short
            script = "\n".join("EXCLUDE_OBJECT NAME=%s" % name for name in skip[start:start + 20])
            self._post("/printer/gcode/script?script=%s" % urllib.parse.quote(script), timeout=max(30, deadline - time.monotonic()))
        for _ in range(20):                            # the printer's own list is the proof
            status = (self._get(query).get("result") or {}).get("status") or {}
            if len((status.get("exclude_object") or {}).get("excluded_objects") or []) == len(skip):
                return keep, total
            time.sleep(KEEP_PARTS_POLL)
        raise FarmError("the printer did not confirm the skipped parts")


_MOONRAKER_ESTIMATES = {}


def count_objects(path):
    """How many separately labelled parts the G-code defines (Klipper EXCLUDE_OBJECT_DEFINE lines).
    0 when the slicer did not label objects, as on a Centauri Carbon 2."""
    found = 0
    with open(path, "rb") as handle:
        for line in handle:
            if line.startswith(b"EXCLUDE_OBJECT_DEFINE"):
                found += 1
    return found


def moonraker_remaining(progress, elapsed, estimated_time=None):
    """Seconds left. progress is 0..1 of the file, elapsed is Klipper's print_duration.

    With the slicer's estimate: the unprinted share of it. Without: extrapolate from elapsed
    time, but only once enough has printed, because print_duration also counts heating and bed
    leveling and that swamps the early figure (29 hours shown for a 5 hour job at 1%)."""
    if estimated_time:
        return int(estimated_time * (1.0 - progress))
    if isinstance(elapsed, (int, float)) and progress >= 0.10:
        return int(elapsed / progress - elapsed)
    return None


def normalize_moonraker_status(status, klippy_info=None, estimated_time=None):
    stats = status.get("print_stats") or {}
    sdcard = status.get("virtual_sdcard") or {}
    raw_state = str(stats.get("state") or "").lower()
    mapping = {"standby": ("idle", "Idle"), "complete": ("idle", "Idle (last print complete)"),
               "cancelled": ("idle", "Idle (last print cancelled)"), "printing": ("printing", "Printing"),
               "paused": ("paused", "Paused"), "error": ("error", "Error")}
    state, text = mapping.get(raw_state, ("busy", raw_state.capitalize() or "Unknown"))
    result = raw_state if raw_state in ("complete", "cancelled") else ""
    if not raw_state and klippy_info is not None:
        info = klippy_info()
        if info.get("state") != "ready":
            state, text = "error", "Klipper: %s" % (info.get("state_message") or info.get("state") or "not ready").splitlines()[0]
    if state == "error" and stats.get("message"):
        text = "Error: %s" % str(stats["message"]).splitlines()[0]
    out = {"state": state, "state_text": text, "filename": stats.get("filename") or "", "progress": None,
           "remaining_s": None, "nozzle": None, "bed": _temp(status.get("heater_bed")), "errors": [],
           "last_result": result}
    if isinstance(stats.get("filament_used"), (int, float)):
        out["extruded_mm"] = float(stats["filament_used"])      # 0 while a print is still heating
    # Report the hottest tool: on a tool changer the parked tools sit near ambient.
    tools = [_temp(status.get(name)) for name in ("extruder", "extruder1", "extruder2", "extruder3")]
    tools = [tool for tool in tools if tool]
    if tools:
        out["nozzle"] = max(tools, key=lambda tool: (tool["target"], tool["current"]))
    out["tools"] = _moonraker_tools(status.get("print_task_config"))
    out["has_print_options"] = isinstance(status.get("print_task_config"), dict)
    if state in ("printing", "paused"):
        progress = sdcard.get("progress")
        if isinstance(progress, (int, float)):
            out["progress"] = max(0.0, min(100.0, float(progress) * 100.0))
            progress = max(0.0, min(1.0, float(progress)))
            out["remaining_s"] = moonraker_remaining(progress, stats.get("print_duration"), estimated_time)
    return out


def _moonraker_tools(config):
    """What a Snapmaker reports as loaded in each toolhead: [{slot, color, type, loaded}]."""
    if not isinstance(config, dict):
        return []
    colors = config.get("filament_color_rgba") or []
    types = config.get("filament_type") or []
    exists = config.get("filament_exist") or []
    tools = []
    for index in range(max(len(colors), len(types))):
        raw = str(colors[index]) if index < len(colors) else ""
        tools.append({"slot": index + 1,
                      "color": ("#" + raw[:6]) if re.match(r"^[0-9A-Fa-f]{6}", raw) else "",
                      "type": str(types[index]) if index < len(types) else "",
                      "loaded": bool(exists[index]) if index < len(exists) else True})
    return tools


# --------------------------------------------------------------------------------------
# Printer registry
# --------------------------------------------------------------------------------------

def make_client(printer, timeout=6):
    kind = printer.get("kind")
    if kind == KIND_CC2:
        return ElegooCC2(printer.get("address", ""), printer.get("access_code", ""), timeout=timeout,
                         mqtt_port=printer.get("mqtt_port") or 1883)
    if kind == KIND_MOONRAKER:
        return Moonraker(printer.get("address", ""), printer.get("api_key", ""), timeout=timeout)
    raise FarmError("Unknown printer type: %s" % kind)


def validate_printer(printer):
    name = (printer.get("name") or "").strip()
    address = host_of(printer.get("address"))
    if not name:
        raise FarmError("Give the printer a name")
    if printer.get("kind") not in KINDS:
        raise FarmError("Choose a printer type")
    if not re.match(r"^[A-Za-z0-9._\-]+(:\d{1,5})?$", address):
        raise FarmError("Enter the printer's IP address or host name")
    if not (printer.get("model") or "").strip():
        raise FarmError("Enter the printer model, exactly as the printer preset names it")
    return {
        "id": printer.get("id") or uuid.uuid4().hex[:12],
        "name": name[:60],
        "kind": printer["kind"],
        "address": address,
        "model": printer["model"].strip()[:80],
        "access_code": (printer.get("access_code") or "").strip(),
        "api_key": (printer.get("api_key") or "").strip(),
        "mqtt_port": int(printer.get("mqtt_port") or 0),
        "nozzle": first_number(printer.get("nozzle")),   # mm, from the printer preset; None when unknown
    }


def toolhead_map_table(mapping, count=4):
    """Snapmaker map_table rows [slicer slot index, physical toolhead index], both from 0.

    mapping is {slicer slot: toolhead}, both counted from 1 as the panel shows them; slots not
    named print from their own toolhead. Verified on a U1: the printer reports the table back in
    print_task_config.extruder_map_table and heats the mapped toolhead."""
    table = [[index, index] for index in range(count)]
    for slot, tool in (mapping or {}).items():
        try:
            slot, tool = int(slot), int(tool)
        except (TypeError, ValueError):
            continue
        if 1 <= slot <= count and 1 <= tool <= count:
            table[slot - 1] = [slot - 1, tool - 1]
    return table


COLOR_GAP = 90     # summed RGB difference above which two colors count as different (black vs dark gray passes)


def color_gap(a, b):
    """How far apart two #RRGGBB colors are, 0 when either is missing or unreadable."""
    values = []
    for color in (a, b):
        if not isinstance(color, str) or not re.match(r"^#[0-9a-fA-F]{6}", color):
            return 0
        values.append([int(color[i:i + 2], 16) for i in (1, 3, 5)])
    return sum(abs(x - y) for x, y in zip(*values))


def toolhead_color_problem(tools, tool, color):
    """Why this toolhead would print the wrong color, judged from what the printer reports, or ""."""
    match = [entry for entry in tools or [] if entry.get("slot") == tool]
    if match and match[0].get("loaded") and color_gap(match[0].get("color"), color) > COLOR_GAP:
        return "toolhead %s holds a different color than the plate was sliced with" % tool
    return ""


def toolhead_problem(tools, tool, material):
    """Why a plate cannot print from this toolhead, judged from what the printer reports, or "".

    tools: the printer's toolhead report [{slot, color, type, loaded}]; empty when the printer
    reports none, in which case nothing can be checked."""
    if not tools:
        return ""
    match = [entry for entry in tools if entry.get("slot") == tool]
    if not match:
        return "the printer has no toolhead %s" % tool
    if not match[0].get("loaded"):
        return "toolhead %s reports no filament" % tool
    loaded = str(match[0].get("type") or "").strip().upper()
    wanted = str(material or "").strip().upper()
    if loaded and wanted and loaded != wanted:
        return "toolhead %s holds %s and the plate was sliced for %s" % (tool, loaded, wanted)
    return ""


def first_number(value):
    """0.4 from 0.4, "0.4", "0.4,0.4,0.4,0.4", ["0.4", "0.4"] or "0.4;0.4". None when there is none."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    match = re.match(r"\s*(\d+(?:\.\d+)?)", str(value or ""))
    return float(match.group(1)) if match and float(match.group(1)) > 0 else None


def nozzle_mismatch(plate_nozzle, printer_nozzle):
    """Text describing a nozzle-size mismatch, or "" when they agree or either side is unknown."""
    plate, printer = first_number(plate_nozzle), first_number(printer_nozzle)
    if plate is None or printer is None or abs(plate - printer) < 0.01:
        return ""
    return "this plate was sliced for a %g mm nozzle and the printer is set up with %g mm" % (plate, printer)


AUTO_OPEN_MARK = "farm_send_opening"


def auto_open_begin(folder):
    """Leave a mark before the panel is opened by itself. False when the mark of an earlier attempt
    is still there: that attempt never finished, so OrcaSlicer most likely went down while opening
    the panel at start, and it must not be tried again."""
    path = os.path.join(folder, AUTO_OPEN_MARK)
    if os.path.exists(path):
        return False
    write_private(path, "1")
    return True


def auto_open_done(folder):
    try:
        os.remove(os.path.join(folder, AUTO_OPEN_MARK))
    except OSError:
        pass


class PrinterStore:
    """Printers saved as JSON in the plugin's storage folder. The file holds LAN access codes,
    so it is written with owner-only permissions."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.printers = []
        self.combos = {}
        self.cleanup = dict(DEFAULT_CLEANUP)
        self.auto_open = True
        self._stamp = None
        self.load()

    def sync(self):
        """Pick up what another OrcaSlicer window saved. Called before every change, so a change made
        here is applied on top of the other window's and neither is lost. True when it reloaded."""
        if file_stamp(self.path) == self._stamp:
            return False
        self.load()
        return True

    def set_cleanup(self, rule):
        self.sync()
        self.cleanup = clean_rule(rule)
        self.save()

    def set_auto_open(self, value):
        self.sync()
        self.auto_open = bool(value)
        self.save()

    def load(self):
        self.combos = {}
        self.cleanup = dict(DEFAULT_CLEANUP)
        self.auto_open = True
        self._stamp = file_stamp(self.path)
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.printers = [p for p in data.get("printers", []) if isinstance(p, dict) and p.get("id")]
            combos = data.get("combos", {})
            if isinstance(combos, dict):
                self.combos = {key: value for key, value in combos.items() if isinstance(value, dict) and value.get("printer")}
            self.cleanup = clean_rule(data.get("cleanup"))
            self.auto_open = data.get("auto_open") is not False
        except (OSError, ValueError, AttributeError):
            self.printers = []

    def save(self):
        with self._lock:
            temp = self.path + ".tmp"
            # Created owner-only in one step: the file holds access codes, so it must never exist
            # with wider permissions, and O_NOFOLLOW refuses a symlink planted at the temp name.
            write_private(temp, json.dumps({"version": 1, "printers": self.printers, "combos": self.combos,
                                            "cleanup": self.cleanup, "auto_open": self.auto_open}, indent=2))
            os.replace(temp, self.path)
            self._stamp = file_stamp(self.path)

    def get(self, printer_id):
        for printer in self.printers:
            if printer["id"] == printer_id:
                return printer
        return None

    def upsert(self, printer):
        printer = validate_printer(printer)
        self.sync()
        for index, existing in enumerate(self.printers):
            if existing["id"] == printer["id"]:
                self.printers[index] = printer
                break
        else:
            self.printers.append(printer)
        self.save()
        return printer

    def remove(self, printer_id):
        self.sync()
        self.printers = [p for p in self.printers if p["id"] != printer_id]
        self.save()

    def set_combo(self, model, printer, process, filaments):
        """Remember which presets to slice with for one printer type."""
        key = model_key(model)
        if not key or not printer:
            raise FarmError("Select a printer preset in OrcaSlicer first")
        self.sync()
        self.combos[key] = {"label": model.strip(), "printer": printer, "process": process or "",
                            "filaments": [name for name in (filaments or []) if name]}
        self.save()
        return key

    def remove_combo(self, key):
        self.sync()
        if self.combos.pop(key, None) is not None:
            self.save()

    def hosts(self):
        return sorted({host_of(p.get("address", "")).split(":")[0] for p in self.printers if p.get("address")})


# --------------------------------------------------------------------------------------
# Cleaning old G-code off the printers
# --------------------------------------------------------------------------------------

DEFAULT_CLEANUP = {"mode": "age", "days": 30, "keep": 50, "auto": False}


def clean_rule(rule):
    """A cleanup rule with every field present and within sane limits."""
    rule = rule if isinstance(rule, dict) else {}
    out = dict(DEFAULT_CLEANUP)
    if rule.get("mode") in ("age", "count"):
        out["mode"] = rule["mode"]
    for key, low, high in (("days", 1, 3650), ("keep", 1, 5000)):
        try:
            out[key] = max(low, min(high, int(rule.get(key, out[key]))))
        except (TypeError, ValueError):
            pass
    out["auto"] = bool(rule.get("auto", False))
    return out


def rule_text(rule):
    rule = clean_rule(rule)
    if rule["mode"] == "count":
        return "keep the newest %d files" % rule["keep"]
    return "keep files newer than %d day%s" % (rule["days"], "" if rule["days"] == 1 else "s")


def files_to_clean(files, rule, protect=(), now=None):
    """The stored files a rule removes, oldest first.

    Never chosen: a protected name (the file being printed), and in age mode a file with no
    date, because "unknown age" is not "old"."""
    rule = clean_rule(rule)
    now = time.time() if now is None else now
    protected = {os.path.basename(str(name)) for name in protect if name}
    newest_first = sorted(files, key=lambda item: item.get("modified") or 0, reverse=True)
    if rule["mode"] == "count":
        chosen = newest_first[rule["keep"]:]
    else:
        limit = now - rule["days"] * 86400
        chosen = [item for item in newest_first if 0 < (item.get("modified") or 0) < limit]
    chosen = [item for item in chosen if os.path.basename(item["name"]) not in protected]
    return sorted(chosen, key=lambda item: item.get("modified") or 0)


def storage_summary(files):
    dated = [item["modified"] for item in files if item.get("modified")]
    return {"count": len(files), "bytes": sum(item.get("size") or 0 for item in files), "oldest": min(dated) if dated else None}


def file_stamp(path):
    """Changes whenever the file is rewritten; None when it does not exist."""
    try:
        info = os.stat(path)
        return (info.st_mtime_ns, info.st_size)
    except OSError:
        return None


def plan_order(remaining, printers, entries):
    """What to start on which free printer to work off `remaining` parts, spread evenly.

    printers: free printers in the order to fill them. entries: the kept plates of the item.
    A printer that can skip parts (not a Centauri Carbon 2, plate labelled part by part) prints
    exactly its share, so ten parts over five such printers is two each. A printer that can only
    print whole plates gets the kept plate nearest its share, and is left out when that plate would
    be more than double the share. With no part-skipping printer at all, whole plates are handed
    out one printer after another until the order is covered (a few extras possible).
    Each pick: printer_id, entry_id, parts (to print there), plate (parts on that kept plate)."""
    def fitting(printer):
        return [e for e in entries if model_matches(e.get("printer_model"), printer.get("model"))
                and not nozzle_mismatch(e.get("nozzle_diameter"), printer.get("nozzle")) and (e.get("parts") or 0) > 0]

    def countable(printer):
        if printer.get("kind") == KIND_CC2:
            return []
        return [e for e in fitting(printer) if e.get("objects") == e.get("parts") and e["parts"] > 1]

    usable = [p for p in printers if fitting(p)]
    flexible = [p for p in usable if countable(p)]
    plan = []
    if remaining <= 0 or not usable:
        return plan
    if not flexible:
        for printer in usable:
            if remaining <= 0:
                break
            fits = fitting(printer)
            under = [e for e in fits if e["parts"] <= remaining]
            pick = max(under, key=lambda e: e["parts"]) if under else min(fits, key=lambda e: e["parts"])
            plan.append({"printer_id": printer["id"], "entry_id": pick["id"], "parts": pick["parts"], "plate": pick["parts"]})
            remaining -= pick["parts"]
        return plan
    share = -(-remaining // len(usable))
    for printer in usable:
        if printer in flexible or remaining <= 0:
            continue
        fits = fitting(printer)
        over = [e for e in fits if share <= e["parts"] <= 2 * share]
        under = [e for e in fits if e["parts"] < share]
        pick = min(over, key=lambda e: e["parts"]) if over else (max(under, key=lambda e: e["parts"]) if under else None)
        if pick is not None:
            plan.append({"printer_id": printer["id"], "entry_id": pick["id"], "parts": pick["parts"], "plate": pick["parts"]})
            remaining -= pick["parts"]
    while remaining > 0 and flexible:
        base, extra = divmod(remaining, len(flexible))
        for index, printer in enumerate(list(flexible)):
            want = base + (1 if index < extra else 0)
            plates = countable(printer)
            pick = min([e for e in plates if e["parts"] >= want] or [max(plates, key=lambda e: e["parts"])], key=lambda e: e["parts"])
            count = min(want, pick["parts"])
            if count > 0:
                plan.append({"printer_id": printer["id"], "entry_id": pick["id"], "parts": count, "plate": pick["parts"]})
                remaining -= count
        break       # one plate per printer: whatever is still left waits for the next round
    order = {p["id"]: n for n, p in enumerate(printers)}
    return sorted(plan, key=lambda pick: order[pick["printer_id"]])


class OrderBook:
    """Orders being worked off, kept in a file so an unfinished order survives a restart.

    An order: item, quantity wanted, optional toolhead and note, and the plates started for it.
    A plate is "printing" from its start until the user clears that bed and says how many parts
    were good; only then does it count toward the order."""

    def __init__(self, path):
        self.path, self._lock, self._stamp = path, threading.Lock(), None
        self.load()

    def sync(self):
        """As PrinterStore.sync: reload when another window rewrote the file."""
        if file_stamp(self.path) == self._stamp:
            return False
        self.load()
        return True

    def load(self):
        self.orders, self.toolhead_names = [], ["", "", "", ""]
        self._stamp = file_stamp(self.path)
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.orders = [o for o in data.get("orders", []) if isinstance(o, dict) and o.get("id")]
            names = data.get("toolhead_names")
            if isinstance(names, list):
                self.toolhead_names = ([str(n)[:20] for n in names] + [""] * 4)[:4]
        except (OSError, ValueError, AttributeError):
            pass

    def save(self):
        with self._lock:
            write_private(self.path + ".tmp", json.dumps({"version": 1, "orders": self.orders,
                                                         "toolhead_names": self.toolhead_names}, indent=1))
            os.replace(self.path + ".tmp", self.path)
            self._stamp = file_stamp(self.path)

    def get(self, order_id):
        return next((o for o in self.orders if o["id"] == order_id), None)

    def add(self, item, quantity, toolhead=None, note=""):
        self.sync()
        order = {"id": uuid.uuid4().hex[:10], "item": item, "quantity": quantity, "toolhead": toolhead,
                 "note": note, "created": time.time(), "plates": []}
        self.orders.append(order)
        self.save()
        return order

    def delete(self, order_id):
        self.sync()
        self.orders = [o for o in self.orders if o["id"] != order_id]
        self.save()

    def open_plate(self, printer_id):
        """(order, plate) still on this printer and not yet counted, or (None, None)."""
        for order in self.orders:
            for plate in order["plates"]:
                if plate.get("printer_id") == printer_id and plate.get("state") == "printing":
                    return order, plate
        return None, None

    def add_plate(self, order_id, printer_id, printer_name, parts, filename):
        self.sync()
        _, earlier = self.open_plate(printer_id)
        if earlier is not None:            # the bed was reused without a count: it yields nothing
            earlier.update(state="counted", good=0)
        order = self.get(order_id)
        if order is not None:
            order["plates"].append({"printer_id": printer_id, "printer_name": printer_name, "parts": parts,
                                    "filename": filename, "sent_at": time.time(), "state": "printing", "good": None})
        self.save()

    def count(self, printer_id, good):
        self.sync()
        order, plate = self.open_plate(printer_id)
        if plate is None:
            return None
        plate.update(state="counted", good=max(0, min(int(good), 99999)))
        self.save()
        return order

    @staticmethod
    def totals(order):
        good = sum(p.get("good") or 0 for p in order["plates"] if p.get("state") == "counted")
        printing = sum(p.get("parts") or 0 for p in order["plates"] if p.get("state") == "printing")
        return good, printing, max(0, order["quantity"] - good - printing)


class BedLog:
    """One small value per printer, kept in a file so it survives restarting OrcaSlicer and is the
    same in every OrcaSlicer window. Used for two things, each in its own file:

    beds: True = clear the bed first, False = the user said it is clear, absent = never seen.
    cleanups: the time old files were last removed automatically."""

    def __init__(self, path):
        self.path, self._stamp, self._beds = path, None, {}

    def _load(self):
        try:
            info = os.stat(self.path)
            stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return
        self._stamp, self._beds = stamp, {}
        if stamp is None:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._beds = {str(key): value for key, value in data.items() if isinstance(value, (bool, int, float))}
        except (OSError, ValueError):
            pass

    def get(self, printer_id):
        self._load()
        return self._beds.get(printer_id)

    def set(self, printer_id, needs_clearing):
        self._load()
        if printer_id in self._beds and self._beds[printer_id] == needs_clearing:
            return
        self._beds[printer_id] = needs_clearing
        self._write()

    def forget(self, printer_id):
        self._load()
        if self._beds.pop(printer_id, None) is not None:
            self._write()

    def _write(self):
        temp = "%s.%d.tmp" % (self.path, os.getpid())
        try:
            write_private(temp, json.dumps(self._beds))
            os.replace(temp, self.path)
        except OSError:
            pass   # still right in this window; only the sharing and the restart memory are lost
        self._stamp = None


def bed_needs_clearing(known, status):
    """New value for a printer's bed flag after a status read, or None to leave it as it is.

    known: True, False, or None when this printer was never seen. A print that is running leaves a
    part on the bed. A printer first seen idle that reports a completed or cancelled last print is
    assumed to still hold it; after the user says the bed is clear, only a new print sets it again."""
    state = status.get("state")
    if state in ("printing", "paused"):
        # A print that has not extruded anything yet (heating, leveling) has put nothing on the
        # bed, so cancelling it there leaves the bed as it was. Unknown counts as something.
        return None if status.get("extruded_mm") == 0 else True
    if state == "idle" and known is None:
        return bool(status.get("last_result"))
    return None


STORE_FILE = "farm_send_printers.json"


def data_dir_of(path):
    """OrcaSlicer's data folder, found from any path inside its orca_plugins folder.

    A plugin installed from a file lives in <data>/orca_plugins/<folder>/ with its storage in
    <data>/orca_plugins/plugin_data/<key>/. One installed from the Plugin Hub sits two levels
    deeper, under _subscribed/<user id>/. Walking up to "orca_plugins" works for both."""
    path = os.path.abspath(path or "")
    while True:
        parent = os.path.dirname(path)
        if os.path.basename(path) == "orca_plugins":
            return parent
        if not parent or parent == path:
            return ""
        path = parent


def saved_store_files(data_dir, plugin_file=""):
    """The printer file(s) belonging to THIS copy of the plugin.

    A copy installed from the Plugin Hub keeps its data in plugin_data/_subscribed/<user id>/<key>/
    and its code in _subscribed/<user id>/<key>/, so its own data folder is known exactly. A copy
    installed from a file uses plugin_data/<key>/. The two never read each other's printers:
    otherwise each would ask for network access to the other's addresses."""
    if not data_dir:
        return []
    base = os.path.join(data_dir, "orca_plugins", "plugin_data")
    parts = os.path.abspath(plugin_file or "").split(os.sep)
    if "_subscribed" in parts:
        at = parts.index("_subscribed")
        if len(parts) < at + 3:
            return []
        path = os.path.join(base, "_subscribed", parts[at + 1], parts[at + 2], STORE_FILE)
        return [path] if os.path.isfile(path) else []
    found = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return found
    for name in names:
        path = os.path.join(base, name, STORE_FILE)
        if name != "_subscribed" and os.path.isfile(path):
            found.append(path)
    return found


def preset_to_printer(preset, fallback_name=""):
    """A printer entry from one OrcaSlicer printer preset (its settings as a dict), or None when
    the preset has no printer address or is not a printer type this plugin drives."""
    address = host_of(_first(preset.get("print_host")))
    if not address:
        return None
    inherits = _first(preset.get("inherits")) or ""
    label = _first(preset.get("name")) or fallback_name
    model = _first(preset.get("printer_model")) or re.sub(r"\s*\(?\d+(\.\d+)?\s*(mm)?\s*nozzle\)?.*$", "", inherits).strip()
    lowered = (model + " " + inherits + " " + label).lower()
    host_type = (_first(preset.get("host_type")) or "").lower()
    if "centauri carbon 2" in lowered:
        kind = KIND_CC2
    elif "snapmaker" in lowered or host_type in ("moonraker", "klipper"):
        kind = KIND_MOONRAKER
    else:
        return None
    key = _first(preset.get("printhost_apikey")) or ""
    return {"name": short_printer_name(label, inherits, model), "kind": kind, "address": address, "model": model,
            "access_code": key if kind == KIND_CC2 else "", "api_key": key if kind == KIND_MOONRAKER else "",
            "nozzle": first_number(preset.get("nozzle_diameter"))}


def presets_to_printers(preset_dir):
    """Printer entries from the printer preset files in one folder. Used only when OrcaSlicer's own
    preset list is not available; inside OrcaSlicer the live list is used instead, because a
    signed-in user has a second preset folder and only one of the two is in use."""
    found = []
    try:
        names = sorted(os.listdir(preset_dir))
    except OSError:
        return found
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(preset_dir, name), "r", encoding="utf-8") as handle:
                preset = json.load(handle)
        except (OSError, ValueError):
            continue
        printer = preset_to_printer(preset, name[:-5]) if isinstance(preset, dict) else None
        if printer:
            found.append(printer)
    return found


def short_printer_name(label, inherits, model):
    """'Snapmaker U1 (0.4 nozzle) - #1' -> 'U1 #1', 'Elegoo Centauri Carbon 2 0.4 nozzle - CC1' -> 'CC1'.

    A user preset is normally named '<system preset> - <suffix>'; the suffix is the part that tells
    two printers apart, and it is what gets cut off in a narrow panel if the full name is kept."""
    suffix = ""
    if inherits and label.startswith(inherits):
        suffix = label[len(inherits):].strip(" -_:")
    if not suffix:
        return label
    if re.match(r"^[#\d]", suffix) and model:
        return "%s %s" % (model.split()[-1], suffix)
    return suffix


def _first(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return value


def can_start(status):
    return status.get("state") == "idle"
