# Panel unit tests

Pure-logic tests for the browser panel, run by Node's built-in test runner with
no npm dependencies and no browser:

    node --test tests/ui

`pytest` runs the same suite through `tests/unit/test_ui_modules.py`, which
skips when Node is unavailable. Anything that needs a live server belongs in
`ifc-console dev --check` instead.
