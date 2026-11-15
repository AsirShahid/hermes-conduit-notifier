"""Topic chats for cron reports: a job the user filed under a topic shows its report there.

A profile has one Bot Chat, so every job delivered to it (``deliver: bot-chat``) lands in the same
chat. The user can file jobs under topics instead (Briefings, Sports, ...): each topic is an ordinary
chat of this profile, and ``cron_bot_chat.show`` writes a filed job's report into it, and pushes it
from there, exactly as it would into the Bot Chat.

State lives in the profile's ``conduit-cron-topics.json`` beside ``conduit-cron.json``:
``{"jobs": {"<job id>": "<topic>"}, "chats": {"<topic>": "<session id>"}}``. It is read on every
report and never written here, so a report never creates a chat. Anything that doesn't resolve to an
existing, unarchived chat (no file, a job not filed, a topic without a chat, a chat deleted or
archived since, a file that doesn't parse) leaves the report in the Bot Chat, as before.

Only this profile's own Bot Chat deliveries are rerouted, and never a job set to "Let the bot act on
it": that one needs a real turn in the Bot Chat (``cron_bot_chat._replacement`` hands it to Hermes
before this is consulted).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

FILE_NAME = "conduit-cron-topics.json"


def _load(home: Path) -> dict[str, Any]:
    path = Path(home) / FILE_NAME
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("conduit_push: %s is unreadable; cron reports stay in the Bot Chat", path)
        return {}
    return raw if isinstance(raw, dict) else {}


def topic_of(home: Path, job_id: Any) -> Optional[str]:
    """The topic the user filed this job under, or None."""
    jobs = _load(home).get("jobs")
    topic = jobs.get(str(job_id)) if isinstance(jobs, dict) and job_id else None
    return topic.strip() if isinstance(topic, str) and topic.strip() else None


def topic_chat(home: Path, job_id: Any) -> Optional[str]:
    """The chat a filed job's report goes to, or None for the Bot Chat."""
    state = _load(home)
    jobs, chats = state.get("jobs"), state.get("chats")
    if not (isinstance(jobs, dict) and isinstance(chats, dict) and job_id):
        return None
    topic = jobs.get(str(job_id))
    chat = chats.get(topic) if isinstance(topic, str) else None
    if not isinstance(chat, str) or not chat.strip():
        return None
    chat = chat.strip()
    from hermes_state import SessionDB

    db = None
    try:
        db = SessionDB(db_path=Path(home) / "state.db", read_only=True)
        row = db.get_session(chat)
    except Exception:  # noqa: BLE001 - an unreadable store keeps the Bot Chat
        logger.warning("conduit_push: couldn't check topic chat %s; the report stays in the Bot Chat", chat,
                       exc_info=True)
        return None
    finally:
        if db is not None:
            db.close()
    if not row or row.get("archived"):
        logger.info("conduit_push: topic %r's chat %s is gone or archived; the report stays in the Bot Chat",
                    topic, chat)
        return None
    return chat
