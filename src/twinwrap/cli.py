"""twinwrap command line."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__, build, conformance, manifest, package


def _load(args) -> manifest.Manifest:
    return manifest.load(args.dir)


def cmd_build(args) -> int:
    m = _load(args)
    want_py = not args.npm_only
    want_npm = not args.python_only
    print(f"twinwrap: building {m.name} {m.version} ({m.lang}) on {build.host_description()}", file=sys.stderr)
    built = build.build(m, native=want_py, wasm=want_npm, target=args.target)
    dist = (m.root / args.out).resolve()
    dist.mkdir(parents=True, exist_ok=True)
    work = build.work_dir(m)
    report: dict = {"name": m.name, "version": m.version}
    if want_py:
        whl = package.wheel(m, built["native"], dist, tag=args.plat_name)
        package.python_package_dir(m, built["native"], work / "python")
        report["wheel"] = str(whl)
        print(f"  wheel   {whl.relative_to(m.root) if whl.is_relative_to(m.root) else whl}")
    if want_npm:
        d = package.npm_dir(m, built["wasm"], work / "npm")
        files = package.npm_check(d, m)
        report["npm_dir"] = str(d)
        report["npm_files"] = files
        if args.pack:
            tgz = package.npm_tarball(d, dist)
            report["npm_tarball"] = str(tgz)
            print(f"  npm     {tgz.relative_to(m.root) if tgz.is_relative_to(m.root) else tgz}")
        else:
            print(f"  npm     {d.relative_to(m.root)} ({len(files)} files)")
    if args.json:
        print(json.dumps(report, indent=2))
    return 0


def cmd_test(args) -> int:
    m = _load(args)
    if not args.no_build:
        built = build.build(m)
        work = build.work_dir(m)
        package.python_package_dir(m, built["native"], work / "python")
        package.npm_dir(m, built["wasm"], work / "npm")
    work = build.work_dir(m)
    results = conformance.run(m, work / "python", work / "npm")
    if not results:
        print("twinwrap: no [[test]] entries in twinwrap.toml", file=sys.stderr)
        return 1
    failed = 0
    for r in results:
        mark = "ok  " if r.ok else "FAIL"
        args_s = ", ".join(json.dumps(a) for a in r.args)
        print(f"  {mark} {r.call}({args_s})  python={_short(r.python)} node={_short(r.node)}"
              + (f"  [{r.reason}]" if r.reason else ""))
        failed += not r.ok
    print(f"twinwrap: {len(results) - failed}/{len(results)} tests agree across Python and Node")
    return 1 if failed else 0


def _short(d: dict) -> str:
    s = json.dumps(d.get("ok", d.get("error")))
    return s if len(s) < 60 else s[:57] + "..."


def cmd_clean(args) -> int:
    d = Path(args.dir).resolve() / ".twinwrap"
    if d.exists():
        shutil.rmtree(d)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="twinwrap", description=__doc__)
    p.add_argument("--version", action="version", version=f"twinwrap {__version__}")
    p.add_argument("-C", "--dir", default=".", help="project directory (default: .)")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build the wheel for this platform and the npm package")
    b.add_argument("--out", default="dist", help="output directory (default: dist)")
    b.add_argument("--python-only", action="store_true")
    b.add_argument("--npm-only", action="store_true")
    b.add_argument("--pack", action="store_true", help="also write the npm .tgz to --out")
    b.add_argument("--target", help="Rust target triple for the native library")
    b.add_argument("--plat-name", help="override the wheel platform tag")
    b.add_argument("--json", action="store_true")
    b.set_defaults(func=cmd_build)

    t = sub.add_parser("test", help="run [[test]] cases in Python and Node and compare bits")
    t.add_argument("--no-build", action="store_true", help="use the last build")
    t.set_defaults(func=cmd_test)

    c = sub.add_parser("clean", help="remove .twinwrap/")
    c.set_defaults(func=cmd_clean)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except (manifest.ManifestError, build.BuildError, package.PackageError, RuntimeError) as e:
        print(f"twinwrap: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
