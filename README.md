# Farm Send for OrcaSlicer

A docked panel for small print farms: slice a plate once per printer type, then upload it and start it on several printers at once, with live status for every printer.

![Farm Send panel](docs/panel.png)

- **Elegoo Centauri Carbon 2**: upload and start the print. Stock OrcaSlicer can upload to a Centauri Carbon 2 but cannot start it.
- **Snapmaker U1**: upload and start the print, with bed leveling, flow calibration and time-lapse options.
- **Other Moonraker / Klipper printers**: upload and a plain start. Not tested beyond the U1.

Each printer card shows state, progress, time left with the expected finish time, temperatures and when it was last checked. Status updates on its own every few seconds; **Refresh** in the header asks every printer now, and the refresh icon on a card asks that one.

While the panel is open, OrcaSlicer shows a notification when a print finishes, is cancelled, pauses, or a printer stops with an error. The same note stays on the printer card until the printer moves on. Nothing is reported while OrcaSlicer is closed, and the notification appears inside OrcaSlicer, not as a system notification.

## Status: early release

Farm Send uses OrcaSlicer's Python plugin system (OrcaSlicer 2.5.1 or newer), which OrcaSlicer itself calls experimental.

| | |
|---|---|
| Developed and tested on | Linux, OrcaSlicer 2.5.1 |
| Windows, macOS | **Not tested.** Reports welcome |
| Centauri Carbon 2 | Tested on two printers, single-filament jobs. Multi-color jobs through the multi-color unit are not tested |
| Snapmaker U1 | Tested on three printers, firmware 2.1.2. Slicer filament slot N prints on toolhead N |

The printer protocols were worked out from the vendors' own clients, not from published specifications. A firmware update can break them.

**It starts real prints on real machines.** Look at the bed before you confirm.

## Use

The panel reads top to bottom in the order you work:

1. **Plates** has one row per printer type. Each row shows the plate held for that type (time, grams, layers, toolhead and color), or "Not sliced yet". Slice once for each type you want to print on.
2. The printer list is grouped: **Needs attention** (paused, error, offline, or a finished print still on the bed), **Available** (idle with a clear bed) and **Busy** (one line each; click a line for details).
3. On a printer card click **Upload**, or **Upload and print**, and confirm. The printer gets the plate sliced for its type.

To send everything at once, click **select all available**, then **Upload and print on all**.
The U1s get the U1 plate and the CC2s get the CC2 plate, in one action.

The plugin keeps its own copy of each slice, because OrcaSlicer overwrites its temp G-code on the next slice.
Slicing again for a type replaces that type's plate only.
You can re-arrange the bed between types: the "plate changed" warning only ever applies to the most recent slice. Copies are deleted when the project closes.

**Presets per printer type**

Select the printer, process and filament presets you use for one printer type, click **presets** on that type's row, then **Remember current**.
**Use presets** on the row then switches all three OrcaSlicer selections back in one click. Then slice.
If a preset has unsaved edits, OrcaSlicer shows its normal save or discard dialog first.
The plugin cannot press Slice for you.

**Library**

For parts you print again and again. On a sliced plate's row click **Save**, name the item and enter how many parts are on the plate (the count is filled in when the G-code labels its parts). A plate already in the library shows **saved**.
The plate is kept on this computer, in the plugin's own data folder, and survives closing the project and restarting OrcaSlicer.
You can also put it in a folder. The save dialog can make a new folder on the spot, inside the folder you picked. Folders start collapsed. **New folder** in the Library view makes one at the top, and the plus on a folder makes one inside it, up to four levels. The pencil and bin on a folder rename or remove it; removing a folder keeps what is in it and moves it up one level. The pencil on a library row renames the item, corrects the count or moves it to another folder.
In the Plates box, **Library** lists what you kept, grouped by folder, with search. **Send** shows the printers of the right type with their state, and the total parts for the printers you tick, then uploads and starts them. No project, no slicing.

A kept plate is the G-code exactly as sliced: printer type, nozzle, material and temperatures do not change. To change any of those, slice again and save a new plate.
On Snapmaker U1s a one-filament plate can be printed from any toolhead, so one kept plate covers every color: choose the toolhead in the send dialog, which lists what each printer's toolheads hold.
The same toolhead number is used on every printer you tick, so this assumes your printers keep the same color in the same position.
A start is refused when that toolhead reports no filament or holds a different material. Printing as sliced is also refused when the toolhead reports a clearly different color than the plate was sliced with, until you confirm the loaded color in the dialog. When you pick another toolhead, the dialog shows what each printer holds there and warns if they differ.
Verified on a U1 up to the point where the printer heats the chosen toolhead; a complete print through a remapped toolhead has not been run yet.

**Printing part of a plate (Snapmaker U1)**

In the library send dialog, **Parts to print per printer** defaults to the full plate. Enter less and the printer is told to skip the other parts as it starts; the panel reports the start only after the printer confirms, and cancels the print if it does not. So one full plate in the library covers any quantity.
This works when the plate's parts are labelled one by one in the G-code and that number equals the parts count you entered. The kept parts are the first ones in the plate's own order. The time and grams shown are still those of the full plate.
A Centauri Carbon 2 prints whole plates only, so keep a small plate for it if you want small quantities there.

