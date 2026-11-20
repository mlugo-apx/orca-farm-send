# BUNDLE-STRIP-BEGIN
import os
import tempfile
import threading

import orca
from app import FarmApp, WorkerPool
from core import PrinterStore, auto_open_begin, auto_open_done, data_dir_of, saved_store_files
PANEL_HTML = ""
# BUNDLE-STRIP-END

PLUGIN_TITLE = "Farm Send"


def _data_dir_from_file():
    """OrcaSlicer's data folder, derived from where this file is installed."""
    try:
        return data_dir_of(__file__)
    except NameError:
        return ""


def _gcode_temp_trees():
    """Folders OrcaSlicer writes the per-plate G-code into after a slice.

    2.5 uses a per-user temp root, <tmp>/orcaslicer_<uid>/orcaslicer_model; earlier builds wrote
    straight to <tmp>/orcaslicer_model. Both are declared so one prompt at load covers either."""
    temp = tempfile.gettempdir()
    trees = [os.path.join(temp, "orcaslicer_model", "*")]
    if hasattr(os, "getuid"):
        trees.insert(0, os.path.join(temp, "orcaslicer_%d" % os.getuid(), "orcaslicer_model", "*"))
    return trees


def _saved_hosts():
    """Printer hosts saved by an earlier run, read without the plugin storage API because that API
    only works inside a capability call and permissions are declared before any exists."""
    base = _data_dir_from_file()
    if not base:
        return []
    hosts = set()
    try:
        plugin_file = __file__
    except NameError:
        plugin_file = ""
    for path in saved_store_files(base, plugin_file):
        hosts.update(PrinterStore(path).hosts())
    return sorted(hosts)


