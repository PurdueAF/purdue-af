#!/usr/bin/env python3
# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
import json
import os
from pathlib import Path

import requests

# A number of operations below delibrately don't check for possible errors
# As this is a healthcheck, it should succeed or raise an exception on error

runtime_dir = Path("/home/") / os.environ["NB_USER"] / ".local/share/jupyter/runtime/"
json_files = sorted(runtime_dir.glob("*.json"))
if not json_files:
    raise FileNotFoundError(f"No Jupyter runtime JSON found in {runtime_dir}")
json_file = json_files[-1]  # most recent if multiple

url = json.loads(json_file.read_bytes())["url"]
url = url + "api"

r = requests.get(url, verify=False, timeout=3)  # request without SSL verification
r.raise_for_status()
print(r.content)
