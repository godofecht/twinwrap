"""Compile the library twice: a native shared library and a wasm module.

Everything is built under ``<project>/.twinwrap/``:

    .twinwrap/glue/       generated Rust crate or C unit
    .twinwrap/native/     lib<name>.so | lib<name>.dylib | <name>.dll
    .twinwrap/wasm/       <name>.wasm
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import glue, wasmcheck
from .manifest import Manifest


class BuildError(RuntimeError):
    pass


def work_dir(m: Manifest) -> Path:
    d = m.root / ".twinwrap"
    d.mkdir(exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")
    return d


def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None, quiet: bool = False) -> str:
    shown = " ".join(str(c) for c in cmd)
    if not quiet:
        print(f"  $ {shown}", file=sys.stderr)
    p = subprocess.run(
        [str(c) for c in cmd], cwd=cwd, env={**os.environ, **(env or {})},
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-40:])
        raise BuildError(f"command failed ({p.returncode}): {shown}\n{tail}")
    return p.stdout


def native_filename(lib: str, system: str | None = None) -> str:
    system = system or sys.platform
    if system.startswith("win"):
        return f"{lib}.dll"
    if system == "darwin":
        return f"lib{lib}.dylib"
    return f"lib{lib}.so"


# ------------------------------------------------------------- toolchains


def _first(*cands: str | Path | None) -> Path | None:
    for c in cands:
        if c and Path(c).exists():
            return Path(c)
    return None


def flow_cli() -> Path:
    """The Flow 2.x command line: $TWINWRAP_FLOW, `flow` on PATH, or ~/flow/flow."""
    env = os.environ.get("TWINWRAP_FLOW")
    if env:
        p = Path(env).expanduser()
        p = p / "flow" if p.is_dir() else p
        if not p.exists():
            raise BuildError(f"TWINWRAP_FLOW={env} has no flow command")
        return p
    on_path = shutil.which("flow")
    found = _first(on_path, Path.home() / "flow" / "flow")
    if not found:
        raise BuildError(
            "cannot find the Flow compiler. Set TWINWRAP_FLOW to a Flow 2.x checkout "
            "(https://github.com/flooooooooooow/flow)."
        )
    return found


def wasm_clang() -> Path:
    env = os.environ.get("TWINWRAP_CLANG")
    sdk = os.environ.get("WASI_SDK_PATH")
    found = _first(
        env,
        sdk and Path(sdk) / "bin" / "clang",
        "/opt/homebrew/opt/llvm/bin/clang",
        "/usr/local/opt/llvm/bin/clang",
        "/opt/wasi-sdk/bin/clang",
    )
    if not found:
        raise BuildError("no clang with a wasm32 target. Install wasi-sdk and set WASI_SDK_PATH.")
    return found


def wasi_sysroot() -> Path:
    env = os.environ.get("TWINWRAP_WASI_SYSROOT")
    sdk = os.environ.get("WASI_SDK_PATH")
    found = _first(
        env,
        sdk and Path(sdk) / "share" / "wasi-sysroot",
        "/opt/homebrew/share/wasi-sysroot",
        "/usr/local/share/wasi-sysroot",
        "/opt/wasi-sdk/share/wasi-sysroot",
    )
    if not found:
        raise BuildError("no WASI sysroot. Install wasi-sdk (WASI_SDK_PATH) or `brew install wasi-libc`.")
    return found


def wasm_ld_flag() -> list[str]:
    """Homebrew's clang does not ship wasm-ld; lld is a separate formula."""
    if os.environ.get("WASI_SDK_PATH"):
        return []
    lld = _first("/opt/homebrew/opt/lld/bin/wasm-ld", "/usr/local/opt/lld/bin/wasm-ld")
    return [f"-fuse-ld={lld}"] if lld else []


# ------------------------------------------------------------------- Flow


def flow_emit(m: Manifest) -> Path:
    """Flow source -> one C unit with the twinwrap glue appended."""
    d = work_dir(m) / "glue"
    d.mkdir(exist_ok=True)
    raw = d / f"{m.lib_name}.flow.c"
    flow = flow_cli()
    names = ",".join(f.name for f in m.functions)
    run(
        [flow, "tool", "compiler/scripts/flowc_emit.flow", "--library", "--export", names, m.source, raw],
        cwd=flow.resolve().parent,
    )
    text = raw.read_text()
    missing = [f.name for f in m.functions if f"flow_export_{f.name}(" not in text]
    if missing:
        hints = "\n".join("  " + glue.flow_expected_signature(m.function(n)) for n in missing)
        raise BuildError(
            f"{m.source.name} does not define {', '.join(missing)}. Expected:\n{hints}"
        )
    unit = d / f"{m.lib_name}.c"
    unit.write_text(text + glue.flow_glue(m))
    (d / "wasi_compat.h").write_text(glue.WASI_COMPAT_H)
    (d / f"{m.lib_name}.h").write_text(glue.c_header(m))
    return unit


