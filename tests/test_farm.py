import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

if os.environ.get("FARM_SEND_BUNDLE"):
    # Run the same tests against the single-file plugin OrcaSlicer actually loads, with a stand-in
    # for the orca module. Catches anything the bundler loses, which the source modules cannot show.
    import importlib.util
    import types

    orca = types.ModuleType("orca")
    orca.script = types.SimpleNamespace(ScriptPluginCapabilityBase=object)
    orca.base = object
    orca.plugin = lambda cls: cls
    sys.modules["orca"] = orca
    spec = importlib.util.spec_from_file_location("farm_send_bundle", os.environ["FARM_SEND_BUNDLE"])
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    FarmApp = core.FarmApp
    print("testing bundle: %s" % os.environ["FARM_SEND_BUNDLE"], file=sys.stderr)
else:
    import core  # noqa: E402
    from app import FarmApp  # noqa: E402
from fakes import FakeCC2, FakeMoonraker  # noqa: E402

U1_GCODE = "; HEADER_BLOCK_START\n; total layer number: 12\n; filament used [g] = 0.00, 3.50\n; total filament used [g] = 3.50\n" \
           "; estimated printing time (normal mode) = 1h 2m 3s\n; HEADER_BLOCK_END\nG28\nG1 X1\n" \
           "; CONFIG_BLOCK_START\n; filament_colour = #000000;#FFFFFF\n; filament_settings_id = \"PETG Black\";\"PETG White\"\n" \
           "; filament_type = PETG;PETG\n; post_process = \n; printer_model = Snapmaker U1\n" \
           "; printer_settings_id = Snapmaker U1 (0.4 nozzle) - #1\n; CONFIG_BLOCK_END\n"
CC2_GCODE = U1_GCODE.replace("Snapmaker U1 (0.4 nozzle) - #1", "CC1").replace("printer_model = Snapmaker U1", "printer_model = Elegoo Centauri Carbon 2")


def write(folder, name, text):
    path = os.path.join(folder, name)
    with open(path, "w") as handle:
        handle.write(text)
    return path


def wait_for(predicate, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class CoreTests(unittest.TestCase):
    def test_meta(self):
        with tempfile.TemporaryDirectory() as folder:
            meta = core.parse_gcode_meta(write(folder, "a.gcode", U1_GCODE))
        self.assertEqual(meta["printer_model"], "Snapmaker U1")
        self.assertEqual(meta["time_s"], 3723)
        self.assertEqual(meta["filament_g"], 3.5)
        self.assertEqual(meta["layers"], 12)
        self.assertEqual(meta["post_process"], "")
        self.assertEqual(meta["filaments"], [{"slot": 2, "grams": 3.5, "color": "#FFFFFF", "preset": "PETG White", "type": "PETG"}])

    def test_meta_large_file_reads_head_and_tail_only(self):
        with tempfile.TemporaryDirectory() as folder:
            head, _, tail = U1_GCODE.partition("G28\nG1 X1\n")
            path = write(folder, "big.gcode", head + ("G1 X10 Y10 E1\n" * 200000) + tail)
            meta = core.parse_gcode_meta(path)
        self.assertEqual(meta["printer_model"], "Snapmaker U1")
        self.assertEqual(meta["time_s"], 3723)

    def test_model_match(self):
        self.assertTrue(core.model_matches("Elegoo Centauri Carbon 2", "Centauri Carbon 2"))
        self.assertTrue(core.model_matches("Elegoo Centauri Carbon 2", "CentauriCarbon2"))
        self.assertTrue(core.model_matches("Snapmaker U1", "Snapmaker U1"))
        self.assertFalse(core.model_matches("Snapmaker U1", "Elegoo Centauri Carbon 2"))
        self.assertFalse(core.model_matches("Elegoo Centauri Carbon", "Elegoo Centauri Carbon 2"))
        self.assertFalse(core.model_matches("", "Snapmaker U1"))
        self.assertFalse(core.model_matches("Snapmaker U1", ""))

    def test_job_filename(self):
        name = core.job_filename("my part/../x.stl", when=0)
        self.assertRegex(name, r"^x_\d{8}-\d{6}\.gcode$")
        self.assertNotIn("/", core.job_filename("a/b\\c:d*e?"))
        self.assertEqual(core.strip_model_suffix("part.STL"), "part")
        self.assertEqual(core.strip_model_suffix(".stl"), ".stl")
        self.assertEqual(core.strip_model_suffix("v1.2 bracket"), "v1.2 bracket")
        self.assertNotEqual(core.job_filename("a", when=1), core.job_filename("a", when=2))

    def test_validate(self):
        good = {"name": "U1", "kind": core.KIND_MOONRAKER, "address": "http://192.168.0.60/", "model": "Snapmaker U1"}
        self.assertEqual(core.validate_printer(good)["address"], "192.168.0.60")
        for broken in ({"name": ""}, {"kind": "x"}, {"address": "bad host!"}, {"model": ""}, {"address": "1.2.3.4/../x"}):
            with self.assertRaises(core.FarmError):
                core.validate_printer(dict(good, **broken))

    def test_store_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "p.json")
            store = core.PrinterStore(path)
            saved = store.upsert({"name": "CC", "kind": core.KIND_CC2, "address": "10.0.0.5", "model": "Centauri Carbon 2", "access_code": "s3cret"})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            # a leftover world-readable temp file must not leak its mode into the saved file
            with open(path + ".tmp", "w") as handle:
                handle.write("stale")
            os.chmod(path + ".tmp", 0o644)
            store.save()
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            os.symlink(os.path.join(folder, "elsewhere"), path + ".tmp")
            with self.assertRaises(OSError):
                store.save()
            self.assertFalse(os.path.exists(os.path.join(folder, "elsewhere")))
            os.unlink(path + ".tmp")
            again = core.PrinterStore(path)
            self.assertEqual(again.get(saved["id"])["access_code"], "s3cret")
            self.assertEqual(again.hosts(), ["10.0.0.5"])
            again.remove(saved["id"])
            self.assertEqual(core.PrinterStore(path).printers, [])

    def test_store_survives_corrupt_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = write(folder, "p.json", "{not json")
            self.assertEqual(core.PrinterStore(path).printers, [])

    def test_presets_import(self):
        with tempfile.TemporaryDirectory() as folder:
            write(folder, "a.json", json.dumps({"name": "Elegoo Centauri Carbon 2 0.4 nozzle - CC1", "inherits": "Elegoo Centauri Carbon 2 0.4 nozzle", "print_host": "192.168.0.65", "printhost_apikey": "AAA"}))
            write(folder, "b.json", json.dumps({"name": "Snapmaker U1 (0.4 nozzle) - #1", "inherits": "Snapmaker U1 (0.4 nozzle)", "print_host": "192.168.0.60"}))
            write(folder, "c.json", json.dumps({"name": "K2", "inherits": "Anycubic Kobra 2 Max 0.4 nozzle", "print_host": ""}))
            write(folder, "d.json", json.dumps({"name": "MK3", "inherits": "Prusa MK3S 0.4 nozzle", "print_host": "192.168.0.78", "host_type": "octoprint"}))
            found = {p["name"]: p for p in core.presets_to_printers(folder)}
        self.assertEqual(set(found), {"CC1", "U1 #1"})
        self.assertEqual(core.short_printer_name("Elegoo Centauri Carbon 2 0.4 nozzle - CC1", "Elegoo Centauri Carbon 2 0.4 nozzle", "Elegoo Centauri Carbon 2"), "CC1")
        self.assertEqual(core.short_printer_name("Snapmaker U1 (0.4 nozzle) - #3", "Snapmaker U1 (0.4 nozzle)", "Snapmaker U1"), "U1 #3")
        self.assertEqual(core.short_printer_name("My printer", "Snapmaker U1 (0.4 nozzle)", "Snapmaker U1"), "My printer")
        self.assertEqual(found["CC1"]["kind"], core.KIND_CC2)
        self.assertEqual(found["CC1"]["access_code"], "AAA")
        self.assertEqual(found["CC1"]["model"], "Elegoo Centauri Carbon 2")
        self.assertEqual(found["U1 #1"]["model"], "Snapmaker U1")
        self.assertEqual(found["U1 #1"]["api_key"], "")


class MoonrakerTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMoonraker()
        self.addCleanup(self.fake.close)
        self.client = core.Moonraker(self.fake.address)

    def test_upload_never_starts_and_content_is_exact(self):
        with tempfile.TemporaryDirectory() as folder:
            data = os.urandom(700000)
            path = os.path.join(folder, "x.gcode")
            with open(path, "wb") as handle:
                handle.write(data)
            seen = []
            remote = self.client.upload(path, "x_1.gcode", progress=lambda a, b: seen.append((a, b)))
        self.assertEqual(remote, "x_1.gcode")
        self.assertEqual(self.fake.files["x_1.gcode"], data)
        self.assertEqual(self.fake.started, [])
        self.assertEqual(seen[-1], (700000, 700000))

    def test_status_and_start(self):
        status = self.client.status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["nozzle"]["current"], 220.0)  # the active tool, not the parked one
        self.fake.files["a.gcode"] = b"x"
        self.client.start_print("a.gcode")
        self.assertEqual(self.fake.started, ["a.gcode"])
        status = self.client.status()
        self.assertEqual((status["state"], round(status["progress"])), ("printing", 25))
        self.assertEqual(status["remaining_s"], 1800)

    def test_time_left_uses_the_slicer_estimate(self):
        self.fake.state, self.fake.estimated_time = "printing", 4000
        core._MOONRAKER_ESTIMATES.clear()
        self.assertEqual(self.client.status()["remaining_s"], 3000)   # 75% of the estimate is left
        self.client.status()
        self.assertEqual(self.fake.metadata_calls, 1)                 # asked once per file

    def test_time_left_early_in_a_print(self):
        # 25 minutes of heating and leveling, 1% of the file printed: no extrapolated 41 hours.
        self.assertIsNone(core.moonraker_remaining(0.01, 1500, None))
        self.assertEqual(core.moonraker_remaining(0.01, 1500, 18849), 18660)
        self.assertEqual(core.moonraker_remaining(0.25, 600, None), 1800)

    def test_start_missing_file_is_an_error(self):
        with self.assertRaises(core.FarmError):
            self.client.start_print("nope.gcode")

    def test_generic_moonraker_falls_back_to_the_standard_start(self):
        self.fake.files["a.gcode"] = b"x"
        self.assertEqual(self.client.start_print("a.gcode", {"bed_leveling": True}), "standard")
        self.assertEqual(self.client.status()["tools"], [])
        self.assertFalse(self.client.status()["has_print_options"])

    def test_snapmaker_start_carries_the_print_options(self):
        fake = FakeMoonraker(snapmaker=True)
        self.addCleanup(fake.close)
        client = core.Moonraker(fake.address)
        fake.files["a.gcode"] = b"x"
        self.assertEqual(client.start_print("a.gcode"), "snapmaker")          # leveling is on unless turned off
        fake.state = "standby"
        client.start_print("a.gcode", {"bed_leveling": False, "flow_calibrate": True, "timelapse": True})
        first, second = (call["options"] for call in fake.start_options)
        self.assertEqual((first["bed_level"], first["flow_calibrate"], first["time_lapse_camera"]), (1, 0, 0))
        self.assertEqual((second["bed_level"], second["flow_calibrate"], second["time_lapse_camera"]), (0, 1, 1))
        self.assertEqual(fake.start_options[0]["type"], "gcode")
        self.assertEqual(fake.started, ["a.gcode", "a.gcode"])
        with self.assertRaises(core.FarmError) as caught:
            client.start_print("missing.gcode")
        self.assertIn("File not found", str(caught.exception))

    def test_oversized_websocket_frame_is_refused_without_reading_it(self):
        import socket as socket_module
        server = socket_module.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def serve():
            conn, _ = server.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")
            conn.recv(4096)
            conn.sendall(b"\x81\x7f" + (2 ** 40).to_bytes(8, "big") + b"x" * 64)    # claims a 1 TB text frame
            time.sleep(1)
            conn.close()

        threading.Thread(target=serve, daemon=True).start()
        begin = time.time()
        with self.assertRaises(core.FarmError) as caught:
            core.websocket_rpc("127.0.0.1:%d" % server.getsockname()[1], "x", {}, timeout=5)
        self.assertIn("oversized", str(caught.exception))
        self.assertLess(time.time() - begin, 3)

    def test_snapmaker_reports_what_each_toolhead_holds(self):
        fake = FakeMoonraker(snapmaker=True)
        self.addCleanup(fake.close)
        tools = core.Moonraker(fake.address).status()["tools"]
        self.assertEqual(tools[1], {"slot": 2, "color": "#FFFFFF", "type": "PETG", "loaded": True})
        self.assertFalse(tools[3]["loaded"])

    def test_api_key(self):
        fake = FakeMoonraker(api_key="k")
        self.addCleanup(fake.close)
        with self.assertRaises(core.FarmError) as caught:
            core.Moonraker(fake.address).status()
        self.assertIn("Access denied", str(caught.exception))
        self.assertEqual(core.Moonraker(fake.address, "k").status()["state"], "idle")

    def test_unreachable_is_fast_and_clean(self):
        begin = time.time()
        with self.assertRaises(core.FarmError):
            core.Moonraker("127.0.0.1:9", timeout=2).status()
        self.assertLess(time.time() - begin, 4)


