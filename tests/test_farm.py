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
            # A symlink planted at the temp name must never be written through. The link itself is
            # removed and a fresh private file is created in its place; its target stays untouched.
            target = os.path.join(folder, "elsewhere")
            with open(target, "w") as handle:
                handle.write("not ours")
            os.symlink(target, path + ".tmp")
            store.save()
            with open(target) as handle:
                self.assertEqual(handle.read(), "not ours")
            self.assertFalse(os.path.islink(path))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertFalse(os.path.lexists(path + ".tmp"))
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


class InstallLayoutTests(unittest.TestCase):
    """A plugin installed from a file and one installed from the Plugin Hub live in different folders."""

    def test_data_dir_and_saved_printers_for_both_layouts(self):
        with tempfile.TemporaryDirectory() as data:
            plugins = os.path.join(data, "orca_plugins")
            layouts = {
                "file": (os.path.join(plugins, "farm_send.py", "farm_send.py"),
                         os.path.join(plugins, "plugin_data", "farm_send")),
                "hub": (os.path.join(plugins, "_subscribed", "user-123", "farm_send", "farm_send_any.py"),
                        os.path.join(plugins, "plugin_data", "_subscribed", "user-123", "farm_send")),
            }
            for name, (plugin_file, storage) in layouts.items():
                self.assertEqual(core.data_dir_of(plugin_file), data, name)
                self.assertEqual(core.data_dir_of(storage), data, name)
                os.makedirs(storage)
                store = core.PrinterStore(os.path.join(storage, core.STORE_FILE))
                store.upsert({"name": name, "kind": core.KIND_MOONRAKER, "address": "10.0.0.%d" % (len(name)), "model": "Snapmaker U1"})
            # Each copy asks for network access to its OWN printers only, never the other copy's.
            hosts = lambda name: sorted(host for path in core.saved_store_files(data, layouts[name][0])
                                        for host in core.PrinterStore(path).hosts())
            self.assertEqual(hosts("file"), ["10.0.0.4"])
            self.assertEqual(hosts("hub"), ["10.0.0.3"])
            # another plugin's data is not this plugin's business
            os.makedirs(os.path.join(plugins, "plugin_data", "other_plugin"))
            open(os.path.join(plugins, "plugin_data", "other_plugin", "secrets.json"), "w").close()
            self.assertEqual(len(core.saved_store_files(data, layouts["file"][0])), 1)
            # a hub copy that has saved nothing yet asks for nothing
            fresh = os.path.join(plugins, "_subscribed", "user-999", "farm_send", "farm_send_any.py")
            self.assertEqual(core.saved_store_files(data, fresh), [])

    def test_import_uses_the_presets_orcaslicer_has_loaded(self):
        with tempfile.TemporaryDirectory() as folder:
            live = [
                {"name": "Snapmaker U1 (0.4 nozzle) - #1", "print_host": "10.1.1.60", "printhost_apikey": "", "host_type": "moonraker",
                 "printer_model": "Snapmaker U1", "inherits": "Snapmaker U1 (0.4 nozzle)"},
                {"name": "Elegoo Centauri Carbon 2 0.4 nozzle - CC1", "print_host": ["10.1.1.65"], "printhost_apikey": "code1",
                 "host_type": None, "printer_model": "Elegoo Centauri Carbon 2", "inherits": "Elegoo Centauri Carbon 2 0.4 nozzle"},
                {"name": "Some other printer", "print_host": "", "printhost_apikey": None, "host_type": None, "printer_model": "X", "inherits": ""},
            ]
            stale_dir = os.path.join(folder, "stale")           # a second preset folder on disk: must be ignored
            os.makedirs(stale_dir)
            write(stale_dir, "old.json", json.dumps({"name": "Snapmaker U1 (0.4 nozzle) - #1", "inherits": "Snapmaker U1 (0.4 nozzle)",
                                                     "print_host": "10.9.9.99", "host_type": "moonraker"}))
            posts = []
            app = FarmApp(os.path.join(folder, "store"), posts.append, preset_dirs=[stale_dir], list_presets=lambda: live) \
                if os.makedirs(os.path.join(folder, "store")) is None else None
            app.handle({"command": "import_presets"})
            self.assertEqual(sorted(p["address"] for p in app.store.printers), ["10.1.1.60", "10.1.1.65"])
            cc = [p for p in app.store.printers if p["kind"] == core.KIND_CC2][0]
            self.assertEqual(cc["access_code"], "code1")

    def test_paths_outside_orcaslicer_give_nothing(self):
        self.assertEqual(core.data_dir_of("/somewhere/else/file.py"), "")
        self.assertEqual(core.data_dir_of(""), "")
        self.assertEqual(core.saved_store_files(""), [])


class WorkerPoolTests(unittest.TestCase):
    """All background work runs on workers started once, never on a thread started while a panel
    message is being handled (OrcaSlicer can credit such a thread to another plugin)."""

    def _pool_class(self):
        return core.WorkerPool if hasattr(core, "WorkerPool") else __import__("app").WorkerPool

    def test_jobs_run_and_a_failing_job_does_not_stop_the_workers(self):
        pool = self._pool_class()(size=3)
        pool.start()
        pool.start()                                   # starting twice adds no threads
        self.assertEqual(len(pool._threads), 3)
        done = []
        pool.submit(lambda: 1 / 0)
        for n in range(20):
            pool.submit(done.append, n)
        self.assertTrue(wait_for(lambda: len(done) == 20))
        self.assertEqual(sorted(done), list(range(20)))

    def test_no_thread_is_started_while_handling_panel_messages(self):
        fake = FakeMoonraker()
        self.addCleanup(fake.close)
        pool = self._pool_class()(size=6)
        pool.start()                                   # what execute() does, once
        with tempfile.TemporaryDirectory() as folder:
            posts = []
            app = FarmApp(folder, posts.append, spawn=pool.submit)
            printer = app.store.upsert({"name": "U1", "kind": core.KIND_MOONRAKER, "address": fake.address, "model": "Snapmaker U1"})
            started = []
            original = threading.Thread.start
            # Record what each new thread is for at the moment it starts. The fake printer's own
            # web server starts one thread per request; those are not the plugin's.
            threading.Thread.start = lambda self_: (started.append(repr(getattr(self_, "_target", None))), original(self_))[1]
            try:
                for command in ({"command": "init"}, {"command": "poll"}, {"command": "refresh"},
                                {"command": "refresh", "id": printer["id"]}, {"command": "check_storage"},
                                {"command": "cleanup", "printer_ids": [printer["id"]]}):
                    app.handle(command)
                self.assertTrue(wait_for(lambda: app.status.get(printer["id"], {}).get("state") == "idle"))
                self.assertTrue(wait_for(lambda: printer["id"] in app.storage))
            finally:
                threading.Thread.start = original
            mine = [t for t in started if "process_request" not in t and "serve_forever" not in t]
            self.assertEqual(mine, [], "a thread was started while handling a panel message")

    def test_before_start_jobs_still_run(self):
        pool = self._pool_class()(size=2)
        done = []
        pool.submit(done.append, "x")
        self.assertTrue(wait_for(lambda: done == ["x"]))


