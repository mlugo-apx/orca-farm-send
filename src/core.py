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
    colour, and nothing else in the slice summary shows it."""
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
        filename = safe_filename(filename)
        size = os.path.getsize(path)
        if size <= 0:
            raise FarmError("The G-code file is empty")
        md5 = file_md5(path)
        url = "http://%s/upload" % self.address
        sent = 0
        with open(path, "rb") as handle:
            while sent < size:
                if cancelled is not None and cancelled():
                    raise FarmError("Upload cancelled")
                chunk = handle.read(CHUNK)
                if not chunk:
                    raise FarmError("The G-code file changed while uploading")
                headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/octet-stream",
                    "Content-Range": "bytes %d-%d/%d" % (sent, sent + len(chunk) - 1, size),
                    "X-File-Name": filename,   # raw, exactly as OrcaSlicer's own ElegooLink client sends it
                    "X-File-MD5": md5,
                    "X-Token": self.token,
                }
                reply = http_json(url, method="PUT", data=chunk, headers=headers, timeout=180)
                _cc2_check(reply if "error_code" in reply else {"error_code": -1}, "Upload failed")
                sent += len(chunk)
                if progress is not None:
                    progress(sent, size)
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
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(uuid.uuid4().hex)
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
        params = {"type": "gcode", "path": filename, "options": {
            "map_table": [[index, index] for index in range(4)],
            "bed_level": 1 if options.get("bed_leveling", True) else 0,
            "flow_calibrate": 1 if options.get("flow_calibrate", False) else 0,
            "time_lapse_camera": 1 if options.get("timelapse", False) else 0,
        }}
        try:
            result = websocket_rpc(self.address, "server.files.start_local_print", params, timeout=30)
        except RpcError as error:
            text = str(error).lower()
            if error.code == -32601 or "not found" in text or "unknown method" in text:
                self._post("/printer/print/start?filename=%s" % urllib.parse.quote(filename), timeout=30)
                return "standard"
            raise FarmError("Printer refused to start the print: %s" % error)
        if isinstance(result, dict) and str(result.get("state", "")).lower() not in ("success", "ok", ""):
            raise FarmError("Printer refused to start the print: %s" % (result.get("message") or result.get("state")))
        return "snapmaker"


_MOONRAKER_ESTIMATES = {}


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
    }


class PrinterStore:
    """Printers saved as JSON in the plugin's storage folder. The file holds LAN access codes,
    so it is written with owner-only permissions."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.printers = []
        self.combos = {}
        self.cleanup = dict(DEFAULT_CLEANUP)
        self.load()

    def set_cleanup(self, rule):
        self.cleanup = clean_rule(rule)
        self.save()

    def load(self):
        self.combos = {}
        self.cleanup = dict(DEFAULT_CLEANUP)
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.printers = [p for p in data.get("printers", []) if isinstance(p, dict) and p.get("id")]
            combos = data.get("combos", {})
            if isinstance(combos, dict):
                self.combos = {key: value for key, value in combos.items() if isinstance(value, dict) and value.get("printer")}
            self.cleanup = clean_rule(data.get("cleanup"))
        except (OSError, ValueError, AttributeError):
            self.printers = []

    def save(self):
        with self._lock:
            temp = self.path + ".tmp"
            # Created owner-only in one step: the file holds access codes, so it must never exist
            # with wider permissions, and O_NOFOLLOW refuses a symlink planted at the temp name.
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(temp, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), 0o600)   # also tightens a leftover temp file
                json.dump({"version": 1, "printers": self.printers, "combos": self.combos, "cleanup": self.cleanup}, handle, indent=2)
            os.replace(temp, self.path)

    def get(self, printer_id):
        for printer in self.printers:
            if printer["id"] == printer_id:
                return printer
        return None

    def upsert(self, printer):
        printer = validate_printer(printer)
        for index, existing in enumerate(self.printers):
            if existing["id"] == printer["id"]:
                self.printers[index] = printer
                break
        else:
            self.printers.append(printer)
        self.save()
        return printer

    def remove(self, printer_id):
        self.printers = [p for p in self.printers if p["id"] != printer_id]
        self.save()

    def set_combo(self, model, printer, process, filaments):
        """Remember which presets to slice with for one printer type."""
        key = model_key(model)
        if not key or not printer:
            raise FarmError("Select a printer preset in OrcaSlicer first")
        self.combos[key] = {"label": model.strip(), "printer": printer, "process": process or "",
                            "filaments": [name for name in (filaments or []) if name]}
        self.save()
        return key

    def remove_combo(self, key):
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
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._beds, handle)
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
        return True
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
            "access_code": key if kind == KIND_CC2 else "", "api_key": key if kind == KIND_MOONRAKER else ""}


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
