"""No source file grows past a readable size, and the ones already over may only shrink."""

from __future__ import annotations

from scripts import check_file_sizes as sizes


def test_no_file_is_over_the_limit_or_larger_than_it_was() -> None:
    issues = sizes.problems(sizes.measure(), sizes.load_baseline())

    assert not issues, "\n".join(issues)


def test_the_baseline_only_lists_files_that_exist_and_are_still_over() -> None:
    current = sizes.measure()

    stale = [name for name in sizes.load_baseline() if name not in current]

    assert not stale, f"no longer over the limit; run scripts/check_file_sizes.py --update: {stale}"


def test_a_new_oversize_file_is_reported() -> None:
    found = sizes.problems({"src/ifc_console/new.py": 1_600}, {})

    assert found and "over the 1500 line limit" in found[0]


def test_growth_of_an_oversize_file_is_reported() -> None:
    found = sizes.problems({"src/big.py": 2_010}, {"src/big.py": 2_000})

    assert found and "may only shrink" in found[0]
