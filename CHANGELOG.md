# Changelog

## Unreleased

- Add a framework-neutral agent SDK with exact tool selection, live session
  settings, optional LangChain/LangGraph projection, embedded viewer/MCP web
  surfaces, and runtime agent construction.
- Add human-friendly element search by name or GlobalId and a focused property
  agent example with host-owned ChangeSet approval and durable commit.
- Keep agent threads provider-replayable when a run exhausts its tool-call
  budget, and always pair tool_call_started with tool_call_finished events.
- Add a minimal terminal quickstart agent example.

## [0.1.4] - 2026-08-12

- Make the Three.js/web-ifc viewer and browser chat bundle an optional
  `ifc-console[viewer]` installation, with a viewer-free core wheel.
- Reorganize and simplify the documentation, with a shorter onboarding path,
  clearer safety guidance, grouped settings, and task-based navigation.
- Fail closed on Python 3.10 and 3.11 when complete generated-code isolation is
  requested, because those runtimes cannot audit raw thread creation; `auto`
  reports its guarded fallback and `strict` refuses it.

The changelog can be found on the
[GitHub Releases page](https://github.com/nbharathik/ifc-console/releases).
