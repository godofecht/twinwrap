import zipfile
from pathlib import Path

from twinwrap import package
from twinwrap.manifest import from_dict


def test_linux_tag_reads_newest_glibc(tmp_path):
    lib = tmp_path / "libx.so"
    lib.write_bytes(b"\x7fELF...GLIBC_2.17\x00GLIBC_2.28\x00GLIBC_2.3\x00")
    assert package.linux_tag(lib, "x86_64") == "manylinux_2_28_x86_64"


def test_macos_tags(tmp_path, monkeypatch):
    monkeypatch.delenv("MACOSX_DEPLOYMENT_TARGET", raising=False)
    lib = tmp_path / "x.dylib"
    lib.write_bytes(b"")
    assert package.platform_tag(lib, "darwin", "arm64") == "macosx_11_0_arm64"
    assert package.platform_tag(lib, "darwin", "x86_64") == "macosx_10_12_x86_64"
    assert package.platform_tag(lib, "win32", "AMD64") == "win_amd64"


def test_wheel_record_matches_contents(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "d"\nversion = "0.1.0"\n')
    m = from_dict(tmp_path, {
        "package": {"name": "demo", "version": "1.2.3"},
        "source": {"lang": "rust"},
        "function": [{"sig": "fn f() -> i32"}],
    })
    native = tmp_path / "libdemo.so"
    native.write_bytes(b"not really a library")
    whl = package.wheel(m, native, tmp_path / "dist", tag="manylinux_2_17_x86_64")
    assert whl.name == "demo-1.2.3-py3-none-manylinux_2_17_x86_64.whl"
    import base64, csv, hashlib, io
    with zipfile.ZipFile(whl) as z:
        rows = list(csv.reader(io.StringIO(z.read("demo-1.2.3.dist-info/RECORD").decode())))
        for name, digest, size in rows:
            if not digest:
                continue
            data = z.read(name)
            want = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            assert digest == f"sha256={want}" and int(size) == len(data)
        assert "Root-Is-Purelib: false" in z.read("demo-1.2.3.dist-info/WHEEL").decode()
