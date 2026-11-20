"""Run every [[test]] through the Python package and the npm package and
require the results to match bit for bit, and to match ``expect`` when given.

Floats are compared by their IEEE-754 bit patterns, so -0.0 and 0.0 differ
and a NaN payload counts.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .manifest import Manifest

PY_RUNNER = r"""
import array, json, struct, sys
sys.path.insert(0, sys.argv[1])
import importlib
mod = importlib.import_module(sys.argv[2])
cases = json.load(open(sys.argv[3], encoding="utf-8"))

def canon(v, t):
    kind, elem = t
    if kind == "none":
        return None
    if kind == "str":
        return v
    if kind == "array":
        return [canon(x, ("scalar", elem)) for x in v]
    if elem == "f64":
        return struct.pack("<d", v).hex()
    if elem == "f32":
        return struct.pack("<f", v).hex()
    if elem == "bool":
        return bool(v)
    return str(int(v))

out = []
for c in cases:
    fn = getattr(mod, c["call"])
    args = [int(a) if k in ("i64", "u64") else [int(x) for x in a] if k in ("slice:i64", "slice:u64")
            else a for a, k in zip(c["args"], c["kinds"])]
    try:
        out.append({"ok": canon(fn(*args), c["ret"])})
    except Exception as e:
        out.append({"error": type(e).__name__ + ": " + str(e)})
print(json.dumps(out))
"""

JS_RUNNER = r"""
const mod = require(process.argv[2]);
const cases = JSON.parse(require('fs').readFileSync(process.argv[3], 'utf8'));
const hex = (n, f32) => {
  const b = Buffer.alloc(f32 ? 4 : 8);
  f32 ? b.writeFloatLE(n) : b.writeDoubleLE(n);
  return b.toString('hex');
};
function canon(v, [kind, elem]) {
  if (kind === 'none') return null;
  if (kind === 'str') return v;
  if (kind === 'array') return Array.from(v, (x) => canon(x, ['scalar', elem]));
  if (elem === 'f64') return hex(v, false);
  if (elem === 'f32') return hex(v, true);
  if (elem === 'bool') return !!v;
  return String(v);
}
const out = [];
for (const c of cases) {
  const big = (k) => k === 'i64' || k === 'u64';
  const args = c.args.map((a, i) => big(c.kinds[i]) ? BigInt(a)
    : (c.kinds[i] === 'slice:i64' || c.kinds[i] === 'slice:u64') ? a.map(BigInt) : a);
  try {
    out.push({ ok: canon(mod[c.call](...args), c.ret) });
  } catch (e) {
    out.push({ error: e.constructor.name + ': ' + e.message });
  }
}
console.log(JSON.stringify(out));
"""


@dataclass
class Result:
    call: str
    args: list
    python: dict
    node: dict
    expect: object
    ok: bool
    reason: str = ""


def _cases(m: Manifest) -> list[dict]:
    cases = []
    for t in m.tests:
        sig = m.function(t.call)
        kinds = [p.type.elem if p.type.kind == "scalar" else f"{p.type.kind}:{p.type.elem}"
                 for p in sig.params]
        # 64-bit integers travel as strings so JSON and JS numbers cannot round them.
        args = [
            str(a) if k in ("i64", "u64")
            else [str(x) for x in a] if k in ("slice:i64", "slice:u64")
            else a
            for a, k in zip(t.args, kinds)
        ]
        cases.append({"call": t.call, "args": args, "kinds": kinds, "ret": [sig.ret.kind, sig.ret.elem]})
    return cases


def _canon_expect(v, kind: str, elem: str | None):
    if kind == "none":
        return None
    if kind == "str":
        return v
    if kind == "array":
        return [_canon_expect(x, "scalar", elem) for x in v]
    if elem == "f64":
        return struct.pack("<d", float(v)).hex()
    if elem == "f32":
        return struct.pack("<f", float(v)).hex()
    if elem == "bool":
        return bool(v)
    return str(int(v))


def _run_python(m: Manifest, python: str, py_dir: Path | None, cases: list[dict], tdp: Path) -> list[dict]:
    cf = tdp / "cases.json"
    cf.write_text(json.dumps(cases), encoding="utf-8")
    (tdp / "run.py").write_text(PY_RUNNER, encoding="utf-8")
    # An empty path entry keeps the installed package (wheel mode) in charge.
    path_arg = str(py_dir) if py_dir else str(tdp / "empty")
    (tdp / "empty").mkdir(exist_ok=True)
    py = subprocess.run([python, "-I", str(tdp / "run.py"), path_arg, m.import_name, str(cf)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if py.returncode != 0:
        raise RuntimeError(f"Python test run failed:\n{py.stderr}")
    return json.loads(py.stdout.strip().splitlines()[-1])


def _run_node(npm_dir: Path, cases: list[dict], tdp: Path) -> list[dict]:
    cf = tdp / "cases.json"
    cf.write_text(json.dumps(cases), encoding="utf-8")
    (tdp / "run.js").write_text(JS_RUNNER, encoding="utf-8")
    js = subprocess.run(["node", str(tdp / "run.js"), str(npm_dir.resolve()), str(cf)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if js.returncode != 0:
        raise RuntimeError(f"Node test run failed:\n{js.stderr}")
    return json.loads(js.stdout.strip().splitlines()[-1])


def _same_error(a: str, b: str) -> bool:
    """Error classes differ by language; the library's message must not."""
    return a.split(": ", 1)[-1] == b.split(": ", 1)[-1]


