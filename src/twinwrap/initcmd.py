"""`twinwrap init`: draft twinwrap.toml from a library's public functions.

The draft is a starting point. Functions whose types twinwrap cannot carry
are written as comments with the reason, and a returned array's length is a
guess marked with "check".
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import manifest
from .sig import SCALARS, SignatureError, parse_signature


@dataclass
class Found:
    name: str
    sig: str | None  # None when unsupported
    note: str = ""
    rust_path: str = ""  # module path when not at the crate root


# ------------------------------------------------------------------- Rust

_RUST_FN = re.compile(
    r"^\s*pub\s+(?:const\s+)?fn\s+([A-Za-z_]\w*)\s*(<[^>]*>)?\s*\(([^)]*)\)\s*(?:->\s*([^{;]+?))?\s*(?:where[^{]*)?\{",
    re.M | re.S,
)


def _rust_type(t: str, *, ret: bool) -> tuple[str | None, str]:
    t = re.sub(r"\s+", "", t)
    if t in SCALARS:
        return t, ""
    m = re.fullmatch(r"&(?:'\w+)?\[(\w+)\]", t)
    if m and m.group(1) in SCALARS and m.group(1) != "bool" and not ret:
        return f"[{m.group(1)}]", ""
    if t in ("&str", "&'staticstr") and not ret:
        return "str", ""
    if ret and t in ("String", "&'staticstr", "Box<str>"):
        return "str", ""
    m = re.fullmatch(r"(?:Vec|Box)<(\w+)>|Box<\[(\w+)\]>|\[(\w+);(\d+)\]", t)
    if ret and m:
        elem = m.group(1) or m.group(2) or m.group(3)
        if elem in SCALARS and elem != "bool":
            return f"[{elem}; ?]", m.group(4) or ""
    return None, f"type {t} is not supported"


def _strip_rust(text: str) -> str:
    """Remove comments and string literals so braces can be counted."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r'r(#*)".*?"\1', '""', text, flags=re.S)
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    text = re.sub(r"'(?:\\.|[^'\\])'", "' '", text)
    return text


def _top_level(text: str) -> str:
    """Only what sits at module level: the bodies of every block are blanked."""
    out, depth = [], 0
    for ch in text:
        if ch == "{":
            if depth == 0:
                out.append("{")
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _module_files(crate: Path, lib: Path) -> list[tuple[str, Path]]:
    """(module path, file) for the crate root and every `pub mod` reachable from it."""
    seen: list[tuple[str, Path]] = []

    def visit(path: str, file: Path) -> None:
        if not file.exists() or any(f == file for _, f in seen):
            return
        seen.append((path, file))
        top = _top_level(_strip_rust(file.read_text(encoding="utf-8")))
        here = file.parent if file.name in ("lib.rs", "mod.rs", "main.rs") else file.parent / file.stem
        for m in re.finditer(r"\bpub\s+mod\s+([A-Za-z_]\w*)\s*;", top):
            name = m.group(1)
            child = path + "::" + name if path else name
            for cand in (here / f"{name}.rs", here / name / "mod.rs"):
                if cand.exists():
                    visit(child, cand)
                    break

    visit("", lib)
    return seen


def scan_rust(crate: Path) -> list[Found]:
    lib = crate / "src" / "lib.rs"
    data = tomllib.loads((crate / "Cargo.toml").read_text(encoding="utf-8"))
    custom = (data.get("lib") or {}).get("path")
    if custom:
        lib = crate / custom
    if not lib.exists():
        return []
    found: list[Found] = []
    for modpath, file in _module_files(crate, lib):
        text = _top_level(_strip_rust(file.read_text(encoding="utf-8")))
        for f in _scan_rust_text(text):
            f.rust_path = f"{modpath}::{f.name}" if modpath else ""
            found.append(f)
    # Two modules exporting the same name: qualify both.
    names = [f.name for f in found]
    for f in found:
        if names.count(f.name) > 1 and f.rust_path:
            alias = f.rust_path.replace("::", "_")
            if f.sig:
                f.sig = f.sig.replace(f"fn {f.name}(", f"fn {alias}(", 1)
            f.name = alias
    return found