class CC2Tests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCC2()
        self.addCleanup(self.fake.close)
        self.client = core.ElegooCC2(self.fake.address, "abc123", mqtt_port=self.fake.mqtt_port)

    def test_identify_and_status(self):
        self.assertEqual(self.client.identify()["model"], "Centauri Carbon 2")
        self.assertEqual(self.client.status()["state"], "idle")
        self.fake.status_code = 2
        status = self.client.status()
        self.assertEqual((status["state"], status["progress"], status["remaining_s"]), ("printing", 40.0, 1200))
        self.fake.status_code = 5
        self.assertEqual(self.client.status()["state"], "busy")

    def test_chunked_upload_reassembles(self):
        with tempfile.TemporaryDirectory() as folder:
            data = os.urandom(2 * core.CHUNK + 12345)
            path = os.path.join(folder, "x.gcode")
            with open(path, "wb") as handle:
                handle.write(data)
            seen = []
            remote = self.client.upload(path, "pi\u00e8ce+(v2) [a]_1.gcode", progress=lambda a, b: seen.append(a))
        self.assertEqual(remote, "pi_ce+(v2)_[a]_1.gcode")   # ASCII, and identical to what start_print will send
        self.assertEqual(self.fake.files[remote], data)
        self.assertEqual(len(seen), 3)
        self.assertEqual(self.fake.started, [])

    def test_wrong_code(self):
        with self.assertRaises(core.FarmError) as caught:
            core.ElegooCC2(self.fake.address, "nope", mqtt_port=self.fake.mqtt_port).identify()
        self.assertIn("Access denied", str(caught.exception))

    def test_start_and_bed_mesh_retry(self):
        self.fake.files["a.gcode"] = b"x"
        self.fake.need_mesh_force = True
        self.client.start_print("a.gcode", {"bed_leveling": False, "timelapse": True})
        self.assertEqual(self.fake.started, ["a.gcode"])
        first, second = self.fake.start_requests
        self.assertEqual(first["storage_media"], "local")
        self.assertEqual(first["config"]["printer_check"], False)
        self.assertEqual(first["config"]["delay_video"], True)
        self.assertNotIn("bedlevel_force", first["config"])
        self.assertTrue(second["config"]["bedlevel_force"])

    def test_start_errors(self):
        with self.assertRaises(core.FarmError) as caught:
            self.client.start_print("missing.gcode")
        self.assertIn("not on the printer", str(caught.exception))
        self.fake.files["a.gcode"] = b"x"
        self.fake.status_code = 2
        with self.assertRaises(core.FarmError) as caught:
            self.client.start_print("a.gcode")
        self.assertIn("busy", str(caught.exception))

    def test_too_many_clients(self):
        self.fake.register_error = "too many clients"
        with self.assertRaises(core.FarmError) as caught:
            self.client.status()
        self.assertIn("too many", str(caught.exception))

    def test_many_polls_use_one_client_slot(self):
        for _ in range(12):
            self.assertEqual(self.client.status()["state"], "idle")
        fresh = core.ElegooCC2(self.fake.address, "abc123", mqtt_port=self.fake.mqtt_port)
        for _ in range(12):
            fresh.status()
        self.assertEqual(len(self.fake.registered), 1)

    def test_concurrent_requests_do_not_cross(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.client.status()["state"])) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, ["idle"] * 8)

    def test_list_and_delete(self):
        self.fake.files["a.gcode"] = b"x"
        self.assertEqual(self.client.list_files(), ["a.gcode"])
        self.client.delete_file("a.gcode")
        self.assertEqual(self.client.list_files(), [])