def flow_native(m: Manifest, unit: Path) -> Path:
    out = work_dir(m) / "native"
    out.mkdir(exist_ok=True)
    if sys.platform.startswith("win"):
        raise BuildError("Flow libraries do not build on Windows yet: the Flow C prelude needs POSIX")
    lib = out / native_filename(m.lib_name)
    cc = os.environ.get("CC", "cc")
    # No FMA contraction: wasm never fuses a*b+c, so a native build that did
    # would round differently and break bit-for-bit agreement.
    flags = ["-O2", "-fPIC", "-w", "-ffp-contract=off"]
    if sys.platform == "darwin":
        flags += ["-dynamiclib", "-install_name", f"@rpath/{lib.name}"]
    else:
        flags += ["-shared", "-Wl,--as-needed"]
    run([cc, *flags, "-o", lib, unit, "-lm"])
    return lib


def flow_wasm(m: Manifest, unit: Path) -> Path:
    out = work_dir(m) / "wasm"
    out.mkdir(exist_ok=True)
    wasm = out / f"{m.lib_name}.wasm"
    emu = ["process-clocks", "signal", "getpid"]
    run([
        wasm_clang(), "--target=wasm32-wasip1", f"--sysroot={wasi_sysroot()}",
        "-mexec-model=reactor", "-O2", "-w", "-ffp-contract=off",
        "-include", unit.parent / "wasi_compat.h",
        *[f"-D_WASI_EMULATED_{e.upper().replace('-', '_')}" for e in emu],
        *wasm_ld_flag(), "-Wl,--strip-debug",
        "-o", wasm, unit, "-lm", *[f"-lwasi-emulated-{e}" for e in emu],
    ])
    return wasm


# ------------------------------------------------------------------- Rust


def rust_glue_crate(m: Manifest) -> Path:
    d = work_dir(m) / "glue"
    d.mkdir(exist_ok=True)
    (d / "Cargo.toml").write_text(glue.rust_cargo_toml(m, m.source))
    (d / "lib.rs").write_text(glue.rust_glue(m, m.source))
    (d / f"{m.lib_name}.h").write_text(glue.c_header(m))
    return d


def _cargo_target_dir(m: Manifest) -> Path:
    return work_dir(m) / "target"


def rust_native(m: Manifest, crate: Path, target: str | None = None) -> Path:
    out = work_dir(m) / "native"
    out.mkdir(exist_ok=True)
    cmd = ["cargo", "build", "--release", "--manifest-path", crate / "Cargo.toml",
           "--target-dir", _cargo_target_dir(m)]
    if target:
        cmd += ["--target", target]
    run(cmd)
    tdir = _cargo_target_dir(m) / (target or "") / "release"
    system = sys.platform
    if target:
        system = "win32" if "windows" in target else "darwin" if "apple" in target else "linux"
    name = native_filename(m.lib_name, system)
    built = tdir / name
    if not built.exists():
        raise BuildError(f"cargo did not produce {built}")
    lib = out / name
    shutil.copy2(built, lib)
    return lib


def rust_wasm(m: Manifest, crate: Path) -> Path:
    out = work_dir(m) / "wasm"
    out.mkdir(exist_ok=True)
    run(["cargo", "build", "--release", "--target", "wasm32-unknown-unknown",
         "--manifest-path", crate / "Cargo.toml", "--target-dir", _cargo_target_dir(m)])
    built = _cargo_target_dir(m) / "wasm32-unknown-unknown" / "release" / f"{m.lib_name}.wasm"
    wasm = out / f"{m.lib_name}.wasm"
    shutil.copy2(built, wasm)
    opt = shutil.which("wasm-opt")
    if opt:
        run([opt, "-O2", "--strip-debug", "--enable-bulk-memory", "--enable-nontrapping-float-to-int",
             "--enable-sign-ext", "--enable-mutable-globals", "-o", wasm, wasm])
    return wasm


# ----------------------------------------------------------------- driver


def check_wasm_imports(m: Manifest, wasm: Path) -> None:
    foreign = sorted({mod for mod, _ in wasmcheck.imports(wasm) if mod != "wasi_snapshot_preview1"})
    if not foreign:
        return
    hint = ""
    if any("wbindgen" in f for f in foreign):
        hint = ("\nA dependency uses wasm-bindgen on wasm32 (often getrandom's \"js\" feature). "
                "Turn off the Cargo features that pull it in with [source] default_features = false "
                "and features = [...].")
    raise BuildError(f"{wasm.name} imports {', '.join(foreign)}, which the npm loader cannot supply.{hint}")


def build(m: Manifest, *, native: bool = True, wasm: bool = True, target: str | None = None) -> dict[str, Path]:
    built: dict[str, Path] = {}
    if m.lang == "flow":
        unit = flow_emit(m)
        if native:
            built["native"] = flow_native(m, unit)
        if wasm:
            built["wasm"] = flow_wasm(m, unit)
    else:
        crate = rust_glue_crate(m)
        if native:
            built["native"] = rust_native(m, crate, target)
        if wasm:
            built["wasm"] = rust_wasm(m, crate)
    if "wasm" in built:
        check_wasm_imports(m, built["wasm"])
    return built


def host_description() -> str:
    return f"{sys.platform} {platform.machine()}"
