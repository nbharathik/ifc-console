"""Local web viewer: 3D inspection, click-to-select, highlights, screenshots.

The viewer is deliberately unprivileged: it can read the model and report
what the user selected, but it has no mutation surface at all. Everything
here runs on the server event loop; browsers connect over localhost only.

The viewer ships with every install: static assets (three.js, web-ifc) are
bundled in the package and the WebSocket dependency is a core requirement.
It stays off until enabled with /viewer or --viewer.
"""