**Orders**

For "I need 10 of these". In the Plates box open **Orders**, click **New order**, pick a library item and enter the quantity. Optionally pick the toolhead for Snapmaker printers and add a note.
**Print next** splits what is left evenly over the printers that are free right now: idle, bed cleared, and no uncounted plate on them. Ten parts over five printers that can skip parts is two each. A printer that prints whole plates only gets the kept plate nearest its share, and is left out when that plate would be more than double the share. Edit any count, untick what you do not want, then **Start prints**.
When a plate finishes, **Bed cleared** on that printer asks how many parts came out good. Only good parts count. The order line shows good, printing and still to start, and is remembered across restarts.

**Several plates in one project**

After **Slice all**, every plate is held for its printer type, one line each; click a line for its details. **Save all to library** names and keeps every plate not saved yet in one dialog, each with its own name, parts count and folder. The dot in front of a plate marks the one the printer cards send; click another plate's dot to change it. The printer type OrcaSlicer is set to stays open and the others fold to one line; **clear** drops the sliced plates of a type. Slicing a plate again replaces that plate only.

**Toolhead names**

**toolhead names** in the Library view lets you label the four toolhead positions (Black, White and so on) if you keep them the same on every printer. These are labels only; the color check compares what the printer reports with what the plate was sliced with.

**Stored files and cleanup**

The **Stored files** box at the bottom lists every printer with its file count, size and oldest file, and how many files count as old under your rule.
**change** sets the rule: keep files newer than N days, or keep the newest N files. **Clean up** shows what will be removed per printer and asks before deleting.
The file a printer is printing is always kept. A Centauri Carbon 2 is only cleaned while idle. Deleted files cannot be brought back.
"Clean automatically, once a day" is off by default and only runs while the panel is open, so it does not replace a scheduled job for an unattended farm.
A Centauri Carbon 2 holding 250 or more files shows a warning on its card: uploads began failing on printers holding 355 and 516 files.

**Guards**

- G-code sliced for one printer model cannot be sent to another model. Checked in the plugin, not only in the page.
- "Upload and print" only starts when the printer reports idle. If it is busy the file is still uploaded and you are told it was not started.
- Every start asks for confirmation and names the printers.
- Uploaded files are named `<object>_<filament>_<time>_<YYYYMMDD-HHMMSS>.gcode`, so nothing is overwritten.
- A printer with no plate sliced for its type cannot be sent to.
- A plate sliced for a different nozzle size than the printer preset records is refused. Click **Import** once so the plugin knows each printer's nozzle size; a printer with no size on record is not checked.
- After a print ends, the printer is held under Needs attention and cannot be started again until you click **Bed cleared** on its card. Uploading without printing is still allowed. This is remembered across restarts and shared between OrcaSlicer windows. It only knows about prints the panel saw, or a printer that reports a completed print when first seen, so still look at the bed.

## Install

1. OrcaSlicer > menu > **Plugins** > **Install local plugin** > `dist/farm_send/farm_send.py`
2. Tick **Activate**. OrcaSlicer asks once for: read access to its own temp G-code folder, permission to start threads, and network access to each saved printer address. Answering No stops the plugin from loading.
3. Expand the row and click the run arrow. The panel docks to the right of the 3D view.
4. **Import** pulls printers from the printer presets OrcaSlicer has loaded (address and access code). **Add** adds one by hand; **Test connection** fills in the model.

From then on the panel opens by itself when OrcaSlicer starts. It opens once per session, so closing it keeps it closed. Untick **Open this panel when OrcaSlicer starts** at the bottom of the panel to open it only from the Plugins dialog.
After adding a printer, OrcaSlicer asks for network access to it the next time the plugin loads.

If you install both the Plugin Hub copy and a local file copy, only one can be active at a time; OrcaSlicer reports a "capability collision" for the second. Each copy keeps its own printer list.

## Permissions and privacy

- **Network**: only the printer addresses you save. The plugin contacts nothing else: no cloud, no telemetry, no update check.
- **Files**: read access to OrcaSlicer's temp G-code folder, to copy each slice. Everything it writes stays in its own storage folder.
- **Presets**: the panel reads which printer, process and filament presets are selected, and **Import** reads your own printer presets (name, model, nozzle size, address and API key) to offer them as printers. OrcaSlicer has no permission prompt for reading presets, so nothing asks you; nothing read this way leaves your computer except to reach the printer itself.
- **Access codes are stored as plain text** in `<OrcaSlicer config>/orca_plugins/plugin_data/farm_send/farm_send_printers.json`. The file is created readable only by your user account. Anyone with access to your account can read it, as with the printer presets OrcaSlicer itself stores.
- Printers are reached over plain HTTP, MQTT and websocket on your local network, as the vendors' own slicers do. Use it on a network you trust.

## Reporting a problem or requesting an enhancement

