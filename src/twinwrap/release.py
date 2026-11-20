"""`twinwrap doctor` and `twinwrap release`."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import build, ci, manifest

OK, WARN, BAD = "ok  ", "warn", "FAIL"


def _line(status: str, msg: str) -> None:
    print(f"  {status} {msg}")


def _get_json(url: str) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "twinwrap-doctor", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError):
        return 0, None


def _out(cmd: list[str], cwd: Path | None = None) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _git_root(start: Path) -> Path:
    for d in [start, *start.parents]:
        if (d / ".git").exists():
            return d
    return start


def _github_slug(url: str) -> str:
    m = re.match(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", url or "")
    return m.group(1) if m else ""


def cmd_doctor(args) -> int:
    m = manifest.load(args.dir)
    bad = 0
    print(f"twinwrap doctor: {m.name} {m.version} ({m.lang}, {len(m.functions)} functions, {len(m.tests)} tests)")

    print("toolchain")
    if m.lang == "rust":
        cargo = _out(["cargo", "--version"])
        _line(OK if cargo else BAD, cargo or "cargo not found (https://rustup.rs)")
        bad += not cargo
        targets = _out(["rustup", "target", "list", "--installed"]).split()
        has = "wasm32-unknown-unknown" in targets
        _line(OK if has else BAD, "wasm32-unknown-unknown target" if has
              else "missing: rustup target add wasm32-unknown-unknown")
        bad += not has
    else:
        try:
            flow = build.flow_cli()
            _line(OK, f"Flow compiler at {flow.resolve().parent}")
        except build.BuildError as e:
            _line(BAD, str(e))
            bad += 1
        for label, fn in (("clang for wasm32", build.wasm_clang), ("WASI sysroot", build.wasi_sysroot)):
            try:
                _line(OK, f"{label}: {fn()}")
            except build.BuildError as e:
                _line(BAD, str(e))
                bad += 1
    node = _out(["node", "--version"])
    _line(OK if node else BAD, f"node {node}" if node else "node not found; the npm side cannot be tested")
    bad += not node
    npm_v = _out(["npm", "--version"])
    _line(OK if npm_v else WARN, f"npm {npm_v}" if npm_v else "npm not found")

    print("tests")
    _line(OK if m.tests else WARN, f"{len(m.tests)} [[test]] entries" if m.tests
          else "no [[test]] entries: nothing checks that Python and Node agree")

    print("workflow")
    lay = ci.layout(m)
    wf = _git_root(m.root) / ".github" / "workflows" / lay.workflow_file
    if not wf.exists():
        _line(WARN, f"no .github/workflows/{lay.workflow_file}; run `twinwrap workflow`")
    else:
        spec = re.search(r'TWINWRAP: "([^"]*)"', wf.read_text())
        current = ci.workflow(m, spec.group(1) if spec else "", lay)
        _line(OK if wf.read_text() == current else WARN,
              f"{lay.workflow_file} is current" if wf.read_text() == current
              else f"{lay.workflow_file} differs from what this twinwrap generates; run `twinwrap workflow`")

    slug = _github_slug(m.repository)
    print("publishing")
    if not slug:
        _line(WARN, "[package] repository is not a github.com URL; trusted publishing needs one")
    if args.offline:
        _line(WARN, "registry checks skipped (--offline)")
    else:
        code, data = _get_json(f"https://pypi.org/pypi/{m.name}/json")
        if code == 404:
            _line(OK, f"PyPI: {m.name} is free. Add a pending trusted publisher before the first release:")
        elif code == 200:
            home = ((data or {}).get("info") or {}).get("project_urls") or {}
            _line(OK, f"PyPI: {m.name} exists ({', '.join(home.values()) or 'no project URL'}). "
                  "Check it is yours and has this trusted publisher:")
        else:
            _line(WARN, "PyPI: could not reach pypi.org")
        if code in (200, 404):
            print("         https://pypi.org/manage/account/publishing/")
            print(f"         owner/repo {slug or '<owner>/<repo>'}, workflow {lay.workflow_file}, environment pypi")
        code, data = _get_json(f"https://registry.npmjs.org/{m.name}")
        me = _out(["npm", "whoami"])
        if code == 404:
            _line(WARN, f"npm: {m.name} is free, but npm only accepts a trusted publisher on a package "
                  "that exists. Publish the first version by hand:")
            print("         twinwrap build && cd .twinwrap/npm && npm publish --access public")
        elif code == 200:
            maint = [x.get("name") for x in (data or {}).get("maintainers", [])]
            mine = me and me in maint
            _line(OK if mine else WARN, f"npm: {m.name} exists, maintainers {', '.join(maint) or '?'}"
                  + ("" if mine else f" (you are {me or 'not logged in'})"))
        else:
            _line(WARN, "npm: could not reach registry.npmjs.org")
        if code in (200, 404):
            print(f"         then on https://www.npmjs.com/package/{m.name}/access add a GitHub Actions")
            print(f"         trusted publisher: {slug or '<owner>/<repo>'}, workflow {lay.workflow_file}, environment npm,")
            print("         leaving 'Allow npm publish' unticked so releases are staged for approval")
    print(f"twinwrap doctor: {'no blocking problems' if not bad else f'{bad} blocking problem(s)'}")
    return 1 if bad else 0


# ----------------------------------------------------------------- release


def _git(root: Path, *a: str, check: bool = True) -> str:
    p = subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(a)} failed:\n{p.stderr.strip()}")
    return p.stdout.strip()


def cmd_release(args) -> int:
    root = Path(args.dir).resolve()
    m = manifest.load(root)
    new = args.version.removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+([.-]?[A-Za-z0-9.]+)?", new):
        print(f"twinwrap: {args.version} is not a version like 1.2.3", file=sys.stderr)
        return 2
    git_root = _git_root(root)
    if not shutil.which("git") or not (git_root / ".git").exists():
        print("twinwrap: release needs a git repository", file=sys.stderr)
        return 2
    dirty = _git(root, "status", "--porcelain")
    if dirty:
        print(f"twinwrap: commit or stash these first:\n{dirty}", file=sys.stderr)
        return 1
    lay = ci.layout(m)
    tag = f"{lay.tag_prefix}{new}"
    if _git(root, "tag", "--list", tag):
        print(f"twinwrap: tag {tag} already exists", file=sys.stderr)
        return 1
    wf = git_root / ".github" / "workflows" / lay.workflow_file
    if not wf.exists():
        print(f"twinwrap: no .github/workflows/{lay.workflow_file}; run `twinwrap workflow` and commit it",
              file=sys.stderr)
        return 1

    if new != m.version:
        manifest.set_version(root, new)
        m = manifest.load(root)
    if not args.skip_test:
        from .cli import main as cli_main
        rc = cli_main(["-C", str(root), "test"])
        if rc != 0:
            print("twinwrap: tests failed; the version change is left uncommitted", file=sys.stderr)
            return rc
    if _git(root, "status", "--porcelain", manifest.FILENAME):
        _git(root, "add", manifest.FILENAME)
        _git(root, "commit", "-m", f"Release {tag}")
    _git(root, "tag", "-a", tag, "-m", f"{m.name} {new}")
    print(f"twinwrap: committed and tagged {tag}")
    if args.no_push:
        print(f"twinwrap: not pushed. When ready: git push && git push origin {tag}")
        return 0
    if not args.yes:
        ans = input(f"Push {tag}? This starts the release workflow. [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print(f"twinwrap: not pushed. When ready: git push && git push origin {tag}")
            return 0
    _git(root, "push")
    _git(root, "push", "origin", tag)
    slug = _github_slug(m.repository)
    print(f"twinwrap: pushed {tag}")
    if slug:
        print(f"  workflow:  https://github.com/{slug}/actions/workflows/{lay.workflow_file}")
    print(f"  PyPI:      https://pypi.org/project/{m.name}/{new}/ (after the workflow finishes)")
    print(f"  npm:       staged; approve it at https://www.npmjs.com/package/{m.name}")
    return 0