class CC2UploadTests(unittest.TestCase):
    """The printer refuses a burst of connections with HTTP 429, so a plate goes up over one."""

    def setUp(self):
        self.fake = FakeCC2(); self.addCleanup(self.fake.close)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "plate.gcode")
        with open(self.path, "wb") as handle:
            handle.write(os.urandom(10_000))
        self.client = core.ElegooCC2(self.fake.address, "abc123", mqtt_port=self.fake.mqtt_port)
        old = (core.CHUNK, core.CC2_UPLOAD_PAUSE)
        core.CHUNK, core.CC2_UPLOAD_PAUSE = 1000, 0.01                # ten pieces, quick retries
        self.addCleanup(lambda: setattr(core, "CHUNK", old[0]) or setattr(core, "CC2_UPLOAD_PAUSE", old[1]))

    def test_all_pieces_share_one_connection(self):
        seen = []
        self.client.upload(self.path, "plate.gcode", progress=lambda sent, total: seen.append(sent))
        with open(self.path, "rb") as handle:
            self.assertEqual(self.fake.files["plate.gcode"], handle.read())
        self.assertEqual(len(self.fake.put_connections), 1)
        self.assertEqual(seen[-1], 10_000)

    def test_busy_printer_is_retried_and_the_piece_is_resent(self):
        self.fake.busy_puts = 3
        self.client.upload(self.path, "plate.gcode")
        with open(self.path, "rb") as handle:
            self.assertEqual(self.fake.files["plate.gcode"], handle.read())

    def test_gives_up_with_a_plain_message_when_it_stays_busy(self):
        self.fake.busy_puts = 99
        with self.assertRaises(core.FarmError) as caught:
            self.client.upload(self.path, "plate.gcode")
        self.assertIn("HTTP 429", str(caught.exception))
        self.assertNotIn("plate.gcode", self.fake.files)


