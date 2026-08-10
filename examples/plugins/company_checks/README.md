# Company checks example plugin

From the repository root, install this package into the same environment as
ifc-console:

```bash
uv pip install -e examples/plugins/company_checks
ifc-console settings set plugins.enabled true
ifc-console settings set plugins.allow '["company_checks"]'
ifc-console plugins doctor
```

Open a model and call `company_checks_status` from MCP, chat, or the Python SDK:

```python
result = wb.call("company_checks_status")
```

The example declares an output data model, exact capabilities, a versioned
manifest, and a namespaced operation name.
