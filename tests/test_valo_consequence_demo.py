# Copyright 2026 VALO Research
# SPDX-License-Identifier: Apache-2.0

import subprocess
import sys
from pathlib import Path


def test_valo_consequence_demo() -> None:
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "demo_valo_consequence.py")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "fresh authority + fresh reality -> ALLOW -> effect committed" in result.stdout
    assert "changed reality -> DENY -> no effect (RESOURCE_STATE_CHANGED)" in result.stdout
    assert (
        "revoked authority + fresh reality -> DENY -> no effect (AUTHORITY_REVOKED)"
        in result.stdout
    )
    assert "effects committed: 1" in result.stdout
