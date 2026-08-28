"""Verify the exact viewer assets reviewed for the release."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

VENDOR = (
    Path(__file__).parent.parent
    / "src"
    / "ifc_console"
    / "viewer"
    / "static"
    / "vendor"
)

EXPECTED_SHA256 = {
    "LICENSE.three.txt": "bfe119ea4fd413f5f7ca3fcd63adb0c4a073ed39daa2fe7d3e6b769e21272601",
    "LICENSE.web-ifc.md": "1f256ecad192880510e84ad60474eab7589218784b9a50bc7ceee34c2b91f1d5",
    "OrbitControls.js": "6c860c6b342200f8aef65493319c12bfb2d652107355b1d25eb2154371128391",
    "VENDORED.md": "9e9e991344c7c74cc78a07ec382a80acd386f21b9fbd5004094ce62a2cdd0bed",
    "three.core.min.js": "61ba0df005b05991361d040d8ff670e1aadfd0ce7aeebd1fdb0725957a8957de",
    "three.module.min.js": "e2b5ee6bccd38fd6d8a2428546b83c5f2426d84b152ef82be8055556e3b40eb6",
    "web-ifc-api.js": "1f07c3cfcad8309ac92556aa8e6811f7f6c331e9cbd1dfe5037dad5a36c0fe03",
    "web-ifc.wasm": "f466be51ae179fdcbe55e36116bd1dc233f73e64b5a446af761f711c2ded4c3d",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    failures: list[str] = []
    for name, expected in EXPECTED_SHA256.items():
        path = VENDOR / name
        if not path.is_file():
            failures.append(f"missing {name}")
            continue
        actual = _sha256(path)
        if actual != expected:
            failures.append(f"{name}: expected {expected}, got {actual}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(f"ok: verified {len(EXPECTED_SHA256)} vendored viewer assets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
