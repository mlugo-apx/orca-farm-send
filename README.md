# Farm Send for OrcaSlicer

A docked panel that sends the plate you just sliced to your printers, with live status.
Built for OrcaSlicer 2.5.1's Python plugin system.

- **Elegoo Centauri Carbon 2**: upload, and start the print. Stock OrcaSlicer can upload to a CC2 but cannot start it.
- **Snapmaker U1 / any Moonraker printer**: upload, and start the print.
- Not covered: Kobra 2 Max (uses the Desktop-to-Pi helper), MK3S.

## Use

1. Slice the plate once for each printer type you want to print on. Every slice is kept in the panel, one per type, with time, grams, layers and the type it was sliced for.
2. On a printer card click **Upload**, or **Upload and print**, and confirm. The printer gets the plate sliced for its type.

To send everything at once, click **select all with a plate**, then **Upload and print on all**.
The U1s get the U1 plate and the CC2s get the CC2 plate, in one action.

The plugin keeps its own copy of each slice, because OrcaSlicer overwrites its temp G-code on the next slice.
Slicing again for a type replaces that type's plate only.
You can re-arrange the bed between types: the "plate changed" warning only ever applies to the most recent slice. Copies are deleted when the project closes.

**Slice for (presets per printer type)**

Select the printer, process and filament presets you use for one printer type, then click **remember current**.
The panel keeps one entry per type, for example one for the Centauri Carbon 2 and one for the U1.
**Use** switches all three OrcaSlicer selections back in one click. Then slice.
If a preset has unsaved edits, OrcaSlicer shows its normal save or discard dialog first.
The plugin cannot press Slice for you.

**Guards**

- G-code sliced for one printer model cannot be sent to another model. Checked in the plugin, not only in the page.
- "Upload and print" only starts when the printer reports idle. If it is busy the file is still uploaded and you are told it was not started.
- Every start asks for confirmation and names the printers.
- Uploaded files are named `<object>_<filament>_<time>_<YYYYMMDD-HHMMSS>.gcode`, so nothing is overwritten.
- A printer with no plate sliced for its type cannot be sent to.

## Install

1. OrcaSlicer > menu > **Plugins** > **Install local plugin** > `dist/farm_send/farm_send.py`
2. Tick **Activate**. OrcaSlicer asks once for: read access to its own temp G-code folder, permission to start threads, and network access to each saved printer address. Answering No stops the plugin from loading.
3. Expand the row and click the run arrow. The panel docks to the right of the 3D view.
4. **Import** pulls printers from your user printer presets (address and access code). **Add printer** adds one by hand; **Test connection** fills in the model.

The panel has to be opened from the Plugins dialog once per OrcaSlicer session.
After adding a printer, OrcaSlicer asks for network access to it the next time the plugin loads.

Printers are stored in `<OrcaSlicer config>/orca_plugins/plugin_data/farm_send/farm_send_printers.json`, readable only by you because it holds access codes.

## Develop

```
python3 tests/test_farm.py            # 52 tests against in-process fake printers
python3 tests/hardware_check.py       # read-only check of your real printers
python3 tests/hardware_check.py --upload "U1 #2"   # 1 KB upload, list, delete. Never prints.
./install.sh                          # rebuild and copy over the installed plugin
```

`src/core.py` holds the printer protocols, `src/app.py` the logic, `src/panel.html` the UI, `src/shell.py` the OrcaSlicer glue.
`build.py` bundles them into the single file OrcaSlicer requires.

## How it works

- **Capture**: OrcaSlicer fires a `GCodeExportFinished` lifecycle event after each slice with the path of the plate's temp G-code. No preset or post-processing change is needed. Post-processing scripts are therefore not applied; the panel warns when the preset has any.
- **CC2**: chunked HTTP `PUT /upload` with an MD5 header, then MQTT on port 1883 (register, status `1002`, start `1020`). One client id per printer is reused, because the printer has only a few client slots and holds each for about a minute.
- **Snapmaker U1**: `/server/files/upload` with `print=false`, then `server.files.start_local_print` over the websocket with `bed_level`, `flow_calibrate` and `time_lapse_camera` options. That method does not exist over HTTP. A plain Moonraker start carries no options, and the U1 then skips auto bed leveling. Stock OrcaSlicer's U1 start G-code tries `SET_PRINT_AUTO_BED_LEVELING`, which firmware 2.1.2 rejects as unknown.
- **Other Moonraker printers**: fall back to `/printer/print/start`.
- The plate box and the confirmation show the toolhead, colour and grams the G-code uses, and what the printer reports as loaded in that toolhead.

## Verification status (2026-11-18, OrcaSlicer 2.5.1, all five printers mid-print)

In-app checks ran in an OrcaSlicer instance on a hidden virtual display (Xvfb), started through the desktop launcher.

| What | How it was verified |
|---|---|
| Status, all 5 printers | Real printers |
| Upload, list, delete on U1 #1, #2, #3 | Real printers, during a print, print unaffected |
| Upload-and-print refused on a busy U1 | Real printer: file uploaded, not started |
| Cross-model block | Real sliced G-code, refused before any network call |
| CC2 slot leak fixed | Real CC2: 40 polls in 65 s, second client still connects |
| Install, activate, panel, Import, live cards | Inside OrcaSlicer 2.5.1 |
| Slice capture event | Inside OrcaSlicer 2.5.1: event received with the temp path |
| CC2 upload | Fake printer only. Both CC2s were printing |
| Upload and start on U1 #1 and #2, single and batch | Real printers, 2026-11-19, 24-clip plate in white on toolhead 2 |
| U1 start with bed leveling option | Real printers: leveling flag on, and the printer's "does not do auto-leveling" message no longer appears |
| Start print on CC2 | Fake printer only |
| Slice capture, copy and plate display | Inside OrcaSlicer 2.5.1 |
| Slice for U1, switch presets, slice for CC2, both plates kept | Inside OrcaSlicer 2.5.1 |
| Slice-for "Use" switching printer, filament slots and process, both directions | Inside OrcaSlicer 2.5.1 |
| Select all with a plate, grouped confirmation | Inside OrcaSlicer 2.5.1, cancelled before sending |
