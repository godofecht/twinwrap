"""twinwrap.toml: the one file that describes a wrapped library.

    [package]
    name = "flowdemo"              # the PyPI and npm name
    version = "0.1.0"
    description = "..."
    license = "MIT"
    repository = "https://github.com/you/flowdemo"

    [source]
    lang = "rust"                  # or "flow"
    path = "."                     # Rust: the crate directory
    # path = "src/lib.flow"        # Flow: the library file

    [[function]]
    sig = "fn mean(xs: [f64]) -> f64"
    doc = "Arithmetic mean."

    [[test]]                       # run in Python and Node, compared bit for bit
    call = "mean"
    args = [[1.0, 2.0, 4.0]]
    expect = 2.3333333333333335
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .sig import Signature, SignatureError, parse_signature

FILENAME = "twinwrap.toml"
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ManifestError(ValueError):
    pass


@dataclass
class Test:
    call: str
    args: list
    expect: object = None
    has_expect: bool = False


@dataclass
class Manifest:
    root: Path
    name: str
    version: str
    lang: str
    source: Path
    functions: list[Signature]
    tests: list[Test] = field(default_factory=list)
    description: str = ""
    license: str = ""
    repository: str = ""
    authors: list[str] = field(default_factory=list)
    import_name: str = ""
    rust_paths: dict[str, str] = field(default_factory=dict)
    github_scope: str = ""

    @property
    def lib_name(self) -> str:
        """File stem of the native library and the wasm module."""
        return self.import_name

    def function(self, name: str) -> Signature:
        for f in self.functions:
            if f.name == name:
                return f
        raise ManifestError(f"no function named {name!r}")


def load(root: Path | str = ".") -> Manifest:
    root = Path(root).resolve()
    path = root / FILENAME
    if not path.exists():
        raise ManifestError(f"no {FILENAME} in {root}; run `twinwrap init` first")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"{path}: {e}") from e
    return from_dict(root, data)


def from_dict(root: Path, data: dict) -> Manifest:
    pkg = data.get("package") or {}
    src = data.get("source") or {}
    name = pkg.get("name", "")
    if not _NAME.match(name):
        raise ManifestError(f"[package] name {name!r}: lowercase letters, digits, '-', '_', '.'")
    version = str(pkg.get("version", ""))
    if not re.fullmatch(r"\d+\.\d+\.\d+([.-]?[A-Za-z0-9.]+)?", version):
        raise ManifestError(f"[package] version {version!r} is not a version like 1.2.3")
    lang = src.get("lang", "")
    if lang not in ("rust", "flow"):
        raise ManifestError(f"[source] lang must be 'rust' or 'flow', got {lang!r}")
    source = (root / src.get("path", "." if lang == "rust" else "")).resolve()
    if lang == "rust" and not (source / "Cargo.toml").exists():
        raise ManifestError(f"[source] path {source} has no Cargo.toml")
    if lang == "flow" and not (source.is_file() and source.suffix == ".flow"):
        raise ManifestError(f"[source] path {source} is not a .flow file")

    functions: list[Signature] = []
    rust_paths: dict[str, str] = {}
    for i, f in enumerate(data.get("function") or []):
        try:
            sig = parse_signature(f["sig"], f.get("doc", ""))
        except KeyError as e:
            raise ManifestError(f"[[function]] #{i + 1} has no sig") from e
        except SignatureError as e:
            raise ManifestError(str(e)) from e
        if any(s.name == sig.name for s in functions):
            raise ManifestError(f"function {sig.name!r} is listed twice")
        if lang == "flow" and sig.ret.kind == "str":
            raise ManifestError(f"{sig.name}: Flow functions cannot return str yet")
        functions.append(sig)
        if "rust" in f:
            rust_paths[sig.name] = f["rust"]
    if not functions:
        raise ManifestError("no [[function]] entries")

    tests: list[Test] = []
    names = {f.name for f in functions}
    for i, t in enumerate(data.get("test") or []):
        if t.get("call") not in names:
            raise ManifestError(f"[[test]] #{i + 1} calls unknown function {t.get('call')!r}")
        sig = next(f for f in functions if f.name == t["call"])
        args = list(t.get("args", []))
        if len(args) != len(sig.params):
            raise ManifestError(
                f"[[test]] #{i + 1}: {sig.name} takes {len(sig.params)} arguments, got {len(args)}"
            )
        tests.append(Test(t["call"], args, t.get("expect"), "expect" in t))

    import_name = pkg.get("import_name") or re.sub(r"[^a-z0-9_]", "_", name)
    if not import_name.isidentifier():
        raise ManifestError(f"import name {import_name!r} is not a Python identifier")
    authors = pkg.get("authors") or []
    return Manifest(
        root=root,
        name=name,
        version=version,
        lang=lang,
        source=source,
        functions=functions,
        tests=tests,
        description=pkg.get("description", ""),
        license=pkg.get("license", ""),
        repository=pkg.get("repository", ""),
        authors=list(authors),
        import_name=import_name,
        rust_paths=rust_paths,
        github_scope=(data.get("npm") or {}).get("github_scope", ""),
    )


def set_version(root: Path, version: str) -> None:
    """Rewrite [package] version in place, leaving the rest of the file alone."""
    path = Path(root) / FILENAME
    text = path.read_text()
    new, n = re.subn(
        r'(?ms)^(\[package\][^\[]*?^version\s*=\s*)"[^"]*"', rf'\g<1>"{version}"', text, count=1
    )
    if n != 1:
        raise ManifestError(f"could not find [package] version in {path}")
    path.write_text(new)
