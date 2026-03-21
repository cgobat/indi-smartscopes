#!/usr/bin/env python

import os
import sys
import traceback
import configparser
from pathlib import Path
from lxml import etree as xml

COMMON_DIR = Path(__file__).resolve().parent
REPO_DIR = COMMON_DIR.parent
config = configparser.ConfigParser(converters={"path": Path,
                                               "list": lambda l: [x.strip() for x in l.split(",")]})
config.read(COMMON_DIR / "config.ini")
INDI_XML_DIR = config["indi"].getpath("xml_dir")
INDI_BIN_DIR = config["indi"].getpath("bin_dir")
DRIVER_EXE_NAME = config["dwarf"].get("exe_name")
DRIVER_DEF_XML = INDI_XML_DIR / "smart_telescopes.xml"

def install() -> int:

    while (proceed := input("\nYou are about to \033[4minstall\033[m INDI drivers for DWARF, Seestar, and"
                            " Celestron Origin. Proceed? [Y/n] ").lower()) not in ("y", "yes", "n", "no"):
        print(f"Unrecognized input '{proceed}'. Enter 'yes' or 'no'.")
    if proceed.startswith("n"):
        print("Aborting without action.\n")
        return 0

    if not INDI_XML_DIR.is_dir():
        print(f"Error: directory '{INDI_XML_DIR}' does not exist. Is the INDI library installed?")
        return 1

    root = xml.Element("driversList", )
    dev_group: xml._Element = xml.SubElement(root, "devGroup", {"group": "CCDs"})
    for manufacturer, config_key in [("ZWO", "seestar"), ("DWARFLAB", "dwarf"), ("Celestron", "origin")]:
        version = config[config_key].get("version")
        devices = config[config_key].getlist("devices")
        driver_exe = config[config_key].get("exe_name")
        for device_label in devices:
            device_elem = xml.SubElement(dev_group, "device", {"label": device_label, "manufacturer": manufacturer})
            driver_elem = xml.SubElement(device_elem, "driver", {"name": f"pyINDI {config_key.title()}"})
            driver_elem.text = driver_exe
            version_elem = xml.SubElement(device_elem, "version")
            version_elem.text = version
            print(f"- Added '{device_label}' driver definition to {DRIVER_DEF_XML}")
    driver_xml = xml.ElementTree(root)
    xml.indent(driver_xml, space=" "*4)
    driver_xml.write(DRIVER_DEF_XML.as_posix(), encoding="UTF-8", pretty_print=True, xml_declaration=True)

    for config_key in ["dwarf", "origin", "seestar"]:
        driver_dir = REPO_DIR / f"indi-{config_key}"
        driver_source = driver_dir / f"indi_{config_key}.py"
        driver_source.chmod(driver_source.stat().st_mode | 0o111)
        driver_exe_name = config[config_key].get("exe_name")

        driver_destination = INDI_BIN_DIR / driver_exe_name
        driver_destination.unlink(missing_ok=True)
        driver_destination.symlink_to(driver_source)
        print(f"- Installed driver executable: {driver_destination}")

    print(f"\nNOTE: modifying or removing files in the source tree ({REPO_DIR}) may break this installation.\n")
    return 0

def uninstall():

    while (proceed := input("\nYou are about to \033[3;4mun\033[m\033[4minstall\033[m the Seestar/DWARF/Celestron"
                            " Origin INDI drivers. Proceed? [Y/n] ").lower()) not in ("y", "yes", "n", "no"):
        print(f"Unrecognized input '{proceed}'. Enter 'yes' or 'no'.")
    if proceed.startswith("n"):
        print("Aborting without action.\n")
        return 0

    try:
        DRIVER_DEF_XML.unlink()
        print(f"- Deleted '{DRIVER_DEF_XML}'")
    except FileNotFoundError:
        print(f"- File '{DRIVER_DEF_XML}' doesn't exist. No action taken.")
    except:
        print(f"Failed to remove XML definition file '{DRIVER_DEF_XML}' due to exception:\n{traceback.format_exc()}")

    for config_key in ["dwarf", "origin", "seestar"]:
        driver_exe_name = config[config_key].get("exe_name")
        driver_path = INDI_BIN_DIR / driver_exe_name
        try:
            driver_path.unlink()
            print(f"- Deleted '{driver_path}'")
        except FileNotFoundError:
            print(f"= File '{driver_path}' doesn't exist. No action taken.")

    return 0


if __name__ == "__main__":
    if os.geteuid() != 0:
        print(f"This script must be run with root privileges. Try `sudo {sys.argv[0]}`")
        sys.exit(1)

    if "--uninstall" in sys.argv:
        sys.exit(uninstall())
    else:
        sys.exit(install())
