"""Farm Send application logic: jobs, printer cards, sends. No OrcaSlicer imports; the plugin
shell supplies post()/notify() and forwards panel messages and slicer events."""
# BUNDLE-STRIP-BEGIN
import os
import re
import sys
import json
import queue
import threading
import time
import uuid

from core import (toolhead_color_problem, count_objects, file_stamp, OrderBook, plan_order, CC2_MANY_FILES, nozzle_mismatch, toolhead_problem, write_private, file_md5, STORE_FILE, KIND_CC2, WATCHED_STATES, BedLog, FarmError, bed_needs_clearing, files_to_clean, rule_text,
                  storage_summary, PrinterStore, can_start, job_filename, make_client, model_key, model_matches,
                  parse_gcode_meta, preset_to_printer, presets_to_printers, set_shared_dir, status_event, strip_model_suffix, validate_printer)
# BUNDLE-STRIP-END

SLICE_DIR = "slices"
SLICE_KEEP_S = 12 * 3600      # sliced copies of a window that is gone are dropped after this long
SLICE_TOUCH_S = 600
LIBRARY_DIR = "library"   # kept plates: <id>.gcode plus <id>.json describing it
STATUS_MIN_INTERVAL = 3.0
STATUS_MIN_INTERVAL_CC2 = 8.0   # each CC2 poll is a full MQTT connect + register; be gentle
STORAGE_INTERVAL = 600.0        # how often each printer's stored files are counted
AUTO_CLEAN_INTERVAL = 86400.0   # automatic cleanup runs at most once a day per printer
BLOCKED_TEXT = "OrcaSlicer blocked network access to this printer. Approve the permission prompt, then it retries."


def _clock(when):
    return time.strftime("%H:%M", time.localtime(when))


def _short_duration(seconds):
    if seconds is None:
        return ""
    hours, minutes = int(seconds) // 3600, (int(seconds) % 3600) // 60
    return ("%dh%02dm" % (hours, minutes)) if hours else ("%dm" % max(1, minutes))


class WorkerPool:
    """A fixed set of background threads that run the plugin's network and file work.

    Why not start a thread per job: OrcaSlicer decides which plugin a thread belongs to at the
    moment the thread is started, from whichever plugin it believes is running right then. A
    message from a docked panel is delivered without that being set, so a thread started while
    handling one can be credited to another plugin: its printer requests then raise permission
    prompts in that other plugin's name, or are refused. Threads started from the plugin's own
    execute() call are credited correctly, so all workers are started there, once, and jobs are
    handed to them through a queue."""

    def __init__(self, size=16):
        self.size, self._jobs, self._threads = size, queue.Queue(), []
        self._lock = threading.Lock()

    @property
    def started(self):
        return bool(self._threads)

    def start(self):
        """Start the workers. Call only from a place where OrcaSlicer knows the plugin is running."""
        with self._lock:
            if self._threads:
                return
            for _ in range(self.size):
                thread = threading.Thread(target=self._run, daemon=True)
                thread.start()
                self._threads.append(thread)

    def submit(self, target, *args):
        if not self._threads:
            # Not started (tests, or a slice event before the panel was ever opened): run it now on
            # a thread of its own, as before.
            threading.Thread(target=target, args=args, daemon=True).start()
            return
        self._jobs.put((target, args))

    def _run(self):
        while True:
            target, args = self._jobs.get()
            try:
                target(*args)
            except Exception:
                pass   # a failing job must not take a worker down; each job reports its own errors


