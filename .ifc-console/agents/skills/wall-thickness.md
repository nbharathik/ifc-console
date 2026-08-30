---
name: wall-thickness
description: Recorded viewer measurement pattern on IfcRoof
applies_to: IfcRoof
---

## When to use
The user asks to repeat this measurement pattern on similar elements, or names this skill. Recorded in the 3D viewer from 1 measurement(s) on 'model_126.ifc' at 2026-08-30T12:27:23.276237+00:00.

## Recorded example
Elements measured:
- IfcRoof 'Basic Roof:Ziegel - Eindeckung:900072' (1JV4ecXwrFVvB0vISgLezI), type 'Basic Roof:Ziegel - Eindeckung'

| # | kind | value | what it means |
| - | ---- | ----- | ------------- |
| 1 | distance | 5.5519 m | corner to corner snap; on IfcRoof 'Basic Roof:Ziegel - Eindeckung:900072' (1JV4ecXwrFVvB0vISgLezI) |

All values are metres, model axes (z up), as reported by get_viewer_measurements.

## Steps
1. Resolve the targets: the user's viewer selection, or query_elements with a selector (same class: `IfcRoof`; narrow with `, type="..."` or a property filter when the user says so).
2. For each target run analyze_element_geometry and read the dimensions matching the recorded intents (the values above). Keep each value's source.
3. The intent survives a shape change: when a dimension key is missing, fall back to measure_directional_extent along the same model axis, or slice_element_mesh at mid element and read the section. Say which fallback was used.
4. Answer with one markdown table: GlobalId, name, one column per intent, plus source and flags. Do not silently skip an element.

## Verify
Cross-check one element against a second method (profile_parameter vs mesh_section, or the recorded example itself). Report values that disagree beyond tolerance as deviations; never average them away.

## Propose (optional)
Only after the user confirms the table: store values with measure__propose_measured_value (unit metres, method 'recorded skill'), which writes to the IfcConsole_AI_ psets with provenance.
