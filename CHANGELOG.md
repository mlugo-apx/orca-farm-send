# Changelog

## 0.2.2

- **Library folders, with folders inside folders.** **New folder** makes one at the top; the plus on a folder makes one inside it (up to four levels). The pencil and bin rename or remove a folder. Renaming carries everything inside along. Removing a folder keeps what is in it: its plates and subfolders move up one level. Pick a folder when you save a plate, or move a plate later with its pencil. Folders open and close one by one or with expand all / collapse all, a folder shows how many plates are inside it at any depth, and a search looks in every folder.
- **Edit a kept plate.** The pencil on a library row renames the item, corrects the parts count or moves it to another folder. The G-code is not touched.
- "Save to library" is now a button on the sliced plate's row.
- **Orders are switched off for now.** The tab planned whole plates only; splitting a small order into a few parts per printer needs a different approach, so the tab is hidden until that exists. Nothing else depends on it. "toolhead names" moved to the Library view.

## 0.2.1

- Fixes 0.2.0, where a sliced or kept plate no longer showed its filament color: the plugin read the wrong setting name from the G-code. Use 0.2.1 instead of 0.2.0.

## 0.2.0

- **Library.** Click "save to library" on a sliced plate, give it an item name and the number of parts on it, and it is kept on this computer. The Plates box has a Library view with search; Send uploads a kept plate to the printers you tick and starts them, without opening the project or slicing again.
- **Pick the toolhead at send time (Snapmaker U1).** A one-filament plate can be printed from any toolhead, so one kept file covers every color. The send dialog lists what each printer's toolheads hold. A send is refused when the chosen toolhead reports no filament or holds a different material than the plate was sliced for. Material and temperatures stay as sliced.
- The usual guards apply to library sends: printer type, nozzle size and the bed-cleared step.
- **Orders.** An order is a quantity of one library item, with an optional toolhead and note. **Print next** proposes kept plates for the printers that are free right now (largest plate that does not overshoot, a few extras allowed) and starts the ones you tick. The order shows good, printing and still-to-start counts and survives a restart.
- **Good parts at Bed cleared.** When the plate on a bed belonged to an order, Bed cleared asks how many parts came out good. Only those count; a plate that failed defaults to 0.
- **Toolhead names.** Optional labels for the four toolhead positions (for example Black, White), shown wherever a toolhead is picked. Labels only: color is not checked.
- Two OrcaSlicer windows no longer overwrite each other's saved printers, presets, cleanup rule or orders: each change is applied on top of what the other window saved.

## 0.1.4

- A plate sliced for one nozzle size is no longer sent to a printer set up with another. The printer's nozzle size is taken from its printer preset when you click Import; click Import once to record it for printers you already added.
- On Klipper printers other than the Snapmaker U1, start options (bed leveling, time-lapse) cannot be passed. The panel now says so after starting, instead of implying they were applied.

## 0.1.3

- Fixed: with another plugin's tab open, OrcaSlicer could raise a flood of permission prompts naming that other plugin but listing your printers' addresses. Farm Send's background work now runs on workers started once when the panel opens, which OrcaSlicer credits to Farm Send correctly.
- Fixed: OrcaSlicer could ask to allow an "open" or "os.chmod" operation with no visible target, repeatedly, when the plugin saved its settings from a background worker. Settings files are now written only with calls that name the file.

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
