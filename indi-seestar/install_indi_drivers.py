#!/usr/bin/env python

import os
import sys
from pathlib import Path

INDI_XML_DIR = Path("/usr/share/indi")
INDI_BIN_DIR = Path("/usr/bin")
SOURCE_DIR = Path(__file__).absolute().parent
DRIVER_NAMES = ["indi_seestar_scope", "indi_seestar_focuser",
                "indi_seestar_ccd", "indi_seestar_filterwheel"]


def install():

    while (proceed := input("You are about to install INDI drivers for the Seestar S50 mount, camera, "
                            "focuser, and filter wheel. Proceed? [Y/n] ")) not in ("y", "yes", "n", "no"):
        print(f"Unrecognized input '{proceed}'. Enter 'yes' or 'no'.")
    if proceed.lower().strip().startswith("n"):
        print("Aborting without action.")
        return 0

    if not INDI_XML_DIR.is_dir():
        print(f"Error: directory '{INDI_XML_DIR}' does not exist. Is the INDI library installed?")
        return 1
    xml_definition_file = SOURCE_DIR/"indi_seestar.xml"
    xml_destination = INDI_XML_DIR/xml_definition_file.name
    xml_destination.unlink(missing_ok=True)
    xml_destination.symlink_to(xml_definition_file)
    print(f"- Installed driver definition XML: '{xml_destination}'")

    driver_executable = SOURCE_DIR/"indi_seestar.py"
    driver_executable.chmod(driver_executable.stat().st_mode | 0o111) # ensure executability
    for driver_name in DRIVER_NAMES:
        driver_destination = INDI_BIN_DIR/driver_name
        driver_destination.unlink(missing_ok=True)
        driver_destination.symlink_to(driver_executable)
        print(f"- Installed driver executable: '{driver_destination}'")

    print(f"NOTE: modifying or removing files in the source directory ({SOURCE_DIR}) may break this installation.")
    return 0

def uninstall():
    xml_definition_file = INDI_XML_DIR/"indi_seestar.xml"
    try:
        xml_definition_file.unlink()
        print(f"Deleted '{xml_definition_file}'")
    except FileNotFoundError:
        print(f"File '{xml_definition_file} doesn't exist. No action taken.")
    
    for driver_name in DRIVER_NAMES:
        driver_path = INDI_BIN_DIR/driver_name
        try:
            driver_path.unlink()
            print(f"Deleted '{driver_path}'")
        except FileNotFoundError:
            print(f"File '{driver_path} doesn't exist. No action taken.")


if __name__ == "__main__":

    if os.geteuid() != 0:
        print(f"This script must be run with root privileges. Try `sudo {sys.argv[0]}`")
        sys.exit(1)

    if "--uninstall" in sys.argv:
        sys.exit(uninstall())
    else:
        sys.exit(install())