def _judge(t, c: dict, p: dict, j: dict) -> tuple[bool, str]:
    if "error" in p or "error" in j:
        if "error" in p and "error" in j and not t.has_expect:
            if _same_error(p["error"], j["error"]):
                return True, ""
            return False, "both failed, with different messages"
        return False, f"python: {p.get('error', 'ok')}; node: {j.get('error', 'ok')}"
    if p["ok"] != j["ok"]:
        return False, "Python and Node disagree"
    if t.has_expect:
        want = _canon_expect(t.expect, *c["ret"])
        if p["ok"] != want:
            return False, f"expected {want}"
    return True, ""


def run(m: Manifest, py_dir: Path, npm_dir: Path, python: str = sys.executable) -> list[Result]:
    if not m.tests:
        return []
    cases = _cases(m)
    with tempfile.TemporaryDirectory() as td:
        py_out = _run_python(m, python, py_dir, cases, Path(td))
        js_out = _run_node(npm_dir, cases, Path(td))
    results = []
    for t, c, p, j in zip(m.tests, cases, py_out, js_out):
        ok, reason = _judge(t, c, p, j)
        results.append(Result(t.call, t.args, p, j, t.expect, ok, reason))
    return results


def emit(results: list[Result], path: Path) -> None:
    path.write_text(json.dumps(
        [{"call": r.call, "args": r.args, "python": r.python, "node": r.node} for r in results],
        indent=1,
    ), encoding="utf-8")


def against_wheel(m: Manifest, wheel: Path, reference: Path) -> list[Result]:
    """Install ``wheel`` in a fresh venv and compare with reference results."""
    ref = json.loads(reference.read_text(encoding="utf-8"))
    cases = _cases(m)
    if len(ref) != len(cases) or any(r["call"] != c["call"] for r, c in zip(ref, cases)):
        raise RuntimeError(f"{reference} was made from a different test list")
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        venv = tdp / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        inst = subprocess.run([str(py), "-m", "pip", "install", "-q", "--no-deps", str(wheel)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if inst.returncode != 0:
            raise RuntimeError(f"pip could not install {wheel.name}:\n{inst.stderr}")
        got = _run_python(m, str(py), None, cases, tdp)
    results = []
    for t, c, g, r in zip(m.tests, cases, got, ref):
        ok, reason = _judge(t, c, g, r["node"])
        if ok and g != r["python"] and "error" not in g:
            ok, reason = False, "differs from the reference platform"
        results.append(Result(t.call, t.args, g, r["node"], t.expect, ok, reason))
    return results
