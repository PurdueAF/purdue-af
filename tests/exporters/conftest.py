"""Path setup for exporter tests — sources imported directly by test modules."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "docker" / "af-node-monitor"))
sys.path.insert(0, str(REPO / "apps" / "monitoring" / "af-monitoring"))