class FarmApp:
    def __init__(self, storage_dir, post, notify=None, preset_dirs=(), default_name=None, spawn=None,
                 read_presets=None, apply_presets=None, list_presets=None):
        self.store = PrinterStore(os.path.join(storage_dir, STORE_FILE))
        set_shared_dir(storage_dir)   # every OrcaSlicer window shares one client slot per CC2
        self.beds = BedLog(os.path.join(storage_dir, "farm_send_beds.json"))
        self.orders = OrderBook(os.path.join(storage_dir, "farm_send_orders.json"))
        self.cleaned = BedLog(os.path.join(storage_dir, "farm_send_cleaned.json"))   # last automatic cleanup per printer
        self.storage = {}         # printer id -> {count, bytes, oldest, removable, many, error}
        self._storage_at = {}
        self._storage_busy = set()
        self._post = post
        self._notify = notify or (lambda level, text: None)
        self.preset_dirs = list(preset_dirs)
        # list_presets() returns the printer presets OrcaSlicer has loaded right now, each as a dict
        # of its settings. UI thread only. When given, Import uses it instead of reading files.
        self._list_presets = list_presets
        self._default_name = default_name or (lambda: "")
        self._spawn = spawn or self._spawn_thread
        self.lock = threading.RLock()
        # One sliced plate per printer type, newest first. Each is the plugin's OWN copy of the G-code,
        # because OrcaSlicer rewrites its temp file on the next slice: without a copy, slicing for
        # the second printer type would destroy the plate sliced for the first.
        self.jobs = []
        # Each OrcaSlicer window keeps its sliced copies in a folder of its own, so a second window
        # starting up never removes the plates another window still holds.
        self.slice_root = os.path.join(storage_dir, SLICE_DIR)
        self.slice_dir = os.path.join(self.slice_root, uuid.uuid4().hex[:8])
        self._slice_touched = time.monotonic()
        self.library_dir = os.path.join(storage_dir, LIBRARY_DIR)
        self._library_cache, self._library_stamp = None, None
        self.chosen = {}        # printer type key -> id of the sliced plate its cards send
        self._saved_names = {}  # md5 of a kept plate -> its item name, refreshed with every snapshot
        self._clear_slice_dir()
        self.slicing = False
        self.status = {}          # printer id -> normalized status
        self._status_at = {}
        self._status_busy = set()
        self._watched = {}        # printer id -> last status in a watched state, for change notices
        self.notices = {}         # printer id -> {level, text, at}: the last change, kept on the card
        self.sends = {}           # printer id -> {active, text, percent, ok}
        self._cancel = {}
        # Slicing presets. read_presets() must run on the UI thread and returns the current selection
        # {"model", "printer", "process", "filaments"}; apply_presets(combo) switches to a saved one
        # and must run OFF the UI thread, because it waits for OrcaSlicer's own selection dialogs.
        self._read_presets = read_presets
        self._apply_presets = apply_presets
        self.current = None
        self.combo_note = {"text": "", "ok": True, "busy": False}

    def _clear_slice_dir(self, now=None):
        """Drop sliced copies left behind by windows that are gone. There is no way to ask whether
        another window is still open, so a folder counts as left behind once nothing in it has changed
        for SLICE_KEEP_S; an open window refreshes its folder while its panel is in use."""
        now = time.time() if now is None else now
        try:
            names = os.listdir(self.slice_root)
        except OSError:
            return
        for name in names:
            path = os.path.join(self.slice_root, name)
            try:
                if path == self.slice_dir or now - os.path.getmtime(path) < SLICE_KEEP_S:
                    continue
                if os.path.isdir(path):
                    for inner in os.listdir(path):
                        if inner.endswith(".gcode") or inner.endswith(".part"):
                            self._remove_file(os.path.join(path, inner))
                    os.rmdir(path)
                elif name.endswith(".gcode") or name.endswith(".part"):      # from before 0.3.3
                    self._remove_file(path)
            except OSError:
                pass

    def _touch_slice_dir(self):
        """Mark this window's sliced copies as still in use. Cheap, and at most every few minutes."""
        if time.monotonic() - self._slice_touched < SLICE_TOUCH_S:
            return
        self._slice_touched = time.monotonic()
        try:
            os.utime(self.slice_dir, None)
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
        # OrcaSlicer names each plate's file .<process id>.<plate index>.gcode, so "Slice all" on a
        # project with several plates arrives as one export per plate.
        match = re.search(r"\.(\d+)\.gcode$", os.path.basename(path), re.I)
        job = {"id": uuid.uuid4().hex[:10], "source": path, "path": "", "name": name or "", "captured_at": time.time(),
               "ready": False, "error": "", "stale": False, "meta": {}, "key": "", "plate": int(match.group(1)) + 1 if match else 1}
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
            job["objects"], job["md5"] = count_objects(copy), file_md5(copy)
            replaced = []
            with self.lock:
                if job not in self.jobs:          # project closed or superseded while copying
                    replaced.append(copy)
                else:
                    job["path"], job["meta"], job["key"] = copy, meta, model_key(meta["printer_model"])
                    # The newest slice of a plate replaces the older slice of that same plate for that
                    # printer type. Other plates of the project, and other printer types, are kept.
                    same = lambda other: other is not job and other["key"] == job["key"] and other.get("plate") == job["plate"]
                    replaced += [other["path"] for other in self.jobs if same(other)]
                    self.jobs = [other for other in self.jobs if not same(other)]
                    self.chosen[job["key"]] = job["id"]            # the plate just sliced is the one the cards send
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
        """The sliced plate the printer's card sends: the one chosen for its type (the newest slice
        unless the user picked another plate), or None."""
        ready = [job for job in self.jobs if job["ready"] and model_matches(job["meta"].get("printer_model"), printer.get("model"))]
        for job in ready:
            if self.chosen.get(job["key"]) == job["id"]:
                return job
        return ready[0] if ready else None

    def _chosen_plate(self, key):
        """Of the plates held for one printer type, the one its cards send."""
        ready = [job for job in self.jobs if job["ready"] and job.get("key") == key]
        return next((job for job in ready if self.chosen.get(key) == job["id"]), ready[0] if ready else None)

    def _cmd_choose_plate(self, message):
        """Several plates are held for one printer type: send this one from the printer cards."""
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == message.get("id") and j["ready"]), None)
            if job is not None:
                self.chosen[job["key"]] = job["id"]
        self.post_state()

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
            "id": job["id"], "ready": job["ready"], "error": job["error"], "stale": job["stale"], "key": job.get("key", ""),
            "plate": job.get("plate", 1),
            "objects": job.get("objects") or 0,
            "saved_as": self._saved_names.get(job.get("md5") or "", ""),
            "chosen": job is self._chosen_plate(job.get("key")),
            "display_name": self._job_base_name(job), "summary": " · ".join(bits),
            "printer_model": meta.get("printer_model", ""), "sliced_at": _clock(job["captured_at"]),
            "post_process": bool((meta.get("post_process") or "").strip()),
            "filaments": list(meta.get("filaments") or []),
            "presets": " \u00b7 ".join(part for part in (meta.get("process_preset"), meta.get("filament_preset")) if part),
            "printers": [p["name"] for p in self.store.printers if model_matches(meta.get("printer_model"), p.get("model"))],
        }

    def snapshot(self):
        with self.lock:
            # Another OrcaSlicer window may have saved printers, orders or library plates since.
            self.store.sync()
            self.orders.sync()
            stamp = file_stamp(self.library_dir)
            if stamp != self._library_stamp:
                self._library_stamp, self._library_cache = stamp, None
            self._saved_names = {e["md5"]: e.get("item", "") for e in self.library() if e.get("md5")}
            if self.jobs:
                self._touch_slice_dir()
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
                "types": self._type_views(),
                "library": self._library_views(),
                "folders": self.library_folders(),
                "orders": self._order_views(),
                "order_plates": self._open_plates(),
                "toolhead_names": list(self.orders.toolhead_names),
                "storage": dict(self.storage),
                "cleanup": dict(self.store.cleanup, text=rule_text(self.store.cleanup)),
                "current": dict(self.current) if self.current else None,
                "combo_note": dict(self.combo_note),
                "can_switch": self._apply_presets is not None,
                "auto_open": self.store.auto_open,
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

    def _type_views(self):
        """One row per printer type the user works with: its printers, its remembered presets and
        whether OrcaSlicer is set to that type right now. The panel adds the plate held for it."""
        current_key = model_key((self.current or {}).get("model") or "")
        combos = {view["key"]: view for view in self._combo_views()}
        rows = {}
        for printer in self.store.printers:
            key = model_key(printer.get("model"))
            if key:
                rows.setdefault(key, {"key": key, "label": printer.get("model", ""), "printers": []})["printers"].append(printer["name"])
        for key, combo in combos.items():
            rows.setdefault(key, {"key": key, "label": combo["label"], "printers": []})["label"] = combo["label"]
        for job in self.jobs:
            if job["ready"] and job.get("key") and not job.get("settled"):
                rows.setdefault(job["key"], {"key": job["key"], "label": job["meta"].get("printer_model", ""), "printers": []})
        for key, row in rows.items():
            row["combo"] = combos.get(key)
            row["selected"] = bool(current_key) and current_key == key
        return sorted(rows.values(), key=lambda row: row["label"].lower())

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

    def report_text(self):
        """Facts worth pasting into a trouble report. No printer names, addresses, codes or keys."""
        lines = ["Farm Send %s" % globals().get("PLUGIN_VERSION", "dev"), "System: %s" % sys.platform,
                 "OrcaSlicer version: (Help > About)", ""]
        with self.lock:
            printers = [dict(p) for p in self.store.printers]
            status, sends = dict(self.status), dict(self.sends)
            lines.append("Library plates: %d, open orders: %d" % (len(self._library_views()), len(self.orders.orders)))
        lines.append("Printers: %d" % len(printers))
        for number, printer in enumerate(printers, 1):
            state = status.get(printer["id"]) or {}
            lines.append("%d. %s (%s): %s" % (number, printer.get("model") or "unknown model", printer.get("kind", ""),
                                              state.get("state_text") or state.get("state") or "not checked yet"))
            for label, text in (("error", state.get("error")), ("notice", (state.get("notice") or {}).get("text")),
                                ("last send", (sends.get(printer["id"]) or {}).get("text"))):
                if text:
                    lines.append("   %s: %s" % (label, text))
        return "\n".join(lines)

    def _cmd_report(self, message):
        self.post({"command": "report", "text": self.report_text()})

    def _cmd_bed_cleared(self, message):
        """The user took the finished print off this printer's bed."""
        printer_id = message.get("id")
        with self.lock:
            if self.store.get(printer_id) is None:
                return
            self.beds.set(printer_id, False)
            counted = None
            if message.get("good") is not None:     # this bed held a plate of an order: how many came out good
                try:
                    counted = self.orders.count(printer_id, int(message.get("good")))
                except (TypeError, ValueError):
                    pass
            status = self.status.get(printer_id)
            if status is not None:
                status = self.status[printer_id] = dict(status, needs_clearing=False)
        if status is not None:
            self.post({"command": "status", "id": printer_id, "status": status})
        if counted is not None:
            self.post_state()

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
            for table in (self.status, self._status_at, self.sends, self._watched, self.notices, self.storage, self._storage_at):
                table.pop(printer_id, None)
            self.cleaned.forget(printer_id)
            self.beds.forget(printer_id)
        self.post_state()

    def _cmd_import_presets(self, message):
        added, seen = [], {p["address"] for p in self.store.printers}
        candidates = []
        if self._list_presets is not None:
            try:
                candidates = [printer for printer in (preset_to_printer(preset) for preset in self._list_presets()) if printer]
            except Exception:
                candidates = []
        else:
            for folder in self.preset_dirs:
                candidates += presets_to_printers(folder)
        for candidate in candidates:
            if candidate["address"] in seen:
                # Already saved: only fill in a nozzle size it does not have yet.
                for known in self.store.printers:
                    if known["address"] == candidate["address"] and candidate.get("nozzle") and not known.get("nozzle"):
                        try:
                            self.store.upsert(dict(known, nozzle=candidate["nozzle"]))
                        except (FarmError, OSError):
                            pass
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
                if start and self.beds.get(printer_id) and self.status.get(printer_id, {}).get("state") == "idle":
                    self._set_send(printer_id, False, "Not started: clear the bed, then click Bed cleared.", ok=False)
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
                wrong = nozzle_mismatch(job["meta"].get("nozzle_diameter"), printer.get("nozzle"))
                if wrong:
                    self._set_send(printer_id, False, "Blocked: %s." % wrong, ok=False)
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
            how = client.start_print(remote, options)
            note = ""
            if how == "standard":
                # Only Snapmaker firmware takes start options. Say so instead of implying they applied.
                note = ". This printer does not accept start options, so bed leveling and time-lapse follow its own settings"
            if snapshot.get("keep"):
                self._set_send(printer_id, True, "Started. Telling the printer to print %d part%s only..." % (
                    snapshot["keep"], "" if snapshot["keep"] == 1 else "s"), percent=100)
                try:
                    kept, total = client.keep_parts(snapshot["keep"], cancelled=cancel.is_set)
                except FarmError as error:
                    # Unconfirmed skips would print the whole plate, so stop it here.
                    try:
                        client.cancel_print()
                        stopped = "The print was cancelled."
                    except FarmError:
                        stopped = "CANCEL IT ON THE PRINTER: it may print the whole plate."
                    raise FarmError("Could not limit the plate to %d parts (%s). %s" % (snapshot["keep"], error, stopped))
                note = ": printing %d of %d parts%s" % (kept, total, note)
            if snapshot.get("order"):
                # Recorded only once the printer accepted the start, so a failed send never counts.
                self.orders.add_plate(snapshot["order"]["id"], printer_id, name, snapshot["order"]["parts"], remote)
                self.post_state()
            self._set_send(printer_id, False, "Started %s%s" % (remote, note), ok=True, started=True)
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
            self.check_storage(force=True, only=printer_id)

    # ---- library: plates kept for reprinting -------------------------------------------------------
    def library(self):
        """Every kept plate, read from the folder. Each plate is a G-code file with a small JSON file
        next to it; there is no index to drift out of step. A plate whose G-code is missing is skipped."""
        if self._library_cache is None:
            entries = []
            try:
                names = sorted(os.listdir(self.library_dir))
            except OSError:
                names = []
            for name in names:
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(self.library_dir, name), "r", encoding="utf-8") as handle:
                        entry = json.load(handle)
                except (OSError, ValueError):
                    continue
                path = os.path.join(self.library_dir, name[:-5] + ".gcode")
                if isinstance(entry, dict) and entry.get("id") == name[:-5] and os.path.isfile(path):
                    if "objects" not in entry:          # kept before part counts were recorded: count once
                        try:
                            entry["objects"] = count_objects(path)
                            write_private(os.path.join(self.library_dir, name), json.dumps(entry, indent=1))
                        except OSError:
                            entry.pop("objects", None)
                    entries.append(dict(entry, path=path))
            entries.sort(key=lambda entry: (str(entry.get("folder", "")).lower(), str(entry.get("item", "")).lower(),
                                            entry.get("parts") or 0))
            self._library_cache = entries
        return self._library_cache

    FOLDERS_FILE = "folders.json"   # folders the user made, so an empty one is still there
    FOLDER_SEP = " / "              # a folder inside a folder is a path: "Clips / Vertical"
    FOLDER_DEPTH = 4

    @classmethod
    def folder_path(cls, value):
        """A folder path as typed, tidied: parts trimmed, empty parts dropped, depth and length capped."""
        parts = [" ".join(part.split())[:40] for part in str(value or "").split("/")]
        return cls.FOLDER_SEP.join([part for part in parts if part][:cls.FOLDER_DEPTH])

    @classmethod
    def _under(cls, path, folder):
        """path is folder itself or somewhere inside it."""
        return path == folder or path.startswith(folder + cls.FOLDER_SEP)

    def library_folders(self):
        """Every folder, sorted: the ones made by hand plus any a kept plate names."""
        names = set()
        try:
            with open(os.path.join(self.library_dir, self.FOLDERS_FILE), "r", encoding="utf-8") as handle:
                names = {str(n) for n in json.load(handle) if isinstance(n, str) and n}
        except (OSError, ValueError, TypeError):
            pass
        names |= {e["folder"] for e in self.library() if e.get("folder")}
        for name in list(names):            # a folder's parents exist too
            parts = name.split(self.FOLDER_SEP)
            names |= {self.FOLDER_SEP.join(parts[:depth]) for depth in range(1, len(parts))}
        return sorted(names, key=str.lower)

    def _save_folders(self, names):
        os.makedirs(self.library_dir, exist_ok=True)
        write_private(os.path.join(self.library_dir, self.FOLDERS_FILE), json.dumps(sorted(set(names), key=str.lower)))
        self._library_cache = None

    def _move_folder(self, old, new):
        """Everything in folder old, at any depth, goes to the same place under new ("" = top level)."""
        moved = lambda path: (new + path[len(old):]).lstrip(" /") if new else path[len(old):].lstrip(" /")
        for entry in self.library():
            if self._under(entry.get("folder", ""), old):
                saved = {k: v for k, v in entry.items() if k != "path"}
                saved["folder"] = self.folder_path(moved(entry["folder"]))
                write_private(os.path.join(self.library_dir, entry["id"] + ".json"), json.dumps(saved, indent=1))
        self._library_cache = None
        return moved

    def _cmd_library_folder(self, message):
        """action add / rename / remove. name and new are full paths. Removing a folder keeps what is
        in it: its plates and its subfolders move up into the folder that held it."""
        name, new, action = self.folder_path(message.get("name")), self.folder_path(message.get("new")), message.get("action")
        folders = self.library_folders()
        try:
            if action == "add":
                if not name:
                    raise FarmError("Give the folder a name")
                self._save_folders(folders + [name])
            elif action in ("rename", "remove") and name in folders:
                if action == "rename":
                    if not new:
                        raise FarmError("Give the folder a name")
                    if self._under(new, name) and new != name:
                        raise FarmError("A folder cannot be moved into itself")
                else:
                    new = self.FOLDER_SEP.join(name.split(self.FOLDER_SEP)[:-1])      # its parent, or the top level
                moved = self._move_folder(name, new)
                kept = [f for f in folders if not self._under(f, name)]
                inside = [self.folder_path(moved(f)) for f in folders if self._under(f, name) and (action == "rename" or f != name)]
                self._save_folders([f for f in kept + inside if f])
            self.post({"command": "library_result", "ok": True, "text": ""})
        except FarmError as error:
            self.post({"command": "library_result", "ok": False, "text": str(error)})
        except OSError as error:
            self.post({"command": "library_result", "ok": False, "text": "Could not save: %s" % (error.strerror or error)})
        self.post_state()

    def _library_views(self):
        views = []
        for entry in self.library():
            bits = []
            if entry.get("time_s") is not None:
                bits.append(_short_duration(entry["time_s"]))
            if entry.get("filament_g") is not None:
                bits.append("%.1f g" % entry["filament_g"])
            if entry.get("layers"):
                bits.append("%d layers" % entry["layers"])
            views.append({"id": entry["id"], "item": entry.get("item", ""), "parts": entry.get("parts") or 0,
                          "folder": entry.get("folder", ""),
                          # A part count can be chosen when every part is labelled on its own.
                          "countable": bool(entry.get("parts")) and entry.get("objects") == entry.get("parts") and entry["parts"] > 1,
                          "key": entry.get("key", ""), "printer_model": entry.get("printer_model", ""),
                          "summary": " \u00b7 ".join(bits), "filaments": list(entry.get("filaments") or []),
                          "presets": entry.get("presets", ""), "saved_at": entry.get("saved_at") or 0,
                          "printers": [p["id"] for p in self.store.printers
                                       if model_matches(entry.get("printer_model"), p.get("model"))]})
        return views

    @staticmethod
    def _library_fields(message):
        """Item name, parts count and folder as typed, tidied. Raises FarmError when unusable."""
        item = " ".join(str(message.get("item") or "").split())[:80]
        folder = FarmApp.folder_path(message.get("folder"))
        try:
            parts = int(message.get("parts"))
        except (TypeError, ValueError):
            parts = 0
        if not item:
            raise FarmError("Give the item a name")
        if not 1 <= parts <= 9999:
            raise FarmError("Enter how many parts are on this plate")
        return item, parts, folder

    def _cmd_library_edit(self, message):
        """Rename a kept plate, correct its parts count, or move it to another folder."""
        entry = next((e for e in self.library() if e["id"] == message.get("id")), None)
        try:
            if entry is None:
                raise FarmError("That library plate no longer exists.")
            item, parts, folder = self._library_fields(message)
            saved = {k: v for k, v in entry.items() if k != "path"}
            saved.update(item=item, parts=parts, folder=folder)
            write_private(os.path.join(self.library_dir, entry["id"] + ".json"), json.dumps(saved, indent=1))
            self._library_cache = None
            self.post({"command": "library_result", "ok": True, "text": "Updated %s x%d." % (item, parts)})
        except FarmError as error:
            self.post({"command": "library_result", "ok": False, "text": str(error)})
        except OSError as error:
            self.post({"command": "library_result", "ok": False, "text": "Could not save: %s" % (error.strerror or error)})
        self.post_state()

    def _library_keep(self, job, item, parts, folder):
        """Copy one sliced plate into the library. Raises FarmError or OSError."""
        if job is None:
            raise FarmError("That plate is no longer here. Slice it again.")
        if job.get("stale"):
            raise FarmError("The plate changed after this slice. Slice again, then save.")
        os.makedirs(self.library_dir, exist_ok=True)
        entry_id = "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), job["id"][:6])
        target = os.path.join(self.library_dir, entry_id + ".gcode")
        self._copy_file(job["path"], target + ".part")
        os.replace(target + ".part", target)
        meta = job["meta"]
        entry = {"id": entry_id, "item": item, "parts": parts, "folder": folder, "key": job.get("key", ""),
                 "printer_model": meta.get("printer_model", ""), "time_s": meta.get("time_s"),
                 "filament_g": meta.get("filament_g"), "layers": meta.get("layers"),
                 "filaments": list(meta.get("filaments") or []), "nozzle_diameter": meta.get("nozzle_diameter"),
                 "presets": " \u00b7 ".join(x for x in (meta.get("process_preset"), meta.get("filament_preset")) if x),
                 "saved_at": time.time(), "size": os.path.getsize(target), "md5": file_md5(target),
                 "objects": count_objects(target)}
        # The description is written last: a plate only counts as kept once it is complete.
        write_private(os.path.join(self.library_dir, entry_id + ".json"), json.dumps(entry, indent=1))
        self._library_cache = None
        return entry

    def _cmd_library_save(self, message):
        """Keep the plate sliced just now, under an item name and with the number of parts on it."""
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == message.get("job_id") and j["ready"]), None)
        try:
            item, parts, folder = self._library_fields(message)
            self._library_keep(job, item, parts, folder)
            self.post({"command": "library_result", "ok": True, "text": "Saved %s x%d to the library." % (item, parts)})
        except FarmError as error:
            self.post({"command": "library_result", "ok": False, "text": str(error)})
        except OSError as error:
            self.post({"command": "library_result", "ok": False, "text": "Could not save: %s" % (error.strerror or error)})
        self.post_state()

    def _cmd_library_save_all(self, message):
        """Keep several sliced plates in one go. rows: [{job_id, item, parts, folder}]. Every row is
        checked before anything is copied, so a typing mistake never leaves half of them saved."""
        rows = [row for row in (message.get("rows") or []) if isinstance(row, dict)]
        try:
            if not rows:
                raise FarmError("Nothing to save")
            todo = []
            with self.lock:
                jobs = {j["id"]: j for j in self.jobs if j["ready"]}
            for number, row in enumerate(rows, 1):
                job = jobs.get(row.get("job_id"))
                label = "Plate %s" % job["plate"] if job else "Row %d" % number
                try:
                    item, parts, folder = self._library_fields(row)
                    if job is None:
                        raise FarmError("That plate is no longer here. Slice it again.")
                    if job.get("stale"):
                        raise FarmError("The plate changed after this slice. Slice again, then save.")
                except FarmError as error:
                    raise FarmError("%s: %s" % (label, error))
                todo.append((job, item, parts, folder))
            for job, item, parts, folder in todo:
                self._library_keep(job, item, parts, folder)
            self.post({"command": "library_result", "ok": True,
                       "text": "Saved %d plate%s to the library." % (len(todo), "" if len(todo) == 1 else "s")})
        except FarmError as error:
            self.post({"command": "library_result", "ok": False, "text": str(error)})
        except OSError as error:
            self._library_cache = None
            self.post({"command": "library_result", "ok": False, "text": "Could not save: %s" % (error.strerror or error)})
        self.post_state()

    def _cmd_clear_slices(self, message):
        """Drop the sliced plates held for one printer type. Kept library plates are not touched."""
        key = message.get("key")
        with self.lock:
            gone = [job for job in self.jobs if job.get("key") == key and job["ready"]]
            self.jobs = [job for job in self.jobs if job not in gone]
            self.chosen.pop(key, None)
        for job in gone:
            self._remove_file(job["path"])
        self.post_state()

    def _cmd_library_delete(self, message):
        entry = next((e for e in self.library() if e["id"] == message.get("id")), None)
        if entry is not None:
            for name in (entry["id"] + ".json", entry["id"] + ".gcode"):   # description first: never half an entry
                try:
                    os.remove(os.path.join(self.library_dir, name))
                except OSError:
                    pass
            self._library_cache = None
        self.post_state()

    def _cmd_library_send(self, message):
        """Send a kept plate to the listed printers. toolhead: print a one-filament plate from this
        toolhead instead of the one it was sliced for (Snapmaker only)."""
        entry = next((e for e in self.library() if e["id"] == message.get("id")), None)
        self._library_start(entry, message.get("printer_ids") or [], bool(message.get("start")),
                            dict(message.get("options") or {}), message.get("toolhead"), keep=message.get("keep"))

    def _library_start(self, entry, printer_ids, start, options, toolhead=None, order_id=None, keep=None):
        """keep: print only this many parts of the plate (Snapmaker / Klipper printers; the rest are
        skipped on the printer). None or the full count prints the whole plate."""
        work = []
        try:
            keep = int(keep) if keep is not None else None
        except (TypeError, ValueError):
            keep = None
        if entry is not None and keep is not None and keep >= (entry.get("parts") or 0):
            keep = None
        with self.lock:
            name = None
            for printer_id in printer_ids:
                printer = self.store.get(printer_id)
                if printer is None or self.sends.get(printer_id, {}).get("active"):
                    continue
                problem = ""
                if entry is None:
                    problem = "That library plate no longer exists."
                elif not model_matches(entry.get("printer_model"), printer.get("model")):
                    problem = "Blocked: this plate was sliced for %s." % (entry.get("printer_model") or "another printer")
                elif nozzle_mismatch(entry.get("nozzle_diameter"), printer.get("nozzle")):
                    problem = "Blocked: %s." % nozzle_mismatch(entry.get("nozzle_diameter"), printer.get("nozzle"))
                elif start and self.beds.get(printer_id) and self.status.get(printer_id, {}).get("state") == "idle":
                    problem = "Not started: clear the bed, then click Bed cleared."
                mine = dict(options)
                fewer = keep if (keep and printer.get("kind") != KIND_CC2) else None
                if not problem and fewer:
                    if not start:
                        problem = "Not sent: a part count needs Start print, because the printer skips the other parts as it starts."
                    elif fewer < 1 or entry.get("objects") != entry.get("parts"):
                        problem = "Not started: this plate cannot print a part count."
                if not problem and start:
                    used = entry.get("filaments") or []
                    tools = (self.status.get(printer_id) or {}).get("tools") or []
                    if toolhead and printer.get("kind") != KIND_CC2:
                        if len(used) != 1:
                            problem = "Not started: only a one-filament plate can be moved to another toolhead."
                        else:
                            mine["toolhead_map"] = {used[0]["slot"]: int(toolhead)}
                            problem = toolhead_problem(tools, int(toolhead), used[0].get("type"))
                    else:
                        for filament in used:   # printing as sliced: every toolhead it uses must be usable
                            problem = problem or toolhead_problem(tools, filament.get("slot"), filament.get("type"))
                        if not problem and not mine.get("any_color"):
                            # As sliced means the sliced color. A swapped spool is caught here unless the
                            # user confirmed the loaded color in the dialog.
                            for filament in used:
                                problem = problem or toolhead_color_problem(tools, filament.get("slot"), filament.get("color"))
                            if problem:
                                problem += ". Pick the toolhead with the right color, or confirm the loaded color"
                    if problem and not problem.startswith("Not started"):
                        problem = "Not started: %s." % problem
                if problem:
                    self._set_send(printer_id, False, problem, ok=False)
                    continue
                if name is None:
                    label = "%s_x%d" % (entry.get("item") or "plate", entry.get("parts") or 0)
                    if entry.get("time_s"):
                        label += "_" + _short_duration(entry["time_s"])
                    name = job_filename(label)
                self._cancel[printer_id] = threading.Event()
                self._set_send(printer_id, True, "Preparing...", percent=0)
                snapshot = {"path": entry["path"], "name": name}
                if fewer:
                    snapshot["keep"] = fewer
                if order_id and start:
                    snapshot["order"] = {"id": order_id, "parts": fewer or entry.get("parts") or 0}
                work.append((dict(printer), snapshot, mine))
        for printer, snapshot, mine in work:
            self._spawn(self._send_worker, printer, snapshot, start, mine)

    # ---- orders: a quantity of one item, worked off plate by plate ---------------------------------
    def _order_views(self):
        items = {str(e.get("item", "")).lower() for e in self.library()}
        views = []
        for order in self.orders.orders:
            good, printing, remaining = OrderBook.totals(order)
            views.append({"id": order["id"], "item": order["item"], "quantity": order["quantity"],
                          "toolhead": order.get("toolhead"), "note": order.get("note", ""),
                          "good": good, "printing": printing, "remaining": remaining,
                          "in_library": order["item"].lower() in items,
                          "plates": [{k: plate.get(k) for k in ("printer_id", "printer_name", "parts", "state", "good")}
                                     for plate in order["plates"]]})
        return views

    def _open_plates(self):
        found = {}
        for order in self.orders.orders:
            for plate in order["plates"]:
                if plate.get("state") == "printing":
                    found[plate["printer_id"]] = {"order_id": order["id"], "item": order["item"], "parts": plate.get("parts") or 0}
        return found

    def _cmd_order_save(self, message):
        item = " ".join(str(message.get("item") or "").split())[:80]
        try:
            quantity = int(message.get("quantity"))
        except (TypeError, ValueError):
            quantity = 0
        try:
            toolhead = int(message.get("toolhead"))
        except (TypeError, ValueError):
            toolhead = None
        if item and 1 <= quantity <= 99999:
            self.orders.add(item, quantity, toolhead if toolhead in (1, 2, 3, 4) else None,
                            " ".join(str(message.get("note") or "").split())[:60])
        self.post_state()

    def _cmd_order_delete(self, message):
        self.orders.delete(message.get("id"))
        self.post_state()

    def _cmd_save_toolhead_names(self, message):
        names = message.get("names")
        if isinstance(names, list):
            self.orders.sync()
            self.orders.toolhead_names = ([" ".join(str(n).split())[:20] for n in names] + [""] * 4)[:4]
            self.orders.save()
        self.post_state()

    def _free_printers(self):
        busy = self._open_plates()
        return [p for p in self.store.printers
                if self.status.get(p["id"], {}).get("state") == "idle" and not self.beds.get(p["id"])
                and not self.sends.get(p["id"], {}).get("active") and p["id"] not in busy]

    def _cmd_order_plan(self, message):
        """Propose the next plates for an order across the printers that are free right now."""
        order = self.orders.get(message.get("id"))
        if order is None:
            return
        entries = [e for e in self.library() if str(e.get("item", "")).lower() == order["item"].lower()]
        with self.lock:
            free = self._free_printers()
        remaining = OrderBook.totals(order)[2]
        self.post({"command": "order_plan", "id": order["id"], "remaining": remaining,
                   "plan": plan_order(remaining, free, entries),
                   "usable": [p["id"] for p in free if plan_order(1, [p], entries)]})

    def _cmd_order_send(self, message):
        """Start the confirmed plates of a plan. Each pick is {printer_id, entry_id}."""
        order = self.orders.get(message.get("id"))
        if order is None:
            return
        entries = {e["id"]: e for e in self.library() if str(e.get("item", "")).lower() == order["item"].lower()}
        with self.lock:
            free = {p["id"] for p in self._free_printers()}
        for pick in message.get("picks") or []:
            entry = entries.get(pick.get("entry_id"))
            if entry is None or pick.get("printer_id") not in free:
                if self.store.get(pick.get("printer_id")):
                    self._set_send(pick["printer_id"], False, "Not started: the printer is no longer free. Plan the order again.", ok=False)
                continue
            free.discard(pick["printer_id"])
            self._library_start(entry, [pick["printer_id"]], True, dict(message.get("options") or {}),
                                order.get("toolhead"), order_id=order["id"], keep=pick.get("parts"))

    # ---- stored files and cleanup ------------------------------------------------------------------
    def check_storage(self, force=False, only=None):
        """Count the files stored on each printer, now and then. A slow, quiet background job."""
        now = time.monotonic()
        todo = []
        with self.lock:
            for printer in self.store.printers:
                printer_id = printer["id"]
                if only is not None and printer_id != only:
                    continue
                if printer_id in self._storage_busy or self.sends.get(printer_id, {}).get("active"):
                    continue
                if not force and printer_id in self._storage_at and now - self._storage_at[printer_id] < STORAGE_INTERVAL:
                    continue
                self._storage_busy.add(printer_id)
                self._storage_at[printer_id] = now
                todo.append(dict(printer))
        for printer in todo:
            self._spawn(self._storage_worker, printer)

    def _protected(self, printer_id):
        """Files a cleanup must not touch: whatever the printer is printing or has loaded."""
        status = self.status.get(printer_id) or {}
        return [status["filename"]] if status.get("filename") else []

    def _cleanup_block(self, printer):
        """Why old files cannot be removed from this printer right now, or ''."""
        status = self.status.get(printer["id"]) or {}
        state = status.get("state")
        if state in (None, "unknown", "offline"):
            return "printer is not reachable"
        if printer.get("kind") == KIND_CC2 and state != "idle":
            return "printer is busy; it is cleaned when idle"
        return ""

    def _set_storage(self, printer, files=None, error=""):
        printer_id = printer["id"]
        with self.lock:
            if self.store.get(printer_id) is None:
                return None
            if files is None:
                entry = dict(self.storage.get(printer_id) or {}, error=error)
            else:
                entry = storage_summary(files)
                entry["removable"] = len(files_to_clean(files, self.store.cleanup, self._protected(printer_id)))
                entry["many"] = printer.get("kind") == KIND_CC2 and entry["count"] >= CC2_MANY_FILES
                entry["error"] = ""
            self.storage[printer_id] = entry
        self.post({"command": "storage", "id": printer_id, "storage": entry})
        return entry

    def _storage_worker(self, printer):
        printer_id = printer["id"]
        entry = None
        try:
            entry = self._set_storage(printer, make_client(printer, timeout=8).list_stored())
        except FarmError as error:
            self._set_storage(printer, error=str(error))
        except Exception:
            self._set_storage(printer, error="Could not read the stored files")
        finally:
            with self.lock:
                self._storage_busy.discard(printer_id)
        # Automatic cleanup: only when the user switched it on, at most once a day per printer.
        if entry and entry.get("removable") and self.store.cleanup.get("auto"):
            with self.lock:
                last = self.cleaned.get(printer_id) or 0
                due = time.time() - last >= AUTO_CLEAN_INTERVAL and not self._cleanup_block(printer) \
                    and not self.sends.get(printer_id, {}).get("active")
                if due:
                    self.cleaned.set(printer_id, time.time())
                    self._cancel[printer_id] = threading.Event()
                    self._set_send(printer_id, True, "Removing old files...", percent=0)
            if due:
                self._cleanup_worker(printer, automatic=True)

    def _cmd_save_cleanup(self, message):
        try:
            self.store.set_cleanup(message.get("rule"))
        except OSError as error:
            self._notify("error", "Farm Send: could not save the cleanup rule: %s" % (error.strerror or error))
        self.post_state()
        self.check_storage(force=True)

    def _cmd_set_auto_open(self, message):
        try:
            self.store.set_auto_open(message.get("on"))
        except OSError as error:
            self._notify("error", "Farm Send: could not save the setting: %s" % (error.strerror or error))
        self.post_state()

    def _cmd_check_storage(self, message):
        self.check_storage(force=True)

    def _cmd_cleanup(self, message):
        """Remove the files the rule marks as old from each listed printer."""
        work = []
        with self.lock:
            for printer_id in message.get("printer_ids") or []:
                printer = self.store.get(printer_id)
                if printer is None or self.sends.get(printer_id, {}).get("active"):
                    continue
                block = self._cleanup_block(printer)
                if block:
                    self._set_send(printer_id, False, "Not cleaned: %s." % block, ok=False)
                    continue
                self._cancel[printer_id] = threading.Event()
                self._set_send(printer_id, True, "Checking stored files...", percent=0)
                work.append(dict(printer))
        for printer in work:
            self._spawn(self._cleanup_worker, printer)

    def _cleanup_worker(self, printer, automatic=False):
        printer_id, name = printer["id"], printer["name"]
        cancel = self._cancel.get(printer_id) or threading.Event()
        try:
            client = make_client(printer, timeout=8)
            # Ask the printer again right now: what it is printing decides what must be kept.
            status = client.status()
            if printer.get("kind") == KIND_CC2 and status.get("state") != "idle":
                raise FarmError("Not cleaned: the printer is %s." % str(status.get("state_text") or status.get("state")).lower())
            protect = [status["filename"]] if status.get("filename") else []
            files = client.list_stored()
            chosen = files_to_clean(files, self.store.cleanup, protect)
            if not chosen:
                self._set_send(printer_id, False, "Nothing to remove: %s." % rule_text(self.store.cleanup), ok=True)
                self._set_storage(printer, files)
                return

            def progress(done, total):
                self._set_send(printer_id, True, "Removing old files: %d of %d" % (done, total), percent=100.0 * done / max(1, total))

            progress(0, len(chosen))
            client.delete_files([item["name"] for item in chosen], progress=progress, cancelled=cancel.is_set)
            # The printer says "ok" even when it kept a file, so count what is really gone.
            left = client.list_stored()
            still = {item["name"] for item in left}
            gone = [item for item in chosen if item["name"] not in still]
            freed = sum(item.get("size") or 0 for item in gone) / 1048576.0
            text = "Removed %d old file%s (%.0f MB)" % (len(gone), "" if len(gone) == 1 else "s", freed)
            if len(gone) < len(chosen):
                text += ", %d could not be removed" % (len(chosen) - len(gone)) if not cancel.is_set() else ", stopped early"
            self._set_send(printer_id, False, text, ok=len(gone) == len(chosen) or cancel.is_set())
            self._set_storage(printer, left)
            if automatic and gone:
                self._notify("info", "Farm Send: %s: %s" % (name, text.lower()))
        except FarmError as error:
            self._set_send(printer_id, False, str(error), ok=False)
        except PermissionError:
            self._set_send(printer_id, False, BLOCKED_TEXT, ok=False)
        except Exception as error:
            self._set_send(printer_id, False, "Unexpected error: %s" % error, ok=False)
        finally:
            self._cancel.pop(printer_id, None)

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
        if only is None:
            self.check_storage()

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
            event = None
            if status.get("state") in WATCHED_STATES:
                name = (self.store.get(printer_id) or printer).get("name") or "Printer"
                event = status_event(self._watched.get(printer_id), status, name)
                self._watched[printer_id] = status
                note = self.notices.get(printer_id)
                if event:
                    self.notices[printer_id] = {"level": event[0], "text": event[1], "at": status["checked_at"],
                                                "state": status["state"]}
                elif note and note["state"] != status["state"] and status["state"] in ("idle", "printing"):
                    # Over once the printer moved on: the next print runs, a pause was resumed, an
                    # error was cleared. A note raised while printing stays for the rest of the print.
                    del self.notices[printer_id]
            if printer_id in self.notices:
                status["notice"] = self.notices[printer_id]
            change = bed_needs_clearing(self.beds.get(printer_id), status)
            if change is not None:
                self.beds.set(printer_id, change)
            status["needs_clearing"] = status.get("state") == "idle" and bool(self.beds.get(printer_id))
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
        if event:
            self._notify(event[0], "Farm Send: %s" % event[1])
        self.post({"command": "status", "id": printer_id, "status": status})
