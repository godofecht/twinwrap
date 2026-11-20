"""Function signatures and the C interface they map to.

A signature is written the way it reads in the manifest:

    fn mean(xs: [f64]) -> f64
    fn scale(xs: [f64], k: f64) -> [f64; len(xs)]
    fn greet(name: str) -> str

Every library, Rust or Flow, exports the same C function for a signature,
named ``twinwrap_<name>``. Its parameters follow these rules:

    scalar ``T``          one C parameter of that type
    slice ``[T]``         ``const T *p_ptr, int32_t p_len``
    ``str``               ``const uint8_t *p_ptr, int32_t p_len`` (UTF-8, no NUL)
    returns ``T``         the C return value
    returns ``[T; n]``    trailing ``T *out_ptr, int32_t out_len``; returns void
    returns ``str``       trailing ``int32_t *out_len``; returns a ``uint8_t *``
                          the caller frees with ``twinwrap_free(ptr, len)``

The wrappers compute an output array's length from the expression after
the semicolon, allocate it, and pass it in, so the library never allocates
memory the caller has to free except for returned strings.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

# name -> (C type, ctypes name, wasm value type, byte size, JS typed array)
SCALARS: dict[str, tuple[str, str, str, int, str | None]] = {
    "i8": ("int8_t", "c_int8", "i32", 1, "Int8Array"),
    "u8": ("uint8_t", "c_uint8", "i32", 1, "Uint8Array"),
    "i16": ("int16_t", "c_int16", "i32", 2, "Int16Array"),
    "u16": ("uint16_t", "c_uint16", "i32", 2, "Uint16Array"),
    "i32": ("int32_t", "c_int32", "i32", 4, "Int32Array"),
    "u32": ("uint32_t", "c_uint32", "i32", 4, "Uint32Array"),
    "i64": ("int64_t", "c_int64", "i64", 8, "BigInt64Array"),
    "u64": ("uint64_t", "c_uint64", "i64", 8, "BigUint64Array"),
    "f32": ("float", "c_float", "f32", 4, "Float32Array"),
    "f64": ("double", "c_double", "f64", 8, "Float64Array"),
    "bool": ("bool", "c_bool", "i32", 1, None),
}

RUST_SCALAR = {k: k for k in SCALARS}
FLOW_SCALAR = {k: k for k in SCALARS}


class SignatureError(ValueError):
    pass


@dataclass(frozen=True)
class Type:
    kind: str  # "scalar" | "slice" | "str" | "array" | "none"
    elem: str | None = None  # scalar name for scalar/slice/array
    length: str | None = None  # length expression for "array"

    def __str__(self) -> str:
        if self.kind == "scalar":
            return self.elem or ""
        if self.kind == "slice":
            return f"[{self.elem}]"
        if self.kind == "array":
            return f"[{self.elem}; {self.length}]"
        if self.kind == "str":
            return "str"
        return "()"


@dataclass(frozen=True)
class Param:
    name: str
    type: Type


@dataclass(frozen=True)
class Signature:
    name: str
    params: tuple[Param, ...]
    ret: Type
    doc: str = ""

    @property
    def symbol(self) -> str:
        return f"twinwrap_{self.name}"

    def __str__(self) -> str:
        ps = ", ".join(f"{p.name}: {p.type}" for p in self.params)
        r = "" if self.ret.kind == "none" else f" -> {self.ret}"
        return f"fn {self.name}({ps}){r}"


_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_SIG = re.compile(rf"^\s*fn\s+({_IDENT})\s*\((.*)\)\s*(?:->\s*(.+?))?\s*$", re.S)
RESERVED = {"twinwrap", "out", "out_len", "out_ptr"}


def _split_params(s: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts]


def parse_type(text: str, *, is_return: bool) -> Type:
    t = text.strip()
    if t in ("", "()"):
        return Type("none")
    if t == "str":
        return Type("str")
    if t in SCALARS:
        return Type("scalar", t)
    m = re.fullmatch(r"\[\s*(\w+)\s*(?:;\s*(.+))?\]", t)
    if m:
        elem, length = m.group(1), m.group(2)
        if elem not in SCALARS or elem == "bool":
            raise SignatureError(f"array element must be a numeric type, got {elem!r}")
        if is_return:
            if not length:
                raise SignatureError(
                    f"a returned array needs its length: [{elem}; <expression>]"
                )
            return Type("array", elem, length.strip())
        if length:
            raise SignatureError("only returned arrays take a length")
        return Type("slice", elem)
    raise SignatureError(f"unknown type {t!r}")


def parse_signature(text: str, doc: str = "") -> Signature:
    m = _SIG.match(text)
    if not m:
        raise SignatureError(f"cannot read signature {text!r}; expected 'fn name(a: T) -> R'")
    name, params_s, ret_s = m.group(1), m.group(2), m.group(3) or ""
    if name.startswith("twinwrap"):
        raise SignatureError(f"{name}: names starting with 'twinwrap' are reserved")
    params: list[Param] = []
    seen: set[str] = set()
    for p in _split_params(params_s):
        pm = re.fullmatch(rf"({_IDENT})\s*:\s*(.+)", p)
        if not pm:
            raise SignatureError(f"{name}: cannot read parameter {p!r}")
        pname = pm.group(1)
        if pname in seen:
            raise SignatureError(f"{name}: parameter {pname!r} appears twice")
        if pname in RESERVED:
            raise SignatureError(f"{name}: parameter name {pname!r} is reserved")
        seen.add(pname)
        params.append(Param(pname, parse_type(pm.group(2), is_return=False)))
    ret = parse_type(ret_s, is_return=True)
    sig = Signature(name, tuple(params), ret, doc)
    if ret.kind == "array":
        LengthExpr(ret.length or "", sig)  # validate now
    return sig


class LengthExpr:
    """The length of a returned array, in terms of the inputs.

    Allowed: integer literals, integer scalar parameters, ``len(slice)``,
    ``+ - *``, ``//`` and parentheses. It is translated, not evaluated, so
    the Python and JavaScript wrappers compute the same number.
    """

    def __init__(self, text: str, sig: Signature):
        self.text = text
        self.sig = sig
        try:
            tree = ast.parse(text, mode="eval")
        except SyntaxError as e:
            raise SignatureError(f"{sig.name}: bad length expression {text!r}") from e
        self.tree = tree.body
        self._check(self.tree)

    def _param(self, name: str) -> Param:
        for p in self.sig.params:
            if p.name == name:
                return p
        raise SignatureError(f"{self.sig.name}: length uses unknown parameter {name!r}")

    def _check(self, n: ast.AST) -> None:
        if isinstance(n, ast.Constant) and isinstance(n.value, int) and not isinstance(n.value, bool):
            return
        if isinstance(n, ast.Name):
            p = self._param(n.id)
            if p.type.kind != "scalar" or p.type.elem in ("f32", "f64", "bool"):
                raise SignatureError(
                    f"{self.sig.name}: length uses {n.id!r}, which is not an integer"
                )
            return
        if isinstance(n, ast.Call):
            if not (isinstance(n.func, ast.Name) and n.func.id == "len" and len(n.args) == 1
                    and isinstance(n.args[0], ast.Name) and not n.keywords):
                raise SignatureError(f"{self.sig.name}: only len(param) is allowed in a length")
            p = self._param(n.args[0].id)
            if p.type.kind not in ("slice", "str"):
                raise SignatureError(f"{self.sig.name}: len() needs a slice or str parameter")
            return
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv)):
            self._check(n.left)
            self._check(n.right)
            return
        raise SignatureError(f"{self.sig.name}: unsupported length expression {self.text!r}")

    def render(self, lang: str, ref) -> str:
        """``ref(param_name, is_len)`` gives the target-language expression."""

        def go(n: ast.AST) -> str:
            if isinstance(n, ast.Constant):
                return str(n.value)
            if isinstance(n, ast.Name):
                return ref(n.id, False)
            if isinstance(n, ast.Call):
                return ref(n.args[0].id, True)  # type: ignore[attr-defined]
            assert isinstance(n, ast.BinOp)
            a, b = go(n.left), go(n.right)
            if isinstance(n.op, ast.FloorDiv):
                return f"({a} // {b})" if lang == "py" else f"Math.floor({a} / {b})"
            op = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*"}[type(n.op)]
            return f"({a} {op} {b})"

        return go(self.tree)


def c_params(sig: Signature) -> list[tuple[str, str]]:
    """(C type, name) for each parameter of ``twinwrap_<name>``."""
    out: list[tuple[str, str]] = []
    for p in sig.params:
        t = p.type
        if t.kind == "scalar":
            out.append((SCALARS[t.elem][0], p.name))  # type: ignore[index]
        elif t.kind == "slice":
            out.append((f"const {SCALARS[t.elem][0]} *", f"{p.name}_ptr"))  # type: ignore[index]
            out.append(("int32_t", f"{p.name}_len"))
        elif t.kind == "str":
            out.append(("const uint8_t *", f"{p.name}_ptr"))
            out.append(("int32_t", f"{p.name}_len"))
    if sig.ret.kind == "array":
        out.append((f"{SCALARS[sig.ret.elem][0]} *", "out_ptr"))  # type: ignore[index]
        out.append(("int32_t", "out_len"))
    elif sig.ret.kind == "str":
        out.append(("int32_t *", "out_len"))
    return out


def c_return(sig: Signature) -> str:
    r = sig.ret
    if r.kind == "scalar":
        return SCALARS[r.elem][0]  # type: ignore[index]
    if r.kind == "str":
        return "uint8_t *"
    return "void"


def c_prototype(sig: Signature) -> str:
    ps = ", ".join(f"{t} {n}".replace("* ", "*") for t, n in c_params(sig)) or "void"
    ret = c_return(sig)
    sep = "" if ret.endswith("*") else " "
    return f"{ret}{sep}{sig.symbol}({ps})"
