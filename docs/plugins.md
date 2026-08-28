# Operation plugins

Plugins add trusted Python operations to the SDK, MCP server, and CLI through
one registration. They also appear in provider chat when the separate
`ifc-console-agents` extension is installed.

Use a plugin when an operation should appear everywhere. Use
`FunctionToolSource` for one agent application, or a [workflow](workflows.md)
for read-only validation and queries.

## Trust boundary

Plugins run inside the ifc-console process and can access anything that process
can. Install only reviewed packages.

Discovery reads metadata without importing code. Plugins are disabled by
default and load only when their exact entry-point name is in the user
allowlist. Project settings cannot enable them.

## Package contract

Declare one entry point:

```toml
[project.entry-points."ifc_console.plugins"]
company_checks = "company_ifc_checks:CompanyChecks"
```

Provide a versioned manifest and `register(api)`:

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
        description="Company submission checks.",
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
            return api.success({
                "model": session.name,
                "ready": not session.dirty,
            })
```

Rules:

- declare exact capabilities for every operation;
- prefix names to avoid collisions;
- keep structured output enabled;
- return `Envelope`, `api.success(...)`, or `api.failure(...)`;
- use a Pydantic `data_model` when callers need a checked output contract.

Registration is atomic. Duplicate names, invalid manifests, malformed output,
and uncaught exceptions fail safely. The host owns session metadata,
correlation IDs, output limits, and untrusted-text warnings.

## Install and enable

Install the plugin into the same environment as ifc-console, then allow it in
user settings:

```bash
ifc-console plugins list
ifc-console settings set plugins.enabled true
ifc-console settings set plugins.allow '["company_checks"]'
ifc-console plugins doctor
```

- `plugins list` reads metadata without importing plugin code.
- `plugins doctor` imports only allowed plugins and validates their contract.
- `--json` makes either command suitable for automation.

The repository includes a complete example at
`examples/plugins/company_checks`.

## Call an operation

```python
from ifc_console import Workbench

with Workbench.open("tower.ifc") as wb:
    result = wb.call("company_checks_status")
```

The operation also appears through MCP and, when `ifc-console-agents` is
installed, provider chat when its required capabilities are allowed. Use
`tools(permitted_only=True)` when an agent should see only currently permitted
operations.

Operation plugins and product extensions are separate contracts. Companion
products such as the agent package register routes, state, and declarative
browser panels through `ifc_console.extensions`; an operation plugin should not
try to recreate that lifecycle.

## Cleanup and compatibility

A plugin that owns resources may define synchronous cleanup:

```python
def shutdown(self, api: PluginAPI) -> None:
    self.client.close()
```

The host calls cleanup once in reverse load order. Cleanup failures are audited
and do not stop other services from closing.

`manifest.api_version="1"` defines the current public plugin contract. A future
incompatible contract will use a new version rather than silently changing
version 1. `OperationPlugin` is exported for static checking, and the package
includes PEP 561 type information.
