"""Model providers: the one table every routing decision reads (`registry`)."""

from core.providers.windows import window_tokens  # noqa: E402,F401 — one window per model (F1251, D339)
