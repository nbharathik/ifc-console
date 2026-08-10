"""Local web viewer: 3D inspection, click-to-select, highlights, screenshots.

The viewer is deliberately unprivileged: it can read the model and report
what the user selected, but it has no mutation surface at all. Everything
here runs on the server event loop; browsers connect over localhost only.

Viewer routes and WebSocket support ship with the core. The static three.js,
web-ifc, and SPA bundle is the optional `ifc-console[viewer]` extra, so a base
install stays small. When installed, it stays off until enabled with /viewer
or --viewer.
"""
