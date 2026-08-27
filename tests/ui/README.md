# Panel unit tests

Pure-logic tests for the browser panel run through Node's built-in test runner
with no npm dependencies and no browser. From a source checkout, the
cross-platform shortcut enumerates the test files through the existing pytest
wrapper and also checks the panel's DOM contracts:

    npm test

The suite covers conversation history, markdown, execution flow, AI SDK
message compatibility, sidebar and inspector models, and Agent setup drafts.

`pytest` runs the same suite through `tests/unit/test_ui_modules.py`, which
skips when Node is unavailable. Anything that needs a live server belongs in
`npm run check` (or `ifc-console dev --check`) instead.
