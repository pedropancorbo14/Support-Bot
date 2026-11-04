"""Montaje del prompt.

Orden de los mensajes:

1. `system`: system prompt fijo (primero, para aprovechar la caché de prefijo), seguido de
   los datos del usuario, la documentación recuperada y el resumen del historial antiguo.
2. Historial reciente literal (`user` / `assistant`).
3. Pregunta actual (`user`).

Todo va en un único mensaje `system` porque algunas plantillas de chat (Gemma, por ejemplo)
no admiten varios.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.llm import ChatMessage, LlmClient, estimate_tokens

logger = logging.getLogger(__name__)

PLACEHOLDERS = ("app_name", "user_name", "user_plan", "user_locale")
_PLACEHOLDER = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

UNKNOWN = "desconocido"


@dataclass(frozen=True, slots=True)
class EndUser:
    """Usuario final de una app cliente, tal y como lo describe su JWT."""

    user_id: str
    name: str | None = None
    plan: str | None = None
    locale: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    id: int
    role: str  # "user" o "assistant"
    content: str


class SystemPrompt:
    """System prompt editable en disco. Se relee si el fichero cambia, sin reiniciar."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._mtime: float | None = None
        self._template = ""

    @property
    def template(self) -> str:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            logger.warning("No existe el system prompt %s", self._path)
            return ""
        if mtime != self._mtime:
            raw = self._path.read_text(encoding="utf-8")
            self._template = _HTML_COMMENT.sub("", raw).strip()
            self._mtime = mtime
            if not self._template:
                logger.warning("El system prompt %s está vacío", self._path)
        return self._template

    def render(self, *, app_name: str, user: EndUser) -> str:
        """Sustituye solo los placeholders conocidos; el resto de llaves se respeta."""
        values = {
            "app_name": app_name,
            "user_name": user.name or UNKNOWN,
            "user_plan": user.plan or UNKNOWN,
            "user_locale": user.locale or UNKNOWN,
        }
        return _PLACEHOLDER.sub(lambda match: values[match.group(1)], self.template)


def build_messages(
    *,
    system_prompt: str,
    user: EndUser,
    context: str,
    summary: str | None,
    history: Sequence[HistoryMessage],
    question: str,
) -> list[ChatMessage]:
    sections = [
        system_prompt,
        "## Datos del usuario\n"
        f"- Nombre: {user.name or UNKNOWN}\n"
        f"- Plan: {user.plan or UNKNOWN}\n"
        f"- Idioma: {user.locale or UNKNOWN}",
        f"## Documentación\n{context}",
    ]
    if summary:
        sections.append(f"## Resumen de la conversación anterior\n{summary}")
    messages: list[ChatMessage] = [
        {"role": "system", "content": "\n\n".join(s for s in sections if s)}
    ]
    messages.extend({"role": m.role, "content": m.content} for m in history)  # type: ignore[misc]
    messages.append({"role": "user", "content": question})
    return messages


def split_history(
    messages: Sequence[HistoryMessage], *, max_messages: int, max_tokens: int
) -> tuple[list[HistoryMessage], list[HistoryMessage]]:
    """Divide el historial en (antiguos, recientes).

    Los recientes son los últimos `max_messages` que caben en `max_tokens`. Van literales
    en el prompt. Los antiguos se condensan en el resumen de la conversación.
    """
    recent: list[HistoryMessage] = []
    budget = max_tokens
    for message in reversed(messages):
        cost = estimate_tokens(message.content)
        if len(recent) >= max_messages or cost > budget:
            break
        recent.insert(0, message)
        budget -= cost
    return list(messages[: len(messages) - len(recent)]), recent


SUMMARY_INSTRUCTIONS = (
    "Resume de forma breve y factual la conversación de soporte que se muestra. Conserva "
    "el problema del usuario, los datos relevantes que haya dado y lo que ya se le ha "
    "respondido. No añadas nada que no esté en la conversación. Responde solo con el resumen."
)


def summary_messages(previous_summary: str | None, transcript: str) -> list[ChatMessage]:
    content = transcript
    if previous_summary:
        content = f"Resumen previo:\n{previous_summary}\n\nMensajes nuevos:\n{transcript}"
    return [
        {"role": "system", "content": SUMMARY_INSTRUCTIONS},
        {"role": "user", "content": content},
    ]


async def summarize(llm: LlmClient, transcript: str, previous: str | None = None) -> str:
    """Resume una conversación con el LLM. Lanza `LlmError` si falla."""
    result = await llm.chat(summary_messages(previous, transcript))
    return result.content.strip()


def transcript(messages: Sequence[tuple[str, str]]) -> str:
    """Texto plano `Rol: contenido` a partir de pares (rol, contenido)."""
    labels = {"user": "Usuario", "assistant": "Asistente", "agent": "Agente"}
    return "\n".join(f"{labels.get(role, role)}: {content}" for role, content in messages)
