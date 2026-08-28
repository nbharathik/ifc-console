# IFC Console Agents

Optional agent and LLM capabilities for [IFC Console](https://github.com/nbharathik/ifc-console).
The distribution provides the typed agent SDK, provider-backed chat, built-in
and project-local agent packs, reusable skills, and the browser agent panel.

```bash
pip install ifc-console-agents
```

Installing the package registers the `agents` IFC Console extension. Start
`ifc-console` normally and use `/chat` to enable its panel. Deterministic SDK,
workflow, MCP, console, and 3D viewer features remain available from the base
`ifc-console` package without an LLM provider.

Agent primitives use the canonical `ifc_console_agents` namespace:

```python
from ifc_console_agents import Agent, AgentLimits
```

Provider keys are kept in process memory or the operating-system keyring; the
browser never receives stored secret material.

PDF ingestion and page images are optional (`ifc-console-agents[documents]`).
Durable graph orchestration is available through `ifc-console-agents[graph]`;
`ifc-console-agents[full]` installs both sets of capabilities.

The former `ifc_console.agents`, `ifc_console.chat`, and
`ifc_console.credentials` imports remain as deprecated one-release shims and
will be removed in the next minor release (0.2). New code should import from
`ifc_console_agents`.
