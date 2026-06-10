#!/usr/bin/env python3
"""Rewrite coverage.xml so every filename is repo-rooted.

coverage.py writes <class filename="..."> relative to one of the <source>
directories; Codecov reads the filename attribute as-is and never joins it
with <source>, so files outside the upload's working directory fail to map
to the repository tree. This script joins each filename with the <source>
dir that contains it and resets <sources> to the repo root.

Usage: python scripts/fix-coverage-paths.py [coverage.xml]
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main() -> None:
    xml_path = Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.xml")
    tree = ET.parse(xml_path)
    root = tree.getroot()

    sources = [Path(s.text) for s in root.iter("source") if s.text]

    for cls in root.iter("class"):
        filename = cls.get("filename", "")
        if Path(filename).exists():  # already repo-rooted
            continue
        for src in sources:
            candidate = src / filename
            if candidate.exists():
                cls.set("filename", candidate.as_posix())
                break
        else:
            sys.exit(f"error: cannot resolve '{filename}' under {sources}")

    sources_el = root.find("sources")
    if sources_el is not None:
        for el in list(sources_el):
            sources_el.remove(el)
        new_source = ET.SubElement(sources_el, "source")
        new_source.text = "."

    tree.write(xml_path, encoding="utf-8", xml_declaration=True)
    print(f"normalised paths in {xml_path}")


if __name__ == "__main__":
    main()
