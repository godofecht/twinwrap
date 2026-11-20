from pathlib import Path

import pytest

from twinwrap.manifest import ManifestError, from_dict, set_version


def base(tmp_path: Path, **over):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\nversion = "0.1.0"\n')
    d = {
        "package": {"name": "demo-lib", "version": "1.0.0"},
        "source": {"lang": "rust", "path": "."},
        "function": [{"sig": "fn add(a: i32, b: i32) -> i32"}],
        "test": [{"call": "add", "args": [1, 2], "expect": 3}],
    }
    for k, v in over.items():
        d[k] = v
    return d


def test_loads(tmp_path):
    m = from_dict(tmp_path, base(tmp_path))
    assert m.import_name == "demo_lib" and m.tests[0].has_expect


@pytest.mark.parametrize("over,needle", [
    ({"package": {"name": "Bad Name", "version": "1.0.0"}}, "name"),
    ({"package": {"name": "x", "version": "one"}}, "version"),
    ({"source": {"lang": "go"}}, "lang"),
    ({"function": []}, r"no \[\[function\]\]"),
    ({"test": [{"call": "nope", "args": []}]}, "unknown function"),
    ({"test": [{"call": "add", "args": [1]}]}, "takes 2 arguments"),
])
def test_rejects(tmp_path, over, needle):
    with pytest.raises(ManifestError, match=needle):
        from_dict(tmp_path, base(tmp_path, **over))


def test_flow_cannot_return_str(tmp_path):
    (tmp_path / "lib.flow").write_text("")
    d = base(tmp_path, source={"lang": "flow", "path": "lib.flow"},
             function=[{"sig": "fn f(s: str) -> str"}], test=[])
    with pytest.raises(ManifestError, match="cannot return str"):
        from_dict(tmp_path, d)


def test_set_version_touches_only_package(tmp_path):
    p = tmp_path / "twinwrap.toml"
    p.write_text('[package]\nname = "x"\nversion = "1.0.0"\n\n[other]\nversion = "9.9.9"\n')
    set_version(tmp_path, "1.1.0")
    t = p.read_text()
    assert 'version = "1.1.0"' in t and 'version = "9.9.9"' in t
