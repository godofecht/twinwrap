import json
import shutil
import subprocess
from pathlib import Path

import pytest

from twinwrap import ci
from twinwrap.manifest import load

EX = Path(__file__).resolve().parent.parent / "examples"


@pytest.mark.parametrize("ex", ["rust-stats", "flow-stats"])
def test_workflow_is_valid_yaml_with_expected_jobs(ex):
    m = load(EX / ex)
    text = ci.workflow(m, "twinwrap==0.1.0")
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("ruby not available for YAML parsing")
    out = subprocess.run([ruby, "-ryaml", "-rjson", "-e", "puts JSON.dump(YAML.load(STDIN.read))"],
                         input=text, capture_output=True, text=True, check=True).stdout
    wf = json.loads(out)
    assert set(wf["jobs"]) == {"reference", "wheels", "pypi", "npm"}
    labels = [i["label"] for i in wf["jobs"]["wheels"]["strategy"]["matrix"]["include"]]
    assert ("windows-x86_64" in labels) == (ex == "rust-stats")
    assert wf["jobs"]["pypi"]["permissions"] == {"id-token": "write"}
    assert "npm stage publish" in text
