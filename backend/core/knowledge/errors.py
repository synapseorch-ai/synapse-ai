# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""Knowledge's exceptions — each with a stable `code`.

The code is what a document's `status_reason`, an ingestion report's
`ErrorInfo.code`, a trace's degraded note and an API error carry. The message
is for people and may change; the code may not.

Pure Python: the cloud API imports this module (it has no numpy, no provider
clients), so nothing heavier than the standard library belongs here.
"""
from __future__ import annotations


class KnowledgeError(Exception):
    """Base class. `retryable` says whether trying the same work again can succeed."""

    code: str = "knowledge_error"
    retryable: bool = False

    def __init__(self, message: str = "", *, code: str | None = None, retryable: bool | None = None):
        super().__init__(message or self.__class__.__doc__ or self.code)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable


class KnowledgeDisabled(KnowledgeError):
    """Knowledge is switched off (`knowledge_enabled` is false)."""

    code = "knowledge_disabled"


class KnowledgeUnavailable(KnowledgeError):
    """A part Knowledge needs is not installed or not configured."""

    code = "knowledge_unavailable"


class EmbedderUnavailable(KnowledgeUnavailable):
    """No embedder is configured for this deployment."""

    code = "embedder_unavailable"


class EmbeddingError(KnowledgeError):
    """An embedding call failed. Raised, never answered with zero vectors."""

    code = "embed_failed"
    retryable = True


class EmbeddingInvalid(EmbeddingError):
    """An embedder answered, but with vectors that cannot be stored.

    Wrong count, mixed widths, a width other than the one asked for, a
    non-finite value, or a zero vector. Not retryable: the same call answers
    the same way.
    """

    code = "embedding_invalid"
    retryable = False


class RerankError(KnowledgeError):
    """A rerank call failed. The search degrades to the fused order and says so."""

    code = "rerank_failed"
    retryable = True


class OcrError(KnowledgeError):
    """An OCR call failed. The pages are reported, never silently skipped."""

    code = "ocr_failed"
    retryable = True


class KnowledgeIndexError(KnowledgeError):
    """The search index refused a write or a query."""

    code = "index_failed"
    retryable = True


class IndexDimensionError(KnowledgeIndexError):
    """Vectors of different widths in one snapshot, or a query of the wrong width."""

    code = "index_dimension_mismatch"
    retryable = False


class BudgetExceeded(KnowledgeError):
    """The knowledge base's monthly budget would be exceeded. Pauses; never retried in a loop."""

    code = "budget_cap"

    def __init__(self, kb_id: str, spent_usd: float, cap_usd: float, needed_usd: float = 0.0):
        super().__init__(
            f"Knowledge base {kb_id} has spent ${spent_usd:.4f} of its ${cap_usd:.2f} monthly budget"
            + (f"; this needs about ${needed_usd:.4f} more." if needed_usd else ".")
        )
        self.kb_id = kb_id
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd
        self.needed_usd = needed_usd


class WalletExhausted(KnowledgeError):
    """The model wallet refused the call (a 402). Pauses; never retried in a loop."""

    code = "wallet_402"

    def __init__(self, message: str = "", *, needed_microusd: int | None = None,
                 available_microusd: int | None = None):
        super().__init__(message or "The model wallet refused the call.")
        self.needed_microusd = needed_microusd
        self.available_microusd = available_microusd


class EnrichmentError(KnowledgeError):
    """The model writing Contextual Retrieval's contexts still failed after the provider's own retries
    (CCR-10). The document fails and may be retried; contexts already written are kept."""

    code = "enrich_failed"
    retryable = True


class EgressBlocked(KnowledgeError):
    """A fetch was refused by the egress guard or the egress proxy (a 407). Not retried."""

    code = "egress_blocked"

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class FilterError(KnowledgeError, ValueError):
    """A metadata filter that does not parse or validate. A route answers it with 422.

    `path` is a JSON pointer into the filter (``/$and/1/year/$gte``) and
    `filter_code` one of `filters.FILTER_ERROR_CODES`.
    """

    code = "invalid_filter"

    def __init__(self, filter_code: str, path: str, message: str):
        super().__init__(f"{message} (at {path or '/'})")
        self.filter_code = filter_code
        self.path = path
        self.detail = message


class KnowledgePathError(KnowledgeError, ValueError):
    """A knowledge base folder or document path that is not allowed."""

    code = "invalid_path"


class InvalidTransition(KnowledgeError):
    """A document status change the status machine does not allow."""

    code = "invalid_transition"

    def __init__(self, current: str, target: str):
        super().__init__(f"A document cannot go from {current!r} to {target!r}.")
        self.current = current
        self.target = target


class StaleRevision(KnowledgeError):
    """A knowledge base was saved from an older revision than the stored one (a 409)."""

    code = "stale_revision"
