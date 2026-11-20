# twinwrap

Wrap a Rust or Flow library for PyPI and npm from one function list.

You list the functions to expose in `twinwrap.toml`. twinwrap compiles the
library twice, as a native shared library and as a WebAssembly module, and
generates a Python package and an npm package around them. A test step runs
the same calls through both and requires the results to match bit for bit.

```
twinwrap init       # draft twinwrap.toml from the library's public functions
twinwrap test       # build both, run [[test]] in Python and Node, compare bits
twinwrap build      # dist/<name>-<version>-py3-none-<platform>.whl and the npm package
twinwrap workflow   # .github/workflows/twinwrap.yml
twinwrap doctor     # toolchains, registry names, trusted publisher setup
twinwrap release 1.2.0
```

## How it works

Both languages export the same C interface, one `twinwrap_<name>` function
per signature.

- **Rust.** twinwrap generates a separate `cdylib` crate in `.twinwrap/glue`
  that depends on yours by path. Your crate does not change. Each export
  turns C pointers into `&[T]` and `&str`, calls your function, and copies
  the result out. Panics are caught and raised in Python and JavaScript
  with their message.
- **Flow.** twinwrap runs `flowc --library --export` (Flow 2.x) and appends a
  small C unit that maps each `twinwrap_<name>` to `flow_export_<name>`.

The native library goes into a wheel that loads it with `ctypes`. It does
not use the CPython C API, so one wheel per platform serves every Python 3
from 3.8. The wasm module goes into an npm package with a CommonJS loader
and TypeScript declarations.

| | native (wheel) | wasm (npm) |
|---|---|---|
| Rust | `cargo build`, cdylib | `wasm32-unknown-unknown`, no imports |
| Flow | `cc -shared -ffp-contract=off` | `clang --target=wasm32-wasip1`, reactor |

Flow C is compiled with `-ffp-contract=off` because wasm never fuses
`a * b + c` and a native build that did would round differently. The Flow
example has a test that fails without the flag.

## twinwrap.toml

```toml
[package]
name = "rust-stats"            # PyPI and npm name
version = "0.1.0"
description = "..."
license = "MIT"
repository = "https://github.com/you/rust-stats"

[source]
lang = "rust"                  # or "flow"
path = "."                     # Rust: crate directory. Flow: the .flow file
# default_features = false     # Rust: Cargo features for the wrapped crate
# features = ["simd"]

[[function]]
sig = "fn scale(xs: [f64], k: f64) -> [f64; len(xs)]"
doc = "Multiply every element by k."
# rust = "ops::scale"          # Rust path, when not at the crate root

[[test]]
call = "scale"
args = [[1.0, -2.5], -0.0]
expect = [-0.0, 2.5]           # optional
```

### Types

| Signature | Python | JavaScript | C interface |
|---|---|---|---|
| `i8` ... `i32`, `u8` ... `u32` | `int` | `number` | the C integer |
| `i64`, `u64` | `int` | `bigint` | `int64_t`, `uint64_t` |
| `f32`, `f64` | `float` | `number` | `float`, `double` |
| `bool` | `bool` | `boolean` | `bool` |
| `str` | `str` | `string` | `const uint8_t *p, int32_t len` (UTF-8) |
| `[T]` input | any sequence or buffer | typed array or array | `const T *p, int32_t len` |
| `-> [T; n]` | `array.array` | typed array | trailing `T *out, int32_t out_len` |
| `-> str` (Rust only) | `str` | `string` | returns a buffer the wrapper frees |

A returned array's length is an expression over the inputs: integer
literals, integer parameters, `len(param)`, `+ - *`, `//` and parentheses.
The wrappers compute it and allocate the output, so the library never
allocates memory the caller has to free, except returned strings.

Integers are range-checked in both wrappers. A 64-bit array accepts JS
numbers only when they are safe integers; larger values must be `BigInt`.

### Writing the Rust side

Plain Rust. `&[T]` for inputs, `Vec<T>` (or anything `AsRef<[T]>`) for a
returned array, `&str` in, `String` out.

```rust
pub fn scale(xs: &[f64], k: f64) -> Vec<f64> {
    xs.iter().map(|x| x * k).collect()
}
```

If a dependency uses `wasm-bindgen` on wasm32, the build stops and says so.
Turn the feature off with `default_features` and `features`.

### Writing the Flow side

The Flow function takes the C interface's parameters in order: an input
array is a `ptr<T>` followed by an `i32` length, and a returned array is a
trailing `ptr<T>` and `i32` length that the caller allocates.

```flow
function scale(xs: ptr<f64>, xs_len: i32, k: f64, out: ptr<f64>, out_len: i32) -> i32 {
    let mut i: i32 = 0
    while i < out_len {
        out[i] = xs[i] * k
        i = i + 1
    }
    return 0
}
```

If a function is missing or misnamed, the build prints the declaration it
expected.

## Toolchains

- Rust: `cargo` and `rustup target add wasm32-unknown-unknown`.
  `wasm-opt` is used when it is on PATH.
- Flow: a Flow 2.x checkout (`TWINWRAP_FLOW`, `flow` on PATH, or `~/flow`),
  a C compiler, and for wasm a clang with the wasm32 target plus a WASI
  sysroot. On macOS: `brew install llvm lld wasi-libc wasi-runtimes`.
  Elsewhere: [wasi-sdk](https://github.com/WebAssembly/wasi-sdk) with
  `WASI_SDK_PATH` set.
- Node 18 or later for the npm package and the tests.

Flow libraries do not build on Windows yet, because the Flow C prelude
needs POSIX. Rust libraries build everywhere.

## Releasing

`twinwrap workflow` writes `.github/workflows/twinwrap.yml`. GitHub Actions
is used because PyPI and npm trusted publishing both verify a GitHub OIDC
token. On a `v*` tag it:

1. builds both packages on Linux x86-64, runs `twinwrap test`, and saves
   the results as the reference;
2. builds a wheel on Linux aarch64, macOS arm64 and x86-64 (and Windows for
   Rust), installs each in a fresh venv, and requires its results to match
   the reference bit for bit;
3. uploads every wheel to PyPI with trusted publishing;
4. stages the npm package on npmjs.com, where it waits until you approve
   it with your security key, and publishes it to GitHub Packages when
   `[npm] github_scope` is set.

One-time setup, which `twinwrap doctor` prints with your repository filled in:

- **PyPI.** Add a trusted publisher (a pending one for a new name): this
  repository, workflow `twinwrap.yml`, environment `pypi`.
- **npm.** A trusted publisher can only be added to a package that exists,
  so publish the first version by hand from `.twinwrap/npm`. Then add a
  GitHub Actions trusted publisher on the package's access page: this
  repository, workflow `twinwrap.yml`, environment `npm`, with "Allow npm
  publish" left unticked so releases are staged.

Then each release is:

```
twinwrap release 1.2.0     # sets the version, tests, commits, tags, pushes
```

## Examples

`examples/rust-stats` and `examples/flow-stats` cover every type. Run
`twinwrap -C examples/rust-stats test`.

## Limits

- Functions only: no classes, callbacks, or structs. Pass richer data as
  JSON through `str`.
- The npm package targets Node. It loads the wasm synchronously from disk.
- Linux wheels are tagged with the newest glibc symbol the library uses,
  so a library built on Ubuntu 22.04 may need glibc 2.34 or later.

MIT licence.
