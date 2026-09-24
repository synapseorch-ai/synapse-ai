"""The CLI's API client: `synapse agents|orchestrations|run|runs|schedules|chat` (D126).

Everything here talks to `/api/v1` over HTTP with an API key — the same client
drives this machine's install, a remote one, and cloud (D124: one origin, the
same paths). Nothing here reaches the engine except to mint this machine's own
key (`target.py`), which is what "installed is enough" means.
"""
