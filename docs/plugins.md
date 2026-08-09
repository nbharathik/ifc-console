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
from ifc_console.core import Capability


class CompanyChecks:
    manifest = {
        "api_version": "1",
        "name": "company_checks",
        "version": "1.0.0",
        "description": "Company model submission checks.",
    }

    def register(self, api):
        @api.registry.tool(
            name="company_model_status",
            description="Return the company submission status.",
            required_capabilities=[Capability.MODEL_READ],
        )
        async def company_model_status():
            session = api.core.session
            session.require_loaded()
            return {
                "ok": True,
                "data": {"model": session.name, "ready": not session.dirty},
                "meta": api.core.session_meta(),
            }
```

Declare exact `required_capabilities` for every operation. Registration is
atomic: if setup fails, operations added during that attempt are removed.
Duplicate operation names are rejected.

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

## Compatibility

The plugin API is independently versioned by `manifest.api_version`. Version
`1` covers `PluginManifest`, `PluginAPI.registry`, `PluginAPI.core`, operation
registration, and capability declarations. A future incompatible host API will
use a new value instead of silently loading an incompatible plugin.
