"""Farm Send application logic: jobs, printer cards, sends. No OrcaSlicer imports; the plugin
shell supplies post()/notify() and forwards panel messages and slicer events."""
# BUNDLE-STRIP-BEGIN
import os
import threading
import time
import uuid

from core import (KIND_CC2, FarmError, PrinterStore, can_start, job_filename, make_client, model_key, model_matches,
                  parse_gcode_meta, presets_to_printers, strip_model_suffix, validate_printer)
# BUNDLE-STRIP-END

MAX_JOBS = 8
STATUS_MIN_INTERVAL = 3.0
STATUS_MIN_INTERVAL_CC2 = 8.0   # each CC2 poll is a full MQTT connect + register; be gentle
BLOCKED_TEXT = "OrcaSlicer blocked network access to this printer. Approve the permission prompt, then it retries."


def _clock(when):
    return time.strftime("%H:%M", time.localtime(when))


def _short_duration(seconds):
    if seconds is None:
        return ""
    hours, minutes = int(seconds) // 3600, (int(seconds) % 3600) // 60
    return ("%dh%02dm" % (hours, minutes)) if hours else ("%dm" % max(1, minutes))


class FarmApp:
    def __init__(self, storage_dir, post, notify=None, preset_dirs=(), default_name=None, spawn=None,
                 read_presets=None, apply_presets=None):
        self.store = PrinterStore(os.path.join(storage_dir, "farm_send_printers.json"))
        self._post = post
        self._notify = notify or (lambda level, text: None)
        self.preset_dirs = list(preset_dirs)
        self._default_name = default_name or (lambda: "")
        self._spawn = spawn or self._spawn_thread
        self.lock = threading.RLock()
        self.jobs = []            # newest first
        self.selected_job = None
        self.slicing = False
        self.status = {}          # printer id -> normalized status
        self._status_at = {}
        self._status_busy = set()
        self.sends = {}           # printer id -> {active, text, percent, ok}
        self._cancel = {}
        # Slicing presets. read_presets() must run on the UI thread and returns the current selection
        # {"model", "printer", "process", "filaments"}; apply_presets(combo) switches to a saved one
        # and must run OFF the UI thread, because it waits for OrcaSlicer's own selection dialogs.
        self._read_presets = read_presets
        self._apply_presets = apply_presets
        self.current = None
        self.combo_note = {"text": "", "ok": True, "busy": False}

    @staticmethod
    def _spawn_thread(target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        return thread

    def post(self, message):
        try:
            self._post(message)
        except Exception:
            pass

    # ---- slicer events (called from the slicing worker thread: never touch the UI here) ----------
    def on_slice_started(self):
        with self.lock:
            self.slicing = True
        self.post({"command": "state", "slicing": True})

    def on_slice_done(self):
        with self.lock:
            self.slicing = False
        self.post_state()

    def on_gcode_exported(self, path, name=""):
        if not path or not path.lower().endswith(".gcode"):
            return
        job = {"id": uuid.uuid4().hex[:10], "path": path, "name": name or "", "captured_at": time.time(),
               "ready": False, "error": "", "stale": False, "meta": {}, "mtime": None, "size": None}
        with self.lock:
            self.slicing = False
            # A re-slice of the same plate reuses the same temp file: the old entry is now wrong.
            self.jobs = [existing for existing in self.jobs if existing["path"] != path]
            self.jobs.insert(0, job)
            del self.jobs[MAX_JOBS:]
            self.selected_job = job["id"]
        self.post_state()
        self._spawn(self._read_job, job)

    def _read_job(self, job):
        try:
            stat = os.stat(job["path"])
            meta = parse_gcode_meta(job["path"])
            with self.lock:
                job["mtime"], job["size"], job["meta"] = stat.st_mtime, stat.st_size, meta
                if not meta.get("printer_model"):
                    job["error"] = "The sliced G-code does not name a printer model, so it cannot be matched to a printer."
                else:
                    job["ready"] = True
        except PermissionError:
            with self.lock:
                job["error"] = "OrcaSlicer blocked reading the sliced G-code. Approve the permission prompt and slice again."
        except OSError as error:
            with self.lock:
                job["error"] = "Cannot read the sliced G-code: %s" % (error.strerror or error)
        self.post_state()

    def mark_stale(self):
        changed = False
        with self.lock:
            for job in self.jobs:
                if not job["stale"]:
                    job["stale"] = changed = True
        if changed:
            self.post_state()

    def project_closed(self):
        with self.lock:
            self.jobs, self.selected_job, self.slicing = [], None, False
        self.post_state()

    # ---- state for the panel ---------------------------------------------------------------------
    def _current_job(self):
        for job in self.jobs:
            if job["id"] == self.selected_job:
                return job
        return self.jobs[0] if self.jobs else None

    def _job_base_name(self, job):
        if not job["name"]:
            try:
                job["name"] = self._default_name() or ""
            except Exception:
                job["name"] = ""
        job["name"] = strip_model_suffix(job["name"])
        return job["name"] or "plate"

    def _job_view(self, job):
        meta = job["meta"]
        bits = []
        if meta.get("time_s") is not None:
            bits.append(_short_duration(meta["time_s"]))
        if meta.get("filament_g") is not None:
            bits.append("%.1f g" % meta["filament_g"])
        if meta.get("layers"):
            bits.append("%d layers" % meta["layers"])
        if meta.get("filament_type"):
            bits.append(meta["filament_type"].split(";")[0])
        if meta.get("size"):
            bits.append("%.1f MB" % (meta["size"] / 1048576.0))
        return {
            "id": job["id"], "ready": job["ready"], "error": job["error"], "stale": job["stale"],
            "display_name": self._job_base_name(job), "summary": " · ".join(bits),
            "printer_model": meta.get("printer_model", ""), "sliced_at": _clock(job["captured_at"]),
            "post_process": bool((meta.get("post_process") or "").strip()),
            "presets": " \u00b7 ".join(part for part in (meta.get("process_preset"), meta.get("filament_preset")) if part),
            "matches": {p["id"]: model_matches(meta.get("printer_model"), p.get("model")) for p in self.store.printers},
        }

    def snapshot(self):
        with self.lock:
            job = self._current_job()
            return {
                "command": "state",
                "printers": [{k: p.get(k, "") for k in ("id", "name", "kind", "address", "model", "access_code", "api_key")}
                             for p in self.store.printers],
                "job": self._job_view(job) if job else None,
                "jobs": [self._job_view(j) for j in self.jobs if j["ready"]],
                "slicing": self.slicing,
                "status": dict(self.status),
                "sends": dict(self.sends),
                "combos": self._combo_views(),
                "current": dict(self.current) if self.current else None,
                "combo_note": dict(self.combo_note),
                "can_switch": self._apply_presets is not None,
            }

    def _combo_views(self):
        current = self.current or {}
        views = []
        for key, combo in sorted(self.store.combos.items(), key=lambda item: item[1].get("label", "").lower()):
            active = (current.get("printer") == combo.get("printer") and current.get("process") == combo.get("process")
                      and list(current.get("filaments") or [])[:len(combo.get("filaments") or [])] == list(combo.get("filaments") or []))
            views.append({"key": key, "label": combo.get("label", key), "printer": combo.get("printer", ""),
                          "process": combo.get("process", ""), "filaments": list(combo.get("filaments") or []),
                          "active": bool(active),
                          "printers": [p["name"] for p in self.store.printers if model_key(p.get("model")) == key]})
        return views

    def refresh_current(self):
        """Re-read OrcaSlicer's selected presets. UI thread only. Returns True when they changed."""
        if self._read_presets is None:
            return False
        try:
            current = self._read_presets()
        except Exception:
            return False
        if current != self.current:
            self.current = current
            return True
        return False

    def post_state(self):
        self.post(self.snapshot())

    # ---- panel messages (called on the UI thread: keep it quick) ------------------------------------
    def handle(self, message):
        command = (message or {}).get("command")
        handler = getattr(self, "_cmd_" + str(command), None)
        if handler is not None:
            handler(message)

    def _cmd_init(self, message):
        self.refresh_current()
        self.post_state()
        self.poll(force=True)

    def _cmd_poll(self, message):
        if self.refresh_current():
            self.post_state()
        self.poll()

    def _set_combo_note(self, text, ok=True, busy=False):
        with self.lock:
            self.combo_note = {"text": text, "ok": ok, "busy": busy}

    def _cmd_save_combo(self, message):
        self.refresh_current()
        current = self.current or {}
        try:
            if not current.get("model"):
                raise FarmError("Select a printer preset in OrcaSlicer first")
            self.store.set_combo(current["model"], current.get("printer"), current.get("process"), current.get("filaments"))
            self._set_combo_note("Saved the current presets for %s." % current["model"])
        except FarmError as error:
            self._set_combo_note(str(error), ok=False)
        except OSError as error:
            self._set_combo_note("Could not save: %s" % (error.strerror or error), ok=False)
        self.post_state()

    def _cmd_delete_combo(self, message):
        try:
            self.store.remove_combo(message.get("key"))
            self._set_combo_note("")
        except OSError as error:
            self._set_combo_note("Could not save: %s" % (error.strerror or error), ok=False)
        self.post_state()

    def _cmd_load_combo(self, message):
        combo = self.store.combos.get(message.get("key"))
        if combo is None or self._apply_presets is None:
            return
        with self.lock:
            if self.combo_note.get("busy"):
                return
            self.combo_note = {"text": "Switching presets to %s..." % combo.get("label", ""), "ok": True, "busy": True}
        self.post_state()
        self._spawn(self._load_combo_worker, dict(combo))

    def _load_combo_worker(self, combo):
        try:
            skipped = self._apply_presets(combo) or []
            text = "Presets set for %s. Slice the plate." % combo.get("label", "")
            if skipped:
                text += " Not applied: %s." % "; ".join(skipped)
            self._set_combo_note(text, ok=not skipped)
        except Exception as error:
            self._set_combo_note("Presets not switched: %s" % error, ok=False)
        # The selection is re-read on the next poll, which runs on the UI thread.
        self.post_state()

    def _cmd_select_job(self, message):
        with self.lock:
            if any(job["id"] == message.get("job_id") for job in self.jobs):
                self.selected_job = message.get("job_id")
        self.post_state()

    def _cmd_test_printer(self, message):
        try:
            printer = validate_printer(dict(message.get("printer") or {}, model=(message.get("printer") or {}).get("model") or "?"))
            info = make_client(printer, timeout=4).identify()
            model = info.get("model", "")
            text = "Connected: %s%s" % (model or "printer", (" · firmware " + info["firmware"]) if info.get("firmware") else "")
            self.post({"command": "form_result", "ok": True, "text": text, "model": model})
        except FarmError as error:
            self.post({"command": "form_result", "ok": False, "text": str(error)})
        except PermissionError:
            self.post({"command": "form_result", "ok": False, "text": "OrcaSlicer blocked the connection. Approve the prompt and test again."})

    def _cmd_save_printer(self, message):
        try:
            printer = self.store.upsert(message.get("printer") or {})
        except FarmError as error:
            self.post({"command": "form_result", "ok": False, "text": str(error)})
            return
        except OSError as error:
            self.post({"command": "form_result", "ok": False, "text": "Could not save: %s" % (error.strerror or error)})
            return
        with self.lock:
            self.status.pop(printer["id"], None)
            self._status_at.pop(printer["id"], None)
        self.post({"command": "form_result", "ok": True, "saved": True, "text": "Saved"})
        self.post_state()
        self.poll(force=True)

    def _cmd_delete_printer(self, message):
        printer_id = message.get("id")
        with self.lock:
            if self.sends.get(printer_id, {}).get("active"):
                return
            self.store.remove(printer_id)
            for table in (self.status, self._status_at, self.sends):
                table.pop(printer_id, None)
        self.post_state()

    def _cmd_import_presets(self, message):
        added, seen = [], {p["address"] for p in self.store.printers}
        for folder in self.preset_dirs:
            for candidate in presets_to_printers(folder):
                if candidate["address"] in seen:
                    continue
                try:
                    self.store.upsert(candidate)
                except (FarmError, OSError):
                    continue
                seen.add(candidate["address"])
                added.append("%s (%s)" % (candidate["name"], candidate["address"]))
        if added:
            text = "Added %d printer%s from your printer presets:" % (len(added), "" if len(added) == 1 else "s")
        else:
            text = ("No new printers found. Import reads user printer presets that have a printer address set, "
                    "for Centauri Carbon 2 and Snapmaker printers.")
        self.post({"command": "import_result", "text": text, "added": added})
        self.post_state()
        self.poll(force=True)

    def _cmd_cancel_send(self, message):
        event = self._cancel.get(message.get("id"))
        if event is not None:
            event.set()

    def _cmd_send(self, message):
        start = bool(message.get("start"))
        options = message.get("options") or {}
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == message.get("job_id")), None)
            targets = []
            for printer_id in message.get("printer_ids") or []:
                printer = self.store.get(printer_id)
                if printer is None:
                    continue
                problem = self._send_problem(job, printer)
                if problem:
                    self._set_send(printer_id, False, problem, ok=False)
                    continue
                self._cancel[printer_id] = threading.Event()
                self._set_send(printer_id, True, "Preparing...", percent=0)
                targets.append(dict(printer))
            if job is not None and targets:
                # One file name for the whole batch, fixed now so every printer gets the same one.
                meta = job["meta"]
                parts = [self._job_base_name(job)]
                if meta.get("filament_type"):
                    parts.append(meta["filament_type"].split(";")[0])
                if meta.get("time_s"):
                    parts.append(_short_duration(meta["time_s"]))
                filename = job_filename("_".join(parts))
                snapshot = {"path": job["path"], "mtime": job["mtime"], "size": job["size"], "name": filename}
        for printer in targets:
            self._spawn(self._send_worker, printer, snapshot, start, options)

    def _send_problem(self, job, printer):
        if self.slicing:
            return "Slicing is in progress"
        if job is None or not job["ready"]:
            return "That sliced plate is no longer available. Slice again."
        if not model_matches(job["meta"].get("printer_model"), printer.get("model")):
            return "Blocked: sliced for %s, this printer is %s" % (job["meta"].get("printer_model") or "unknown", printer.get("model"))
        if self.sends.get(printer["id"], {}).get("active"):
            return "A send is already running"
        return ""

    def _set_send(self, printer_id, active, text, percent=None, ok=None):
        entry = {"active": active, "text": text, "percent": percent, "ok": ok}
        with self.lock:
            self.sends[printer_id] = entry
        self.post({"command": "send", "id": printer_id, "send": entry})

    def _send_worker(self, printer, snapshot, start, options):
        printer_id, name = printer["id"], printer["name"]
        cancel = self._cancel.get(printer_id) or threading.Event()
        try:
            stat = os.stat(snapshot["path"])
            if stat.st_size != snapshot["size"] or stat.st_mtime != snapshot["mtime"]:
                raise FarmError("The plate was sliced again while sending. Send the new slice.")
            client = make_client(printer)
            last = [0.0]

            def progress(sent, total):
                now = time.monotonic()
                if now - last[0] >= 0.25 or sent >= total:
                    last[0] = now
                    self._set_send(printer_id, True, "Uploading %d%%" % (100 * sent // max(1, total)), percent=100.0 * sent / max(1, total))

            self._set_send(printer_id, True, "Uploading 0%", percent=0)
            remote = client.upload(snapshot["path"], snapshot["name"], progress=progress, cancelled=cancel.is_set)
            stat = os.stat(snapshot["path"])
            if stat.st_size != snapshot["size"] or stat.st_mtime != snapshot["mtime"]:
                raise FarmError("The plate was sliced again during the upload. The uploaded file may be incomplete; send again.")
            if not start:
                self._set_send(printer_id, False, "Uploaded %s" % remote, ok=True)
                self._notify("info", "Farm Send: uploaded %s to %s" % (remote, name))
                return
            if cancel.is_set():
                raise FarmError("Cancelled after upload. The file is on the printer but was not started.")
            self._set_send(printer_id, True, "Starting print...", percent=100)
            status = client.status()
            if not can_start(status):
                raise FarmError("Uploaded, but not started: the printer is %s." % status.get("state_text", status.get("state")).lower())
            client.start_print(remote, options)
            self._set_send(printer_id, False, "Printing %s" % remote, ok=True)
            self._notify("info", "Farm Send: %s started printing %s" % (name, remote))
        except FarmError as error:
            self._set_send(printer_id, False, str(error), ok=False)
            self._notify("error", "Farm Send: %s: %s" % (name, error))
        except PermissionError:
            self._set_send(printer_id, False, BLOCKED_TEXT, ok=False)
        except OSError as error:
            self._set_send(printer_id, False, "Cannot read the sliced G-code: %s" % (error.strerror or error), ok=False)
        except Exception as error:  # never leave a card stuck on "Uploading"
            self._set_send(printer_id, False, "Unexpected error: %s" % error, ok=False)
        finally:
            self._cancel.pop(printer_id, None)
            self.poll(force=True, only=printer_id)

    # ---- status polling --------------------------------------------------------------------------
    def poll(self, force=False, only=None):
        now = time.monotonic()
        todo = []
        with self.lock:
            for printer in self.store.printers:
                printer_id = printer["id"]
                if only is not None and printer_id != only:
                    continue
                if printer_id in self._status_busy:
                    continue
                interval = STATUS_MIN_INTERVAL_CC2 if printer.get("kind") == KIND_CC2 else STATUS_MIN_INTERVAL
                if not force and now - self._status_at.get(printer_id, 0) < interval:
                    continue
                self._status_busy.add(printer_id)
                todo.append(dict(printer))
        for printer in todo:
            self._spawn(self._status_worker, printer)

    def _status_worker(self, printer):
        printer_id = printer["id"]
        try:
            status = make_client(printer, timeout=5).status()
        except FarmError as error:
            status = {"state": "offline", "state_text": "Offline", "detail": str(error), "filename": "", "progress": None,
                      "remaining_s": None, "nozzle": None, "bed": None, "errors": []}
        except PermissionError:
            status = {"state": "offline", "state_text": "Blocked", "detail": BLOCKED_TEXT, "filename": "", "progress": None,
                      "remaining_s": None, "nozzle": None, "bed": None, "errors": []}
        except Exception as error:
            status = {"state": "offline", "state_text": "Error", "detail": str(error), "filename": "", "progress": None,
                      "remaining_s": None, "nozzle": None, "bed": None, "errors": []}
        with self.lock:
            self._status_busy.discard(printer_id)
            self._status_at[printer_id] = time.monotonic()
            if self.store.get(printer_id) is None:
                return
            self.status[printer_id] = status
        self.post({"command": "status", "id": printer_id, "status": status})
