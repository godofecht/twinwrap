"""Read a wasm module's import section without any dependencies."""

from __future__ import annotations

from pathlib import Path


def _uleb(b: bytes, i: int) -> tuple[int, int]:
    r = shift = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << shift
        if not x & 0x80:
            return r, i
        shift += 7


def _name(b: bytes, i: int) -> tuple[str, int]:
    n, i = _uleb(b, i)
    return b[i:i + n].decode("utf-8", "replace"), i + n


def imports(path: Path) -> list[tuple[str, str]]:
    b = path.read_bytes()
    if b[:4] != b"\0asm":
        raise ValueError(f"{path} is not a wasm module")
    i = 8
    out: list[tuple[str, str]] = []
    while i < len(b):
        sid = b[i]
        size, i = _uleb(b, i + 1)
        end = i + size
        if sid == 2:
            count, j = _uleb(b, i)
            for _ in range(count):
                mod, j = _name(b, j)
                name, j = _name(b, j)
                kind = b[j]
                j += 1
                if kind == 0:      # function: type index
                    _, j = _uleb(b, j)
                elif kind == 1:    # table: reftype + limits
                    j += 1
                    flag = b[j]
                    _, j = _uleb(b, j + 1)
                    if flag & 1:
                        _, j = _uleb(b, j)
                elif kind == 2:    # memory: limits
                    flag = b[j]
                    _, j = _uleb(b, j + 1)
                    if flag & 1:
                        _, j = _uleb(b, j)
                elif kind == 3:    # global: valtype + mutability
                    j += 2
                else:              # tag
                    j += 1
                    _, j = _uleb(b, j)
                out.append((mod, name))
            return out
        i = end
    return out
