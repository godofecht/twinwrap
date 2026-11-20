"""Generate .github/workflows/twinwrap.yml for a wrapped project.

On every push and pull request it builds and runs ``twinwrap test``. On a
``v*`` tag it also:

  1. builds the npm package and the Linux x86-64 wheel, runs the Python and
     Node conformance test, and records the reference results;
  2. builds a wheel on every other platform, installs it in a clean venv,
     and requires its results to match the reference bit for bit;
  3. uploads the wheels to PyPI with trusted publishing;
  4. stages the npm package on npmjs.com (it goes live when you approve it
     there) and publishes it to GitHub Packages under the owner's scope.

GitHub Actions is the only CI here because PyPI and npm trusted
publishing both verify a GitHub OIDC token.
"""

from __future__ import annotations

from .manifest import Manifest

from dataclasses import dataclass

FLOW_REF = "v2.0.0"
WASI_SDK = "25"
NPM_WITH_STAGE = "11.21.3"

# (runner, label, flow supported)
PLATFORMS = [
    ("ubuntu-22.04", "linux-x86_64", True),
    ("ubuntu-22.04-arm", "linux-aarch64", True),
    ("macos-15", "macos-arm64", True),
    ("macos-15-intel", "macos-x86_64", True),
    ("windows-2022", "windows-x86_64", False),
]


@dataclass(frozen=True)
class Layout:
    """Where the project sits in its git repository.

    At the root: workflow twinwrap.yml, tags v1.2.3. In a subdirectory (one
    repo, several libraries): workflow twinwrap-<name>.yml, tags
    <name>-v1.2.3, and every command runs with -C <dir>.
    """

    rel: str  # posix path from the repo root, "." for the root
    name: str

    @property
    def at_root(self) -> bool:
        return self.rel == "."

    @property
    def workflow_file(self) -> str:
        return "twinwrap.yml" if self.at_root else f"twinwrap-{self.name}.yml"

    @property
    def tag_prefix(self) -> str:
        return "v" if self.at_root else f"{self.name}-v"

    @property
    def c(self) -> str:
        return "" if self.at_root else f" -C {self.rel}"

    @property
    def p(self) -> str:
        return "" if self.at_root else f"{self.rel}/"


def layout(m: Manifest) -> Layout:
    root = m.root
    for d in [m.root, *m.root.parents]:
        if (d / ".git").exists():
            root = d
            break
    rel = m.root.relative_to(root).as_posix() or "."
    return Layout(rel, m.name)


def _toolchain_steps(m: Manifest, wasm: bool, lay: Layout) -> str:
    if m.lang == "rust":
        target = "\n          targets: wasm32-unknown-unknown" if wasm else ""
        return f"""      - uses: dtolnay/rust-toolchain@stable
        with:
          toolchain: stable{target}
      - uses: Swatinem/rust-cache@v2
        with:
          workspaces: "{lay.p}.twinwrap/glue -> ../target"
"""
    steps = f"""      - name: Get the Flow compiler ({FLOW_REF})
        shell: bash
        run: |
          git clone --depth 1 --branch "$FLOW_REF" https://github.com/flooooooooooow/flow.git "$RUNNER_TEMP/flow"
          echo "TWINWRAP_FLOW=$RUNNER_TEMP/flow" >> "$GITHUB_ENV"
"""
    if wasm:
        steps += f"""      - name: Get wasi-sdk {WASI_SDK}
        shell: bash
        run: |
          v={WASI_SDK}
          case "$(uname -m)" in x86_64) arch=x86_64 ;; aarch64|arm64) arch=arm64 ;; esac
          url="https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-$v/wasi-sdk-$v.0-$arch-linux.tar.gz"
          curl -sSfL "$url" | tar -xz -C "$RUNNER_TEMP"
          echo "WASI_SDK_PATH=$RUNNER_TEMP/wasi-sdk-$v.0-$arch-linux" >> "$GITHUB_ENV"
"""
    return steps


def _paths(lay: Layout, indent: int = 4) -> str:
    if lay.at_root:
        return ""
    pad = " " * indent
    return (f"\n{pad}paths:\n{pad}  - \"{lay.rel}/**\"\n"
            f"{pad}  - \".github/workflows/{lay.workflow_file}\"")


