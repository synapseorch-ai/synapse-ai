"""Knowledge — agents that answer from your documents, with sources (D153).

A knowledge base is one Vault folder made searchable. Every source — uploads,
public web pages, records sent by the API or a step — becomes a Vault file, and
one ingestion path parses, chunks, embeds and indexes it. Agents search with
`search_knowledge`, steps with Retrieve, programs through the API and MCP;
every search leaves a replayable trace and every call a cost row.

The design and the build plan are in the cloud repo's `MEMORY/16-knowledge.md`;
the decisions in its D153. This package imports nothing on purpose — import the
module you need. The contract (frozen by KB0, changed only by a request in
16 §8):

| Module | What it fixes |
|---|---|
| `errors` | exceptions, each with a stable `code` |
| `ids` | id formats; the deterministic chunk id |
| `types` | `Usage`, `Attribution`, the seams' results, the index's queries and results |
| `seams` | the five pluggable parts and their defaults |
| `parsed` | `ParsedDocument`: what every parser returns; the parse cache |
| `chunks` | `Chunk` |
| `status` | the document status machine; `IngestionReport` |
| `filters`, `filters_sql` | the metadata filter language, in Python and in SQL |
| `textsearch` | keyword-leg languages; the Postgres tsvector expression |
| `vectors` | how an embedding is stored as bytes |
| `trace` | the retrieval trace (v1) and replay |
| `events` | run/chat events, lifecycle events, webhook names |
| `ledger` | cost rows, the `usage_logs` mirror, budgets |
| `pipeline_contract` | the one call from ingestion into indexing |
| `kb_settings` | the knowledge base record and its folder rules |
| `config` | the settings, and the `knowledge_enabled()` gate |
| `api_models` | every API body, and the route table (the eval's too: CCR-6) |
| `agent_contract` | the agent tools' names and schemas; `[K#]` labels |
| `telemetry` | span names |
| `testing` | reference fakes and the index conformance suite |

The engine tables are in `core/store/models.py` (`Knowledge*DB`); the Vault
change hook is `core/vault_events.py`.
"""
