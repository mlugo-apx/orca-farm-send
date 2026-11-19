"""In-process fake printers: a Moonraker HTTP server and an Elegoo CC2 (HTTP + MQTT broker)."""
import base64
import hashlib
import json
import re
import socket
import struct
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeMoonraker:
    def __init__(self, api_key="", snapmaker=False):
        self.files, self.started, self.state, self.api_key = {}, [], "standby", api_key
        self.estimated_time, self.metadata_calls = None, 0   # slicer estimate served as file metadata
        self.snapmaker, self.start_options = snapmaker, []
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
                        **({"print_task_config": outer.tools} if outer.snapmaker else {})}}})
                elif self.path.startswith("/server/files/metadata") and outer.estimated_time is not None:
                    outer.metadata_calls += 1
                    self._json(200, {"result": {"estimated_time": outer.estimated_time}})
                elif self.path.startswith("/server/files/list"):
                    self._json(200, {"result": [{"path": name} for name in outer.files]})
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
        self.progress = None   # override the reported progress, e.g. 100 on an idle printer after a print
        self.partial = {}
        self.need_mesh_force = False
        self.register_error = "ok"
        self.registered, self.max_clients = set(), 3
        self.start_requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
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
                self._json(200, {"error_code": 0, "system_info": {"sn": outer.serial, "hostname": "FAKE", "machine_model": "Centauri Carbon 2",
                                                                    "software_version": {"ota_version": "1.0"}}})

            def do_PUT(self):
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
            result = {"error_code": 0, "file_list": [{"filename": name} for name in self.files]}
        elif method == 1047:
            paths = params.get("file_path")
            if not isinstance(paths, list):   # the real printer rejects a bare string
                result = {"error_code": 1003, "message": "invalid parameter"}
            else:
                for path in paths:
                    self.files.pop(path, None)
                result = {"error_code": 0}
        else:
            result = {"error_code": 1001}
        self._send_publish(conn, "%s/%s/api_response" % (base, match.group(1)), {"id": body.get("id"), "method": method, "result": result})
