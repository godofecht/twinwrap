"""Assemble the Python wheel and the npm package from built artifacts."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from . import __version__ as TOOL_VERSION
from . import jsgen, pygen
from .glue import ABI_VERSION
from .manifest import Manifest


class PackageError(RuntimeError):
    pass


# -------------------------------------------------------------- platform tag

_GLIBC = re.compile(rb"GLIBC_2\.(\d+)")


def linux_tag(lib: Path, machine: str) -> str:
    """manylinux tag from the newest glibc symbol version the library needs."""
    data = lib.read_bytes()
    if b"musl" in data and not _GLIBC.search(data):
        return f"musllinux_1_2_{machine}"
    minors = [int(x) for x in _GLIBC.findall(data)]
    minor = max(minors) if minors else 17
    return f"manylinux_2_{max(minor, 17)}_{machine}"


def platform_tag(lib: Path, system: str | None = None, machine: str | None = None) -> str:
    system = system or sys.platform
    machine = (machine or platform.machine()).lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64" if system.startswith("linux") else "arm64"}.get(machine, machine)
    if system == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
        default = "11.0" if arch == "arm64" else "10.12"
        target = os.environ.get("MACOSX_DEPLOYMENT_TARGET", default)
        major, _, minor = target.partition(".")
        minor = "0" if int(major) >= 11 else (minor or "0")
        return f"macosx_{major}_{minor}_{arch}"
    if system.startswith("win"):
        return "win_arm64" if machine in ("arm64", "aarch64") else "win_amd64"
    if machine == "arm64":
        machine = "aarch64"
    return linux_tag(lib, machine)


# ------------------------------------------------------------------- wheel


def _dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name)


def _metadata(m: Manifest) -> str:
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {m.name}",
        f"Version: {m.version}",
        "Requires-Python: >=3.8",
    ]
    if m.description:
        lines.append(f"Summary: {m.description}")
    if m.license:
        lines.append(f"License: {m.license}")
    for a in m.authors:
        lines.append(f"Author: {a}")
    if m.repository:
        lines.append(f"Project-URL: Source, {m.repository}")
    readme = _readme(m)
    if readme:
        lines.append("Description-Content-Type: text/markdown")
        return "\n".join(lines) + "\n\n" + readme
    return "\n".join(lines) + "\n"


def _readme(m: Manifest) -> str:
    for n in ("README.md", "readme.md"):
        p = m.root / n
        if p.exists():
            return p.read_text()
    return ""


def _license_file(m: Manifest) -> Path | None:
    for n in ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE"):
        p = m.root / n
        if p.exists():
            return p
    return None


def python_package_dir(m: Manifest, native: Path, dest: Path) -> Path:
    """The importable package: <dest>/<import_name>/{__init__.py,_native/...}."""
    pkg = dest / m.import_name
    if pkg.exists():
        shutil.rmtree(pkg)
    (pkg / "_native").mkdir(parents=True)
    (pkg / "__init__.py").write_text(pygen.python_module(m, ABI_VERSION))
    (pkg / "py.typed").write_text("")
    shutil.copy2(native, pkg / "_native" / native.name)
    return pkg


def wheel(m: Manifest, native: Path, out_dir: Path, tag: str | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = tag or platform_tag(native)
    dist = _dist_name(m.name)
    info = f"{dist}-{m.version}.dist-info"
    files: dict[str, bytes] = {
        f"{m.import_name}/__init__.py": pygen.python_module(m, ABI_VERSION).encode(),
        f"{m.import_name}/py.typed": b"",
        f"{m.import_name}/_native/{native.name}": native.read_bytes(),
        f"{info}/METADATA": _metadata(m).encode(),
        f"{info}/WHEEL": (
            f"Wheel-Version: 1.0\nGenerator: twinwrap {TOOL_VERSION}\n"
            f"Root-Is-Purelib: false\nTag: py3-none-{tag}\n"
        ).encode(),
    }
    lic = _license_file(m)
    if lic:
        files[f"{info}/licenses/{lic.name}"] = lic.read_bytes()
    record = io.StringIO()
    w = csv.writer(record, lineterminator="\n")
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        w.writerow([name, f"sha256={digest}", len(data)])
    w.writerow([f"{info}/RECORD", "", ""])
    files[f"{info}/RECORD"] = record.getvalue().encode()

    path = out_dir / f"{dist}-{m.version}-py3-none-{tag}.whl"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            zi = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            zi.external_attr = (0o755 if name.endswith((".so", ".dylib", ".dll")) else 0o644) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
    return path


# --------------------------------------------------------------------- npm


def npm_package_json(m: Manifest, name: str | None = None, registry: str | None = None) -> dict:
    p: dict = {
        "name": name or m.name,
        "version": m.version,
        "description": m.description,
        "main": "index.js",
        "types": "index.d.ts",
        "files": ["index.js", "index.d.ts", f"{m.lib_name}.wasm", "README.md", "LICENSE"],
        "engines": {"node": ">=18"},
        "keywords": ["wasm", "webassembly", "twinwrap"],
    }
    if m.license:
        p["license"] = m.license
    if m.authors:
        p["author"] = m.authors[0]
    if m.repository:
        url = m.repository if m.repository.endswith(".git") else m.repository + ".git"
        p["repository"] = {"type": "git", "url": "git+" + url}
        p["bugs"] = {"url": m.repository.rstrip("/") + "/issues"}
        p["homepage"] = m.repository
    if registry:
        p["publishConfig"] = {"registry": registry}
    return p


def npm_dir(m: Manifest, wasm: Path, dest: Path, *, scoped: str | None = None) -> Path:
    """Write the npm package to <dest>. ``scoped`` renames it, e.g. @owner/name."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    registry = "https://npm.pkg.github.com" if scoped else None
    pj = npm_package_json(m, scoped, registry)
    (dest / "package.json").write_text(json.dumps(pj, indent=2) + "\n")
    (dest / "index.js").write_text(jsgen.js_module(m, ABI_VERSION))
    (dest / "index.d.ts").write_text(jsgen.dts(m))
    shutil.copy2(wasm, dest / wasm.name)
    readme = _readme(m)
    (dest / "README.md").write_text(readme or f"# {m.name}\n\n{m.description}\n")
    lic = _license_file(m)
    if lic:
        shutil.copy2(lic, dest / "LICENSE")
    else:
        pj["files"].remove("LICENSE")
        (dest / "package.json").write_text(json.dumps(pj, indent=2) + "\n")
    return dest


def npm_check(dest: Path, m: Manifest) -> list[str]:
    """Files `npm pack` would include. Raises if a runtime file is missing."""
    out = subprocess.run(
        ["npm", "pack", "--dry-run", "--json", "--ignore-scripts"],
        cwd=dest, capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise PackageError(f"npm pack failed:\n{out.stderr}")
    files = [f["path"] for f in json.loads(out.stdout)[0]["files"]]
    need = ["index.js", "index.d.ts", f"{m.lib_name}.wasm", "package.json"]
    missing = [f for f in need if f not in files]
    if missing:
        raise PackageError(f"npm tarball would be missing: {', '.join(missing)}")
    return files


def npm_tarball(dest: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        ["npm", "pack", "--json", "--ignore-scripts", "--pack-destination", str(out_dir)],
        cwd=dest, capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise PackageError(f"npm pack failed:\n{res.stderr}")
    return out_dir / json.loads(res.stdout)[0]["filename"]
