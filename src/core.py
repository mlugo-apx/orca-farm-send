"""Farm Send core: printer protocols and job logic. Pure standard library, no OrcaSlicer imports,
so it can be tested outside the slicer against real printers."""
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
    return meta


def normalize_model(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


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

    def delete_file(self, filename):
        with self._session() as session:
            reply = session.request(CC2_DELETE_FILE, {"storage_media": "local", "file_path": filename})
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


def _cc2_client_id(serial):
    """One client id per printer for the life of the process.

    The printer keeps a registration slot per client id for about a minute after the client goes
    away and has only a few slots. A fresh id on every status poll fills them all within a minute
    and locks out ElegooSlicer and the printer's own web page; a stable id re-uses one slot."""
    with _CC2_CLIENT_LOCK:
        if serial not in _CC2_CLIENT_IDS:
            # Same shape as the vendor web client: "0" + tag + hex, 10 characters.
            _CC2_CLIENT_IDS[serial] = ("0fs" + uuid.uuid4().hex)[:10]
            _CC2_SERIAL_LOCKS[serial] = threading.Lock()
        return _CC2_CLIENT_IDS[serial]


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
        try:
            self.mqtt.connect()
        except BaseException:
            self._serial_lock.release()
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
            self._serial_lock.release()
            raise

    def __exit__(self, *exc):
        try:
            self.mqtt.close()
        finally:
            self._serial_lock.release()
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
    if out["errors"] and state == "idle":
        out["state_text"] = "Idle (warning %s)" % ", ".join(str(e) for e in out["errors"])
    return out


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
                          "&extruder1&extruder2&extruder3")
        return normalize_moonraker_status(reply.get("result", {}).get("status", {}), self._klippy_state)

    def _klippy_state(self):
        return (self._get("/printer/info").get("result") or {})

    def list_files(self, limit=None):
        reply = self._get("/server/files/list?root=gcodes", timeout=15)
        return [entry.get("path", "") for entry in reply.get("result") or []]

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
        self._post("/printer/print/start?filename=%s" % urllib.parse.quote(filename), timeout=30)


def normalize_moonraker_status(status, klippy_info=None):
    stats = status.get("print_stats") or {}
    sdcard = status.get("virtual_sdcard") or {}
    raw_state = str(stats.get("state") or "").lower()
    mapping = {"standby": ("idle", "Idle"), "complete": ("idle", "Idle (last print complete)"),
               "cancelled": ("idle", "Idle (last print cancelled)"), "printing": ("printing", "Printing"),
               "paused": ("paused", "Paused"), "error": ("error", "Error")}
    state, text = mapping.get(raw_state, ("busy", raw_state.capitalize() or "Unknown"))
    if not raw_state and klippy_info is not None:
        info = klippy_info()
        if info.get("state") != "ready":
            state, text = "error", "Klipper: %s" % (info.get("state_message") or info.get("state") or "not ready").splitlines()[0]
    if state == "error" and stats.get("message"):
        text = "Error: %s" % str(stats["message"]).splitlines()[0]
    out = {"state": state, "state_text": text, "filename": stats.get("filename") or "", "progress": None,
           "remaining_s": None, "nozzle": None, "bed": _temp(status.get("heater_bed")), "errors": []}
    # Report the hottest tool: on a tool changer the parked tools sit near ambient.
    tools = [_temp(status.get(name)) for name in ("extruder", "extruder1", "extruder2", "extruder3")]
    tools = [tool for tool in tools if tool]
    if tools:
        out["nozzle"] = max(tools, key=lambda tool: (tool["target"], tool["current"]))
    if state in ("printing", "paused"):
        progress = sdcard.get("progress")
        if isinstance(progress, (int, float)):
            out["progress"] = max(0.0, min(100.0, float(progress) * 100.0))
            elapsed = stats.get("print_duration")
            if isinstance(elapsed, (int, float)) and progress > 0.01:
                out["remaining_s"] = int(elapsed / progress - elapsed)
    return out


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
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.printers = [p for p in data.get("printers", []) if isinstance(p, dict) and p.get("id")]
        except (OSError, ValueError):
            self.printers = []

    def save(self):
        with self._lock:
            temp = self.path + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "printers": self.printers}, handle, indent=2)
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
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

    def hosts(self):
        return sorted({host_of(p.get("address", "")).split(":")[0] for p in self.printers if p.get("address")})


def presets_to_printers(preset_dir):
    """Build printer entries from OrcaSlicer user machine presets that have a print host set."""
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
        address = host_of(_first(preset.get("print_host")))
        if not address:
            continue
        inherits = _first(preset.get("inherits")) or ""
        label = _first(preset.get("name")) or name[:-5]
        model = _first(preset.get("printer_model")) or re.sub(r"\s*\(?\d+(\.\d+)?\s*(mm)?\s*nozzle\)?.*$", "", inherits).strip()
        lowered = (model + " " + inherits + " " + label).lower()
        host_type = (_first(preset.get("host_type")) or "").lower()
        if "centauri carbon 2" in lowered:
            kind = KIND_CC2
        elif "snapmaker" in lowered or host_type in ("moonraker", "klipper"):
            kind = KIND_MOONRAKER
        else:
            continue
        found.append({"name": short_printer_name(label, inherits, model), "kind": kind, "address": address, "model": model,
                      "access_code": _first(preset.get("printhost_apikey")) or "" if kind == KIND_CC2 else "",
                      "api_key": _first(preset.get("printhost_apikey")) or "" if kind == KIND_MOONRAKER else ""})
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
