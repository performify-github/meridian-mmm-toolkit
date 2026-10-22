"""A real Meridian fit on synthetic data (smoke sampling, ~1 minute). Needs requirements.txt.

    MMM_SLOW=1 python3 -m pytest tests/test_fit_local.py
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT

pytestmark = pytest.mark.skipif(not os.environ.get("MMM_SLOW") or importlib.util.find_spec("meridian") is None,
                                reason="set MMM_SLOW=1 with google-meridian installed")


def test_local_smoke_fit(brand, tmp_path):
    cfg = str(brand / "mmm.yaml")
    subprocess.run([sys.executable, ROOT / "mmm/synthetic.py", "--config", cfg, "--weeks", "120"], check=True)
    subprocess.run([sys.executable, ROOT / "mmm/run_mmm.py", "--config", cfg, "local", "--sampling", "smoke",
                    "--run-id", "t", "--out", str(tmp_path), "--no-report", "--no-optimise"], check=True)
    s = json.loads((tmp_path / "t" / "run_summary.json").read_text())
    assert set(s["channels"]) >= set(s["data"]["media_spend"])
    assert (tmp_path / "t" / "model.binpb").exists()
