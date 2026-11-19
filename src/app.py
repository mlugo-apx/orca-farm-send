"""Farm Send application logic: jobs, printer cards, sends. No OrcaSlicer imports; the plugin
shell supplies post()/notify() and forwards panel messages and slicer events."""
# BUNDLE-STRIP-BEGIN
import os
import threading
import time
import uuid

from core import (KIND_CC2, FarmError, PrinterStore, can_start, job_filename, make_client, model_key, model_matches,
                  parse_gcode_meta, presets_to_printers, set_shared_dir, strip_model_suffix, validate_printer)
# BUNDLE-STRIP-END

SLICE_DIR = "slices"
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
        set_shared_dir(storage_dir)   # every OrcaSlicer window shares one client slot per CC2
        self._post = post
        self._notify = notify or (lambda level, text: None)
        self.preset_dirs = list(preset_dirs)
        self._default_name = default_name or (lambda: "")
        self._spawn = spawn or self._spawn_thread
        self.lock = threading.RLock()
        # One sliced plate per printer type, newest first. Each is the plugin's OWN copy of the G-code,
        # because OrcaSlicer rewrites its temp file on the next slice: without a copy, slicing for
        # the second printer type would destroy the plate sliced for the first.
        self.jobs = []
        self.slice_dir = os.path.join(storage_dir, SLICE_DIR)
        self._clear_slice_dir()
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

    def _clear_slice_dir(self):
        """Drop copies left by an earlier session or project. They are only useful while their plate is open."""
        try:
            for name in os.listdir(self.slice_dir):
                if name.endswith(".gcode") or name.endswith(".part"):
                    self._remove_file(os.path.join(self.slice_dir, name))
        except OSError:
            pass

    @staticmethod
    def _copy_file(source, target):
        """Plain read-then-write copy.

        shutil.copyfile is avoided on purpose: OrcaSlicer's plugin audit treats it as a read/write
        request on BOTH paths and prompts for write access to its temp G-code. Opening the source
        read-only needs just the read grant the plugin already declares."""
        with open(source, "rb") as reader, open(target, "wb") as writer:
            for block in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(block)

    @staticmethod
    def _remove_file(path):
        try:
            os.remove(path)
        except OSError:
            pass

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
        job = {"id": uuid.uuid4().hex[:10], "source": path, "path": "", "name": name or "", "captured_at": time.time(),
               "ready": False, "error": "", "stale": False, "meta": {}, "key": ""}
        with self.lock:
            self.slicing = False
            # Earlier failed or still-pending captures are superseded by this slice.
            self.jobs = [existing for existing in self.jobs if existing["ready"]]
            self.jobs.insert(0, job)
        self.post_state()
        self._spawn(self._read_job, job)

    def _read_job(self, job):
        """Copy the freshly sliced G-code into plugin storage, then read its metadata."""
        copy = os.path.join(self.slice_dir, job["id"] + ".gcode")
        try:
            os.makedirs(self.slice_dir, exist_ok=True)
            before = os.stat(job["source"])
            self._copy_file(job["source"], copy + ".part")
            after = os.stat(job["source"])
            if (before.st_size, before.st_mtime) != (after.st_size, after.st_mtime) or os.path.getsize(copy + ".part") != after.st_size:
                raise FarmError("The plate was sliced again while it was being read. The newer slice replaces it.")
            os.replace(copy + ".part", copy)
            meta = parse_gcode_meta(copy)
            if not meta.get("printer_model"):
                raise FarmError("The sliced G-code does not name a printer model, so it cannot be matched to a printer.")
            replaced = []
            with self.lock:
                if job not in self.jobs:          # project closed or superseded while copying
                    replaced.append(copy)
                else:
                    job["path"], job["meta"], job["key"] = copy, meta, model_key(meta["printer_model"])
                    # The newest slice for a printer type replaces the older one for that type only.
                    replaced += [other["path"] for other in self.jobs if other is not job and other["key"] == job["key"]]
                    self.jobs = [other for other in self.jobs if other is job or other["key"] != job["key"]]
                    job["ready"] = True
                    # Plates sliced earlier for other printer types are finished work. Re-arranging
                    # the bed for the next type is normal and must not flag them as out of date, so
                    # only the newest slice keeps tracking changes to the plate.
                    for other in self.jobs:
                        if other is not job:
                            other["settled"], other["stale"] = True, False
            for path in replaced:
                self._remove_file(path)
        except FarmError as error:
            job["error"] = str(error)
        except PermissionError:
            job["error"] = "OrcaSlicer blocked reading the sliced G-code. Approve the permission prompt and slice again."
        except OSError as error:
            job["error"] = "Cannot read the sliced G-code: %s" % (error.strerror or error)
        except Exception as error:      # never leave the panel stuck on "Reading..."
            job["error"] = "Could not read the sliced G-code: %s" % error
        if job["error"]:
            self._remove_file(copy + ".part")
            self._remove_file(copy)
        self.post_state()

    def mark_stale(self):
        """The plate on screen changed. Only the newest slice can be out of date because of it."""
        changed = False
        with self.lock:
            for job in self.jobs:
                if job["ready"] and not job.get("settled") and not job["stale"]:
                    job["stale"] = changed = True
        if changed:
            self.post_state()

    def project_closed(self):
        with self.lock:
            paths = [job["path"] for job in self.jobs if job["path"]]
            self.jobs, self.slicing = [], False
        for path in paths:
            self._remove_file(path)
        self.post_state()

    def _job_for(self, printer):
        """The ready sliced plate for this printer's type, or None."""
        for job in self.jobs:
            if job["ready"] and model_matches(job["meta"].get("printer_model"), printer.get("model")):
                return job
        return None

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
            "filaments": list(meta.get("filaments") or []),
            "presets": " \u00b7 ".join(part for part in (meta.get("process_preset"), meta.get("filament_preset")) if part),
            "printers": [p["name"] for p in self.store.printers if model_matches(meta.get("printer_model"), p.get("model"))],
        }

    def snapshot(self):
        with self.lock:
            job_for = {}
            for printer in self.store.printers:
                match = self._job_for(printer)
                job_for[printer["id"]] = match["id"] if match else None
            return {
                "command": "state",
                "printers": [{k: p.get(k, "") for k in ("id", "name", "kind", "address", "model", "access_code", "api_key")}
                             for p in self.store.printers],
                "jobs": [self._job_view(job) for job in self.jobs],
                "job_for": job_for,
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

    def _cmd_refresh(self, message):
        """Refresh button: ask now instead of waiting for the next timed poll. One printer, or all."""
        printer_id = message.get("id")
        self.poll(force=True, only=printer_id if isinstance(printer_id, str) and printer_id else None)

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
        """Send to each listed printer the plate that was sliced for its type."""
        start = bool(message.get("start"))
        options = message.get("options") or {}
        work = []
        with self.lock:
            names = {}
            for printer_id in message.get("printer_ids") or []:
                printer = self.store.get(printer_id)
                if printer is None:
                    continue
                if self.sends.get(printer_id, {}).get("active"):
                    continue
                job = self._job_for(printer)
                if job is None:
                    other = next((j for j in self.jobs if j["ready"]), None)
                    if other is not None:
                        problem = "Blocked: no plate sliced for %s (the sliced plate is for %s)" % (
                            printer.get("model"), other["meta"].get("printer_model") or "another printer")
                    else:
                        problem = "No sliced plate for this printer type. Slice one first."
                    self._set_send(printer_id, False, problem, ok=False)
                    continue
                if job["id"] not in names:
                    # One file name per plate, fixed now so every printer of that type gets the same one.
                    meta = job["meta"]
                    parts = [self._job_base_name(job)]
                    if meta.get("filament_type"):
                        parts.append(meta["filament_type"].split(";")[0])
                    if meta.get("time_s"):
                        parts.append(_short_duration(meta["time_s"]))
                    names[job["id"]] = job_filename("_".join(parts))
                self._cancel[printer_id] = threading.Event()
                self._set_send(printer_id, True, "Preparing...", percent=0)
                work.append((dict(printer), {"path": job["path"], "name": names[job["id"]]}))
        for printer, snapshot in work:
            self._spawn(self._send_worker, printer, snapshot, start, options)

    def _set_send(self, printer_id, active, text, percent=None, ok=None, started=False):
        entry = {"active": active, "text": text, "percent": percent, "ok": ok, "started": started, "at": time.monotonic()}
        with self.lock:
            self.sends[printer_id] = entry
        self.post({"command": "send", "id": printer_id, "send": entry})

    def _send_worker(self, printer, snapshot, start, options):
        printer_id, name = printer["id"], printer["name"]
        cancel = self._cancel.get(printer_id) or threading.Event()
        try:
            if not os.path.exists(snapshot["path"]):
                raise FarmError("That sliced plate was replaced by a newer slice. Send again.")
            client = make_client(printer)
            last = [0.0]

            def progress(sent, total):
                now = time.monotonic()
                if now - last[0] >= 0.25 or sent >= total:
                    last[0] = now
                    self._set_send(printer_id, True, "Uploading %d%%" % (100 * sent // max(1, total)), percent=100.0 * sent / max(1, total))

            self._set_send(printer_id, True, "Uploading 0%", percent=0)
            remote = client.upload(snapshot["path"], snapshot["name"], progress=progress, cancelled=cancel.is_set)
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
            self._set_send(printer_id, False, "Started %s" % remote, ok=True, started=True)
            self._notify("info", "Farm Send: %s started printing %s" % (name, remote))
        except FarmError as error:
            self._set_send(printer_id, False, str(error), ok=False)
            self._notify("error", "Farm Send: %s: %s" % (name, error))
        except PermissionError:
            self._set_send(printer_id, False, BLOCKED_TEXT, ok=False)
        except FileNotFoundError:
            # A re-slice for this printer type, or closing the project, removed the copy mid-upload.
            self._set_send(printer_id, False, "The sliced plate was replaced or closed during the upload. Send again.", ok=False)
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
            status["checked_at"] = time.time()   # wall clock, shown on the card
            self.status[printer_id] = status
            # "Started <file>" is only true while that print runs. Once the printer is idle again
            # (finished or cancelled) the line would be stale, so it is dropped.
            sent = self.sends.get(printer_id)
            stale = bool(sent and sent.get("started") and not sent.get("active") and status.get("state") == "idle"
                         and time.monotonic() - sent.get("at", 0) > 30)
            if stale:
                del self.sends[printer_id]
        if stale:
            self.post({"command": "send", "id": printer_id, "send": None})
        self.post({"command": "status", "id": printer_id, "status": status})
