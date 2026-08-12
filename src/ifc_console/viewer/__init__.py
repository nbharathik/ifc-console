"""Local web viewer: 3D inspection, click-to-select, highlights, screenshots.

The viewer is deliberately unprivileged: it can read the model and report
what the user selected, but it has no mutation surface at all. Everything
here runs on the server event loop; browsers connect over localhost only.

The server integration stays lightweight. The static three.js/web-ifc bundle
and WebSocket runtime are installed only through ``ifc-console[viewer]``. The
viewer stays off until enabled with ``/viewer`` or ``--viewer``.
"""
