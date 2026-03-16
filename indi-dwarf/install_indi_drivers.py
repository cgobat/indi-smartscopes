#!/usr/bin/env python3

import os
import sys
import configparser
from pathlib import Path
from lxml import etree as xml

SOURCE_DIR = Path(__file__).resolve().parent
config = configparser.ConfigParser(converters={"path": Path})
config.read(SOURCE_DIR / "config.ini")
INDI_XML_DIR = config["indi"].getpath("xml_dir")
INDI_BIN_DIR = config["indi"].getpath("bin_dir")
DRIVER_EXE_NAME = "indi_dwarf"
__version__ = "0.1"


def install() -> int:
    if not INDI_XML_DIR.is_dir():
        print(f"Error: directory '{INDI_XML_DIR}' does not exist.")
        return 1
    xml_definition_file = INDI_XML_DIR / "drivers.xml"
    driver_xml: xml._ElementTree = xml.parse(xml_definition_file.as_posix())
    dev_group: xml._Element = driver_xml.find("devGroup[@group='Telescopes']")
    for device_label in ["DWARF 3", "DWARF mini", "DWARF 2"]:
        existing = dev_group.find(f"device[@label='{device_label}']")
        if existing is None:
            device_elem = xml.SubElement(dev_group, "device", {"label": device_label, "manufacturer": "DWARF Lab"})
            driver_elem = xml.SubElement(device_elem, "driver", {"name": "pyINDI DWARF"})
            driver_elem.text = DRIVER_EXE_NAME
            version_elem = xml.SubElement(device_elem, "version")
            version_elem.text = __version__
    xml.indent(driver_xml, space=" "*4)
    driver_xml.write(xml_definition_file.as_posix(), encoding="UTF-8", pretty_print=True, xml_declaration=True)

    driver_source = SOURCE_DIR / "indi_dwarf.py"
    driver_source.chmod(driver_source.stat().st_mode | 0o111)

    driver_destination = INDI_BIN_DIR / DRIVER_EXE_NAME
    driver_destination.unlink(missing_ok=True)
    driver_destination.symlink_to(driver_source)
    print(f"Installed {driver_destination}")
    return 0


if __name__ == "__main__":
    if os.geteuid() != 0:
        print(f'This script must be run with root privileges. Try `sudo {sys.argv[0]}`')
        sys.exit(1)
    sys.exit(install())
