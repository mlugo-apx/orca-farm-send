# Plugin Hub listing (copy into cloud.orcaslicer.com > Plugins > Shared Plugins > +)

| Field | Value |
|---|---|
| File | `dist/farm_send_any.py` |
| Name | Farm Send |
| Version | 0.3.2 |
| Type | Script |
| Min. OrcaSlicer version | 2.5.0 (the only choice the form offers) |
| Icon | `docs/icon.png` (square; the hub stores it at 320 x 320) |
| Description media | `docs/screenshot.png` |
| Tags | print farm, send, multi printer, elegoo, centauri carbon 2, snapmaker u1, moonraker |
| Public Plugin | on |

## Description

Farm Send is a docked panel for small print farms. Slice a plate once per printer type, then upload it and start it on several printers at once.

- Elegoo Centauri Carbon 2: upload and start. Stock OrcaSlicer can upload to this printer but cannot start it.
- Snapmaker U1: upload and start, with bed leveling, flow calibration and time-lapse options.
- Live status for every printer: progress, time left, expected finish time, temperatures.
- Printers grouped into Needs attention, Available and Busy.
- A notification when a print finishes, pauses or fails.
- A "Bed cleared" step so a new print is never started onto a finished one.
- Stored-file counts per printer and cleanup of old G-code by age or count.
- G-code sliced for one printer model is never sent to another.
- A library of kept plates: send a part you print often without opening the project or slicing again. On the Snapmaker U1, pick the toolhead when you send, so one kept plate covers every color.
- Library folders, with folders inside folders, to keep kept plates organized.
- Print only some parts of a kept plate on a Snapmaker U1: the printer skips the rest, so one full plate covers any quantity.
- Orders: enter a quantity and it is split evenly over the free printers, with a count of good parts.
- Every plate of a multi-plate project is held after Slice all.

Early release. Developed and tested on Linux with two Centauri Carbon 2s and three Snapmaker U1s (firmware 2.1.2). Windows and macOS are untested; reports welcome. It talks only to the printer addresses you save: no cloud, no telemetry.

Found a problem, or want something added? Click **Report a problem or request an enhancement** at the bottom of the panel and open an issue at https://github.com/mlugo-apx/orca-farm-send/issues

It starts real prints. Look at the bed before you confirm.

## Changelog

First public release. See CHANGELOG.md.
