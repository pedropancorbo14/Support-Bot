"""Métricas agregadas de una app en un intervalo de tiempo."""

from datetime import datetime
from typing import Any

from sqlalchemy import and_, any_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Conversation,
    ConversationKind,
    DeliveryStatus,
    Message,
    MessageFlag,
    MessageRole,
    WebhookDelivery,
)


def _count_if(condition: Any) -> Any:
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def _has_flag(flag: MessageFlag) -> Any:
    return flag.value == any_(Message.flags)


async def compute_metrics(
    session: AsyncSession, app_id: int, since: datetime, until: datetime
) -> dict[str, Any]:
    in_range_conversation = and_(
        Conversation.app_id == app_id,
        Conversation.created_at >= since,
        Conversation.created_at < until,
    )
    conversations = (
        await session.execute(
            select(
                _count_if(Conversation.kind == ConversationKind.BOT).label("bot"),
                _count_if(Conversation.kind == ConversationKind.HUMAN).label("human"),
                _count_if(Conversation.escalated_by == "model").label("by_model"),
                _count_if(Conversation.escalated_by == "user").label("by_user"),
            ).where(in_range_conversation)
        )
    ).one()

    answers = (
        await session.execute(
            select(
                func.count().label("total"),
                func.avg(Message.latency_ms).label("latency_avg"),
                func.percentile_cont(0.5).within_group(Message.latency_ms).label("latency_p50"),
                func.percentile_cont(0.95).within_group(Message.latency_ms).label("latency_p95"),
                func.avg(Message.ttft_ms).label("ttft_avg"),
                func.percentile_cont(0.95).within_group(Message.ttft_ms).label("ttft_p95"),
                func.coalesce(func.sum(Message.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(Message.completion_tokens), 0).label("completion_tokens"),
                _count_if(_has_flag(MessageFlag.NO_CONTEXT)).label("no_context"),
                _count_if(_has_flag(MessageFlag.LLM_ERROR)).label("llm_error"),
                _count_if(_has_flag(MessageFlag.ESCALATED)).label("escalated"),
                _count_if(Message.rating == 1).label("rating_up"),
                _count_if(Message.rating == -1).label("rating_down"),
            )
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.app_id == app_id,
                Message.role == MessageRole.ASSISTANT,
                Message.created_at >= since,
                Message.created_at < until,
            )
        )
    ).one()

    messages_by_role = dict(
        (
            await session.execute(
                select(Message.role, func.count())
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Conversation.app_id == app_id,
                    Message.created_at >= since,
                    Message.created_at < until,
                )
                .group_by(Message.role)
            )
        ).all()
    )

    webhooks = dict(
        (
            await session.execute(
                select(WebhookDelivery.status, func.count())
                .where(
                    WebhookDelivery.app_id == app_id,
                    WebhookDelivery.created_at >= since,
                    WebhookDelivery.created_at < until,
                )
                .group_by(WebhookDelivery.status)
            )
        ).all()
    )

    total = answers.total
    rated = answers.rating_up + answers.rating_down
    return {
        "since": since,
        "until": until,
        "conversations": {
            "bot": conversations.bot,
            "human": conversations.human,
            "escalated_by_model": conversations.by_model,
            "escalated_by_user": conversations.by_user,
            "escalation_rate": _ratio(
                conversations.by_model + conversations.by_user, conversations.bot
            ),
        },
        "messages": {role: messages_by_role.get(role, 0) for role in MessageRole},
        "answers": {
            "total": total,
            "latency_ms": {
                "avg": _round(answers.latency_avg),
                "p50": _round(answers.latency_p50),
                "p95": _round(answers.latency_p95),
            },
            "ttft_ms": {"avg": _round(answers.ttft_avg), "p95": _round(answers.ttft_p95)},
            "tokens": {"prompt": answers.prompt_tokens, "completion": answers.completion_tokens},
            "no_context": answers.no_context,
            "no_context_rate": _ratio(answers.no_context, total),
            "llm_errors": answers.llm_error,
            "llm_error_rate": _ratio(answers.llm_error, total),
            "escalated": answers.escalated,
        },
        "ratings": {
            "up": answers.rating_up,
            "down": answers.rating_down,
            "satisfaction": _ratio(answers.rating_up, rated),
            "rated_rate": _ratio(rated, total),
        },
        "webhooks": {status: webhooks.get(status, 0) for status in DeliveryStatus},
    }


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _round(value: Any) -> int | None:
    return None if value is None else int(round(float(value)))