class LibraryTests(unittest.TestCase):
    """Plates kept for reprinting: save from a slice, send later without slicing, pick the toolhead."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.u1, self.cc2 = FakeMoonraker(snapmaker=True), FakeCC2()
        for fake in (self.u1, self.cc2):
            self.addCleanup(fake.close)
        self.posts = []
        self.app = FarmApp(self.folder.name, self.posts.append, default_name=lambda: "rail clip.stl")
        self.p_u1 = self.app.store.upsert({"name": "U1 #1", "kind": core.KIND_MOONRAKER, "address": self.u1.address,
                                           "model": "Snapmaker U1", "nozzle": 0.4})
        self.p_cc2 = self.app.store.upsert({"name": "CC1", "kind": core.KIND_CC2, "address": self.cc2.address,
                                            "model": "Centauri Carbon 2", "access_code": "abc123", "mqtt_port": self.cc2.mqtt_port})

    def _slice_and_save(self, item="Rail clip", parts=24):
        path = write(self.tmp.name, ".1.0.gcode", U1_GCODE)
        self.app.on_slice_started(); self.app.on_gcode_exported(path, "")
        job = self.app.jobs[0]
        self.assertTrue(wait_for(lambda: job["ready"] or job["error"]))
        self.app.handle({"command": "library_save", "job_id": job["id"], "item": item, "parts": parts})
        return [post for post in self.posts if post.get("command") == "library_result"][-1]

    def _send(self, entry_id, printers, start=True, **extra):
        self.app.handle(dict({"command": "library_send", "id": entry_id, "printer_ids": [p["id"] for p in printers], "start": start}, **extra))
        self.assertTrue(wait_for(lambda: all(not self.app.sends.get(p["id"], {"active": True})["active"] for p in printers)))
        return [self.app.sends[p["id"]] for p in printers]

    def _status(self, printer):
        seen = lambda: len([post for post in self.posts if post.get("command") == "status" and post.get("id") == printer["id"]])
        before = seen()
        self.app.poll(force=True, only=printer["id"])
        self.assertTrue(wait_for(lambda: seen() > before))            # a fresh read, not the one already held

    def test_save_survives_the_project_closing_and_a_restart(self):
        self.assertTrue(self._slice_and_save()["ok"])
        (view,) = self.app.snapshot()["library"]
        self.assertEqual((view["item"], view["parts"], view["printer_model"]), ("Rail clip", 24, "Snapmaker U1"))
        self.assertEqual(view["printers"], [self.p_u1["id"]])
        self.assertEqual([f["slot"] for f in view["filaments"]], [2])
        self.app.project_closed()                                   # the slice copy goes, the kept plate stays
        other = FarmApp(self.folder.name, lambda message: None)
        (entry,) = other.library()
        with open(entry["path"]) as handle:
            self.assertEqual(handle.read(), U1_GCODE)
        self.assertEqual(entry["md5"], core.file_md5(entry["path"]))

    def test_save_needs_a_name_a_count_and_a_fresh_slice(self):
        self.assertFalse(self._slice_and_save(item="  ")["ok"])
        self.assertFalse(self._slice_and_save(parts=0)["ok"])
        self.assertFalse(self._slice_and_save(parts="many")["ok"])
        self.assertEqual(self.app.library(), [])
        self.app.handle({"command": "library_save", "job_id": "nope", "item": "X", "parts": 1})
        self.assertIn("no longer here", self.posts[-2]["text"] if self.posts[-1].get("command") == "state" else self.posts[-1]["text"])

    def test_send_as_sliced_and_from_another_toolhead(self):
        self._slice_and_save()
        entry_id = self.app.library()[0]["id"]
        self.app.project_closed()
        self._status(self.p_u1)
        (sent,) = self._send(entry_id, [self.p_u1])
        self.assertTrue(sent["ok"], sent)
        self.assertEqual(self.u1.start_options[-1]["options"]["map_table"], [[0, 0], [1, 1], [2, 2], [3, 3]])
        self.assertIn("Rail_clip_x24", self.u1.started[-1])
        self.u1.state = "standby"; self.app.beds.set(self.p_u1["id"], False); self._status(self.p_u1)
        (sent,) = self._send(entry_id, [self.p_u1], toolhead=1)      # the file is for toolhead 2; print it from 1
        self.assertTrue(sent["ok"], sent)
        self.assertEqual(self.u1.start_options[-1]["options"]["map_table"], [[0, 0], [1, 0], [2, 2], [3, 3]])

    def test_send_is_refused_for_an_empty_toolhead_wrong_material_or_wrong_printer(self):
        self._slice_and_save()
        entry_id = self.app.library()[0]["id"]
        self._status(self.p_u1); self._status(self.p_cc2)
        started = len(self.u1.started)
        (empty,) = self._send(entry_id, [self.p_u1], toolhead=4)     # the fake reports toolhead 4 empty
        self.assertIn("toolhead 4 reports no filament", empty["text"])
        self.u1.tools["filament_type"][0] = "PLA"
        self._status(self.p_u1)
        (material,) = self._send(entry_id, [self.p_u1], toolhead=1)
        self.assertIn("holds PLA", material["text"])
        (wrong,) = self._send(entry_id, [self.p_cc2])
        self.assertIn("sliced for Snapmaker U1", wrong["text"])
        self.assertEqual(len(self.u1.started), started)
        self.assertEqual(self.cc2.started, [])
        (upload,) = self._send(entry_id, [self.p_u1], start=False)   # uploading without printing is still fine
        self.assertTrue(upload["ok"], upload)

    def test_remap_is_refused_on_a_printer_that_cannot_do_it(self):
        plain = FakeMoonraker()                                       # an ordinary Klipper printer
        self.addCleanup(plain.close)
        printer = self.app.store.upsert({"name": "Other", "kind": core.KIND_MOONRAKER, "address": plain.address, "model": "Snapmaker U1"})
        self._slice_and_save()
        self._status(printer)
        (sent,) = self._send(self.app.library()[0]["id"], [printer], toolhead=1)
        self.assertFalse(sent["ok"])
        self.assertIn("cannot print from a different toolhead", sent["text"])
        self.assertEqual(plain.started, [])

    def test_delete_removes_both_files_and_a_half_entry_is_ignored(self):
        self._slice_and_save()
        entry = self.app.library()[0]
        os.remove(entry["path"])                                      # G-code gone: not listed
        self.app._library_cache = None
        self.assertEqual(self.app.library(), [])
        self._slice_and_save(item="Second")
        entry = self.app.library()[0]
        self.app.handle({"command": "library_delete", "id": entry["id"]})
        self.assertEqual(self.app.library(), [])
        self.assertFalse(os.path.exists(entry["path"]))

    def test_plan_whole_plates_when_no_printer_can_skip_parts(self):
        big, small = {"id": "b", "parts": 24, "printer_model": "Snapmaker U1"}, {"id": "s", "parts": 6, "printer_model": "Snapmaker U1"}
        cc = {"id": "c", "parts": 12, "printer_model": "Centauri Carbon 2"}
        u1 = lambda n: {"id": "u%d" % n, "model": "Snapmaker U1", "kind": core.KIND_MOONRAKER}
        plan = core.plan_order(30, [u1(1), u1(2), u1(3)], [big, small, cc])          # unlabelled plates: whole ones
        self.assertEqual([(x["printer_id"], x["entry_id"], x["parts"]) for x in plan], [("u1", "b", 24), ("u2", "s", 6)])
        self.assertEqual([x["entry_id"] for x in core.plan_order(4, [u1(1)], [big, small])], ["s"])       # smallest overshoot
        self.assertEqual(core.plan_order(10, [{"id": "k", "model": "Kobra"}], [big]), [])
        self.assertEqual(core.plan_order(0, [u1(1)], [big]), [])

    def test_plan_spreads_an_order_evenly_over_printers_that_can_skip_parts(self):
        plate = {"id": "p", "parts": 28, "objects": 28, "printer_model": "Snapmaker U1"}
        cc28 = {"id": "c28", "parts": 28, "printer_model": "Centauri Carbon 2"}
        cc2 = {"id": "c2", "parts": 2, "printer_model": "Centauri Carbon 2"}
        u1 = lambda n: {"id": "u%d" % n, "model": "Snapmaker U1", "kind": core.KIND_MOONRAKER}
        cc = lambda n: {"id": "c%d" % n, "model": "Centauri Carbon 2", "kind": core.KIND_CC2}
        counts = lambda plan: [(x["printer_id"], x["parts"]) for x in plan]
        self.assertEqual(counts(core.plan_order(10, [u1(1), u1(2), u1(3)], [plate])), [("u1", 4), ("u2", 3), ("u3", 3)])
        # A CC2 with only the full plate would print 28 for a share of 2: left out, the U1s take it all.
        self.assertEqual(counts(core.plan_order(10, [cc(1), cc(2), u1(1), u1(2), u1(3)], [plate, cc28])), [("u1", 4), ("u2", 3), ("u3", 3)])
        # With a small CC2 plate kept, it is two per printer.
        self.assertEqual(counts(core.plan_order(10, [cc(1), cc(2), u1(1), u1(2), u1(3)], [plate, cc28, cc2])),
                         [("c1", 2), ("c2", 2), ("u1", 2), ("u2", 2), ("u3", 2)])
        self.assertEqual(counts(core.plan_order(100, [u1(1), u1(2)], [plate])), [("u1", 28), ("u2", 28)])   # capped by the plate
        self.assertEqual(counts(core.plan_order(1, [u1(1), u1(2)], [plate])), [("u1", 1)])

    def test_order_is_worked_off_and_remembered(self):
        self._slice_and_save()                                        # Rail clip x24 for the U1
        self._status(self.p_u1)
        self.app.handle({"command": "order_save", "item": "Rail clip", "quantity": 40, "toolhead": 1, "note": "black"})
        (order,) = self.app.snapshot()["orders"]
        self.assertEqual((order["remaining"], order["in_library"], order["toolhead"]), (40, True, 1))
        self.app.handle({"command": "order_plan", "id": order["id"]})
        plan = [post for post in self.posts if post.get("command") == "order_plan"][-1]
        self.assertEqual([(x["printer_id"], x["parts"]) for x in plan["plan"]], [(self.p_u1["id"], 24)])
        self.app.handle({"command": "order_send", "id": order["id"], "picks": plan["plan"], "options": {}})
        self.assertTrue(wait_for(lambda: self.app.snapshot()["orders"][0]["printing"] == 24))
        self.assertEqual(self.u1.start_options[-1]["options"]["map_table"][1], [1, 0])          # the order's toolhead
        self.assertEqual(self.app.snapshot()["orders"][0]["remaining"], 16)
        self.assertEqual(self.app.snapshot()["order_plates"][self.p_u1["id"]]["parts"], 24)
        self.app.handle({"command": "order_plan", "id": order["id"]})                            # that printer is taken now
        self.assertEqual([post for post in self.posts if post.get("command") == "order_plan"][-1]["plan"], [])
        self.app.handle({"command": "bed_cleared", "id": self.p_u1["id"], "good": 22})          # two came out bad
        order = self.app.snapshot()["orders"][0]
        self.assertEqual((order["good"], order["printing"], order["remaining"]), (22, 0, 18))
        again = FarmApp(self.folder.name, lambda message: None).snapshot()["orders"][0]          # survives a restart
        self.assertEqual((again["good"], again["remaining"], again["note"]), (22, 18, "black"))
        self.app.handle({"command": "order_delete", "id": order["id"]})
        self.assertEqual(self.app.snapshot()["orders"], [])

    def test_a_failed_start_does_not_count_toward_the_order(self):
        self._slice_and_save()
        self._status(self.p_u1)
        self.app.handle({"command": "order_save", "item": "Rail clip", "quantity": 24, "toolhead": 4})   # toolhead 4 is empty
        order = self.app.snapshot()["orders"][0]
        self.app.handle({"command": "order_send", "id": order["id"], "options": {},
                         "picks": [{"printer_id": self.p_u1["id"], "entry_id": self.app.library()[0]["id"]}]})
        self.assertTrue(wait_for(lambda: self.app.sends.get(self.p_u1["id"], {}).get("ok") is False))
        self.assertEqual(self.app.snapshot()["orders"][0]["printing"], 0)
        self.assertEqual(self.u1.started, [])

    def test_open_at_start_setting_defaults_on_and_is_remembered(self):
        self.assertTrue(self.app.snapshot()["auto_open"])
        self.app.handle({"command": "set_auto_open", "on": False})
        self.assertFalse(self.app.snapshot()["auto_open"])
        again = core.PrinterStore(self.app.store.path)
        self.assertFalse(again.auto_open)
        self.assertEqual(len(again.printers), 2)
        self.app.handle({"command": "set_auto_open", "on": True})
        self.assertTrue(core.PrinterStore(self.app.store.path).auto_open)

    def test_open_at_start_is_not_retried_after_an_attempt_that_never_finished(self):
        self.assertTrue(core.auto_open_begin(self.folder.name))
        self.assertFalse(core.auto_open_begin(self.folder.name))     # the earlier attempt left its mark
        core.auto_open_done(self.folder.name)
        self.assertTrue(core.auto_open_begin(self.folder.name))
        core.auto_open_done(self.folder.name)
        core.auto_open_done(self.folder.name)                        # removing twice is harmless

    def test_saving_into_a_folder_that_does_not_exist_yet_makes_it(self):
        self.app.handle({"command": "library_folder", "action": "add", "name": "Clips"})
        path = write(self.tmp.name, ".1.0.gcode", U1_GCODE)
        self.app.on_slice_started(); self.app.on_gcode_exported(path, "")
        job = self.app.jobs[0]
        self.assertTrue(wait_for(lambda: job["ready"] or job["error"]))
        self.app.handle({"command": "library_save", "job_id": job["id"], "item": "Rail clip", "parts": 24,
                         "folder": "Clips / Brand new"})
        self.assertEqual(self.app.library()[0]["folder"], "Clips / Brand new")
        self.assertIn("Clips / Brand new", self.app.snapshot()["folders"])

    def _two_plates(self):
        jobs = []
        for index in (0, 1):
            path = write(self.tmp.name, ".1.%d.gcode" % index, U1_GCODE + "; plate %d\n" % index)
            self.app.on_slice_started(); self.app.on_gcode_exported(path, "")
            job = self.app.jobs[0]
            self.assertTrue(wait_for(lambda: job["ready"] or job["error"]))
            jobs.append(job)
        return jobs

    def test_save_all_keeps_every_plate_with_its_own_name_and_folder(self):
        first, second = self._two_plates()
        self.app.handle({"command": "library_save_all", "rows": [
            {"job_id": first["id"], "item": "Basket small", "parts": 1, "folder": "Baskets / U1"},
            {"job_id": second["id"], "item": "Basket large", "parts": 2, "folder": ""}]})
        result = [post for post in self.posts if post.get("command") == "library_result"][-1]
        self.assertTrue(result["ok"], result)
        kept = {e["item"]: e for e in self.app.library()}
        self.assertEqual(kept["Basket small"]["folder"], "Baskets / U1")
        self.assertEqual(kept["Basket large"]["folder"], "")
        self.assertEqual(kept["Basket large"]["parts"], 2)
        views = {view["plate"]: view for view in self.app.snapshot()["jobs"]}
        self.assertEqual(views[1]["saved_as"], "Basket small")       # the panel marks them as saved
        self.assertEqual(views[2]["saved_as"], "Basket large")

    def test_save_all_saves_nothing_when_one_row_is_wrong(self):
        first, second = self._two_plates()
        self.app.handle({"command": "library_save_all", "rows": [
            {"job_id": first["id"], "item": "Basket small", "parts": 1, "folder": ""},
            {"job_id": second["id"], "item": "", "parts": 2, "folder": ""}]})
        result = [post for post in self.posts if post.get("command") == "library_result"][-1]
        self.assertFalse(result["ok"])
        self.assertIn("Plate 2", result["text"])
        self.assertEqual(self.app.library(), [])

    def test_a_sliced_plate_reports_its_labelled_parts_and_clear_drops_one_type_only(self):
        first, second = self._two_plates()
        view = self.app.snapshot()["jobs"][0]
        self.assertEqual(view["objects"], core.count_objects(first["path"]))
        self.assertEqual(view["saved_as"], "")
        self.app.handle({"command": "library_save", "job_id": first["id"], "item": "Kept", "parts": 3})
        self.app.handle({"command": "clear_slices", "key": first["key"]})
        self.assertEqual(self.app.jobs, [])
        self.assertFalse(os.path.exists(first["path"]))
        self.assertEqual(len(self.app.library()), 1)                 # the kept plate is untouched

    def test_a_second_window_does_not_remove_the_first_windows_sliced_plates(self):
        first, second = self._two_plates()
        other = FarmApp(self.folder.name, [].append, default_name=lambda: "x.stl")     # another window starts
        self.assertTrue(os.path.exists(first["path"]))
        self.assertTrue(os.path.exists(second["path"]))
        self.assertNotEqual(other.slice_dir, self.app.slice_dir)
        # Copies nobody touched for longer than the keep time are dropped, folder and all.
        other._clear_slice_dir(now=time.time() + 13 * 3600)
        self.assertFalse(os.path.exists(first["path"]))
        self.assertFalse(os.path.exists(self.app.slice_dir))

    def test_run_log_records_a_started_print_and_how_it_ended(self):
        self._slice_and_save(parts=24)
        entry = self.app.library()[0]
        self._send(entry["id"], [self.p_u1])
        self.assertTrue(wait_for(lambda: (self.app.sends.get(self.p_u1["id"]) or {}).get("started")))
        run = self.app.runs.runs[-1]
        self.assertEqual((run["printer"], run["item"], run["parts"], run["source"]), ("U1 #1", "Rail clip", 24, "library"))
        self.assertIsNone(run["result"])
        # another window sees it, and an ending is matched by printer and file
        other = core.RunLog(self.app.runs.path)
        self.assertEqual(len(other.runs), 1)
        self.assertFalse(other.end(self.p_u1["id"], "finished", "something_else.gcode"))
        self.assertTrue(other.end(self.p_u1["id"], "finished", run["file"]))
        self.assertFalse(other.end(self.p_u1["id"], "cancelled", run["file"]))      # already closed
        self.assertTrue(self.app.runs.good(self.p_u1["id"], 22))
        self.app.handle({"command": "runs"})
        shown = [post for post in self.posts if post.get("command") == "runs"][-1]
        self.assertEqual(shown["runs"][0]["result"], "finished")
        self.assertEqual(shown["runs"][0]["good"], 22)
        self.assertEqual(shown["printers"], [{"name": "U1 #1", "started": 1, "finished": 1, "failed": 0}])

    def test_run_result_reads_how_a_print_ended(self):
        printing = {"state": "printing", "progress": 40}
        self.assertEqual(core.run_result(printing, {"state": "idle", "last_result": "cancelled"}), "cancelled")
        self.assertEqual(core.run_result(printing, {"state": "idle", "last_result": "complete"}), "finished")
        self.assertEqual(core.run_result({"state": "printing", "progress": 100}, {"state": "idle"}), "finished")
        self.assertEqual(core.run_result(printing, {"state": "idle"}), "stopped")
        self.assertEqual(core.run_result(printing, {"state": "error"}), "error")
        self.assertIsNone(core.run_result(printing, {"state": "printing", "progress": 41}))
        self.assertIsNone(core.run_result(None, {"state": "idle"}))
        self.assertIsNone(core.run_result({"state": "idle"}, {"state": "idle"}))

    def test_report_details_leave_out_names_addresses_and_codes(self):
        self.app.handle({"command": "report"})
        text = [post for post in self.posts if post.get("command") == "report"][-1]["text"]
        self.assertIn("Farm Send", text)
        self.assertIn("Snapmaker U1", text)
        self.assertIn("Centauri Carbon 2", text)
        for secret in ("abc123", self.u1.address, self.cc2.address, "U1 #1", "CC1"):
            self.assertNotIn(secret, text)

    def test_order_input_is_checked_and_toolhead_names_are_kept(self):
        for bad in ({"item": "", "quantity": 5}, {"item": "X", "quantity": 0}, {"item": "X", "quantity": "lots"}):
            self.app.handle(dict({"command": "order_save"}, **bad))
        self.assertEqual(self.app.snapshot()["orders"], [])
        self.app.handle({"command": "save_toolhead_names", "names": ["Black", "White", "Gray"]})
        self.assertEqual(FarmApp(self.folder.name, lambda message: None).snapshot()["toolhead_names"], ["Black", "White", "Gray", ""])

    def test_two_windows_do_not_overwrite_each_other(self):
        other = FarmApp(self.folder.name, lambda message: None)       # a second OrcaSlicer window, opened earlier
        added = self.app.store.upsert({"name": "U1 #9", "kind": core.KIND_MOONRAKER, "address": "192.0.2.9", "model": "Snapmaker U1"})
        other.store.set_cleanup({"mode": "count", "keep": 7})         # it saves a different setting
        names = lambda store: sorted(p["name"] for p in store.printers)
        self.assertIn("U1 #9", names(other.store))                    # ... without dropping the new printer
        self.app.handle({"command": "order_save", "item": "A", "quantity": 3})
        other.handle({"command": "order_save", "item": "B", "quantity": 4})
        self.assertEqual(sorted(o["item"] for o in self.app.snapshot()["orders"]), ["A", "B"])
        self.assertEqual(self.app.store.cleanup["keep"], 7)
        self._slice_and_save()
        self.assertEqual(len(other.snapshot()["library"]), 1)         # a plate kept in one window shows in the other
        self.assertIsNotNone(other.store.get(added["id"]))

    def test_folders_and_editing_a_kept_plate(self):
        path = write(self.tmp.name, ".1.0.gcode", U1_GCODE)
        self.app.on_slice_started(); self.app.on_gcode_exported(path, "")
        job = self.app.jobs[0]
        self.assertTrue(wait_for(lambda: job["ready"]))
        self.app.handle({"command": "library_save", "job_id": job["id"], "item": "Rail clip", "parts": 24, "folder": "  Fence   clips "})
        (view,) = self.app.snapshot()["library"]
        self.assertEqual(view["folder"], "Fence clips")
        self.app.handle({"command": "library_edit", "id": view["id"], "item": "Rail clip v2", "parts": 20, "folder": "Deck"})
        (view,) = FarmApp(self.folder.name, lambda message: None).snapshot()["library"]      # written to disk
        self.assertEqual((view["item"], view["parts"], view["folder"], view["printer_model"]), ("Rail clip v2", 20, "Deck", "Snapmaker U1"))
        self.app.handle({"command": "library_edit", "id": view["id"], "item": "", "parts": 20, "folder": "Deck"})
        self.assertFalse([post for post in self.posts if post.get("command") == "library_result"][-1]["ok"])
        self.assertEqual(self.app.snapshot()["library"][0]["item"], "Rail clip v2")            # a bad edit changes nothing
        with open(self.app.library()[0]["path"]) as handle:
            self.assertEqual(handle.read(), U1_GCODE)                                           # the G-code is never touched

    def test_folders_can_be_made_renamed_and_removed(self):
        folder = lambda **m: self.app.handle(dict({"command": "library_folder"}, **m))
        folder(action="add", name=" Fence  clips ")
        self.assertEqual(self.app.snapshot()["folders"], ["Fence clips"])                        # an empty folder stays
        self._slice_and_save()
        entry_id = self.app.library()[0]["id"]
        self.app.handle({"command": "library_edit", "id": entry_id, "item": "Rail clip", "parts": 24, "folder": "Fence clips"})
        folder(action="rename", name="Fence clips", new="Clips")
        self.assertEqual(self.app.snapshot()["folders"], ["Clips"])
        self.assertEqual(self.app.snapshot()["library"][0]["folder"], "Clips")                   # its plate moved with it
        self.assertEqual(FarmApp(self.folder.name, lambda message: None).snapshot()["folders"], ["Clips"])
        folder(action="add", name="")
        self.assertFalse([post for post in self.posts if post.get("command") == "library_result"][-1]["ok"])
        folder(action="remove", name="Clips")
        snap = self.app.snapshot()
        self.assertEqual((snap["folders"], len(snap["library"]), snap["library"][0]["folder"]), ([], 1, ""))   # plate kept
        self.assertTrue(os.path.isfile(self.app.library()[0]["path"]))

    def test_folders_inside_folders(self):
        folder = lambda **m: self.app.handle(dict({"command": "library_folder"}, **m))
        folders = lambda: self.app.snapshot()["folders"]
        folder(action="add", name="Clips/ Vertical /Left")
        self.assertEqual(folders(), ["Clips", "Clips / Vertical", "Clips / Vertical / Left"])    # parents come with it
        self._slice_and_save()
        plate = lambda: self.app.snapshot()["library"][0]
        self.app.handle({"command": "library_edit", "id": plate()["id"], "item": "Rail clip", "parts": 24, "folder": "Clips / Vertical / Left"})
        folder(action="rename", name="Clips / Vertical", new="Clips / Upright")                  # carries what is inside
        self.assertEqual(folders(), ["Clips", "Clips / Upright", "Clips / Upright / Left"])
        self.assertEqual(plate()["folder"], "Clips / Upright / Left")
        folder(action="rename", name="Clips", new="Clips / Upright / Deeper")                    # not into itself
        self.assertIn("into itself", [post for post in self.posts if post.get("command") == "library_result"][-1]["text"])
        folder(action="remove", name="Clips / Upright")                                          # contents move up one level
        self.assertEqual(folders(), ["Clips", "Clips / Left"])
        self.assertEqual(plate()["folder"], "Clips / Left")
        folder(action="remove", name="Clips")
        self.assertEqual((folders(), plate()["folder"]), (["Left"], "Left"))
        self.assertEqual(FarmApp(self.folder.name, lambda message: None).snapshot()["folders"], ["Left"])
        self.assertEqual(FarmApp.folder_path("a/b/c/d/e/f"), "a / b / c / d")                    # depth is capped
        self.assertTrue(os.path.isfile(self.app.library()[0]["path"]))

    def _save_labelled(self, parts=4):
        """A U1 plate whose parts are labelled one by one, kept in the library."""
        labels = "".join("EXCLUDE_OBJECT_DEFINE NAME=clip_%d CENTER=1,1 POLYGON=[[0,0]]\n" % n for n in range(parts))
        path = write(self.tmp.name, ".1.0.gcode", labels + U1_GCODE)
        self.app.on_slice_started(); self.app.on_gcode_exported(path, "")
        job = self.app.jobs[0]
        self.assertTrue(wait_for(lambda: job["ready"]))
        self.app.handle({"command": "library_save", "job_id": job["id"], "item": "Clip", "parts": parts})
        self.u1.objects = ["clip_%d" % n for n in range(parts)]
        old = (core.KEEP_PARTS_POLL, core.KEEP_PARTS_WAIT)
        core.KEEP_PARTS_POLL, core.KEEP_PARTS_WAIT = 0.01, 3
        self.addCleanup(lambda: setattr(core, "KEEP_PARTS_POLL", old[0]) or setattr(core, "KEEP_PARTS_WAIT", old[1]))
        self._status(self.p_u1)
        return self.app.snapshot()["library"][0]

    def test_print_only_some_parts_of_a_plate(self):
        view = self._save_labelled(4)
        self.assertTrue(view["countable"])
        (sent,) = self._send(view["id"], [self.p_u1], keep=1)
        self.assertTrue(sent["ok"], sent)
        self.assertIn("printing 1 of 4 parts", sent["text"])
        self.assertEqual(self.u1.excluded, ["clip_1", "clip_2", "clip_3"])     # the first part is kept
        self.assertEqual(self.u1.cancels, 0)

    def test_the_whole_plate_needs_no_skips(self):
        view = self._save_labelled(4)
        (sent,) = self._send(view["id"], [self.p_u1], keep=4)
        self.assertTrue(sent["ok"], sent)
        self.assertEqual(self.u1.excluded, [])
        self.assertNotIn("parts", sent["text"])

    def test_unconfirmed_skips_cancel_the_print(self):
        view = self._save_labelled(4)
        self.u1.ignore_skips = True                                            # the printer never confirms
        (sent,) = self._send(view["id"], [self.p_u1], keep=2)
        self.assertFalse(sent["ok"])
        self.assertIn("The print was cancelled", sent["text"])
        self.assertEqual(self.u1.cancels, 1)

    def test_a_part_count_is_refused_where_it_cannot_work(self):
        self._slice_and_save()                                                 # no labelled parts in this G-code
        view = self.app.snapshot()["library"][0]
        self.assertFalse(view["countable"])
        self._status(self.p_u1)
        (sent,) = self._send(view["id"], [self.p_u1], keep=2)
        self.assertIn("cannot print a part count", sent["text"])
        labelled = self._save_labelled(4)
        (upload,) = self._send(labelled["id"], [self.p_u1], start=False, keep=2)
        self.assertIn("needs Start print", upload["text"])
        self.assertEqual(self.u1.started, [])

    def test_an_order_prints_only_what_it_needs(self):
        view = self._save_labelled(4)
        self.app.handle({"command": "order_save", "item": "Clip", "quantity": 3})
        order = self.app.snapshot()["orders"][0]
        self.app.handle({"command": "order_plan", "id": order["id"]})
        plan = [post for post in self.posts if post.get("command") == "order_plan"][-1]["plan"]
        self.assertEqual([(x["parts"], x["plate"]) for x in plan], [(3, 4)])
        self.app.handle({"command": "order_send", "id": order["id"], "picks": plan, "options": {}})
        self.assertTrue(wait_for(lambda: self.app.snapshot()["orders"][0]["printing"] == 3))
        self.assertEqual(self.u1.excluded, ["clip_3"])
        self.assertEqual(self.app.snapshot()["order_plates"][self.p_u1["id"]]["parts"], 3)
        self.assertEqual(self.app.snapshot()["orders"][0]["remaining"], 0)

    def test_every_plate_of_a_project_is_held(self):
        def export(plate, text):
            path = os.path.join(self.tmp.name, ".4242.%d.gcode" % plate)
            with open(path, "w") as handle:
                handle.write(text)
            self.app.on_slice_started(); self.app.on_gcode_exported(path, "")
            job = self.app.jobs[0]
            self.assertTrue(wait_for(lambda: job["ready"] or job["error"]), job)
            return job
        first = export(0, U1_GCODE)
        second = export(1, U1_GCODE + "; second plate\n")                    # "Slice all": one export per plate
        jobs = {view["plate"]: view for view in self.app.snapshot()["jobs"]}
        self.assertEqual(sorted(jobs), [1, 2])
        self.assertTrue(jobs[2]["chosen"] and not jobs[1]["chosen"])          # the cards send the newest slice
        self.assertEqual(self.app.snapshot()["job_for"][self.p_u1["id"]], second["id"])
        self.app.handle({"command": "choose_plate", "id": first["id"]})
        self.assertEqual(self.app.snapshot()["job_for"][self.p_u1["id"]], first["id"])
        self.app.handle({"command": "library_save", "job_id": first["id"], "item": "Left", "parts": 2})
        self.app.handle({"command": "library_save", "job_id": second["id"], "item": "Right", "parts": 2})
        self.assertEqual(sorted(view["item"] for view in self.app.snapshot()["library"]), ["Left", "Right"])
        again = export(0, U1_GCODE + "; resliced\n")                          # re-slicing plate 1 replaces plate 1 only
        self.assertEqual(sorted(view["plate"] for view in self.app.snapshot()["jobs"]), [1, 2])
        self.assertFalse(os.path.exists(first["path"]))
        self.assertTrue(os.path.exists(second["path"]) and os.path.exists(again["path"]))

    def test_a_swapped_color_is_caught_unless_confirmed(self):
        self._slice_and_save()                                               # sliced for toolhead 2, white
        entry_id = self.app.library()[0]["id"]
        self.u1.tools["filament_color_rgba"][1] = "FFD400FF"                 # someone loaded yellow there
        self._status(self.p_u1)
        (sent,) = self._send(entry_id, [self.p_u1])
        self.assertIn("toolhead 2 holds a different color", sent["text"])
        self.assertEqual(self.u1.started, [])
        (sent,) = self._send(entry_id, [self.p_u1], options={"any_color": True})
        self.assertTrue(sent["ok"], sent)
        self.assertEqual(core.color_gap("#000000", "#101010"), 48)           # near-black still counts as black
        self.assertEqual(core.color_gap("#FFFFFF", None), 0)

    def test_a_print_cancelled_while_heating_leaves_the_bed_clear(self):
        self.assertIsNone(core.bed_needs_clearing(False, {"state": "printing", "extruded_mm": 0.0}))
        self.assertTrue(core.bed_needs_clearing(False, {"state": "printing", "extruded_mm": 12.5}))
        self.assertTrue(core.bed_needs_clearing(False, {"state": "printing"}))               # not reported: assume a part

    def test_toolhead_map_table(self):
        self.assertEqual(core.toolhead_map_table(None), [[0, 0], [1, 1], [2, 2], [3, 3]])
        self.assertEqual(core.toolhead_map_table({2: 1}), [[0, 0], [1, 0], [2, 2], [3, 3]])
        self.assertEqual(core.toolhead_map_table({"3": "4", 9: 1, "x": 2}), [[0, 0], [1, 1], [2, 3], [3, 3]])
        self.assertEqual(core.toolhead_problem([], 2, "PETG"), "")   # nothing reported: nothing to check


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
        folder = self.app.slice_dir            # each window keeps its sliced copies in its own folder
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

    def _stock(self, fake, ages_days):
        """Put files of the given ages on a fake printer; returns their names, oldest last."""
        names = []
        for index, days in enumerate(ages_days):
            name = "old_%02d_%dd.gcode" % (index, days)
            fake.files[name] = b"x" * 1000
            fake.file_times[name] = time.time() - days * 86400
            names.append(name)
        return names

    def _cleanup(self, printers):
        self.app.handle({"command": "cleanup", "printer_ids": [p["id"] for p in printers]})
        self.assertTrue(wait_for(lambda: all(not self.app.sends.get(p["id"], {"active": True})["active"] for p in printers)))
        return [self.app.sends[p["id"]] for p in printers]

    def test_cleanup_removes_only_old_files_on_both_printer_types(self):
        for fake in (self.u1, self.cc2):
            self._stock(fake, [1, 10, 40, 90])
        self._read(self.p_u1), self._read(self.p_cc2)
        self.app.handle({"command": "save_cleanup", "rule": {"mode": "age", "days": 30}})
        self.assertTrue(wait_for(lambda: self.app.storage.get(self.p_cc2["id"], {}).get("removable") == 2
                                 and self.app.storage.get(self.p_u1["id"], {}).get("removable") == 2))
        self.assertEqual(self.app.storage[self.p_cc2["id"]]["count"], 4)
        results = self._cleanup([self.p_u1, self.p_cc2])
        for result in results:
            self.assertTrue(result["ok"], result)
            self.assertIn("Removed 2 old files", result["text"])
        for fake in (self.u1, self.cc2):
            self.assertEqual(sorted(fake.files), ["old_00_1d.gcode", "old_01_10d.gcode"])
        self.assertEqual(self.app.storage[self.p_cc2["id"]]["removable"], 0)
        (again,) = self._cleanup([self.p_u1])
        self.assertIn("Nothing to remove", again["text"])

    def test_cleanup_keeps_the_file_being_printed_and_skips_a_busy_cc2(self):
        names = self._stock(self.u1, [50, 60, 70])
        self.u1.files["busy.gcode"] = b"x"
        self.u1.file_times["busy.gcode"] = time.time() - 80 * 86400   # old, but it is printing
        self.u1.state = "printing"
        self._stock(self.cc2, [50, 60])
        self.cc2.status_code = 2
        self._read(self.p_u1), self._read(self.p_cc2)
        u1, cc2 = self._cleanup([self.p_u1, self.p_cc2])
        self.assertTrue(u1["ok"], u1)
        self.assertEqual(sorted(self.u1.files), ["busy.gcode"])
        self.assertFalse(cc2["ok"])
        self.assertIn("busy", cc2["text"])
        self.assertEqual(len(self.cc2.files), 2)
        self.assertEqual(self.cc2.delete_calls, 0)
        self.assertEqual(len(names), 3)

    def test_cleanup_by_count_pages_through_a_full_cc2_and_reports_stuck_files(self):
        names = self._stock(self.cc2, list(range(1, 251)))        # 250 files: three pages of 100
        self.cc2.stuck = {names[-1]}                              # the printer keeps one and still says ok
        self._read(self.p_cc2)
        self.app.handle({"command": "save_cleanup", "rule": {"mode": "count", "keep": 20}})
        self.assertTrue(wait_for(lambda: self.app.storage.get(self.p_cc2["id"], {}).get("removable") == 230))
        self.assertTrue(self.app.storage[self.p_cc2["id"]]["many"])
        (result,) = self._cleanup([self.p_cc2])
        self.assertFalse(result["ok"])
        self.assertIn("Removed 229 old files", result["text"])
        self.assertIn("1 could not be removed", result["text"])
        self.assertEqual(sorted(self.cc2.files), sorted(names[:20] + [names[-1]]))
        self.assertEqual(self.cc2.delete_calls, 23)               # ten files per request, one session

    def test_automatic_cleanup_is_off_by_default_and_runs_once_a_day(self):
        self._stock(self.u1, [40, 50])
        self._read(self.p_u1)
        self.app.check_storage(force=True, only=self.p_u1["id"])
        self.assertTrue(wait_for(lambda: self.app.storage.get(self.p_u1["id"], {}).get("removable") == 2))
        time.sleep(0.3)
        self.assertEqual(len(self.u1.files), 2)                   # counted, not touched
        self.app.handle({"command": "save_cleanup", "rule": {"mode": "age", "days": 30, "auto": True}})
        self.assertTrue(wait_for(lambda: not self.u1.files))
        self.assertTrue(wait_for(lambda: any("removed 2 old files" in text for _, text in self.notes)))
        self._stock(self.u1, [45])                                # more old files the same day: left alone
        self.app.check_storage(force=True, only=self.p_u1["id"])
        self.assertTrue(wait_for(lambda: self.app.storage[self.p_u1["id"]].get("removable") == 1))
        time.sleep(0.3)
        self.assertEqual(len(self.u1.files), 1)

    def test_cleanup_rules(self):
        now = 1000 * 86400.0
        files = [{"name": "f%d.gcode" % d, "modified": now - d * 86400, "size": 1} for d in (1, 5, 31, 60)]
        files.append({"name": "undated.gcode", "modified": 0, "size": 1})
        pick = lambda rule, protect=(): [f["name"] for f in core.files_to_clean(files, rule, protect, now)]
        self.assertEqual(pick({"mode": "age", "days": 30}), ["f60.gcode", "f31.gcode"])       # oldest first, undated kept
        self.assertEqual(pick({"mode": "age", "days": 30}, ["sub/f60.gcode"]), ["f31.gcode"])
        self.assertEqual(pick({"mode": "count", "keep": 2}), ["undated.gcode", "f60.gcode", "f31.gcode"])
        self.assertEqual(pick({"mode": "count", "keep": 99}), [])
        self.assertEqual(core.clean_rule({"mode": "nonsense", "days": 0, "keep": "x"}),
                         {"mode": "age", "days": 1, "keep": 50, "auto": False})
        self.assertEqual(core.rule_text({"mode": "count", "keep": 20}), "keep the newest 20 files")
        self.assertEqual(core.storage_summary(files)["count"], 5)

    def test_cleanup_rule_is_saved(self):
        self.app.handle({"command": "save_cleanup", "rule": {"mode": "count", "keep": 12}})
        other = FarmApp(self.folder.name, lambda message: None)
        self.assertEqual((other.store.cleanup["mode"], other.store.cleanup["keep"]), ("count", 12))
        self.assertEqual(self.app.snapshot()["cleanup"]["text"], "keep the newest 12 files")

    def test_plate_for_another_nozzle_size_is_blocked(self):
        self.slice(U1_GCODE.replace("; printer_model = Snapmaker U1", "; nozzle_diameter = 0.6,0.6,0.6,0.6\n; printer_model = Snapmaker U1"))
        self.assertEqual(self.app.jobs[0]["meta"].get("nozzle_diameter"), "0.6,0.6,0.6,0.6")
        self.p_u1 = self.app.store.upsert(dict(self.p_u1, nozzle=0.4))      # as Import records it
        blocked, unknown = self.send([self.p_u1, self.p_u1b], True)         # U1 #2 has no nozzle size on record
        self.assertFalse(blocked["ok"])
        self.assertIn("0.6 mm nozzle", blocked["text"])
        self.assertIn("0.4 mm", blocked["text"])
        self.assertTrue(unknown["ok"], unknown)
        self.assertEqual(self.u1.started, [])
        self.assertEqual(len(self.u1b.started), 1)

    def test_nozzle_helpers_and_import_fills_missing_size(self):
        self.assertEqual(core.first_number("0.4,0.4"), 0.4)
        self.assertEqual(core.first_number(["0.6"]), 0.6)
        self.assertIsNone(core.first_number(""))
        self.assertEqual(core.nozzle_mismatch("0.4", 0.4), "")
        self.assertEqual(core.nozzle_mismatch(None, 0.4), "")
        self.assertIn("0.6 mm nozzle", core.nozzle_mismatch("0.6;0.6", "0.4"))
        live = [{"name": "Snapmaker U1 (0.4 nozzle) - #1", "print_host": self.p_u1["address"], "host_type": "moonraker",
                 "printer_model": "Snapmaker U1", "inherits": "Snapmaker U1 (0.4 nozzle)", "nozzle_diameter": ["0.4", "0.4", "0.4", "0.4"]}]
        self.app._list_presets = lambda: live
        self.app.handle({"command": "import_presets"})
        self.assertEqual(self.app.store.get(self.p_u1["id"])["nozzle"], 0.4)
        self.assertEqual(len(self.app.store.printers), 3)                   # nothing added twice

    def test_plain_moonraker_start_says_options_were_not_applied(self):
        self.slice()
        (sent,) = self.send([self.p_u1], True)                              # the fake is a plain Moonraker printer
        self.assertTrue(sent["ok"], sent)
        self.assertIn("does not accept start options", sent["text"])

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

    def test_leftover_copies_are_cleared_at_startup_only_once_they_are_old(self):
        self.slice(U1_GCODE)
        self.assertEqual(len(self.copies()), 1)
        later = FarmApp(self.folder.name, lambda message: None)          # another window, or a restart soon after
        self.assertEqual(len(self.copies()), 1)
        old = time.time() - 13 * 3600
        os.utime(self.app.slice_dir, (old, old))
        loose = os.path.join(self.folder.name, "slices", "left-by-an-older-version.gcode")
        with open(loose, "w") as handle:
            handle.write("G28\n")
        os.utime(loose, (old, old))
        FarmApp(self.folder.name, lambda message: None)
        self.assertEqual(self.copies(), [])
        self.assertFalse(os.path.exists(loose))
        self.assertTrue(later is not None)

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
