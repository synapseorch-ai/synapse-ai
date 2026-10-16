"""The product's actions and reads, once, for every surface that is not a screen (MCP2, D292; ASSIST1, D291).

The hosted MCP server and the in-app assistant do the same things through the same code. Each entry
(`registry.ProductTool`) says what it is: a read or an act, the least role that may call it, the scope a
connection needs, whether it can be undone, and whether it stays in the app. `registry.invoke` is the one door:
it checks the surface, the scope and the role before any handler runs, asks for a confirmation token where an
act needs one (`confirm`), and tells the host's audit about every act it does (`hooks`). The adapters only
translate: MCP lists `for_caller(caller)`; the assistant registers the entries as builder tools.

The host binds the organisation before a handler runs (the MCP server's `bind`, the fleet's tenant scope), so no
handler takes an org id, and a handler never returns a secret's value.
"""
