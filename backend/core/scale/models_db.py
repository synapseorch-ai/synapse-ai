"""
Compatibility shim. The models moved to ``core/store/models.py``.

They are no longer "the scale layer's" tables — they are the engine's only
durable state, in every mode. This module re-exports them so existing importers
(including out-of-tree ones such as an Alembic ``env.py`` pointed at
``core.scale.models_db.Base``) keep working.

Import from ``core.store`` in new code.
"""
from core.store.models import (  # noqa: F401
    DEFAULT_TENANT,
    AgentDB,
    Base,
    ChatSessionDB,
    DeadLetterQueueDB,
    JSONType,
    MCPServerDB,
    OrchestrationDB,
    OrchestrationRunDB,
    SettingDB,
    TenantDB,
    ToolDB,
    WorkerDB,
)

__all__ = [
    "AgentDB",
    "Base",
    "ChatSessionDB",
    "DEFAULT_TENANT",
    "DeadLetterQueueDB",
    "JSONType",
    "MCPServerDB",
    "OrchestrationDB",
    "OrchestrationRunDB",
    "SettingDB",
    "TenantDB",
    "ToolDB",
    "WorkerDB",
]
