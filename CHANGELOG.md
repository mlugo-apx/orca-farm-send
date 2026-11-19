# Changelog

## 0.1.2

- Import now asks OrcaSlicer for the printer presets it has loaded, instead of reading preset files. A signed-in user has two preset folders on disk, and the old Import mixed printers from both.
- A copy installed from the Plugin Hub and a copy installed from a file no longer read each other's printer list, so neither asks for network access to the other's addresses.
- Note: the two copies cannot be active at the same time. Deactivate one before activating the other.

## 0.1.1

- Fixed: a copy installed from the OrcaSlicer Plugin Hub could not find its saved printers or your printer presets, because hub installs use a different folder layout than file installs. File installs were not affected.

## 0.1.0

First public release.

- Send a sliced plate to Elegoo Centauri Carbon 2 and Snapmaker U1 printers: upload, or upload and start.
- One plate kept per printer type; send to several printers at once, each getting the plate sliced for its type.
- Start options: bed leveling, flow calibration (Snapmaker), time-lapse.
- Live status per printer with time left and expected finish time; refresh one printer or all.
- Printers grouped into Needs attention, Available and Busy.
- Notification when a print finishes, is cancelled, pauses or errors.
- "Bed cleared" step before a printer can be started again.
- Remembered presets per printer type, switched in one click.
- Stored-file counts per printer and cleanup of old files by age or count.
- Guards: G-code only goes to its own printer model; every start is confirmed.
