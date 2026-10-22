import pathlib
import shutil
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mmm"))


@pytest.fixture()
def brand(tmp_path):
    """A copy of the example brand folder that a test can edit."""
    dst = tmp_path / "brand"
    shutil.copytree(ROOT / "example", dst, ignore=shutil.ignore_patterns("data", "sql"))
    return dst
