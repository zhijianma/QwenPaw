# -*- coding: utf-8 -*-
"""SQLite storage primitives for the normalized Chat data model."""

from .catalog import (
    CATALOG_SCHEMA_VERSION,
    initialize_catalog_database,
    validate_catalog_database,
)
from .catalog_repository import (
    CatalogEntry,
    CatalogEntryNotFoundError,
    CatalogRepository,
    CatalogStateConflictError,
)
from .common import ChatStorageSchemaError
from .backup import (
    CHAT_STORAGE_BACKUP_FORMAT,
    CHAT_STORAGE_MANIFEST,
    ChatStorageSnapshot,
    is_live_chat_sqlite_file,
    snapshot_workspace_chat_storage,
    sqlite_online_backup,
    validate_workspace_chat_storage,
)
from .conversation import (
    CONVERSATION_SCHEMA_VERSION,
    initialize_conversation_database,
    validate_conversation_database,
)
from .conversation_repository import (
    MaterializationResult,
    initialize_timeline,
    materialize_normalized_transcript,
    read_transcript,
)
from .coordinator import (
    ChatStorageCoordinator,
    ConversationCopyRequest,
    ConversationCreateRequest,
)
from .copy_repository import (
    ChatCopyError,
    ChatCopyResult,
    copy_conversation_at_turn,
    inspect_copy,
)
from .deletion import (
    DELETE_CHECKPOINTS,
    DELETE_DATABASE,
    DELETE_LEGACY,
    DELETE_PROJECTIONS,
    DELETE_RUNTIME_STOPPED,
    DELETE_TOMBSTONE,
    ChatDeletionTarget,
    begin_conversation_deletion,
    list_pending_deletions,
    purge_conversation_storage,
    record_deletion_step,
)
from .paths import ChatStoragePaths
from .materializer import (
    PARTIAL_FLUSH_BYTES,
    PARTIAL_FLUSH_INTERVAL_SECONDS,
    PartialBlockHandle,
    PartialBlockMaterializer,
    RuntimeRunHandle,
    RunFencingError,
    append_completed_message,
    append_partial_block,
    begin_partial_text_block,
    complete_partial_message,
    finalize_run,
    load_runtime_run_handle,
)
from .pool import ConversationConnectionPool
from .projection import (
    ChatProjectionError,
    ProjectionResult,
    enqueue_projection_repair,
    project_conversation,
    repair_pending_projections,
    rebuild_all_recall_projections,
)
from .recall import (
    RECALL_SCHEMA_VERSION,
    initialize_recall_database,
    validate_recall_database,
)
from .recall_search import (
    SEARCH_PAGE_LIMIT,
    ChatRecallHit,
    ChatRecallSearchPage,
    search_chat_recall,
)
from .sequence import RecallSequenceLease, reserve_recall_sequences
from .runtime_repository import (
    AcceptanceRequest,
    AcceptanceResult,
    RestartRecoveryResult,
    accept_input,
    cancel_accepted_input,
    claim_queued_input,
    interrupt_nonterminal_work,
)

__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "CHAT_STORAGE_BACKUP_FORMAT",
    "CHAT_STORAGE_MANIFEST",
    "AcceptanceRequest",
    "AcceptanceResult",
    "CatalogEntry",
    "CatalogEntryNotFoundError",
    "CatalogRepository",
    "CatalogStateConflictError",
    "CONVERSATION_SCHEMA_VERSION",
    "DELETE_CHECKPOINTS",
    "DELETE_DATABASE",
    "DELETE_LEGACY",
    "DELETE_PROJECTIONS",
    "DELETE_RUNTIME_STOPPED",
    "DELETE_TOMBSTONE",
    "RECALL_SCHEMA_VERSION",
    "SEARCH_PAGE_LIMIT",
    "ChatStoragePaths",
    "ChatStorageSnapshot",
    "ChatCopyError",
    "ChatCopyResult",
    "ChatDeletionTarget",
    "ChatStorageCoordinator",
    "ChatStorageSchemaError",
    "ChatProjectionError",
    "ChatRecallHit",
    "ChatRecallSearchPage",
    "ConversationConnectionPool",
    "ConversationCopyRequest",
    "ConversationCreateRequest",
    "MaterializationResult",
    "PARTIAL_FLUSH_BYTES",
    "PARTIAL_FLUSH_INTERVAL_SECONDS",
    "PartialBlockHandle",
    "PartialBlockMaterializer",
    "ProjectionResult",
    "RecallSequenceLease",
    "RestartRecoveryResult",
    "RuntimeRunHandle",
    "RunFencingError",
    "initialize_catalog_database",
    "initialize_conversation_database",
    "initialize_timeline",
    "initialize_recall_database",
    "interrupt_nonterminal_work",
    "is_live_chat_sqlite_file",
    "materialize_normalized_transcript",
    "read_transcript",
    "project_conversation",
    "rebuild_all_recall_projections",
    "reserve_recall_sequences",
    "search_chat_recall",
    "snapshot_workspace_chat_storage",
    "sqlite_online_backup",
    "accept_input",
    "cancel_accepted_input",
    "claim_queued_input",
    "append_completed_message",
    "append_partial_block",
    "begin_partial_text_block",
    "begin_conversation_deletion",
    "complete_partial_message",
    "copy_conversation_at_turn",
    "enqueue_projection_repair",
    "finalize_run",
    "load_runtime_run_handle",
    "inspect_copy",
    "list_pending_deletions",
    "purge_conversation_storage",
    "repair_pending_projections",
    "record_deletion_step",
    "validate_catalog_database",
    "validate_conversation_database",
    "validate_recall_database",
    "validate_workspace_chat_storage",
]
