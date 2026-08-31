"""Conversation repository facade.

The existing memory module remains the implementation while the API is migrated
incrementally to service/repository layering.
"""

from backend.agent.memory import (
    create_session,
    delete_session,
    get_all_sessions,
    get_owned_conversation_id,
    load_history,
    rename_session,
    save_message,
)

__all__ = [
    "create_session",
    "delete_session",
    "get_all_sessions",
    "get_owned_conversation_id",
    "load_history",
    "rename_session",
    "save_message",
]
