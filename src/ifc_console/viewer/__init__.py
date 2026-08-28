"""Local web viewer: 3D inspection, click-to-select, highlights, screenshots.

The viewer is deliberately unprivileged: it can read the model and report
what the user selected, but it has no mutation surface at all. Everything
here runs on the server event loop; browsers connect over localhost only.

The server integration stays lightweight. The static Three.js/web-ifc bundle
and WebSocket runtime ship with IFC Console, but the viewer surface stays off
until enabled with ``/viewer``, ``--viewer``, or the MCP launcher tool.
"""
