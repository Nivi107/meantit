"""Print new signing keys as .env lines.

    .venv/bin/python scripts/generate_keys.py >> .env

Run once. Each key is a P-256 private JWK; on Render, paste the JSON without
the surrounding single quotes. Changing a key later makes old signatures
unverifiable, so keep them.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import slips  # noqa: E402  (needs the path above)

for name in ("MARKETPLACE_SIGNING_KEY", "DEMO_AGENT_SIGNING_KEY", "CHECK_SIGNING_KEY"):
    print(f"{name}='{json.dumps(slips.new_private_jwk(), separators=(',', ':'))}'")
