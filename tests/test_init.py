from pathlib import Path

from twinwrap.initcmd import render, scan_flow, scan_rust
from twinwrap.manifest import from_dict
import tomllib


def test_scan_rust(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\nversion = "0.1.0"\n')
    (tmp_path / "src" / "lib.rs").write_text("""
pub fn add(a: i32, b: i32) -> i32 { a + b }
pub fn scale(xs: &[f64], k: f64) -> Vec<f64> { vec![] }
pub fn name(s: &str) -> String { String::new() }
pub fn generic<T>(x: T) -> T { x }
pub fn hashmap(m: HashMap<String, i32>) {}
fn private() {}
""")
    found = {f.name: f for f in scan_rust(tmp_path)}
    assert found["add"].sig == "fn add(a: i32, b: i32) -> i32"
    assert found["scale"].sig == "fn scale(xs: [f64], k: f64) -> [f64; len(xs)]"
    assert found["name"].sig == "fn name(s: str) -> str"
    assert found["generic"].sig is None and found["hashmap"].sig is None
    assert "private" not in found
    text = render("demo", "rust", ".", list(found.values()))
    m = from_dict(tmp_path, tomllib.loads(text))
    assert [f.name for f in m.functions] == ["add", "scale", "name"]


def test_scan_flow(tmp_path: Path):
    f = tmp_path / "lib.flow"
    f.write_text("""
function mean(xs: ptr<f64>, xs_len: i32) -> f64 { return 0.0 }
function scale(xs: ptr<f64>, xs_len: i32, k: f64, out: ptr<f64>, out_len: i32) -> i32 { return 0 }
function main() -> i32 { return 0 }
function bad(p: ptr<Thing>) -> i32 { return 0 }
""")
    found = {x.name: x for x in scan_flow(f)}
    assert found["mean"].sig == "fn mean(xs: [f64]) -> f64"
    assert found["scale"].sig == "fn scale(xs: [f64], k: f64) -> [f64; len(xs)]"
    assert "main" not in found and found["bad"].sig is None
