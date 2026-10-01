"""Shared Litterbox result-ingest transport (canonical 204 receipt gate).

Single owner for the source-auth ingest POST used by both the kanban
result-card notifier and the assistant-reminder cron delivery path. A
canonical HTTP 204 is the receipt; anything else (including missing
endpoint/token, non-204 status, or transport error) returns False so the
caller retains ordinary Telegram delivery and never loses the output.

Reminder semantics live here too so the two lanes cannot drift:
- classification originates in the producer's stored ``output_kind``
  (``assistant_reminder`` on the cron job), never by regex over content;
- a reminder becomes a timed card (``timed=True``) with a stable
  ``external_id`` per reminder run, so replays upsert idempotently;
- only ordinary reminders suppress Telegram, and only after an actual
  204; failed ingest preserves delivery; urgent/approval retain Telegram;
- unrelated scheduled jobs (any other ``output_kind``) are untouched.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional
from urllib.request import HTTPRedirectHandler, Request, build_opener

logger = logging.getLogger("gateway.run")

ASSISTANT_REMINDER_KIND = "assistant_reminder"
REMINDER_INGEST_KIND = "reminder"

_RESULT_CARD_MAX_FILES = 10
_RESULT_CARD_MAX_BYTES = 8 * 1024 * 1024


class _NoIngestRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def ingest_result_card(card: dict) -> bool:
    """POST one card to the configured source-auth ingest endpoint.

    Returns True only on HTTP 204 (persisted receipt). Returns False on
    missing config, any non-204 status, or any transport error (caller
    must retain Telegram delivery in that case).
    """
    from hermes_cli.config import load_config

    try:
        settings = ((load_config() or {}).get("kanban") or {}).get("result_cards") or {}
        endpoint = settings.get("ingest_url")
        token = os.environ.get("LITTERBOX_SOURCE_TOKEN")
        if not endpoint or not token:
            return False
        request = Request(
            endpoint,
            data=json.dumps(card, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with build_opener(_NoIngestRedirect()).open(request, timeout=10) as response:
            return response.status == 204
    except Exception:
        logger.warning("result card ingest failed; retaining Telegram delivery")
        return False


def encode_result_card_files(paths) -> Optional[list]:
    """Read paths into ingest ``files`` entries; None when unsafe/oversize."""
    import base64 as _b64
    import mimetypes as _mime

    paths = list(paths or [])
    if len(paths) > _RESULT_CARD_MAX_FILES:
        return None
    total = 0
    entries = []
    for path in paths:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        total += len(data)
        if total > _RESULT_CARD_MAX_BYTES:
            return None
        name = Path(path).name
        media_type = _mime.guess_type(name)[0] or "application/octet-stream"
        entries.append(
            {
                "name": name,
                "media_type": media_type,
                "data": _b64.b64encode(data).decode("ascii"),
            }
        )
    return entries


def is_assistant_reminder_job(job: dict) -> bool:
    """True only for explicitly classified assistant-reminder cron jobs."""
    return isinstance(job, dict) and job.get("output_kind") == ASSISTANT_REMINDER_KIND


def is_urgent_or_approval_job(job: dict) -> bool:
    """Urgent/approval reminders retain Telegram (never suppressed)."""
    return bool(job.get("urgent") or job.get("approval_required"))


def build_assistant_reminder_card(job: dict, content: str) -> Optional[dict]:
    """Build the timed ingest card for an assistant-reminder run.

    Returns None when the content is empty/noise (caller must retain
    Telegram semantics for that case). Classification comes from the job's
    stored ``output_kind``; the content is never regex-classified here.
    """
    text = (content or "").strip()
    if not text:
        return None
    title = str(job.get("name") or "Reminder").strip() or "Reminder"
    if text.casefold() == title.casefold():
        return None
    job_id = str(job.get("id") or "unknown")
    claim = job.get("fire_claim") or {}
    at = claim.get("scheduled_at")
    if not isinstance(at, str) or not at.strip():
        return None  # Only a claimed occurrence may become a reminder card.
    at = at.strip()
    return {
        "external_id": f"cron:{job_id}:reminder:{at}",
        "kind": REMINDER_INGEST_KIND,
        "title": title,
        "summary": text,
        "at": at,
        "timed": True,
    }


def try_ingest_assistant_reminder(job: dict, content: str) -> bool:
    """Attempt the shared ingest for one assistant-reminder run.

    Returns True only after an actual HTTP 204 receipt (caller may suppress
    the matching ordinary Telegram text/files). Returns False when the job
    is not an assistant reminder, is urgent/approval, has empty/title-duplicate
    content, carries uninstallable media, or ingest does not return 204
    (caller must retain Telegram delivery in all False cases).
    """
    if not is_assistant_reminder_job(job):
        return False
    if is_urgent_or_approval_job(job):
        return False
    text = (content or "").strip()
    if not text:
        return False
    card = build_assistant_reminder_card(job, text)
    if card is None:
        return False
    try:
        from gateway.platforms.base import BasePlatformAdapter

        media_files, _cleaned = BasePlatformAdapter.extract_media(text)
        if media_files:
            paths = [str(p) for p, _v in media_files]
            files = encode_result_card_files(paths)
            if files is None:
                return False
            if files:
                card["files"] = files
    except Exception:
        return False
    return ingest_result_card(card)