class FarmSend(orca.script.ScriptPluginCapabilityBase):
    panel = None
    pool = None
    app = None
    opened_once = False     # the panel was opened this session, by the run arrow or by itself

    def get_name(self):
        return PLUGIN_TITLE

    # ---- wiring ------------------------------------------------------------------------------
    def _start_workers(self):
        """Start the background workers. Only called from execute(), where OrcaSlicer knows this
        plugin is the one running; never from a panel message."""
        if self.pool is None:
            self.pool = WorkerPool()
        self.pool.start()

    def _ensure_app(self):
        if self.pool is None:
            self.pool = WorkerPool()
        if self.app is None:
            storage = orca.host.plugin.storage()
            self.app = FarmApp(storage, self._post, self._notify, (), self._default_name,
                               read_presets=self._read_presets, apply_presets=self._apply_presets,
                               list_presets=self._list_printer_presets, spawn=self.pool.submit)
        return self.app

    def _post(self, message):
        panel = self.panel
        if panel is not None:
            panel.post(message)

    def _notify(self, level, text):
        levels = orca.host.ui.NotificationLevel
        kind = levels.ErrorNotificationLevel if level == "error" else levels.RegularNotificationLevel
        try:
            orca.host.ui.push_notification(kind, text)
        except Exception:
            pass

    def _default_name(self):
        # Only called from panel messages, which run on the UI thread where the model is safe to read.
        try:
            objects = orca.host.model().objects()
            return objects[0].name if objects else ""
        except Exception:
            return ""

    def _read_presets(self):
        # UI thread only (panel messages).
        bundle = orca.host.preset_bundle()
        printer = bundle.current_printer_preset()
        return {
            "model": printer.config_value("printer_model") or "",
            "printer": printer.name,
            "process": bundle.current_process_preset().name,
            "filaments": list(bundle.current_filament_preset_names()),
        }

    def _list_printer_presets(self):
        """The user's printer presets as OrcaSlicer has them loaded right now. UI thread only.

        Asked from OrcaSlicer, not read from preset files: a signed-in user has two preset
        folders on disk and only OrcaSlicer knows which one is in use."""
        printers = orca.host.preset_bundle().printers
        found = []
        for index in range(printers.size()):
            preset = printers.preset(index)
            if not preset.is_user():
                continue
            entry = {"name": preset.name}
            for key in ("print_host", "printhost_apikey", "host_type", "printer_model", "inherits", "nozzle_diameter"):
                entry[key] = preset.config_value(key)
            found.append(entry)
        return found

    def _apply_presets(self, combo):
        """Switch OrcaSlicer to a saved printer/filament/process selection.

        Runs on a plugin thread, never the UI thread: each call hands the selection to the UI thread
        and waits, including for OrcaSlicer's own save/discard dialog when a preset has unsaved
        edits. begin_update() is used only for its selection step; nothing is staged or committed.
        Printer first, because it decides which filaments and processes can be chosen.
        Returns the parts that could not be applied; raises if the printer could not be selected."""
        skipped = []
        orca.host.preset.printer.begin_update(preset_name=combo["printer"])
        for index, name in enumerate(combo.get("filaments") or []):
            try:
                orca.host.preset.filament.begin_update(index + 1, name)
            except IndexError:
                break      # this printer selection has fewer filament slots than were saved
            except (ValueError, RuntimeError) as error:
                skipped.append("filament %d (%s)" % (index + 1, str(error).splitlines()[0][:120]))
        if combo.get("process"):
            try:
                orca.host.preset.process.begin_update(preset_name=combo["process"])
            except (ValueError, RuntimeError) as error:
                skipped.append("process (%s)" % str(error).splitlines()[0][:120])
        return skipped

    # ---- capability entry points -------------------------------------------------------------
    def execute(self):
        self.opened_once = True
        self._start_workers()
        self._ensure_app()
        if self.panel is not None and self.panel.is_open():
            self.panel.show()
            return orca.ExecutionResult.success("Farm Send is already open.")
        self.panel = orca.host.ui.create_dock_panel(
            html=PANEL_HTML, title=PLUGIN_TITLE, width=340, height=560,
            on_message=self._on_message, on_close=self._on_close, dock="right")
        return orca.ExecutionResult.success("Farm Send opened.")

    def _on_message(self, message):
        try:
            self._ensure_app().handle(message)
        except Exception as error:
            self._notify("error", "Farm Send: %s" % error)

    def _on_close(self):
        self.panel = None

    def on_lifecycle_event(self, event, ctx):
        # Slice events arrive on the slicing worker thread. Nothing here may wait on the UI thread.
        events = orca.LifecycleEvent
        try:
            if not self.opened_once and threading.current_thread() is threading.main_thread():
                # OrcaSlicer has no run-at-start for plugins. The first event it delivers on the UI
                # thread (it sends a few while starting) opens the panel instead, once per session,
                # if the user wants it. Slice events come from a worker thread and never do this.
                self.opened_once = True
                app = self._ensure_app()
                if app.store.auto_open:
                    folder = orca.host.plugin.storage()
                    if not auto_open_begin(folder):
                        # The last attempt never finished. Turn the setting off rather than risk
                        # taking OrcaSlicer down at every start; the Plugins dialog still opens the panel.
                        auto_open_done(folder)
                        app.store.set_auto_open(False)
                    else:
                        try:
                            self.execute()
                        except Exception:
                            self.opened_once = False    # the window was not ready yet: try the next event
                        auto_open_done(folder)
            if event == events.GCodeExportFinished:
                app = self._ensure_app()
                if ctx.code == orca.LifecycleEvtCode.Ok:
                    app.on_gcode_exported(ctx.msg, ctx.name)
                else:
                    app.on_slice_done()
            elif self.app is None:
                return
            elif event == events.SliceStarted:
                self.app.on_slice_started()
            elif event == events.SlicingJobComplete:
                self.app.on_slice_done()
            elif event in (events.ObjectAdded, events.ObjectDeleted, events.ObjectTransformed, events.ObjectChanged):
                # Preset changes are deliberately not treated as "plate changed": switching presets
                # is exactly what happens between slicing for one printer type and the next.
                self.app.mark_stale()
            elif event in (events.ProjectClosed, events.NewProject, events.ProjectOpened):
                self.app.project_closed()
        except Exception:
            pass


@orca.plugin
class FarmSendPlugin(orca.base):
    def register_capabilities(self):
        hosts = ["http://" + host for host in _saved_hosts()]
        orca.request_permissions(fs_read=_gcode_temp_trees(), network=hosts, threading=True)
        orca.register_capability(FarmSend)
