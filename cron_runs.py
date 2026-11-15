"""Which turns are cron runs, and whether a run's push should go out.

Hermes runs every cron job as an agent turn with ``task_id``
``cron:<job id>:<execution>`` (cron/scheduler.py ``_CronRunScope``) and, on
the classic path, session id ``cron_<job id>_<YYYYmmdd_HHMMSS>``. A gateway-
owned run keeps the task id but has a ``local-…`` session id, so the task id
comes first.

A run stays quiet when:
- the user turned the job's notifications off in Conduit (cron_store), or
- its output is Hermes' silence marker by the cron rule: ``[SILENT]`` (or
  SILENT / NO_REPLY) as the whole reply, on its own first or last line, or
  opening the reply (``[SILENT] No changes``). Hermes doesn't deliver such a
  run anywhere else either. Chats keep the exact rule (events).

A run's own turn also stays quiet when its job is delivered to a Conduit chat
(``deliver: bot-chat``, ``shown_in_chat``): the note shown there pushes, opening
that chat (cron_bot_chat.push_report), or with "Let the bot act on it" the bot's
reply there does. The run's own chat is hidden; its push opened it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from hermes_constants import get_hermes_home

from .cron_store import CronStore, valid_job_id
from .events import is_silent_response

logger = logging.getLogger(__name__)

_TASK = re.compile(r"^cron:([^:]+):")
_SESSION = re.compile(r"^cron_(.+)_\d{8}_\d{6}\Z")
_FALLBACK_MARKERS = ("[silent]", "silent", "no_reply", "no reply")


def job_id(session_id: Any = None, task_id: Any = None) -> str | None:
    """The cron job a turn runs, or None for any other turn."""
    for pattern, value in ((_TASK, task_id), (_SESSION, session_id)):
        match = pattern.match(value) if isinstance(value, str) else None
        if match and valid_job_id(match.group(1)):
            return match.group(1)
    return None


def is_silent(response: Any) -> bool:
    """Hermes' cron silence rule, from Hermes itself when it has one."""
    try:
        from gateway.response_filters import is_autonomous_silence_response
    except ImportError:  # an older Hermes: the same rule, kept here
        return _fallback_silent(response)
    try:
        return bool(is_autonomous_silence_response(response))
    except Exception:  # noqa: BLE001 — a Hermes change must not cost the push
        logger.warning("conduit_push: Hermes silence check failed; using the plugin's copy", exc_info=True)
        return _fallback_silent(response)


def _fallback_silent(response: Any) -> bool:
    if is_silent_response(response):
        return True
    stripped = response.strip() if isinstance(response, str) else ""
    if not stripped:
        return False
    if stripped.casefold().startswith("[silent]"):
        return True
    lines = [line for line in stripped.splitlines() if line.strip()]
    return any(" ".join(line.split()).casefold() in _FALLBACK_MARKERS for line in (lines[0], lines[-1]))


def _job(job: str) -> dict | None:
    """Hermes' record of a cron job, or None when it can't be read."""
    try:
        from cron.jobs import get_job
    except ImportError:
        return None
    try:
        found = get_job(job)
    except Exception:  # noqa: BLE001 - unreadable: the run notifies as before
        logger.debug("conduit_push: cron job %s unreadable", job, exc_info=True)
        return None
    return found if isinstance(found, dict) else None


def shown_in_chat(task_id: Any) -> bool:
    """Whether this is a run's own turn (``cron:<job>:<run>``) whose job is delivered to a Conduit chat, which then
    notifies for it. A follow-up asked in a run's chat, or a job Hermes can't describe, notifies as before."""
    match = _TASK.match(task_id) if isinstance(task_id, str) else None
    if not match or not valid_job_id(match.group(1)):
        return False
    record = _job(match.group(1))
    deliver = record.get("deliver") if record else None
    targets = deliver if isinstance(deliver, list) else str(deliver or "").split(",")
    return any(isinstance(target, str) and target.strip().split(":")[0] == "bot-chat" for target in targets)


def muted(job: str) -> bool:
    try:
        return CronStore(get_hermes_home()).is_muted(job)
    except Exception:  # noqa: BLE001 — unreadable: the push goes out
        logger.debug("conduit_push: cron notification setting unreadable", exc_info=True)
        return False


def quiet(session_id: Any, task_id: Any, response: Any = None) -> bool:
    """Whether this turn is a cron run whose push should not go out."""
    job = job_id(session_id, task_id)
    if job is None:
        return False
    return muted(job) or (response is not None and is_silent(response))
