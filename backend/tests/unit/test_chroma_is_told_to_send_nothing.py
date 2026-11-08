"""Chroma's own usage telemetry is off, explicitly.

The locked chromadb (1.5.9) ships a no-op PostHog client, but pyproject allows
`chromadb>=0.5`, whose client sent anonymised usage events by default. The
product says nothing leaves the machine unless its operator sends it, so the
setting is passed rather than relied on.
"""


def test_the_memory_stores_chroma_client_has_telemetry_off():
    from core.memory import MemoryStore

    store = MemoryStore(model="none")
    assert store.client.get_settings().anonymized_telemetry is False
