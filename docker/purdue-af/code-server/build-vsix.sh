#!/bin/bash
# Build a VSIX for the bundled Purdue AF code-server extension.
set -euo pipefail

EXT_DIR="${1:?extension source directory}"
VSIX_OUT="${2:?output vsix path}"

python3 - "$EXT_DIR" "$VSIX_OUT" <<'PY'
import json
import os
import sys
import zipfile

ext_dir, vsix_out = sys.argv[1], sys.argv[2]

with open(os.path.join(ext_dir, "package.json"), "r", encoding="utf-8") as handle:
    package = json.load(handle)

publisher = package["publisher"]
name = package["name"]
version = package["version"]
display = package["displayName"]

content_types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
</Types>
"""

manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}"/>
    <DisplayName>{display}</DisplayName>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>
"""

os.makedirs(os.path.dirname(vsix_out), exist_ok=True)
if os.path.exists(vsix_out):
    os.remove(vsix_out)

with zipfile.ZipFile(vsix_out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("[Content_Types].xml", content_types)
    archive.writestr("extension.vsixmanifest", manifest)
    for filename in ("package.json", "extension.js"):
        source = os.path.join(ext_dir, filename)
        archive.write(source, f"extension/{filename}")

print(f"Built VSIX: {vsix_out}")
PY