def workflow(m: Manifest, twinwrap_spec: str, lay: Layout | None = None) -> str:
    lay = lay or layout(m)
    C, P = lay.c, lay.p
    plats = [p for p in PLATFORMS if m.lang == "rust" or p[2]]
    # The reference job builds linux-x86_64 itself; the matrix covers the rest.
    matrix = "\n".join(
        f"          - {{ os: {os_}, label: {label} }}" for os_, label, _ in plats if label != "linux-x86_64"
    )
    scope = m.github_scope
    gh_publish = ""
    if scope:
        gh_publish = f"""
      # GitHub's npm registry only takes names under the owner's scope.
      - name: Publish to GitHub Packages as @{scope}/{m.name}
        env:
          NODE_AUTH_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
        run: |
          printf '//npm.pkg.github.com/:_authToken=${{NODE_AUTH_TOKEN}}\\n@{scope}:registry=https://npm.pkg.github.com\\n' > "$RUNNER_TEMP/gh.npmrc"
          export NPM_CONFIG_USERCONFIG="$RUNNER_TEMP/gh.npmrc"
          python -m twinwrap -C "$GITHUB_WORKSPACE/{lay.rel}" npm-dir --scope @{scope} --out "$RUNNER_TEMP/gh-npm" --wasm "$GITHUB_WORKSPACE/.twinwrap-npm/{m.lib_name}.wasm"
          cd "$RUNNER_TEMP/gh-npm"
          if npm view "@{scope}/{m.name}@$VERSION" version >/dev/null 2>&1; then
            echo "::notice::GitHub Packages already has $VERSION"
          else
            npm publish
          fi
"""
    return f"""# Generated by twinwrap. Regenerate with `twinwrap workflow`; edits are overwritten.
#
# Push or pull request: build and run `twinwrap test` (Python and Node agree).
# Tag {lay.tag_prefix}*: also build a wheel per platform, check each against the reference
# results, upload to PyPI, stage on npmjs.com for approval{', and publish to GitHub Packages' if scope else ''}.
#
# One-time setup (see `twinwrap doctor`):
#   PyPI   trusted publisher: this repo, workflow {lay.workflow_file}, environment pypi
#   npm    trusted publisher: this repo, workflow {lay.workflow_file}, environment npm,
#          staged publishing only
name: twinwrap{'' if lay.at_root else f' {m.name}'}

on:
  push:
    branches: [main, master]
    tags: ["{lay.tag_prefix}*"]{_paths(lay)}
  pull_request:{_paths(lay, indent=4)}
  workflow_dispatch:

permissions:
  contents: read

# bash on every runner, Windows included, so $VARS and globs mean one thing.
defaults:
  run:
    shell: bash

env:
  TWINWRAP: "{twinwrap_spec}"
  FLOW_REF: "{FLOW_REF}"

jobs:
  reference:
    name: build, test, reference results (linux-x86_64)
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: actions/setup-node@v4
        with:
          node-version: "24"
{_toolchain_steps(m, True, lay)}      - name: Install twinwrap
        run: python -m pip install "$TWINWRAP"
      - name: Build both packages
        run: python -m twinwrap{C} build --out dist
      - name: Python and Node agree
        run: python -m twinwrap{C} test --no-build --emit reference.json
      - name: Version matches the tag
        if: startsWith(github.ref, 'refs/tags/{lay.tag_prefix}')
        run: python -m twinwrap{C} check-tag "$GITHUB_REF_NAME"
      - uses: actions/upload-artifact@v4
        with:
          name: reference
          path: reference.json
      - uses: actions/upload-artifact@v4
        with:
          name: wheel-linux-x86_64
          path: {P}dist/*.whl
      - uses: actions/upload-artifact@v4
        with:
          name: npm
          path: {P}.twinwrap/npm

  wheels:
    name: wheel ${{{{ matrix.label }}}}
    needs: reference
    if: startsWith(github.ref, 'refs/tags/{lay.tag_prefix}') || github.event_name == 'workflow_dispatch'
    runs-on: ${{{{ matrix.os }}}}
    strategy:
      fail-fast: false
      matrix:
        include:
{matrix}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
{_toolchain_steps(m, False, lay)}      - uses: actions/download-artifact@v4
        with:
          name: reference
      - name: Install twinwrap
        run: python -m pip install "$TWINWRAP"
      - name: Build the wheel
        run: python -m twinwrap{C} build --python-only --out dist
      - name: Installed wheel matches the reference bit for bit
        shell: bash
        run: python -m twinwrap{C} test --wheel {P}dist/*.whl --against reference.json
      - uses: actions/upload-artifact@v4
        with:
          name: wheel-${{{{ matrix.label }}}}
          path: {P}dist/*.whl

  pypi:
    name: upload to PyPI
    needs: [reference, wheels]
    if: startsWith(github.ref, 'refs/tags/{lay.tag_prefix}')
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - uses: actions/download-artifact@v4
        with:
          pattern: wheel-*
          merge-multiple: true
          path: dist
      - run: ls -la dist
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          packages-dir: dist
          skip-existing: true

  npm:
    name: stage on npmjs.com{', publish to GitHub Packages' if scope else ''}
    needs: [reference, wheels]
    if: startsWith(github.ref, 'refs/tags/{lay.tag_prefix}')
    runs-on: ubuntu-latest
    environment: npm
    permissions:
      contents: read
      id-token: write
      packages: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: actions/setup-node@v4
        with:
          node-version: "24"
      - name: Install an npm with staged publishing
        run: npm install -g npm@{NPM_WITH_STAGE} && npm -v
      - name: Install twinwrap
        run: python -m pip install "$TWINWRAP"
      - uses: actions/download-artifact@v4
        with:
          name: npm
          path: .twinwrap-npm
      - name: Version
        run: echo "VERSION=${{GITHUB_REF_NAME#{lay.tag_prefix}}}" >> "$GITHUB_ENV"
      # Staged: the version waits on npmjs.com until you approve it there
      # with your security key, or with `npm stage approve <id>`.
      - name: Stage on npmjs.com
        working-directory: .twinwrap-npm
        run: |
          if npm view "{m.name}@$VERSION" version >/dev/null 2>&1; then
            echo "::notice::npmjs.com already has $VERSION"
          else
            npm stage publish . --access public --ignore-scripts
            echo "::notice::{m.name}@$VERSION is staged. Approve it at https://www.npmjs.com/package/{m.name}"
          fi
{gh_publish}"""
