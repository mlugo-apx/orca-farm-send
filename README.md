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
python3 tests/test_farm.py            # 47 tests against in-process fake printers
python3 tests/hardware_check.py       # read-only check of your real printers
python3 tests/hardware_check.py --upload "U1 #2"   # 1 KB upload, list, delete. Never prints.
./install.sh                          # rebuild and copy over the installed plugin
```

`src/core.py` holds the printer protocols, `src/app.py` the logic, `src/panel.html` the UI, `src/shell.py` the OrcaSlicer glue.
`build.py` bundles them into the single file OrcaSlicer requires.

## How it works

- **Capture**: OrcaSlicer fires a `GCodeExportFinished` lifecycle event after each slice with the path of the plate's temp G-code. No preset or post-processing change is needed. Post-processing scripts are therefore not applied; the panel warns when the preset has any.
- **CC2**: chunked HTTP `PUT /upload` with an MD5 header, then MQTT on port 1883 (register, status `1002`, start `1020`). One client id per printer is reused, because the printer has only a few client slots and holds each for about a minute.
- **Moonraker**: `/server/files/upload` with `print=false`, then `/printer/print/start`.

## Verification status (2026-11-18, OrcaSlicer 2.5.1, all five printers mid-print)

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
| Start print, CC2 and U1 | Fake printers only. No real print was started |
| Reading the captured G-code inside OrcaSlicer | Not yet. Permission is now granted; needs one slice in the app |
| Slice-for preset switching | Fake host only. Not yet clicked inside OrcaSlicer |
