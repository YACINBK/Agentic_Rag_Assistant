"""M11 — AuditLog and EscalationEvent persistence.

Nothing wrote either table before this module (verified 2026-08-26: no AuditLog
construction anywhere in app/). Every completed query gets one append-only
AuditLog row; a faithfulness failure or a relevance-gate exhaustion additionally
writes the EscalationEvent the schema has carried since M1.

§12 discipline: AuditLog is append-only — these helpers only INSERT, never
UPDATE or DELETE. `was_escalated` is decided BEFORE the insert and written once,
so the row is never revisited after the escalation lands.

Failure is soft and loud: the answer has already been streamed, so an audit
failure logs and returns rather than breaking the response — the compliance
record is the casualty, which is exactly what the warning line is for.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.models.audit_log import AuditLog
from app.core.models.escalation_event import EscalationEvent

logger = get_logger(__name__)


async def record_query_outcome(
    session: AsyncSession,
    *,
    user_id: str | uuid.UUID,
    query_text: str,
    answer_text: str,
    confidence_score: float | None = None,
    was_escalated: bool = False,
    cache_hit: bool = False,
    chunks_used: list[str] | None = None,
    namespace_queried: str | None = None,
    response_time_ms: int | None = None,
) -> AuditLog | None:
    try:
        row = AuditLog(
            user_id=user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id)),
            query_text=query_text,
            answer_text=answer_text,
            confidence_score=confidence_score,
            was_escalated=was_escalated,
            cache_hit=cache_hit,
            chunks_used=chunks_used or [],
            namespace_queried=namespace_queried,
            response_time_ms=response_time_ms,
        )
        session.add(row)
        await session.flush()  # row.id available for the escalation FK
        await session.commit()
        return row
    except Exception as exc:  # noqa: BLE001 — answer already served
        await session.rollback()
        logger.warning("audit_write_failed", error=str(exc))
        return None


async def record_escalation(
    session: AsyncSession,
    *,
    audit_log_id: uuid.UUID,
    reason: str,
) -> EscalationEvent | None:
    try:
        event = EscalationEvent(
            audit_log_id=audit_log_id,
            reason=reason,
            notified_at=datetime.now(timezone.utc),
        )
        session.add(event)
        await session.commit()
        return event
    except Exception as exc:  # noqa: BLE001 — answer already served
        await session.rollback()
        logger.warning("escalation_write_failed", error=str(exc))
        return None
