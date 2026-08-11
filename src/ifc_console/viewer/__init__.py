"""Local web viewer: 3D inspection, click-to-select, highlights, screenshots.

The viewer is deliberately unprivileged: it can read the model and report
what the user selected, but it has no mutation surface at all. Everything
here runs on the server event loop; browsers connect over localhost only.

The routes, WebSocket support, static three.js and web-ifc assets, and SPA all
ship with ifc-console. The viewer stays off until enabled with /viewer or
--viewer.
"""