Open an issue at https://github.com/mlugo-apx/orca-farm-send/issues and pick the form that fits. For an enhancement, describe what you want to do and how you do it today. For a problem, click **Report a problem or request an enhancement** at the bottom of the panel first: it collects the plugin version, your system, and each printer's model and state, ready to paste. Printer names, addresses and access codes are left out; messages can include file names, so read it before posting. Add your OrcaSlicer version, the printer's firmware version, what you clicked, and the exact message shown.

## Develop

```
python3 tests/test_farm.py            # tests against in-process fake printers
python3 tests/hardware_check.py       # read-only check of your real printers
python3 tests/hardware_check.py --upload "U1 #2"   # 1 KB upload, list, delete. Never prints.
./install.sh                          # rebuild and copy over the installed plugin
```

`src/core.py` holds the printer protocols, `src/app.py` the logic, `src/panel.html` the UI, `src/shell.py` the OrcaSlicer glue.
`build.py` bundles them into the single file OrcaSlicer requires.

## How it works

- **Capture**: OrcaSlicer fires a `GCodeExportFinished` lifecycle event after each slice with the path of the plate's temp G-code. No preset or post-processing change is needed. Post-processing scripts are therefore not applied; the panel warns when the preset has any.
- **CC2**: chunked HTTP `PUT /upload` with an MD5 header, then MQTT on port 1883 (register, status `1002`, start `1020`). One client id per printer is reused, because the printer has only a few client slots and holds each for about a minute. Every OrcaSlicer window of one user shares that id and takes turns through a lock file in the plugin storage folder, so several windows together use one slot.
- **Snapmaker U1**: `/server/files/upload` with `print=false`, then `server.files.start_local_print` over the websocket with `bed_level`, `flow_calibrate` and `time_lapse_camera` options. That method does not exist over HTTP. A plain Moonraker start carries no options, and the U1 then skips auto bed leveling. Stock OrcaSlicer's U1 start G-code tries `SET_PRINT_AUTO_BED_LEVELING`, which firmware 2.1.2 rejects as unknown.
- **Other Moonraker printers**: fall back to `/printer/print/start`.
- The plate box and the confirmation show the toolhead, color and grams the G-code uses, and what the printer reports as loaded in that toolhead.

## What has been verified, and how

Tested 2026-11-18 and 19 on OrcaSlicer 2.5.1 (Linux) with two Centauri Carbon 2s ("CC1", "CC2") and three Snapmaker U1s.

In-app checks ran in an OrcaSlicer instance on a hidden virtual display (Xvfb), started through the desktop launcher.

| What | How it was verified |
|---|---|
| Status, all 5 printers | Real printers |
| Upload, list, delete on U1 #1, #2, #3 | Real printers, during a print, print unaffected |
| Upload-and-print refused on a busy U1 | Real printer: file uploaded, not started |
| Cross-model block | Real sliced G-code, refused before any network call |
| One CC2 slot shared by all windows | Real CC1 and CC2: 8 processes at once, 96 requests, no errors, one client id per printer |
| Refresh one printer and Refresh all, finish time, last-checked time | Clicked inside OrcaSlicer 2.5.1 against all five real printers |
| Notices for finished, cancelled, paused, error | Fake printers. Real CC2s confirmed to report 100% when idle after a completed print; no real print has ended with the panel open yet |
| Plates per printer type, grouped printer list, expandable busy rows, presets dialog, Bed cleared | Clicked inside OrcaSlicer 2.5.1 against all five real printers |
| Stored-file counts, cleanup rule and confirmation dialog | Real printers: counts and rule previews on all five; dialogs opened inside OrcaSlicer 2.5.1 and cancelled |
| Cleanup deleting files | Fake printers (250-file CC2, kept-file case). Real CC2: paging and batch delete proven with probe files only. No real user file has been deleted |
| Time left on a U1 | Real U1s: 4h55m shown 44 minutes into a 5h14m job (was 11 to 12 hours) |
| CC2 slot leak fixed | Real CC2: 40 polls in 65 s, second client still connects |
| Install, activate, panel, Import, live cards | Inside OrcaSlicer 2.5.1 |
| Slice capture event | Inside OrcaSlicer 2.5.1: event received with the temp path |
| CC2 upload, list, delete | Real CC1 and CC2 (2026-11-19) |
| Upload and start on U1 #1 and #2, single and batch | Real printers, 2026-11-19, 24-clip plate in white on toolhead 2 |
| U1 start with bed leveling option | Real printers: leveling flag on, and the printer's "does not do auto-leveling" message no longer appears |
| Start print on CC2, with bed leveling | Real CC1 and CC2 from the panel inside OrcaSlicer: 25-layer test tile completed on both (2026-11-19) |
| Slice capture, copy and plate display | Inside OrcaSlicer 2.5.1 |
| Slice for U1, switch presets, slice for CC2, both plates kept | Inside OrcaSlicer 2.5.1 |
| "Use presets" switching printer, filament slots and process, both directions | Inside OrcaSlicer 2.5.1 |
| Select all available, grouped confirmation | Inside OrcaSlicer 2.5.1, cancelled before sending |
