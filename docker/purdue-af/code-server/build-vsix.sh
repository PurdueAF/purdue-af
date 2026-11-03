#!/bin/bash
# Build a VSIX for the bundled Purdue AF code-server extension, and next to it
# (<vsix>.sha256) a hash of its sources: zip timestamps change the VSIX every build.
set -euo pipefail

EXT_DIR="${1:?extension source directory}"
VSIX_OUT="${2:?output vsix path}"

python3 - "$EXT_DIR" "$VSIX_OUT" <<'PY'
import hashlib
import json
import os
import sys
import zipfile

ext_dir, vsix_out = sys.argv[1], sys.argv[2]

with open(os.path.join(ext_dir, "package.json"), encoding="utf-8") as handle:
    package = json.load(handle)

content_types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
</Types>
"""

manifest = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}"/>
    <DisplayName>{displayName}</DisplayName>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>
""".format(**package)

os.makedirs(os.path.dirname(vsix_out), exist_ok=True)
if os.path.exists(vsix_out):
    os.remove(vsix_out)

digest = hashlib.sha256()
with zipfile.ZipFile(vsix_out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("[Content_Types].xml", content_types)
    archive.writestr("extension.vsixmanifest", manifest)
    for root, dirs, files in os.walk(ext_dir):
        dirs.sort()
        for name in sorted(files):
            source = os.path.join(root, name)
            rel = os.path.relpath(source, ext_dir)
            archive.write(source, "extension/" + rel)
            with open(source, "rb") as handle:
                digest.update(rel.encode() + b"\0" + handle.read())

with open(vsix_out + ".sha256", "w") as handle:
    handle.write(digest.hexdigest() + "\n")

print("Built VSIX: " + vsix_out)
PY
