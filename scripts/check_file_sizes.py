"""Keep source files from growing past a readable size.

No Python file above 1,500 lines and no JavaScript file above 2,500. Files that
were already over when the limit was set are listed in
`tests/budgets/file_lines.json` with their size then, and may only shrink. Run
with `--update` after splitting a file to lower its ceiling.

    uv run --no-sync python scripts/check_file_sizes.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tests" / "budgets" / "file_lines.json"
LIMITS = {".py": 1_500, ".js": 2_500}
SKIP_PARTS = {"vendor", "node_modules", ".venv", "__pycache__", "golden"}
SKIP_NAMES = {"uv.lock"}
ROOTS = ("src", "scripts", "tests")


def line_count(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def measure() -> dict[str, int]:
    """Every source file over its limit, as {posix path: lines}."""
    over: dict[str, int] = {}
    for top in ROOTS:
        for path in sorted((ROOT / top).rglob("*")):
            if path.suffix not in LIMITS or not path.is_file():
                continue
            if SKIP_PARTS & set(path.parts) or path.name in SKIP_NAMES:
                continue
            lines = line_count(path)
            if lines > LIMITS[path.suffix]:
                over[path.relative_to(ROOT).as_posix()] = lines
    return over


def load_baseline() -> dict[str, int]:
    if not BASELINE.is_file():
        return {}
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def problems(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    found = []
    for name, lines in sorted(current.items()):
        allowed = baseline.get(name)
        limit = LIMITS[Path(name).suffix]
        if allowed is None:
            found.append(f"{name} is {lines} lines, over the {limit} line limit")
        elif lines > allowed:
            found.append(f"{name} grew to {lines} lines; it was {allowed} and may only shrink")
    return found


def main(argv: list[str]) -> int:
    current = measure()
    if "--update" in argv:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {BASELINE.relative_to(ROOT)} ({len(current)} files over the limit)")
        return 0
    issues = problems(current, load_baseline())
    for issue in issues:
        print(issue, file=sys.stderr)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
