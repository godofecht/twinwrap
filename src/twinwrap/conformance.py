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
cases = json.load(open(sys.argv[3]))

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


def run(m: Manifest, py_dir: Path, npm_dir: Path, python: str = sys.executable) -> list[Result]:
    if not m.tests:
        return []
    cases = _cases(m)
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        cf = tdp / "cases.json"
        cf.write_text(json.dumps(cases))
        (tdp / "run.py").write_text(PY_RUNNER)
        (tdp / "run.js").write_text(JS_RUNNER)
        py = subprocess.run([python, "-I", str(tdp / "run.py"), str(py_dir), m.import_name, str(cf)],
                            capture_output=True, text=True)
        if py.returncode != 0:
            raise RuntimeError(f"Python test run failed:\n{py.stderr}")
        js = subprocess.run(["node", str(tdp / "run.js"), str(npm_dir.resolve()), str(cf)],
                            capture_output=True, text=True)
        if js.returncode != 0:
            raise RuntimeError(f"Node test run failed:\n{js.stderr}")
        py_out = json.loads(py.stdout.strip().splitlines()[-1])
        js_out = json.loads(js.stdout.strip().splitlines()[-1])

    results = []
    for t, c, p, j in zip(m.tests, cases, py_out, js_out):
        ok, reason = True, ""
        if "error" in p or "error" in j:
            ok = "error" in p and "error" in j and t.has_expect is False
            reason = f"python: {p.get('error', 'ok')}; node: {j.get('error', 'ok')}"
            if not ("error" in p and "error" in j):
                ok = False
        elif p["ok"] != j["ok"]:
            ok, reason = False, "Python and Node disagree"
        elif t.has_expect:
            want = _canon_expect(t.expect, *c["ret"])
            if p["ok"] != want:
                ok, reason = False, f"expected {want}"
        results.append(Result(t.call, t.args, p, j, t.expect, ok, reason))
    return results
