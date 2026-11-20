"""twinwrap: wrap a Rust or Flow library for PyPI and npm from one function list.

  twinwrap init        write twinwrap.toml from the library's public functions
  twinwrap build       build this platform's wheel and the npm package
  twinwrap test        run [[test]] in Python and Node and compare bits
  twinwrap workflow    write .github/workflows/twinwrap.yml
  twinwrap doctor      check toolchains, names and publishing setup
  twinwrap release     bump, test, tag and push a release
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__, build, ci, conformance, initcmd, manifest, package, release

DEFAULT_SPEC = f"twinwrap @ git+https://github.com/godofecht/twinwrap@v{__version__}"


def _load(args) -> manifest.Manifest:
    return manifest.load(args.dir)


def _rel(m: manifest.Manifest, p: Path) -> str:
    return str(p.relative_to(m.root)) if p.is_relative_to(m.root) else str(p)


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
        print(f"  wheel   {_rel(m, whl)}")
    if want_npm:
        d = package.npm_dir(m, built["wasm"], work / "npm")
        files = package.npm_check(d, m)
        report["npm_dir"] = str(d)
        report["npm_files"] = files
        if args.pack:
            tgz = package.npm_tarball(d, dist)
            report["npm_tarball"] = str(tgz)
            print(f"  npm     {_rel(m, tgz)}")
        else:
            print(f"  npm     {_rel(m, d)} ({len(files)} files)")
    if args.json:
        print(json.dumps(report, indent=2))
    return 0


def _print_results(results, label: str) -> int:
    failed = 0
    for r in results:
        mark = "ok  " if r.ok else "FAIL"
        args_s = ", ".join(json.dumps(a) for a in r.args)
        print(f"  {mark} {r.call}({args_s})  python={_short(r.python)} {label}={_short(r.node)}"
              + (f"  [{r.reason}]" if r.reason else ""))
        failed += not r.ok
    return failed


def cmd_test(args) -> int:
    m = _load(args)
    if not m.tests:
        print("twinwrap: no [[test]] entries in twinwrap.toml", file=sys.stderr)
        return 1
    if args.wheel:
        if not args.against:
            print("twinwrap: --wheel needs --against <reference.json>", file=sys.stderr)
            return 2
        results = conformance.against_wheel(m, Path(args.wheel), Path(args.against))
        failed = _print_results(results, "reference")
        print(f"twinwrap: {len(results) - failed}/{len(results)} tests match the reference "
              f"({build.host_description()})")
        return 1 if failed else 0
    work = build.work_dir(m)
    if not args.no_build:
        built = build.build(m)
        package.python_package_dir(m, built["native"], work / "python")
        package.npm_dir(m, built["wasm"], work / "npm")
    if not (work / "python").exists() or not (work / "npm").exists():
        print("twinwrap: nothing built yet; run `twinwrap build` or drop --no-build", file=sys.stderr)
        return 2
    results = conformance.run(m, work / "python", work / "npm")
    failed = _print_results(results, "node")
    if args.emit:
        conformance.emit(results, Path(args.emit))
    print(f"twinwrap: {len(results) - failed}/{len(results)} tests agree across Python and Node")
    return 1 if failed else 0


def _short(d: dict) -> str:
    s = json.dumps(d.get("ok", d.get("error")))
    return s if len(s) < 60 else s[:57] + "..."


def cmd_check_tag(args) -> int:
    m = _load(args)
    tag = args.tag.removeprefix("refs/tags/")
    want = f"{ci.layout(m).tag_prefix}{m.version}"
    if tag != want:
        print(f"twinwrap: tag {tag} does not match twinwrap.toml version {m.version} (want {want})",
              file=sys.stderr)
        return 1
    print(f"twinwrap: tag {tag} matches")
    return 0


def cmd_npm_dir(args) -> int:
    m = _load(args)
    wasm = Path(args.wasm) if args.wasm else build.work_dir(m) / "wasm" / f"{m.lib_name}.wasm"
    if not wasm.exists():
        print(f"twinwrap: no wasm module at {wasm}", file=sys.stderr)
        return 2
    scoped = f"{args.scope}/{m.name}" if args.scope else None
    d = package.npm_dir(m, wasm, Path(args.out).resolve(), scoped=scoped)
    package.npm_check(d, m)
    print(d)
    return 0


def cmd_workflow(args) -> int:
    m = _load(args)
    lay = ci.layout(m)
    git_root = m.root if lay.at_root else Path(str(m.root)[: -len(lay.rel)].rstrip("/"))
    path = git_root / ".github" / "workflows" / lay.workflow_file
    text = ci.workflow(m, args.spec, lay)
    if args.stdout:
        print(text, end="")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"twinwrap: wrote {path}")
    return 0


def cmd_clean(args) -> int:
    d = Path(args.dir).resolve() / ".twinwrap"
    if d.exists():
        shutil.rmtree(d)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="twinwrap", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"twinwrap {__version__}")
    p.add_argument("-C", "--dir", default=".", help="project directory (default: .)")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="write twinwrap.toml from the library's public functions")
    i.add_argument("--lang", choices=["rust", "flow"], help="default: detected")
    i.add_argument("--source", help="Flow: the .flow file; Rust: the crate directory")
    i.add_argument("--name", help="PyPI and npm name (default: crate or file name)")
    i.add_argument("--force", action="store_true", help="overwrite an existing twinwrap.toml")
    i.set_defaults(func=initcmd.cmd_init)

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
    t.add_argument("--emit", help="write the results to this JSON file")
    t.add_argument("--wheel", help="install this wheel in a fresh venv and test it")
    t.add_argument("--against", help="reference results from `test --emit`")
    t.set_defaults(func=cmd_test)

    w = sub.add_parser("workflow", help="write .github/workflows/twinwrap.yml")
    w.add_argument("--spec", default=DEFAULT_SPEC, help="pip requirement CI installs twinwrap from")
    w.add_argument("--stdout", action="store_true")
    w.set_defaults(func=cmd_workflow)

    d = sub.add_parser("doctor", help="check toolchains, registry names and publishing setup")
    d.add_argument("--offline", action="store_true", help="skip registry lookups")
    d.set_defaults(func=release.cmd_doctor)

    r = sub.add_parser("release", help="set the version, test, commit, tag and push")
    r.add_argument("version", help="new version, e.g. 1.2.0")
    r.add_argument("--no-push", action="store_true", help="commit and tag but do not push")
    r.add_argument("--skip-test", action="store_true")
    r.add_argument("-y", "--yes", action="store_true", help="do not ask before pushing")
    r.set_defaults(func=release.cmd_release)

    ct = sub.add_parser("check-tag", help="fail unless TAG is v<version> (used by CI)")
    ct.add_argument("tag")
    ct.set_defaults(func=cmd_check_tag)

    nd = sub.add_parser("npm-dir", help="write the npm package directory (used by CI)")
    nd.add_argument("--out", required=True)
    nd.add_argument("--scope", help="e.g. @owner, for GitHub Packages")
    nd.add_argument("--wasm", help="wasm module to use (default: the last build)")
    nd.set_defaults(func=cmd_npm_dir)

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
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
