# Operation plugins

Plugins add typed operations to the same registry used by the Python SDK, CLI,
MCP server, and chat panel. A plugin therefore integrates once and receives the
same input validation, capability policy, result envelope, correlation IDs, and
audit behavior as a built-in operation.

## Security boundary

Python plugins are trusted code. They run in the ifc-console process and can do
anything allowed to that process. Plugin discovery never imports plugin code,
plugins are disabled by default, and enabling discovery still loads only exact
names in the user-owned allowlist. Project settings cannot enable or allow a
plugin.

Use plugins only from packages you have reviewed. Untrusted rules belong in a
declarative workflow or a future isolated plugin runner, not in this API.

## Package contract

Publish one entry point in the `ifc_console.plugins` group:

```toml
[project.entry-points."ifc_console.plugins"]
company_checks = "company_ifc_checks:CompanyChecks"
```

The exported class or object provides a manifest and `register(api)`:

```python
from pydantic import BaseModel, ConfigDict

from ifc_console import Capability, Envelope, PluginAPI, PluginManifest


class CompanyStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    ready: bool


class CompanyChecks:
    manifest = PluginManifest(
        api_version="1",
        name="company_checks",
        version="1.0.0",
        description="Company model submission checks.",
    )

    def register(self, api: PluginAPI) -> None:
        @api.registry.tool(
            name="company_checks_status",
            description="Return the company submission status.",
            data_model=CompanyStatus,
            required_capabilities=[Capability.MODEL_READ],
        )
        async def company_checks_status() -> Envelope:
            session = api.core.session
            session.require_loaded()
            return api.success({"model": session.name, "ready": not session.dirty})
```

Declare exact `required_capabilities` for every operation. Registration is
atomic: if setup fails, operations added during that attempt are removed.
Existing operations are also restored if a plugin accidentally removes or
replaces one. Duplicate operation names are rejected. Prefix operation names
with the plugin name to avoid collisions with other packages. Names must be 1
to 128 characters and may contain only ASCII letters, digits, dots,
underscores, and hyphens so every loaded operation is MCP-compatible.

API version 1 plugin operations must keep `structured_output=True`, which is the
registry default. Raw transport content is reserved for host-owned adapters so
plugin results cannot bypass the envelope, output, and untrusted-text checks.

`api.success(...)` and `api.failure(...)` build typed envelopes. A plugin may
instead return an `Envelope` or an equivalent mapping. The host normalizes the
result once, adds current session metadata, injects the correlation ID, and
converts malformed output or an uncaught exception to an `INTERNAL_ERROR`
envelope. It also converts values to JSON-safe forms, applies the configured
output limit, and marks instruction-shaped content with the same
`meta.injection_warning` used by built-in operations, including failed plugin
envelopes. Session, request, correlation, truncation, and injection metadata is
host-owned; plugin values cannot spoof those fields. When `data_model` is set,
raw successful data is validated before normalization or truncation and before
it reaches SDK, MCP, or chat clients.
Unexpected exception text is kept out of the client response so a local secret
cannot leak through a plugin failure. `plugins doctor` exposes host-generated
contract diagnostics, but reports third-party setup exceptions by type only.
Shutdown failures are also recorded by exception type without the exception
text.

## Install and enable

Install the package in the same environment as ifc-console, then opt in from
your user settings:

```bash
ifc-console plugins list
ifc-console settings set plugins.enabled true
ifc-console settings set plugins.allow '["company_checks"]'
ifc-console plugins doctor
```

`plugins list` reads entry-point metadata without importing plugin code.
`plugins doctor` imports only allowed plugins, validates their API v1 manifest,
registers their operations, and returns a nonzero status for missing or broken
plugins. Duplicate installed entry-point names fail closed before either package
is imported. Add `--json` to either command for automation.

The repository contains a complete installable package under
`examples/plugins/company_checks`. It can be copied as the starting point for a
private integration.

## Calling a plugin operation

Once loaded, an operation is available on every registry client:

```python
from ifc_console import Workbench

with Workbench.open("tower.ifc") as wb:
    result = wb.call("company_checks_status")
    assert result["ok"]
```

`wb.operation_definitions()` returns typed contracts. `wb.tools()` returns the
provider-neutral dictionary form. Use `wb.tools(permitted_only=True)` when an
embedding should advertise only operations allowed by the current profile.

## Lifecycle

Registration and cleanup hooks are synchronous in API version 1. A plugin that
owns a thread, file handle, or client may provide an optional cleanup hook:

```python
def shutdown(self, api: PluginAPI) -> None:
    self.client.close()
```

The host calls `shutdown(api)` once in reverse load order while core services
are still available. It also attempts cleanup after a failed registration.
Cleanup failures are contained and written to the audit as
`plugin_shutdown_failed`; they do not prevent the remaining application from
closing. Coroutine registration and shutdown hooks are rejected explicitly.

## Compatibility

The plugin API is independently versioned by `manifest.api_version`. Version
`1` covers `PluginManifest`, `PluginAPI.registry`, `PluginAPI.core`, operation
registration, result helpers, capability declarations, and the optional
synchronous shutdown hook. The public package also exports the
`OperationPlugin` protocol for static checking. IFC-Console includes a PEP 561
`py.typed` marker, so plugin projects can type-check these imports directly. A
future incompatible host API will use a new value instead of silently loading
an incompatible plugin.
