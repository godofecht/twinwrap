"""End to end: build both examples and require Python and Node to agree."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

EX = Path(__file__).resolve().parent.parent / "examples"


def _run(ex: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "twinwrap.cli", "-C", str(EX / ex), "test"],
                          capture_output=True, text=True)


@pytest.mark.skipif(not shutil.which("cargo") or not shutil.which("node"), reason="needs cargo and node")
def test_rust_example():
    p = _run("rust-stats")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "11/11 tests agree" in p.stdout


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_flow_example():
    from twinwrap import build
    try:
        build.flow_cli(); build.wasm_clang(); build.wasi_sysroot()
    except build.BuildError as e:
        pytest.skip(str(e))
    p = _run("flow-stats")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "7/7 tests agree" in p.stdout
