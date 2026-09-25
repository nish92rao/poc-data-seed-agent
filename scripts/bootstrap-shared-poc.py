#!/usr/bin/env python3
"""Bootstrap the sample POC's exact GitHub seed inputs and shared-state references."""

from __future__ import annotations

import json
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / "src"))

from dotenv import load_dotenv

from agent_poc_data_seed.bootstrap_shared_fixture import bootstrap_shared_fixture


if __name__ == "__main__":
    load_dotenv(WORKSPACE / ".env")
    pov_id = sys.argv[1] if len(sys.argv) > 1 else "1790237138344"
    print(json.dumps(bootstrap_shared_fixture(pov_id, workspace=WORKSPACE), indent=2))
