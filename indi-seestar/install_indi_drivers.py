#!/usr/bin/env python

import os
import sys
import configparser
from pathlib import Path
from lxml import etree as xml

DRIVER_DIR = Path(__file__).resolve().parent
COMMON_DIR = DRIVER_DIR.parent / "common"
config = configparser.ConfigParser(converters={"path": Path})
config.read(COMMON_DIR / "config.ini")
INDI_XML_DIR = config["indi"].getpath("xml_dir")
INDI_BIN_DIR = config["indi"].getpath("bin_dir")
DRIVER_EXE_NAME = config["seestar"].get("exe_name")
__version__ = config["seestar"].get("version")

def install():

    while (proceed := input("\nYou are about to \033[4minstall\033[m the Seestar INDI driver."
                            " Proceed? [Y/n] ").lower()) not in ("y", "yes", "n", "no"):
        print(f"Unrecognized input '{proceed}'. Enter 'yes' or 'no'.")
    if proceed.startswith("n"):
        print("Aborting without action.\n")
        return 0

    if not INDI_XML_DIR.is_dir():
        print(f"Error: directory '{INDI_XML_DIR}' does not exist. Is the INDI library installed?")
        return 1
    xml_definition_file = INDI_XML_DIR/"drivers.xml"
    if not xml_definition_file.is_file():
        print(f"Error: XML file '{xml_definition_file}' does not exist. Is the INDI library installed?")
        return 1
    driver_xml: xml._ElementTree = xml.parse(xml_definition_file.as_posix())
    devGroup: xml._Element = driver_xml.find(f"devGroup[@group='Telescopes']")
    for device_label in ["Seestar S50", "Seestar S30", "Seestar S30 Pro"]:
        driver_label = "pyINDI Seestar"
        existing = devGroup.find(f"device[@label='{device_label}']")
        if existing is None:
            device_elem = xml.SubElement(devGroup, "device",
                                         {"label": device_label,
                                          "manufacturer": "ZWO"})
            driver_elem = xml.SubElement(device_elem, "driver", {"name": driver_label})
            driver_elem.text = DRIVER_EXE_NAME
            version_elem = xml.SubElement(device_elem, "version")
            version_elem.text = __version__
            print(f"- Added '{device_label}' driver definition to {xml_definition_file}")
        else:
            print(f"- Driver for '{device_label}' already exists in {xml_definition_file}")
    xml.indent(driver_xml, space=" "*4)
    driver_xml.write(xml_definition_file.as_posix(), encoding="UTF-8",
                     pretty_print=True, xml_declaration=True)

    driver_executable = DRIVER_DIR / "indi_seestar.py"
    driver_executable.chmod(driver_executable.stat().st_mode | 0o111) # ensure executability
    driver_destination = INDI_BIN_DIR/DRIVER_EXE_NAME
    driver_destination.unlink(missing_ok=True)
    driver_destination.symlink_to(driver_executable)
    print(f"- Installed driver executable: '{driver_destination}'")

    print(f"\nNOTE: modifying or removing files in the source directory ({DRIVER_DIR}) may break this installation.\n")
    return 0

def uninstall():

    while (proceed := input("\nYou are about to \033[3;4mun\033[m\033[4minstall\033[m the Seestar"
                            " S50 INDI driver. Proceed? [Y/n] ").lower()) not in ("y", "yes", "n", "no"):
        print(f"Unrecognized input '{proceed}'. Enter 'yes' or 'no'.")
    if proceed.startswith("n"):
        print("Aborting without action.\n")
        return 0

    xml_definition_file = INDI_XML_DIR/"drivers.xml"
    try:
        driver_xml: xml._ElementTree = xml.parse(xml_definition_file.as_posix())
        devGroup: xml._Element = driver_xml.find(f"devGroup[@group='Telescopes']")
        for device_label in ["Seestar S50", "Seestar S30", "Seestar S30 Pro"]:
            existing = devGroup.find(f"device[@label='{device_label}']")
            if existing is None:
                print(f"- Driver for '{device_label}' is not currently present in {xml_definition_file}")
            else:
                devGroup.remove(existing)
                print(f"- Removed '{device_label}' driver definition from {xml_definition_file}")
        xml.indent(driver_xml, space=" "*4)
        driver_xml.write(xml_definition_file.as_posix(), encoding="UTF-8",
                         pretty_print=True, xml_declaration=True)
    except OSError:
        print(f"Warning: XML file '{xml_definition_file}' not found. No action taken.")

    driver_path = INDI_BIN_DIR/DRIVER_EXE_NAME
    try:
        driver_path.unlink()
        print(f"- Deleted '{driver_path}'")
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
