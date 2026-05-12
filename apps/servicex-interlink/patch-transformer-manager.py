#!/usr/bin/env python3
from pathlib import Path

MANAGER_PATH = Path("/patch/transformer_manager.py")
SCRATCH_DIR = "/tmp/servicex"


def main() -> None:
    text = MANAGER_PATH.read_text()
    if "/servicex/output" not in text:
        raise SystemExit(
            "transformer_manager.py layout changed; update patch-transformer-manager.py"
        )
    MANAGER_PATH.write_text(text.replace("/servicex/output", SCRATCH_DIR))


if __name__ == "__main__":
    main()