def _scan_rust_text(text: str) -> list[Found]:
    found: list[Found] = []
    for m in _RUST_FN.finditer(text):
        name, generics, params_s, ret_s = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        if generics:
            found.append(Found(name, None, "generic functions are not supported"))
            continue
        params, bad, slices = [], "", []
        for p in [x.strip() for x in params_s.split(",") if x.strip()]:
            if p in ("self", "&self", "&mutself", "&mut self") or ":" not in p:
                bad = "methods are not supported"
                break
            pn, pt = (x.strip() for x in p.split(":", 1))
            pn = pn.removeprefix("mut ").strip()
            t, why = _rust_type(pt, ret=False)
            if not t:
                bad = f"{pn}: {why}"
                break
            if t.startswith("[") or t == "str":
                slices.append(pn)
            params.append(f"{pn}: {t}")
        if bad:
            found.append(Found(name, None, bad))
            continue
        note = ""
        ret = ""
        if ret_s.strip() and ret_s.strip() != "()":
            t, fixed = _rust_type(ret_s, ret=True)
            if not t:
                found.append(Found(name, None, f"return {fixed}"))
                continue
            if "; ?" in t:
                length = fixed or (f"len({slices[0]})" if slices else "1")
                t = t.replace("?", length)
                if not fixed:
                    note = "check the returned length"
            ret = f" -> {t}"
        found.append(Found(name, f"fn {name}({', '.join(params)}){ret}", note))
    return found


# ------------------------------------------------------------------- Flow

_FLOW_FN = re.compile(r"^(?:export\s+|pub\s+)?function\s+([A-Za-z]\w*)\s*\(([^)]*)\)\s*(?:->\s*(\w+))?", re.M)


def scan_flow(path: Path) -> list[Found]:
    found: list[Found] = []
    for m in _FLOW_FN.finditer(path.read_text(encoding="utf-8")):
        name, params_s, ret = m.group(1), m.group(2), (m.group(3) or "").strip()
        if name == "main":
            continue
        ps = [tuple(x.strip() for x in p.split(":", 1)) for p in params_s.split(",") if ":" in p]
        out: list[str] = []
        ret_t = ret if ret in SCALARS else ""
        bad = ""
        i = 0
        slices: list[str] = []
        while i < len(ps):
            pn, pt = ps[i]
            nxt = ps[i + 1] if i + 1 < len(ps) else None
            pm = re.fullmatch(r"ptr<(\w+)>", pt)
            if pm and nxt and nxt[1] == "i32":
                elem = pm.group(1)
                if elem not in SCALARS or elem == "bool":
                    bad = f"{pn}: ptr<{elem}> is not supported"
                    break
                if i + 2 == len(ps) and pn in ("out", "result", "dst"):
                    length = f"len({slices[0]})" if slices else nxt[0]
                    ret_t = f"[{elem}; {length}]"
                else:
                    out.append(f"{pn}: {'str' if elem == 'u8' and pn in ('s', 'text', 'name', 'str') else f'[{elem}]'}")
                    slices.append(pn)
                i += 2
                continue
            if pt in SCALARS:
                out.append(f"{pn}: {pt}")
                i += 1
                continue
            bad = f"{pn}: {pt} is not supported (use scalars, or ptr<T> followed by an i32 length)"
            break
        if bad:
            found.append(Found(name, None, bad))
            continue
        r = f" -> {ret_t}" if ret_t else ""
        note = "check the returned length" if ret_t.startswith("[") else ""
        found.append(Found(name, f"fn {name}({', '.join(out)}){r}", note))
    return found


# ----------------------------------------------------------------- writer