class SharedSlotTests(unittest.TestCase):
    """Every OrcaSlicer window of one user shares a single client slot per Centauri Carbon 2."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(core.set_shared_dir, None)
        core.set_shared_dir(self.folder)

    def _new_window(self):
        core._CC2_CLIENT_IDS.clear()   # what a second OrcaSlicer process starts with

    def test_same_id_across_processes_and_different_per_printer_and_computer(self):
        mine = core._cc2_client_id("SN1")
        # A real second process, always from the source module: the id rule is the same code.
        script = "import sys; sys.path.insert(0, %r); import core; core.set_shared_dir(%r); print(core._cc2_client_id('SN1'))"
        other = subprocess.run([sys.executable, "-c", script % (os.path.join(HERE, "..", "src"), self.folder)],
                               capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(mine, other)
        self.assertEqual(len(mine), 10)
        self.assertNotEqual(mine, core._cc2_client_id("SN2"))
        with tempfile.TemporaryDirectory() as elsewhere:   # another computer has another token
            core.set_shared_dir(elsewhere)
            self.assertNotEqual(mine, core._cc2_client_id("SN1"))

    def test_token_file_is_private_and_survives_damage(self):
        first = core._cc2_client_id("SN1")
        path = os.path.join(self.folder, "cc2_client_token")
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path, "w") as handle:
            handle.write("garbage")
        self._new_window()
        again = core._cc2_client_id("SN1")
        self.assertEqual(len(again), 10)
        self.assertNotEqual(first, again)
        self._new_window()
        self.assertEqual(again, core._cc2_client_id("SN1"))

    def test_windows_take_turns(self):
        first, second = core._cc2_file_lock("SN1"), core._cc2_file_lock("SN1")
        self.assertTrue(first.acquire(1))
        self.assertFalse(second.acquire(0.2))
        self.assertTrue(core._cc2_file_lock("SN2").acquire(0.2))   # another printer is not held up
        first.release()
        self.assertTrue(second.acquire(1))
        second.release()

    def test_many_windows_fit_in_one_printer_slot(self):
        fake = FakeCC2()
        self.addCleanup(fake.close)
        fake.max_clients = 1
        for _ in range(4):
            self._new_window()
            client = core.ElegooCC2(fake.address, "abc123", mqtt_port=fake.mqtt_port)
            self.assertEqual(client.status()["state"], "idle")
        self.assertEqual(len(fake.registered), 1)

    def test_without_a_shared_folder_each_process_has_its_own_id(self):
        core.set_shared_dir(None)
        first = core._cc2_client_id("SN1")
        self._new_window()
        self.assertNotEqual(first, core._cc2_client_id("SN1"))


class AppTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.orca_tmp = tempfile.TemporaryDirectory()      # stands in for OrcaSlicer's temp folder
        self.addCleanup(self.orca_tmp.cleanup)
        self.u1, self.u1b, self.cc2 = FakeMoonraker(), FakeMoonraker(), FakeCC2()
        for fake in (self.u1, self.u1b, self.cc2):
            self.addCleanup(fake.close)
        self.posts, self.notes = [], []
        self.app = FarmApp(self.folder.name, self.posts.append, lambda level, text: self.notes.append((level, text)),
                           default_name=lambda: "widget.stl")
        self.p_u1 = self.app.store.upsert({"name": "U1 #1", "kind": core.KIND_MOONRAKER, "address": self.u1.address, "model": "Snapmaker U1"})
        self.p_u1b = self.app.store.upsert({"name": "U1 #2", "kind": core.KIND_MOONRAKER, "address": self.u1b.address, "model": "Snapmaker U1"})
        self.p_cc2 = self.app.store.upsert({"name": "CC1", "kind": core.KIND_CC2, "address": self.cc2.address, "model": "Centauri Carbon 2",
                                            "access_code": "abc123", "mqtt_port": self.cc2.mqtt_port})

    def slice(self, text=U1_GCODE, model_name=""):
        """Slice 'the plate': OrcaSlicer always rewrites the same temp file, whatever the printer."""
        path = write(self.orca_tmp.name, ".1.0.gcode", text)
        self.app.on_slice_started()
        self.app.on_gcode_exported(path, model_name)
        job = self.app.jobs[0]
        self.assertTrue(wait_for(lambda: job["ready"] or job["error"]))
        return job

    def send(self, printers, start):
        self.app.handle({"command": "send", "printer_ids": [p["id"] for p in printers], "start": start})
        self.assertTrue(wait_for(lambda: all(not self.app.sends.get(p["id"], {"active": True})["active"] for p in printers)))
        return [self.app.sends[p["id"]] for p in printers]

    def copies(self):
        folder = os.path.join(self.folder.name, "slices")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def _statuses(self):
        return [post["id"] for post in self.posts if post.get("command") == "status"]

    def _read(self, printer):
        count = len(self._statuses())
        self.app.handle({"command": "refresh", "id": printer["id"]})
        self.assertTrue(wait_for(lambda: len(self._statuses()) > count))
        return self.app.status[printer["id"]]

    def test_notice_when_a_print_finishes_pauses_or_fails(self):
        self.assertNotIn("notice", self._read(self.p_u1))       # first look: nothing to report
        self.u1.state = "printing"
        self.assertNotIn("notice", self._read(self.p_u1))       # starting is not news
        self.u1.state = "paused"
        self.assertEqual(self._read(self.p_u1)["notice"]["level"], "error")
        self.assertEqual(self.notes[-1], ("error", "Farm Send: U1 #1 paused busy.gcode"))
        self.assertEqual(len(self.notes), 1)
        self._read(self.p_u1)
        self.assertEqual(len(self.notes), 1)                    # told once, but the card keeps the note
        self.assertIn("notice", self.app.status[self.p_u1["id"]])
        self.u1.state = "printing"                              # resumed: the pause note goes away
        self.assertNotIn("notice", self._read(self.p_u1))
        self.u1.state = "complete"
        status = self._read(self.p_u1)
        self.assertEqual(self.notes[-1], ("info", "Farm Send: U1 #1 finished busy.gcode"))
        self.assertEqual(status["notice"]["text"], "U1 #1 finished busy.gcode")
        self.assertIn("notice", self._read(self.p_u1))          # stays on the card while idle
        self.u1.state = "printing"                              # next print: the old note is cleared
        self.assertNotIn("notice", self._read(self.p_u1))
        self.u1.state = "error"
        self._read(self.p_u1)
        self.assertEqual(self.notes[-1][0], "error")
        self.assertIn("stopped with an error", self.notes[-1][1])
        self.assertEqual(len(self.notes), 3)

    def test_notice_survives_a_network_blip_and_reports_cancel(self):
        self.u1.state = "printing"
        self._read(self.p_u1)
        address = self.p_u1["address"]
        self.app.store.upsert(dict(self.p_u1, address="127.0.0.1:9"))   # unreachable for one read
        self.assertEqual(self._read(self.p_u1)["state"], "offline")
        self.assertEqual(self.notes, [])                                # offline alone is not news
        self.app.store.upsert(dict(self.p_u1, address=address))
        self.u1.state = "cancelled"
        self._read(self.p_u1)
        self.assertEqual(self.notes, [("info", "Farm Send: U1 #1: busy.gcode was cancelled")])

    def test_cc2_finish_is_only_claimed_at_100_percent(self):
        self.cc2.status_code = 2
        self._read(self.p_cc2)
        self.cc2.status_code, self.cc2.progress = 1, 100     # idle, and the printer still shows 100%
        self._read(self.p_cc2)
        self.assertIn("CC1 finished", self.notes[-1][1])
        self.cc2.status_code, self.cc2.progress = 2, None
        self._read(self.p_cc2)
        self.cc2.status_code = 1                             # idle at 0%: stopped early, not finished
        self._read(self.p_cc2)
        self.assertIn("CC1 stopped printing", self.notes[-1][1])
        self.assertIn("at 40%", self.notes[-1][1])

    def test_status_event_rules(self):
        idle, printing = {"state": "idle"}, {"state": "printing", "filename": "a.gcode", "progress": 50, "errors": []}
        self.assertIsNone(core.status_event(None, printing, "P"))
        self.assertIsNone(core.status_event(idle, printing, "P"))
        self.assertIsNone(core.status_event(printing, {"state": "offline"}, "P"))
        self.assertIsNone(core.status_event(printing, {"state": "busy"}, "P"))
        self.assertIsNone(core.status_event({"state": "error"}, {"state": "error", "state_text": "Error"}, "P"))
        self.assertEqual(core.status_event(printing, dict(printing, errors=[205]), "P"),
                         ("error", "P reports a problem while printing (code 205)"))
        self.assertIsNone(core.status_event(dict(printing, errors=[205]), dict(printing, errors=[205]), "P"))

    def test_bed_must_be_cleared_before_the_next_print(self):
        self.slice()
        (sent,) = self.send([self.p_u1], True)                 # first print: the bed is not flagged
        self.assertTrue(sent["ok"], sent)
        self.assertFalse(self._read(self.p_u1)["needs_clearing"])   # printing: nothing to clear yet
        self.u1.state = "complete"
        self.assertTrue(self._read(self.p_u1)["needs_clearing"])
        started = list(self.u1.started)
        (sent,) = self.send([self.p_u1], True)                 # a second print would land on the first
        self.assertFalse(sent["ok"])
        self.assertIn("clear the bed", sent["text"])
        self.assertEqual(self.u1.started, started)
        (sent,) = self.send([self.p_u1], False)                # uploading without printing is harmless
        self.assertTrue(sent["ok"], sent)
        self.app.handle({"command": "bed_cleared", "id": self.p_u1["id"]})
        self.assertFalse(self.app.status[self.p_u1["id"]]["needs_clearing"])
        self.assertFalse(self._read(self.p_u1)["needs_clearing"])   # still "complete": not flagged again
        (sent,) = self.send([self.p_u1], True)
        self.assertTrue(sent["ok"], sent)
        self.assertEqual(len(self.u1.started), len(started) + 1)

    def test_bed_flag_survives_a_restart_and_is_shared_between_windows(self):
        self.u1.state = "printing"
        self._read(self.p_u1)
        self.u1.state = "complete"
        other = FarmApp(self.folder.name, lambda message: None)          # a second window, or after a restart
        self.assertTrue(other.beds.get(self.p_u1["id"]))
        self.app.handle({"command": "bed_cleared", "id": self.p_u1["id"]})
        self.assertFalse(other.beds.get(self.p_u1["id"]))                # cleared here, seen there
        if os.name == "posix":
            mode = os.stat(os.path.join(self.folder.name, "farm_send_beds.json")).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_bed_rule(self):
        rule = core.bed_needs_clearing
        self.assertTrue(rule(None, {"state": "printing"}))
        self.assertTrue(rule(False, {"state": "paused"}))
        self.assertIsNone(rule(True, {"state": "idle", "last_result": ""}))        # stays until the user clears it
        self.assertIsNone(rule(False, {"state": "idle", "last_result": "complete"}))  # cleared: old result is not news
        self.assertTrue(rule(None, {"state": "idle", "last_result": "complete"}))  # first seen with a finished print
        self.assertFalse(rule(None, {"state": "idle", "last_result": ""}))
        self.assertIsNone(rule(True, {"state": "offline"}))

    def test_types_list_one_row_per_printer_type(self):
        types = {row["label"]: row for row in self.app.snapshot()["types"]}
        self.assertEqual(sorted(types), ["Centauri Carbon 2", "Snapmaker U1"])
        self.assertEqual(types["Snapmaker U1"]["printers"], ["U1 #1", "U1 #2"])
        self.assertIsNone(types["Snapmaker U1"]["combo"])
        job = self.slice()
        snapshot = self.app.snapshot()
        (view,) = snapshot["jobs"]
        u1 = [row for row in snapshot["types"] if row["label"] == "Snapmaker U1"][0]
        self.assertEqual(view["key"], u1["key"])              # the panel matches plate to type by this
        self.assertEqual(job["key"], u1["key"])

    def test_refresh_one_printer_and_all(self):
        self.app.handle({"command": "poll"})
        self.assertTrue(wait_for(lambda: len(self._statuses()) == 3))
        before = time.time()
        self.app.handle({"command": "poll"})          # too soon: the timed poll asks nobody
        time.sleep(0.3)
        self.assertEqual(len(self._statuses()), 3)
        self.app.handle({"command": "refresh", "id": self.p_cc2["id"]})   # the button asks now
        self.assertTrue(wait_for(lambda: len(self._statuses()) == 4))
        self.assertEqual(self._statuses()[-1], self.p_cc2["id"])
        self.assertGreaterEqual(self.app.status[self.p_cc2["id"]]["checked_at"], before)
        self.app.handle({"command": "refresh"})
        self.assertTrue(wait_for(lambda: len(self._statuses()) == 7))
        self.assertEqual(sorted(self._statuses()[4:]), sorted(p["id"] for p in (self.p_u1, self.p_u1b, self.p_cc2)))

    def test_capture_and_view(self):
        job = self.slice()
        snapshot = self.app.snapshot()
        (view,) = snapshot["jobs"]
        self.assertTrue(view["ready"])
        self.assertEqual(view["display_name"], "widget")
        self.assertIn("1h02m", view["summary"])
        self.assertEqual(view["printers"], ["U1 #1", "U1 #2"])
        self.assertEqual(snapshot["job_for"], {self.p_u1["id"]: job["id"], self.p_u1b["id"]: job["id"], self.p_cc2["id"]: None})
        self.assertFalse(snapshot["slicing"])
        self.assertEqual(self.copies(), [job["id"] + ".gcode"])

    def test_upload_only_does_not_start(self):
        self.slice()
        (result,) = self.send([self.p_u1], start=False)
        self.assertTrue(result["ok"], result)
        (name,) = self.u1.files
        self.assertRegex(name, r"^widget_PETG_1h02m_\d{8}-\d{6}\.gcode$")
        self.assertEqual(self.u1.files[name].decode(), U1_GCODE)
        self.assertEqual(self.u1.started, [])

    def test_plate_view_names_the_toolhead_and_colour(self):
        self.slice()
        self.assertEqual(self.app.snapshot()["jobs"][0]["filaments"][0]["slot"], 2)
        self.assertEqual(self.app.snapshot()["jobs"][0]["filaments"][0]["color"], "#FFFFFF")

    def test_started_line_is_dropped_once_the_printer_is_idle_again(self):
        self.slice()
        self.send([self.p_u1], start=True)
        self.assertTrue(self.app.sends[self.p_u1["id"]]["started"])
        self.app.poll(force=True)
        time.sleep(0.5)
        self.assertIn(self.p_u1["id"], self.app.sends)              # still printing: keep it
        self.u1.state = "cancelled"
        self.app.sends[self.p_u1["id"]]["at"] -= 60
        self.app.poll(force=True)
        self.assertTrue(wait_for(lambda: self.p_u1["id"] not in self.app.sends))

    def test_upload_and_print(self):
        self.slice()
        (result,) = self.send([self.p_u1], start=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.u1.started, list(self.u1.files))
        self.assertTrue(any(level == "info" for level, _ in self.notes))

    def test_wrong_type_is_blocked_before_any_network_call(self):
        self.slice()
        (result,) = self.send([self.p_cc2], start=True)
        self.assertFalse(result["ok"])
        self.assertIn("Blocked", result["text"])
        self.assertEqual((self.cc2.files, self.cc2.started), ({}, []))

    def test_slice_for_both_types_then_send_everything_at_once(self):
        u1_job = self.slice(U1_GCODE)
        cc2_job = self.slice(CC2_GCODE)       # same temp file, rewritten: the U1 plate must survive
        self.assertEqual({job["id"] for job in self.app.jobs}, {u1_job["id"], cc2_job["id"]})
        self.assertEqual(len(self.copies()), 2)
        snapshot = self.app.snapshot()
        self.assertEqual(snapshot["job_for"], {self.p_u1["id"]: u1_job["id"], self.p_u1b["id"]: u1_job["id"], self.p_cc2["id"]: cc2_job["id"]})
        results = self.send([self.p_u1, self.p_u1b, self.p_cc2], start=True)
        self.assertEqual([r["ok"] for r in results], [True, True, True], results)
        self.assertEqual(list(self.u1.files), list(self.u1b.files))                 # one name for the U1 batch
        self.assertEqual(next(iter(self.u1.files.values())).decode(), U1_GCODE)     # each type got its own plate
        self.assertEqual(next(iter(self.cc2.files.values())).decode(), CC2_GCODE)
        self.assertEqual((len(self.u1.started), len(self.u1b.started), len(self.cc2.started)), (1, 1, 1))

    def test_cc2_upload_and_print(self):
        self.slice(CC2_GCODE)
        (result,) = self.send([self.p_cc2], start=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.cc2.started, list(self.cc2.files))
        self.assertEqual(self.u1.files, {})

    def test_print_refused_when_busy_but_file_stays(self):
        self.u1.state = "printing"
        self.slice()
        (result,) = self.send([self.p_u1], start=True)
        self.assertFalse(result["ok"])
        self.assertIn("not started", result["text"])
        self.assertEqual((len(self.u1.files), self.u1.started), (1, []))

    def test_offline_printer_fails_cleanly_and_others_succeed(self):
        self.u1b.close()
        self.slice()
        results = self.send([self.p_u1, self.p_u1b], start=False)
        self.assertEqual([r["ok"] for r in results], [True, False])
        self.assertIn("Cannot reach", results[1]["text"])

    def test_reslice_for_same_type_replaces_only_that_type(self):
        self.slice(CC2_GCODE)
        old = self.slice(U1_GCODE)
        new = self.slice(U1_GCODE + "; changed\n")
        self.assertEqual(len(self.app.jobs), 2)
        self.assertNotIn(old["id"] + ".gcode", self.copies())
        self.assertEqual(len(self.copies()), 2)
        self.send([self.p_u1], start=False)
        self.assertTrue(next(iter(self.u1.files.values())).decode().endswith("; changed\n"))
        self.assertEqual(self.app.snapshot()["job_for"][self.p_u1["id"]], new["id"])

    def test_orca_rewriting_its_temp_file_does_not_change_what_is_sent(self):
        self.slice(U1_GCODE)
        write(self.orca_tmp.name, ".1.0.gcode", "; something else entirely, never announced\n")
        (result,) = self.send([self.p_u1], start=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(next(iter(self.u1.files.values())).decode(), U1_GCODE)

    def test_sending_keeps_working_while_the_next_type_is_slicing(self):
        self.slice(U1_GCODE)
        self.app.on_slice_started()
        (result,) = self.send([self.p_u1], start=False)
        self.assertTrue(result["ok"], result)

    def test_no_model_in_gcode_cannot_be_sent(self):
        job = self.slice("G28\n; nothing useful\n")
        self.assertFalse(job["ready"])
        self.assertIn("printer model", job["error"])
        (result,) = self.send([self.p_u1], start=False)
        self.assertFalse(result["ok"])
        self.assertEqual(self.copies(), [])

    def test_failed_capture_does_not_discard_good_plates(self):
        good = self.slice(U1_GCODE)
        self.slice("G28\n; nothing useful\n")
        self.assertEqual(self.app.snapshot()["job_for"][self.p_u1["id"]], good["id"])
        (result,) = self.send([self.p_u1], start=False)
        self.assertTrue(result["ok"], result)

    def stale_by_type(self):
        return {view["printer_model"]: view["stale"] for view in self.app.snapshot()["jobs"]}

    def test_rearranging_for_the_next_type_does_not_flag_earlier_plates(self):
        self.slice(U1_GCODE)
        self.app.mark_stale()                 # bed re-arranged to fit the other printer
        self.assertEqual(self.stale_by_type(), {"Snapmaker U1": True})
        self.slice(CC2_GCODE)                 # slicing for the next type settles the U1 plate
        self.assertEqual(self.stale_by_type(), {"Snapmaker U1": False, "Elegoo Centauri Carbon 2": False})
        self.app.mark_stale()                 # a change now can only make the newest slice out of date
        self.assertEqual(self.stale_by_type(), {"Snapmaker U1": False, "Elegoo Centauri Carbon 2": True})
        self.slice(U1_GCODE)                  # re-slicing U1 makes it the newest, and settles CC2
        self.app.mark_stale()
        self.assertEqual(self.stale_by_type(), {"Snapmaker U1": True, "Elegoo Centauri Carbon 2": False})

    def test_same_layout_for_both_types_shows_no_warning(self):
        self.slice(U1_GCODE)
        self.slice(CC2_GCODE)
        self.assertEqual(set(self.stale_by_type().values()), {False})

    def test_project_close_removes_copies(self):
        self.slice(U1_GCODE)
        self.slice(CC2_GCODE)
        self.app.project_closed()
        self.assertEqual(self.app.snapshot()["jobs"], [])
        self.assertEqual(self.copies(), [])

    def test_leftover_copies_are_cleared_at_startup(self):
        self.slice(U1_GCODE)
        self.assertEqual(len(self.copies()), 1)
        FarmApp(self.folder.name, lambda message: None)
        self.assertEqual(self.copies(), [])

    def test_poll_reports_each_printer(self):
        self.u1b.close()
        self.cc2.status_code = 2
        self.app.handle({"command": "init"})
        self.assertTrue(wait_for(lambda: len(self.app.status) == 3))
        self.assertEqual(self.app.status[self.p_u1["id"]]["state"], "idle")
        self.assertEqual(self.app.status[self.p_u1b["id"]]["state"], "offline")
        self.assertEqual(self.app.status[self.p_cc2["id"]]["state"], "printing")

    def test_form_test_and_save(self):
        self.app.handle({"command": "test_printer", "printer": {"name": "x", "kind": core.KIND_MOONRAKER, "address": self.u1.address, "model": ""}})
        reply = [p for p in self.posts if p.get("command") == "form_result"][-1]
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["model"], "Snapmaker U1")
        self.app.handle({"command": "save_printer", "printer": {"name": "", "kind": core.KIND_MOONRAKER, "address": "1.2.3.4", "model": "m"}})
        self.assertFalse([p for p in self.posts if p.get("command") == "form_result"][-1]["ok"])
        self.app.handle({"command": "save_printer", "printer": {"name": "U1 #3", "kind": core.KIND_MOONRAKER, "address": "1.2.3.4", "model": "Snapmaker U1"}})
        self.assertTrue([p for p in self.posts if p.get("command") == "form_result"][-1]["saved"])
        self.assertEqual(len(core.PrinterStore(self.app.store.path).printers), 4)

    def test_cancel_during_upload(self):
        self.slice(U1_GCODE.replace("G1 X1\n", "G1 X1\n" * 400000))
        original = core.Moonraker.upload

        def slow(client, path, filename, progress=None, cancelled=None):
            def paced(sent, total):
                time.sleep(0.05)
                if progress:
                    progress(sent, total)
            return original(client, path, filename, progress=paced, cancelled=cancelled)

        core.Moonraker.upload = slow
        self.addCleanup(setattr, core.Moonraker, "upload", original)
        self.app.handle({"command": "send", "printer_ids": [self.p_u1["id"]], "start": True})
        self.assertTrue(wait_for(lambda: (self.app.sends.get(self.p_u1["id"]) or {}).get("percent")))
        self.app.handle({"command": "cancel_send", "id": self.p_u1["id"]})
        self.assertTrue(wait_for(lambda: not self.app.sends[self.p_u1["id"]]["active"]))
        self.assertFalse(self.app.sends[self.p_u1["id"]]["ok"])
        self.assertEqual(self.u1.started, [])


class ComboTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.selected = {"model": "Snapmaker U1", "printer": "Snapmaker U1 (0.4 nozzle) - #1", "process": "0.20 Standard",
                         "filaments": ["PETG Black", "PETG White"]}
        self.applied, self.fail, self.skip = [], None, []

        def apply(combo):
            self.assertIsNot(threading.current_thread(), threading.main_thread())   # never on the UI thread
            if self.fail:
                raise RuntimeError(self.fail)
            self.applied.append(combo)
            self.selected = {"model": combo["label"], "printer": combo["printer"], "process": combo["process"],
                             "filaments": list(combo["filaments"])}
            return list(self.skip)

        self.posts = []
        self.app = FarmApp(self.folder.name, self.posts.append, read_presets=lambda: dict(self.selected), apply_presets=apply)
        self.app.store.upsert({"name": "U1 #1", "kind": core.KIND_MOONRAKER, "address": "10.0.0.1", "model": "Snapmaker U1"})
        self.app.store.upsert({"name": "CC1", "kind": core.KIND_CC2, "address": "10.0.0.2", "model": "Centauri Carbon 2"})

    def note(self):
        return self.app.snapshot()["combo_note"]

    def test_remember_use_and_persist(self):
        self.app.handle({"command": "init"})
        self.app.handle({"command": "save_combo"})
        self.selected = {"model": "Elegoo Centauri Carbon 2", "printer": "CC1 preset", "process": "0.20 CC2", "filaments": ["Rapid PETG"]}
        self.app.handle({"command": "save_combo"})
        combos = {c["label"]: c for c in self.app.snapshot()["combos"]}
        self.assertEqual(set(combos), {"Snapmaker U1", "Elegoo Centauri Carbon 2"})
        self.assertEqual(combos["Snapmaker U1"]["printers"], ["U1 #1"])
        self.assertEqual(combos["Elegoo Centauri Carbon 2"]["printers"], ["CC1"])   # printer says "Centauri Carbon 2"
        self.assertTrue(combos["Elegoo Centauri Carbon 2"]["active"])
        self.assertFalse(combos["Snapmaker U1"]["active"])
        self.app.handle({"command": "load_combo", "key": combos["Snapmaker U1"]["key"]})
        self.assertTrue(wait_for(lambda: self.applied and not self.note()["busy"]))
        self.assertEqual(self.applied[0]["printer"], "Snapmaker U1 (0.4 nozzle) - #1")
        self.assertEqual(self.applied[0]["filaments"], ["PETG Black", "PETG White"])
        self.assertTrue(self.note()["ok"])
        self.app.handle({"command": "poll"})
        self.assertTrue({c["label"]: c["active"] for c in self.app.snapshot()["combos"]}["Snapmaker U1"])
        again = core.PrinterStore(self.app.store.path)
        self.assertEqual((len(again.combos), len(again.printers)), (2, 2))

    def test_saving_again_replaces_the_type_entry(self):
        self.app.handle({"command": "save_combo"})
        self.selected["process"] = "0.28 Draft"
        self.app.handle({"command": "save_combo"})
        self.assertEqual([c["process"] for c in self.app.snapshot()["combos"]], ["0.28 Draft"])

    def test_failure_and_partial_apply_are_reported(self):
        self.app.handle({"command": "save_combo"})
        key = self.app.snapshot()["combos"][0]["key"]
        self.fail = "Switching the edited printer preset was canceled"
        self.app.handle({"command": "load_combo", "key": key})
        self.assertTrue(wait_for(lambda: "canceled" in self.note()["text"]))
        self.assertFalse(self.note()["ok"])
        self.assertFalse(self.note()["busy"])
        self.fail, self.skip = None, ["filament 2 (not offered)"]
        self.app.handle({"command": "load_combo", "key": key})
        self.assertTrue(wait_for(lambda: "filament 2" in self.note()["text"]))
        self.assertFalse(self.note()["ok"])

    def test_delete_and_missing_selection(self):
        self.app.handle({"command": "save_combo"})
        self.app.handle({"command": "delete_combo", "key": self.app.snapshot()["combos"][0]["key"]})
        self.assertEqual(self.app.snapshot()["combos"], [])
        self.selected = {"model": "", "printer": "", "process": "", "filaments": []}
        self.app.handle({"command": "save_combo"})
        self.assertFalse(self.note()["ok"])
        self.assertEqual(core.PrinterStore(self.app.store.path).combos, {})

    def test_old_store_without_combos_still_loads(self):
        path = write(self.folder.name, "old.json", json.dumps({"version": 1, "printers": [{"id": "a", "name": "x"}]}))
        store = core.PrinterStore(path)
        self.assertEqual((len(store.printers), store.combos), (1, {}))


if __name__ == "__main__":
    unittest.main(verbosity=1)
