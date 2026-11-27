"""In-process fake printers: a Moonraker HTTP server and an Elegoo CC2 (HTTP + MQTT broker)."""
import base64
import hashlib
import json
import os
import re
import socket
import struct
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeMoonraker:
    def __init__(self, api_key="", snapmaker=False):
        self.files, self.started, self.state, self.api_key = {}, [], "standby", api_key
        self.file_times = {}   # file name -> modified time, for cleanup tests
        self.camera = None     # camera folder: file name -> (modified, size); None = the printer has no such folder
        self.estimated_time, self.metadata_calls = None, 0   # slicer estimate served as file metadata
        self.snapmaker, self.start_options = snapmaker, []
        self.objects, self.excluded, self.ignore_skips, self.cancels = [], [], False, 0   # labelled parts and skips
        self.tools = {"filament_color_rgba": ["000000FF", "FFFFFFFF", "6F4C2FFF", "FEE5A5FF"], "filament_type": ["PETG"] * 4,
                      "filament_exist": [True, True, True, False]}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth(self):
                if outer.api_key and self.headers.get("X-Api-Key") != outer.api_key:
                    self._json(401, {"error": {"message": "Unauthorized"}})
                    return False
                return True

            def _websocket(self):
                """One JSON-RPC call over a websocket, the only transport Snapmaker's start method has."""
                accept = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] +
                                                        "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                  "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
                self.wfile.flush()
                head = self.rfile.read(2)
                length = head[1] & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self.rfile.read(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self.rfile.read(8))[0]
                mask = self.rfile.read(4)
                call = json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(length))).decode())

                def push(body):
                    raw = json.dumps(body).encode()
                    size = bytes([len(raw)]) if len(raw) < 126 else bytes([126]) + struct.pack("!H", len(raw))
                    self.wfile.write(b"\x81" + size + raw)
                    self.wfile.flush()

                push({"jsonrpc": "2.0", "method": "notify_proc_stat_update", "params": [{}]})   # unrelated chatter first
                if call["method"] == "camera.delete_timelapse_instance":
                    stamp = "_" + str(call["params"].get("date_index"))
                    gone = [name for name in (outer.camera or {})
                            if os.path.splitext(name)[0].replace("_cover", "").endswith(stamp)]
                    for name in gone:
                        outer.camera.pop(name)
                    push({"jsonrpc": "2.0", "id": call["id"], "result": {"deleted_count": 1 if gone else 0, "state": "success"}})
                    return
                if call["method"] != "server.files.start_local_print":
                    push({"jsonrpc": "2.0", "id": call["id"], "error": {"code": -32601, "message": "Method not found"}})
                    return
                params = call["params"]
                if params["path"] not in outer.files:
                    push({"jsonrpc": "2.0", "id": call["id"], "result": {"state": "error", "message": "File not found: " + params["path"]}})
                    return
                outer.start_options.append(params)
                outer.started.append(params["path"])
                outer.state = "printing"
                push({"jsonrpc": "2.0", "id": call["id"], "result": {"state": "success", "message": "Print started"}})

            def do_GET(self):
                if not self._auth():
                    return
                if self.path == "/websocket" and outer.snapmaker:
                    self.close_connection = True
                    self._websocket()
                    return
                if self.path.startswith("/printer/info"):
                    self._json(200, {"result": {"state": "ready", "hostname": "fake", "software_version": "v0"}})
                elif self.path.startswith("/printer/objects/query?configfile"):
                    self._json(200, {"result": {"status": {"configfile": {"settings": {"extruder": {}, "extruder3": {}}}}}})
                elif self.path.startswith("/machine/system_info"):
                    self._json(404, {"error": {"message": "nope"}})
                elif self.path.startswith("/printer/objects/query"):
                    printing = outer.state == "printing"
                    self._json(200, {"result": {"status": {
                        "print_stats": {"state": outer.state, "filename": "busy.gcode" if printing else "", "print_duration": 600},
                        "virtual_sdcard": {"progress": 0.25 if printing else 0},
                        "heater_bed": {"temperature": 60.0, "target": 60.0},
                        "extruder": {"temperature": 25.0, "target": 0.0},
                        "extruder1": {"temperature": 220.0, "target": 220.0},
                        "exclude_object": {"objects": [{"name": name} for name in (outer.objects if printing else [])],
                                           "excluded_objects": list(outer.excluded)},
                        **({"print_task_config": outer.tools} if outer.snapmaker else {})}}})
                elif self.path.startswith("/server/files/metadata") and outer.estimated_time is not None:
                    outer.metadata_calls += 1
                    self._json(200, {"result": {"estimated_time": outer.estimated_time}})
                elif self.path.startswith("/server/files/list?root=camera"):
                    if outer.camera is None:
                        self._json(400, {"error": {"message": "Invalid root"}})
                    else:
                        self._json(200, {"result": [{"path": name, "modified": when, "size": size}
                                                    for name, (when, size) in outer.camera.items()]})
                elif self.path.startswith("/server/files/list"):
                    self._json(200, {"result": [{"path": name, "modified": outer.file_times.get(name, 0), "size": len(data)}
                                                for name, data in outer.files.items()]})
                else:
                    self._json(404, {"error": {"message": "not found"}})

            def do_POST(self):
                if not self._auth():
                    return
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                if self.path.startswith("/server/files/upload"):
                    boundary = self.headers["Content-Type"].split("boundary=")[1].encode()
                    fields = {}
                    for part in body.split(b"--" + boundary):
                        if b"\r\n\r\n" not in part:
                            continue
                        head, _, data = part.partition(b"\r\n\r\n")
                        data = data[:-2] if data.endswith(b"\r\n") else data
                        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
                        filename = re.search(rb'filename="([^"]+)"', head)
                        fields[name] = (filename.group(1).decode() if filename else None, data)
                    filename, data = fields["file"]
                    if fields.get("print", (None, b"false"))[1] != b"false":
                        outer.started.append(filename)
                    outer.files[filename] = data
                    self._json(201, {"result": {"item": {"path": filename, "root": "gcodes"}}})
                elif self.path.startswith("/printer/gcode/script"):
                    script = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)["script"][0]
                    for line in script.splitlines():
                        match = re.match(r"EXCLUDE_OBJECT NAME=(\S+)", line)
                        if match and not outer.ignore_skips:
                            outer.excluded.append(match.group(1))
                    self._json(200, {"result": "ok"})
                elif self.path.startswith("/printer/print/cancel"):
                    outer.cancels += 1
                    outer.state = "standby"
                    self._json(200, {"result": "ok"})
                elif self.path.startswith("/printer/print/start"):
                    filename = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)["filename"][0]
                    if filename not in outer.files:
                        self._json(400, {"error": {"message": "file not found"}})
                    elif outer.state == "printing":
                        self._json(400, {"error": {"message": "printer busy"}})
                    else:
                        outer.started.append(filename)
                        outer.state = "printing"
                        self._json(200, {"result": "ok"})
                else:
                    self._json(404, {"error": {"message": "not found"}})

            def do_DELETE(self):
                name = urllib.parse.unquote(self.path.rsplit("/", 1)[1])
                if "/files/camera/" in self.path:
                    if outer.snapmaker:     # a U1 exposes its camera folder read-only
                        self._json(405, {"error": {"message": "Method Not Allowed"}})
                        return
                    (outer.camera or {}).pop(name, None)
                else:
                    outer.files.pop(name, None)
                self._json(200, {"result": {}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.handle_error = lambda *args: None   # a cancelled upload drops the socket mid-request
        self.address = "127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeCC2:
    """HTTP on a random port; MQTT must be on 1883 for the real client, so tests patch the port."""

    def __init__(self, token="abc123", serial="SNFAKE0001"):
        self.token, self.serial = token, serial
        self.files, self.started, self.status_code = {}, [], 1
        self.put_connections, self.busy_puts = set(), 0   # connections used for uploads; 429s still to give
        self.file_times, self.stuck, self.list_calls, self.delete_calls = {}, set(), 0, 0
        self.progress = None   # override the reported progress, e.g. 100 on an idle printer after a print
        self.partial = {}
        self.need_mesh_force = False
        self.register_error = "ok"
        self.registered, self.max_clients = set(), 3
        self.start_requests = []
        # rolls in the Canvas unit as (color, type); [] = no Canvas connected; None = the printer never answers
        self.rolls = [("#000000", "PETG"), ("#FFFFFF", "PETG"), ("#898989", "PETG"), ("#F88D36", "PETG")]
        self.history = []      # print history entries, as the printer lists them
        self.media = set()     # files outside the G-code folder: "video/<name>.mp4", "picture/<name>/0000.jpeg"
        self.media_stuck = set()   # media files the printer keeps although it answers "ok"
        self.layers = {}       # {G-code name: layer count} that file details report
        self.roll_calls = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"   # keep-alive, like the printer: an upload reuses one connection
            def log_message(self, *args):
                pass

            def _json(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                if query.get("X-Token", [""])[0] != outer.token:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.startswith("/download"):
                    name = query.get("file_name", [""])[0]
                    if name in outer.media:
                        self.send_response(200)
                        self.send_header("Content-Length", "4")
                        self.end_headers()
                        self.wfile.write(b"ftyp")
                    else:      # the real printer gives no answer at all; a quick 404 keeps the tests fast
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    return
                self._json(200, {"error_code": 0, "system_info": {"sn": outer.serial, "hostname": "FAKE", "machine_model": "Centauri Carbon 2",
                                                                    "software_version": {"ota_version": "1.0"}}})

            def do_PUT(self):
                outer.put_connections.add(self.client_address)
                if outer.busy_puts > 0:               # "too many requests", as the real printer answers
                    outer.busy_puts -= 1
                    self.rfile.read(int(self.headers["Content-Length"]))
                    self.send_response(429)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.headers.get("X-Token") != outer.token:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = self.rfile.read(int(self.headers["Content-Length"]))
                name = self.headers["X-File-Name"]
                start, end, total = map(int, re.match(r"bytes (\d+)-(\d+)/(\d+)", self.headers["Content-Range"]).groups())
                buffer = outer.partial.setdefault(name, bytearray())
                if start != len(buffer) or end - start + 1 != len(data):
                    self._json(200, {"error_code": 9000})
                    return
                buffer.extend(data)
                if len(buffer) == total:
                    if hashlib.md5(buffer).hexdigest() != self.headers["X-File-MD5"]:
                        self._json(200, {"error_code": 9004})
                        return
                    outer.files[name] = bytes(outer.partial.pop(name))
                self._json(200, {"error_code": 0})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = "127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.mqtt = socket.socket()
        self.mqtt.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.mqtt.bind(("127.0.0.1", 0))
        self.mqtt.listen(8)
        self.mqtt_port = self.mqtt.getsockname()[1]
        self._open = True
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._open = False
        self.server.shutdown()
        self.server.server_close()
        self.mqtt.close()

    def _accept(self):
        while self._open:
            try:
                conn, _ = self.mqtt.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _read(conn, count):
        data = b""
        while len(data) < count:
            chunk = conn.recv(count - len(data))
            if not chunk:
                raise ConnectionError()
            data += chunk
        return data

    def _packet(self, conn):
        first = self._read(conn, 1)[0]
        length, shift = 0, 0
        while True:
            byte = self._read(conn, 1)[0]
            length |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        return first, self._read(conn, length) if length else b""

    @staticmethod
    def _send_publish(conn, topic, body):
        raw = json.dumps(body).encode()
        topic_raw = topic.encode()
        payload = struct.pack("!H", len(topic_raw)) + topic_raw + raw
        length, encoded = len(payload), bytearray()
        while True:
            byte = length % 128
            length //= 128
            encoded.append(byte | (0x80 if length else 0))
            if not length:
                break
        conn.sendall(b"\x30" + bytes(encoded) + payload)

    def _serve(self, conn):
        try:
            first, data = self._packet(conn)
            # CONNECT: protocol name(6) level(1) flags(1) keepalive(2) then client id, user, password
            offset = 10
            fields = []
            while offset < len(data):
                size = struct.unpack("!H", data[offset:offset + 2])[0]
                fields.append(data[offset + 2:offset + 2 + size].decode())
                offset += 2 + size
            if len(fields) < 3 or fields[1] != "elegoo" or fields[2] != self.token:
                conn.sendall(b"\x20\x02\x00\x05")
                return
            conn.sendall(b"\x20\x02\x00\x00")
            while True:
                first, data = self._packet(conn)
                kind = first & 0xF0
                if kind == 0x80:
                    conn.sendall(b"\x90\x03" + data[:2] + b"\x01")
                elif kind == 0x30:
                    size = struct.unpack("!H", data[:2])[0]
                    topic = data[2:2 + size].decode()
                    offset = 2 + size
                    if (first >> 1) & 3:
                        conn.sendall(b"\x40\x02" + data[offset:offset + 2])
                        offset += 2
                    self._on_publish(conn, topic, json.loads(data[offset:].decode()))
                elif kind == 0xE0:
                    return
        except (ConnectionError, OSError):
            pass
        finally:
            conn.close()

    def _on_publish(self, conn, topic, body):
        base = "elegoo/%s" % self.serial
        if topic == base + "/api_register":
            error = self.register_error
            if error == "ok":
                if body["client_id"] in self.registered:
                    error = "already registered"
                elif len(self.registered) >= self.max_clients:
                    error = "too many clients"
                else:
                    self.registered.add(body["client_id"])
            self._send_publish(conn, "%s/%s/register_response" % (base, body["request_id"]),
                               {"client_id": body["client_id"], "error": error})
            return
        match = re.match(re.escape(base) + r"/([^/]+)/api_request$", topic)
        if not match:
            return
        method, params = body.get("method"), body.get("params") or {}
        if method == 1002:
            printing = self.status_code == 2
            result = {"error_code": 0, "machine_status": {"status": self.status_code, "progress": self.progress if self.progress is not None else (40 if printing else 0), "exception_status": []},
                      "print_status": {"filename": "busy.gcode" if printing else "", "remaining_time_sec": 1200, "state": "printing" if printing else ""},
                      "extruder": {"temperature": 250, "target": 250}, "heater_bed": {"temperature": 70, "target": 70}}
        elif method == 1036:
            result = {"error_code": 0, "history_task_list": list(self.history)}
        elif method == 1046:
            name = params.get("filename")
            result = {"error_code": 0, "filename": name, "layer": self.layers.get(name, 10)} if name in self.files else {"error_code": 1021}
        elif method == 1038:
            gone = set(params.get("list") or [])
            self.history = [task for task in self.history if task["task_id"] not in gone]
            result = {"error_code": 0}
        elif method == 2005:
            self.roll_calls += 1
            if self.rolls is None:
                return
            result = {"error_code": 0, "canvas_info": {"active_canvas_id": 0, "active_tray_id": 1, "canvas_list": [
                {"canvas_id": 0, "connected": 1 if self.rolls else 0, "tray_list": [
                    {"tray_id": index, "filament_color": color, "filament_type": kind, "status": 1}
                    for index, (color, kind) in enumerate(self.rolls)]}]}}
        elif method == 1020:
            self.start_requests.append(params)
            if params.get("filename") not in self.files:
                result = {"error_code": 1021}
            elif self.status_code != 1:
                result = {"error_code": 1009}
            elif self.need_mesh_force and not params.get("config", {}).get("bedlevel_force"):
                result = {"error_code": 1026}
            else:
                self.started.append(params["filename"])
                self.status_code = 2
                result = {"error_code": 0}
        elif method == 1044:
            names = list(self.files)   # paged like the real printer: at most 100 per request
            offset, limit = int(params.get("offset", 0)), min(100, int(params.get("limit", 100)))
            self.list_calls += 1
            result = {"error_code": 0, "total": len(names), "offset": offset,
                      "file_list": [{"filename": name, "type": "file", "size": len(self.files[name]),
                                     "create_time": self.file_times.get(name, 0)} for name in names[offset:offset + limit]]}
        elif method == 1047:
            paths = params.get("file_path")
            if not isinstance(paths, list):   # the real printer rejects a bare string
                result = {"error_code": 1003, "message": "invalid parameter"}
            else:
                self.delete_calls += 1
                for path in paths:
                    if path.startswith("../../"):   # the real printer follows this out of the G-code folder
                        if path[6:] not in self.media_stuck:
                            self.media.discard(path[6:])
                    elif path not in self.stuck:   # the real printer says "ok" even when it keeps a file
                        self.files.pop(path, None)
                result = {"error_code": 0}
        else:
            result = {"error_code": 1001}
        self._send_publish(conn, "%s/%s/api_response" % (base, match.group(1)), {"id": body.get("id"), "method": method, "result": result})


class FakeCC1:
    """The first Elegoo Centauri Carbon, as OrcaSlicer's ElegooLink.cpp and Elegoo's elegoo-link SDK describe
    it: SDCP over a WebSocket, and a chunked multipart upload. Nobody on the project owns one, so this fake
    is the only printer the CC1 code has met; keep it to what the sources say."""

    MAINBOARD = "7f3c0a9d11e2aa00"

    def __init__(self, max_clients=5):
        self.files, self.started, self.stops, self.bad_commands = {}, [], 0, []
        self.codes, self.job_status, self.filename = [0], 0, ""
        self.check_polls = 0           # status reads that still show "checking file" after an upload
        self.no_start_ack = False      # the start command is carried out but never answered
        self.start_ack = 0
        self.prefix = False            # send frames as "<length>{...}", as some firmware does
        self.omit_success = False      # upload answers carry only "code", not "success"
        self.echo_request_id = True
        self.max_clients, self.open, self.opened, self.clean_closes = max_clients, 0, 0, 0
        self.status_reads, self.upload_parts = 0, []
        self._uploads, self._lock = {}, threading.Lock()
        outer = self

        # ---- WebSocket on its own port ------------------------------------------------------
        self.ws = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ws.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ws.bind(("127.0.0.1", 0))
        self.ws.listen(16)
        self.ws_port = self.ws.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

        # ---- upload over HTTP -----------------------------------------------------------------
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if not self.path.startswith("/uploadFile/upload"):
                    self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
                    return
                body = self.rfile.read(int(self.headers["Content-Length"]))
                boundary = re.search(r"boundary=(.+)", self.headers["Content-Type"]).group(1).encode()
                fields, data = {}, b""
                for part in body.split(b"--" + boundary):
                    if b"\r\n\r\n" not in part:
                        continue
                    head, _, value = part.partition(b"\r\n\r\n")
                    value = value[:-2] if value.endswith(b"\r\n") else value
                    name = re.search(rb'name="([^"]+)"', head).group(1).decode()
                    if name == "File":
                        data = value
                        fields["filename"] = re.search(rb'filename="([^"]*)"', head).group(1).decode()
                    else:
                        fields[name] = value.decode()
                outer.upload_parts.append(dict(fields, size=len(data)))
                ok = all(k in fields for k in ("Check", "S-File-MD5", "Offset", "Uuid", "TotalSize"))
                with outer._lock:
                    got = outer._uploads.setdefault(fields.get("Uuid"), bytearray())
                    if ok and int(fields["Offset"]) != len(got):
                        ok = False
                    if ok:
                        got += data
                        if len(got) == int(fields["TotalSize"]):
                            if hashlib.md5(bytes(got)).hexdigest() != fields["S-File-MD5"]:
                                ok = False
                            else:
                                outer.files[fields["filename"]] = bytes(got)
                                outer.check_polls = getattr(outer, "check_after_upload", 0)
                reply = {"code": "000000", "messages": None, "data": {}, "success": True} if ok else \
                        {"code": "111111", "messages": [{"field": "common_field", "message": -2}], "data": {}, "success": False}
                if outer.omit_success:
                    reply.pop("success", None)
                raw = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.upload_port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def printer(self, name="CC1-A"):
        return {"name": name, "kind": "elegoo_cc1", "address": "127.0.0.1", "model": "Elegoo Centauri Carbon",
                "ws_port": self.ws_port, "upload_ports": [self.upload_port]}

    def close(self):
        self.http.shutdown()
        try:
            self.ws.close()
        except OSError:
            pass

    # ---- state as the printer reports it ------------------------------------------------------
    def status(self):
        codes = list(self.codes)
        if self.check_polls > 0:
            self.check_polls -= 1
            codes = [8]
        return {"CurrentStatus": codes, "TempOfNozzle": 215.0, "TempTargetNozzle": 215, "TempOfHotbed": 60.0,
                "TempTargetHotbed": 60, "PrintInfo": {"Status": self.job_status, "CurrentLayer": 3, "TotalLayer": 120,
                                                       "CurrentTicks": 300.5, "TotalTicks": 3900.0, "Progress": 8,
                                                       "Filename": ("/local/" + self.filename) if self.filename else "",
                                                       "ErrorNumber": 0}}

    # ---- WebSocket server ---------------------------------------------------------------------
    def _accept(self):
        while True:
            try:
                conn, _ = self.ws.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        conn.settimeout(10)
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                request += chunk
            with self._lock:
                full = self.open >= self.max_clients
                if not full:
                    self.open += 1
                    self.opened += 1
            if full:
                conn.sendall(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 15\r\n\r\ntoo many client")
                return
            key = re.search(rb"Sec-WebSocket-Key: (\S+)", request).group(1)
            accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            try:
                self._talk(conn)
            finally:
                with self._lock:
                    self.open -= 1
        except OSError:
            pass
        finally:
            conn.close()

    def _read_frame(self, conn, buffer):
        def need(count):
            nonlocal buffer
            while len(buffer) < count:
                chunk = conn.recv(65536)
                if not chunk:
                    raise ConnectionError()
                buffer += chunk
        need(2)
        opcode, length = buffer[0] & 0x0F, buffer[1] & 0x7F
        offset = 2
        if length == 126:
            need(4); length, offset = struct.unpack("!H", buffer[2:4])[0], 4
        elif length == 127:
            need(10); length, offset = struct.unpack("!Q", buffer[2:10])[0], 10
        need(offset + 4 + length)
        mask = buffer[offset:offset + 4]
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(buffer[offset + 4:offset + 4 + length]))
        return opcode, data, buffer[offset + 4 + length:]

    def _send(self, conn, value):
        text = value if isinstance(value, str) else json.dumps(value)
        if self.prefix and not isinstance(value, str):
            text = "%d%s" % (len(text), text)
        raw = text.encode()
        head = bytes([0x81, len(raw)]) if len(raw) < 126 else bytes([0x81, 126]) + struct.pack("!H", len(raw))
        conn.sendall(head + raw)

    def _ack(self, conn, request, data):
        body = request.get("Data") or {}
        self._send(conn, {"Id": "x", "Data": {"Cmd": body.get("Cmd"), "Data": data,
                                              "RequestID": body.get("RequestID") if self.echo_request_id else "other",
                                              "MainboardID": self.MAINBOARD, "TimeStamp": 1735032553},
                          "Topic": "sdcp/response/" + self.MAINBOARD})

    def _talk(self, conn):
        buffer = b""
        while True:
            opcode, data, buffer = self._read_frame(conn, buffer)
            if opcode == 8:
                with self._lock:
                    self.clean_closes += 1
                return
            if opcode != 1:
                continue
            text = data.decode()
            if text == "ping":
                self._send(conn, "pong")
                continue
            request = json.loads(text)
            body = request.get("Data") or {}
            cmd = body.get("Cmd")
            if cmd == 0:
                self.status_reads += 1
                self._ack(conn, request, {"Ack": 0})
                self._send(conn, {"Status": self.status(), "MainboardID": self.MAINBOARD, "TimeStamp": 1735032553,
                                  "Topic": "sdcp/status/" + self.MAINBOARD})
            elif cmd == 1:
                self._ack(conn, request, {"Ack": 0})
                self._send(conn, {"Attributes": {"Name": "Centauri Carbon", "MachineName": "Centauri Carbon",
                                                 "FirmwareVersion": "V1.1.46", "MainboardID": self.MAINBOARD},
                                  "Topic": "sdcp/attributes/" + self.MAINBOARD})
            elif cmd == 128:
                self.started.append(dict(body.get("Data") or {}))
                name = str((body.get("Data") or {}).get("Filename", "")).replace("/local/", "", 1)
                ack = self.start_ack if name in self.files else 2
                if ack == 0:
                    self.codes, self.job_status, self.filename = [1], 13, name
                if not self.no_start_ack:
                    self._ack(conn, request, {"Ack": ack})
            elif cmd == 130:
                self.stops += 1
                self.codes, self.job_status = [0], 8
                self._ack(conn, request, {"Ack": 0})
            else:
                # The real printer's control program can crash on a command it does not expect.
                self.bad_commands.append(cmd)
                return
