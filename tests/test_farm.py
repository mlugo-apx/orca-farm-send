import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

import core  # noqa: E402
from app import FarmApp  # noqa: E402
from fakes import FakeCC2, FakeMoonraker  # noqa: E402

U1_GCODE = "; HEADER_BLOCK_START\n; total layer number: 12\n; total filament used [g] = 3.50\n" \
           "; estimated printing time (normal mode) = 1h 2m 3s\n; HEADER_BLOCK_END\nG28\nG1 X1\n" \
           "; CONFIG_BLOCK_START\n; filament_type = PETG;PETG\n; post_process = \n; printer_model = Snapmaker U1\n" \
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

    def test_start_missing_file_is_an_error(self):
        with self.assertRaises(core.FarmError):
            self.client.start_print("nope.gcode")

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


class AppTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
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

    def slice(self, text=U1_GCODE, name="t.gcode", model_name=""):
        path = write(self.folder.name, name, text)
        self.app.on_slice_started()
        self.app.on_gcode_exported(path, model_name)
        self.assertTrue(wait_for(lambda: self.app.jobs[0]["ready"] or self.app.jobs[0]["error"]))
        return self.app.jobs[0]

    def send(self, job, printers, start):
        self.app.handle({"command": "send", "job_id": job["id"], "printer_ids": [p["id"] for p in printers], "start": start})
        self.assertTrue(wait_for(lambda: all(not self.app.sends.get(p["id"], {"active": True})["active"] for p in printers)))
        return [self.app.sends[p["id"]] for p in printers]

    def test_capture_and_view(self):
        job = self.slice()
        view = self.app.snapshot()["job"]
        self.assertTrue(view["ready"])
        self.assertEqual(view["display_name"], "widget")
        self.assertIn("1h02m", view["summary"])
        self.assertEqual(view["matches"], {self.p_u1["id"]: True, self.p_u1b["id"]: True, self.p_cc2["id"]: False})
        self.assertFalse(self.app.snapshot()["slicing"])
        self.assertEqual(job["meta"]["printer_model"], "Snapmaker U1")

    def test_upload_only_does_not_start(self):
        (result,) = self.send(self.slice(), [self.p_u1], start=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.u1.files), 1)
        name = next(iter(self.u1.files))
        self.assertRegex(name, r"^widget_PETG_1h02m_\d{8}-\d{6}\.gcode$")
        self.assertEqual(self.u1.files[name].decode(), U1_GCODE)
        self.assertEqual(self.u1.started, [])

    def test_upload_and_print(self):
        (result,) = self.send(self.slice(), [self.p_u1], start=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.u1.started, list(self.u1.files))
        self.assertTrue(any(level == "info" for level, _ in self.notes))

    def test_mismatch_is_blocked_before_any_network_call(self):
        (result,) = self.send(self.slice(), [self.p_cc2], start=True)
        self.assertFalse(result["ok"])
        self.assertIn("Blocked", result["text"])
        self.assertEqual(self.cc2.files, {})
        self.assertEqual(self.cc2.started, [])

    def test_cc2_upload_and_print(self):
        (result,) = self.send(self.slice(CC2_GCODE), [self.p_cc2], start=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.cc2.started, list(self.cc2.files))
        self.assertEqual(self.u1.files, {})

    def test_print_refused_when_busy_but_file_stays(self):
        self.u1.state = "printing"
        (result,) = self.send(self.slice(), [self.p_u1], start=True)
        self.assertFalse(result["ok"])
        self.assertIn("not started", result["text"])
        self.assertEqual(len(self.u1.files), 1)
        self.assertEqual(self.u1.started, [])

    def test_batch_uses_one_filename(self):
        results = self.send(self.slice(), [self.p_u1, self.p_u1b, self.p_cc2], start=True)
        self.assertEqual([r["ok"] for r in results], [True, True, False])
        self.assertEqual(list(self.u1.files), list(self.u1b.files))
        self.assertEqual(self.cc2.files, {})

    def test_offline_printer_fails_cleanly_and_others_succeed(self):
        self.u1b.close()
        results = self.send(self.slice(), [self.p_u1, self.p_u1b], start=False)
        self.assertEqual([r["ok"] for r in results], [True, False])
        self.assertIn("Cannot reach", results[1]["text"])

    def test_reslice_replaces_job_and_stale_send_is_refused(self):
        job = self.slice()
        time.sleep(0.02)
        newer = self.slice(U1_GCODE + "; changed\n")
        self.assertEqual(len(self.app.jobs), 1)
        (result,) = self.send(job, [self.p_u1], start=False)
        self.assertFalse(result["ok"])
        self.assertEqual(self.u1.files, {})
        (result,) = self.send(newer, [self.p_u1], start=False)
        self.assertTrue(result["ok"])

    def test_file_changed_on_disk_is_refused(self):
        job = self.slice()
        time.sleep(0.02)
        write(self.folder.name, "t.gcode", U1_GCODE + "; rewritten behind our back\n")
        (result,) = self.send(job, [self.p_u1], start=True)
        self.assertFalse(result["ok"])
        self.assertIn("sliced again", result["text"])
        self.assertEqual(self.u1.started, [])

    def test_no_model_in_gcode_cannot_be_sent(self):
        job = self.slice("G28\n; nothing useful\n")
        self.assertFalse(job["ready"])
        (result,) = self.send(job, [self.p_u1], start=False)
        self.assertFalse(result["ok"])

    def test_sending_blocked_while_slicing(self):
        job = self.slice()
        self.app.on_slice_started()
        (result,) = self.send(job, [self.p_u1], start=False)
        self.assertFalse(result["ok"])
        self.assertIn("Slicing", result["text"])

    def test_stale_flag_and_project_close(self):
        self.slice()
        self.app.mark_stale()
        self.assertTrue(self.app.snapshot()["job"]["stale"])
        self.app.project_closed()
        self.assertIsNone(self.app.snapshot()["job"])

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
        big = U1_GCODE.replace("G1 X1\n", "G1 X1\n" * 400000)
        job = self.slice(big)
        original = core.Moonraker.upload

        def slow(client, path, filename, progress=None, cancelled=None):
            def paced(sent, total):
                time.sleep(0.05)
                if progress:
                    progress(sent, total)
            return original(client, path, filename, progress=paced, cancelled=cancelled)

        core.Moonraker.upload = slow
        self.addCleanup(setattr, core.Moonraker, "upload", original)
        self.app.handle({"command": "send", "job_id": job["id"], "printer_ids": [self.p_u1["id"]], "start": True})
        self.assertTrue(wait_for(lambda: (self.app.sends.get(self.p_u1["id"]) or {}).get("percent")))
        self.app.handle({"command": "cancel_send", "id": self.p_u1["id"]})
        self.assertTrue(wait_for(lambda: not self.app.sends[self.p_u1["id"]]["active"]))
        self.assertFalse(self.app.sends[self.p_u1["id"]]["ok"])
        self.assertEqual(self.u1.started, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
