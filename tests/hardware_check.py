#!/usr/bin/env python3
"""Check Farm Send against your real printers, outside OrcaSlicer.

Default is read-only: identify + status for every printer saved in the plugin.
  --upload NAME   also upload a 1 KB probe file to that printer, list it, and delete it again.
                  Never starts a print. For a Centauri Carbon 2, run it while the printer is idle.
"""
import argparse
import glob
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import core  # noqa: E402

STORE_GLOB = os.path.expanduser("~/.config/OrcaSlicer/orca_plugins/plugin_data/*/farm_send_printers.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--upload", metavar="NAME", help="printer name to run the upload/list/delete probe on")
    parser.add_argument("--store", help="path to farm_send_printers.json (default: the installed plugin's)")
    args = parser.parse_args()
    paths = [args.store] if args.store else glob.glob(STORE_GLOB)
    if not paths:
        sys.exit("No saved printers found. Open the Farm Send panel in OrcaSlicer and use Import first.")
    printers = core.PrinterStore(paths[0]).printers
    failures = 0
    for printer in printers:
        client = core.make_client(printer)
        try:
            info, status = client.identify(), client.status()
            line = "%-8s %-15s %-9s %s" % (printer["name"], printer["address"], status["state"], info.get("model", ""))
            if status.get("filename"):
                line += "  [%s %s%%]" % (status["filename"][:40], "?" if status["progress"] is None else round(status["progress"]))
            print("OK   " + line)
        except core.FarmError as error:
            failures += 1
            print("FAIL %-8s %-15s %s" % (printer["name"], printer["address"], error))
            continue
        if args.upload and printer["name"] == args.upload:
            if printer["kind"] == core.KIND_CC2 and status["state"] != "idle":
                print("     skipped upload probe: run it when this Centauri Carbon 2 is idle (it is %s)" % status["state"])
                continue
            with tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False) as handle:
                handle.write("; Farm Send upload probe. Safe to delete.\nM117 probe\n")
            try:
                name = client.upload(handle.name, core.job_filename("farmsend_probe"))
                listed = name in client.list_files()
                client.delete_file(name)
                gone = name not in client.list_files()
                print("     upload probe: uploaded=%s listed=%s deleted=%s" % (name, listed, gone))
                failures += 0 if (listed and gone) else 1
            except core.FarmError as error:
                failures += 1
                print("     upload probe FAILED: %s" % error)
            finally:
                os.unlink(handle.name)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
