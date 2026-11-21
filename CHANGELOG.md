# Changelog

## 0.3.8

- A Snapmaker card now lists what each toolhead holds, as small tags under "details", the same way a Centauri Carbon 2 card lists its rolls. An empty toolhead says so.

## 0.3.7

- **Clearer printer cards.** Cards have a visible gray outline and a lighter fill, a colored edge and a state label for the printer's state (green idle, blue printing, amber needs attention, red error), and lighter text for the details. The finish time stands on its own at the right, and a folded busy printer shows a thin progress line. The rolls of a Centauri Carbon 2 are shown as small tags.
- **Less on each card.** Temperatures, the rolls and the time of the last check are behind a "details" link on the card. A printer that is printing and cannot take a file no longer shows the two greyed-out send buttons.

## 0.3.6

- **Fix: a Centauri Carbon 2 now prints from the roll the plate was sliced with.** Filament 1 prints from roll 1, filament 2 from roll 2, and so on. Before, the start named no roll and the printer reused the roll of its previous print, so a plate sliced with filament 1 could print from roll 2. The start message says which roll was named.
- A Centauri Carbon 2 card lists the rolls in its Canvas unit, as the printer reports them. The colors are the ones entered on the printer.
- A plate that prints with a filament number the printer has no roll for is not started.
- The printer groups (Needs attention, Available, Busy) fold: click a heading to hide or show its printers. A folded group still shows a printer that is selected or sending.

## 0.3.5

- Clicking **Bed cleared** now also removes the note about how the last print ended, so a free printer no longer shows its previous file.
- Print history names the plate for a print sent straight from "Sliced now". It recorded the printer's name there.

## 0.3.4

- **Print history.** Every print started from the panel is recorded: what, on which printer, when, how many parts, and how it ended (finished, cancelled, error, or stopped early). A good-parts count entered at "Bed cleared" is added to it. **Print history** at the bottom of the panel shows the list with a count per printer, and copies it as CSV. Prints started at the printer are not recorded, and an ending is only seen while the panel is open.
- The printer tag on a library row now sits in front of the name, where a long name cannot push it out of view.

## 0.3.3

- **Fix: a second OrcaSlicer window no longer removes the first window's sliced plates.** The copies of sliced plates were kept in one shared folder that every window emptied when its panel started, and since 0.3.2 the panel starts by itself. Sending or saving a plate from the first window then failed until it was sliced again. Each window now keeps its own copies.
- **A tidier "Sliced now".** Each sliced plate is one line; click it for the details. The printer type OrcaSlicer is set to stays open and the other types fold to one line, so slicing for a second printer type no longer doubles the list.
- **Save all to library.** One dialog for every plate of a project not saved yet: a name, a parts count and a folder for each, with one "folder for all" on top.
- A plate that is already in the library shows "saved" instead of the Save button.
- The parts count is filled in from the labelled objects in the G-code where there are any, and a suggested name includes the plate number.
- **clear** drops the sliced plates of one printer type. Library rows show a short printer tag such as U1 or CC2.
- The Sliced now / Library / Orders tabs no longer break onto two lines in a narrow panel.

## 0.3.2

- **The panel opens by itself when OrcaSlicer starts.** OrcaSlicer has no run-at-start for plugins, so the plugin opens its panel on the first event OrcaSlicer sends it while starting. Once per session: close it and it stays closed. A checkbox at the bottom of the panel turns this off. If OrcaSlicer ever goes down while the panel is opening itself, the setting turns itself off on the next start.
- **Library:** the Save to library and Edit dialogs can make a new folder on the spot, inside the folder picked. Folders start collapsed; the folder you just saved into opens.

## 0.3.1

- The link at the bottom of the panel now reads "Report a problem or request an enhancement", and the GitHub repository has a form for each.

## 0.3.0

- **Print only some parts of a kept plate (Snapmaker U1).** The library send dialog has "Parts to print per printer". Below the full count, the printer is told to skip the other parts as it starts, and the panel waits for the printer to confirm before it reports the start. If the printer does not confirm, the print is cancelled. One full plate in the library now covers any quantity. It needs a plate whose parts are labelled one by one (OrcaSlicer's "Label objects", on by default for Klipper printers) and a parts count that matches. The time and grams shown stay those of the full plate. Verified on a U1 up to the printer confirming 26 of 28 parts skipped before any extrusion; a print of the remaining parts has not been run yet.
- **Orders are back, split evenly.** An order of 10 over five free printers is 2 each on printers that can skip parts. A printer that prints whole plates only, such as a Centauri Carbon 2, gets the kept plate nearest its share and is left out when that plate would be more than double the share. Each count can be edited before starting.
- **Every plate of a project is held.** After Slice all, each plate has its own row and Save to library button, and you choose which one the printer cards send. Re-slicing a plate replaces that plate only.
- A print cancelled before it extruded anything (still heating or leveling) no longer flags the bed as needing to be cleared. Klipper printers only; a Centauri Carbon 2 does not report this.
- **Color check for library sends.** Printing as sliced is refused when the toolhead reports a clearly different color than the plate was sliced with, until you confirm the loaded color in the dialog. The dialog also warns when the ticked printers hold different colors in the chosen toolhead.
- **Report a problem.** A link at the bottom of the panel collects the plugin version, system, and each printer's model and state for a GitHub issue. Printer names, addresses and access codes are left out.

## 0.2.3

- **Fixes "HTTP 429 from printer" when sending to a Centauri Carbon 2.** Any plate larger than about 3 MB failed partway through the upload. The plugin opened a new connection for every 1 MB piece, and the printer refuses a burst of connections. The upload now uses one kept-open connection, as OrcaSlicer's own Elegoo uploader does, and a busy reply or dropped connection is retried instead of failing the send. A 23 MB plate that failed before now uploads in about 5 seconds.

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
