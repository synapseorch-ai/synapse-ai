"""The browser inside Synapse (BRW1, D328).

`host` is the component: its own process, a Chromium per session, Playwright MCP
behind a gate, the network guard, the screencast and the action log. `client` is
the engine's side of it. `gate` holds the rules both share. Design: pro
`MEMORY/drafts/brw1/DESIGN.md`.
"""
