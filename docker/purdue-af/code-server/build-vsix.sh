#!/bin/bash
# Build a VSIX for the bundled Purdue AF code-server extension.
set -euo pipefail

EXT_DIR="${1:?extension source directory}"
VSIX_OUT="${2:?output vsix path}"

# Regenerate icon font when sources exist but font is missing (optional; needs npx).
if [ ! -f "$EXT_DIR/media/purdueaf.woff" ] && [ -d "$EXT_DIR/media" ] && command -v npx >/dev/null 2>&1; then
	npx --yes fantasticon "$EXT_DIR/media" --output "$EXT_DIR/media" --name purdueaf --font-types woff --normalize true
	rm -f "$EXT_DIR/media/purdueaf.css" "$EXT_DIR/media/purdueaf.html" \
		"$EXT_DIR/media/purdueaf.json" "$EXT_DIR/media/purdueaf.ts"
fi

python3 - "$EXT_DIR" "$VSIX_OUT" <<'PY'
import os
import sys
import zipfile

ext_dir, vsix_out = sys.argv[1], sys.argv[2]

content_types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension=".woff" ContentType="font/woff"/>
  <Default Extension=".svg" ContentType="image/svg+xml"/>
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
</Types>
"""

manifest = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="purdue-af-interface-controls" Version="0.1.0" Publisher="purdueaf"/>
    <DisplayName>Purdue AF Interface Controls</DisplayName>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>
"""

skip_names = {
    "fantasticon.config.js",
    "purdueaf.css",
    "purdueaf.html",
    "purdueaf.json",
    "purdueaf.ts",
}

os.makedirs(os.path.dirname(vsix_out), exist_ok=True)
if os.path.exists(vsix_out):
    os.remove(vsix_out)

with zipfile.ZipFile(vsix_out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("[Content_Types].xml", content_types)
    archive.writestr("extension.vsixmanifest", manifest)
    for root, _dirs, files in os.walk(ext_dir):
        for name in files:
            if name in skip_names:
                continue
            source = os.path.join(root, name)
            rel = os.path.relpath(source, ext_dir)
            archive.write(source, f"extension/{rel}")

print(f"Built VSIX: {vsix_out}")
PY