def render(name: str, lang: str, source: str, found: list[Found], repository: str = "") -> str:
    lines = [
        "# twinwrap.toml: the functions to wrap for PyPI and npm.",
        "# Types: i8..i64, u8..u64, f32, f64, bool, str, [T] (an input array),",
        "# and [T; length] for a returned array, e.g. [f64; len(xs)].",
        "",
        "[package]",
        f'name = "{name}"',
        'version = "0.1.0"',
        'description = ""',
        'license = "MIT"',
    ]
    if repository:
        lines.append(f'repository = "{repository}"')
    lines += ["", "[source]", f'lang = "{lang}"', f'path = "{source}"', ""]
    first_ok = None
    for f in found:
        if f.sig:
            try:
                parse_signature(f.sig)
            except SignatureError as e:
                lines += [f"# {f.name}: {e}", ""]
                continue
            first_ok = first_ok or f
            if f.note:
                lines.append(f"# {f.note}")
            lines += ["[[function]]", f'sig = "{f.sig}"']
            if f.rust_path:
                lines.append(f'rust = "{f.rust_path}"')
            lines.append("")
        else:
            lines.append(f"# skipped {f.name}: {f.note}")
    if any(not f.sig for f in found):
        lines.append("")
    lines += [
        "# Each [[test]] runs in Python and in Node; the results must match bit for bit.",
        "# [[test]]",
        f'# call = "{first_ok.name if first_ok else "name"}"',
        "# args = []",
        "# expect = ...",
        "",
    ]
    return "\n".join(lines)


def _git_remote(root: Path) -> str:
    cfg = root / ".git" / "config"
    if not cfg.exists():
        return ""
    m = re.search(r'\[remote "origin"\][^\[]*url\s*=\s*(\S+)', cfg.read_text(encoding="utf-8"))
    if not m:
        return ""
    url = m.group(1)
    url = re.sub(r"^git@github\.com:", "https://github.com/", url)
    return url.removesuffix(".git")


def cmd_init(args) -> int:
    root = Path(args.dir).resolve()
    target = root / manifest.FILENAME
    if target.exists() and not args.force:
        print(f"twinwrap: {target} exists; use --force to overwrite", file=sys.stderr)
        return 1
    lang = args.lang
    if not lang:
        if (root / "Cargo.toml").exists():
            lang = "rust"
        elif args.source and args.source.endswith(".flow") or list(root.glob("*.flow")) or list(root.glob("src/*.flow")):
            lang = "flow"
        else:
            print("twinwrap: no Cargo.toml or .flow file here; pass --lang and --source", file=sys.stderr)
            return 1
    if lang == "rust":
        crate = (root / (args.source or ".")).resolve()
        found = scan_rust(crate)
        default_name = tomllib.loads((crate / "Cargo.toml").read_text(encoding="utf-8"))["package"]["name"]
        source = str(crate.relative_to(root)) if crate.is_relative_to(root) else str(crate)
        source = source if source != "" else "."
    else:
        if args.source:
            src = (root / args.source).resolve()
        else:
            cands = sorted(root.glob("src/*.flow")) or sorted(root.glob("*.flow"))
            if len(cands) != 1:
                print("twinwrap: pass --source with the Flow library file", file=sys.stderr)
                return 1
            src = cands[0]
        found = scan_flow(src)
        default_name = src.stem.replace("_", "-")
        source = str(src.relative_to(root))
    name = (args.name or default_name).lower()
    target.write_text(render(name, lang, source, found, _git_remote(root)))
    ok = sum(1 for f in found if f.sig)
    print(f"twinwrap: wrote {target.name} with {ok} function(s)"
          + (f"; skipped {len(found) - ok}" if len(found) > ok else ""))
    gi = root / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    if ".twinwrap/" not in lines:
        gi.write_text("\n".join(lines + [".twinwrap/"]) + "\n", encoding="utf-8")
    return 0
