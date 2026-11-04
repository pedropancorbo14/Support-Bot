"""Evaluación del bot con un fichero de casos (`eval/cases.toml`).

Cada caso se ejecuta con el `ChatService` real (recuperación, prompt y LLM) dentro de una
transacción que se deshace al final: la evaluación no deja conversaciones ni métricas y
no dispara webhooks.
"""

import math
import tomllib
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.chat.prompt import EndUser, SystemPrompt
from app.chat.service import ChatService, Completed, Failed, Token
from app.config import Settings
from app.llm import LlmClient
from app.models import ClientApp, Message

Behavior = Literal["answer", "unknown", "escalate"]
BEHAVIORS: tuple[str, ...] = ("answer", "unknown", "escalate")


@dataclass(frozen=True, slots=True)
class EvalCase:
    question: str
    expected: Behavior
    keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CaseResult:
    question: str
    expected: str
    predicted: str  # answer | unknown | escalate | error
    correct: bool
    answer: str
    missing_keywords: list[str]
    unsupported: bool  # respondió sin ningún fragmento de documentación
    chunks: int
    top_score: float | None
    latency_ms: int | None
    ttft_ms: int | None


def load_cases(path: Path) -> tuple[list[EvalCase], list[str]]:
    """Lee los casos y los marcadores de "no lo sé". Lanza `ValueError` si no es válido."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    markers = [normalize(m) for m in data.get("unknown_markers", [])]
    cases = []
    for index, raw in enumerate(data.get("case", []), start=1):
        if raw.get("expected") not in BEHAVIORS or not raw.get("question"):
            raise ValueError(
                f"Caso {index}: 'question' es obligatoria y 'expected' debe ser uno de "
                f"{', '.join(BEHAVIORS)}"
            )
        cases.append(EvalCase(raw["question"], raw["expected"], list(raw.get("keywords", []))))
    if not cases:
        raise ValueError(f"{path} no contiene ningún [[case]]")
    return cases, markers


def normalize(text: str) -> str:
    """Minúsculas y sin tildes, para comparar palabras clave y marcadores."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def classify(answer: str, escalated: bool, markers: list[str]) -> Behavior:
    if escalated:
        return "escalate"
    normalized = normalize(answer)
    return "unknown" if any(marker in normalized for marker in markers) else "answer"


async def run_evaluation(
    engine: AsyncEngine,
    llm: LlmClient,
    settings: Settings,
    system_prompt: SystemPrompt,
    client_app: ClientApp,
    cases: list[EvalCase],
    markers: list[str],
) -> list[CaseResult]:
    results = []
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            # Los commit del servicio se convierten en SAVEPOINT dentro de esta transacción.
            session = AsyncSession(
                bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
            )
            chat = ChatService(session, llm, settings, system_prompt)
            for case in cases:
                results.append(await _run_case(chat, session, client_app, case, markers))
            await session.close()
        finally:
            await transaction.rollback()
    return results


async def _run_case(
    chat: ChatService,
    session: AsyncSession,
    client_app: ClientApp,
    case: EvalCase,
    markers: list[str],
) -> CaseResult:
    user = EndUser(user_id=f"eval-{uuid.uuid4()}", name="Evaluación")
    conversation = await chat.start_conversation(client_app, user)
    text: list[str] = []
    escalated = failed = False
    message_id: int | None = None
    async for event in chat.reply(client_app, conversation, case.question):
        match event:
            case Token(chunk):
                text.append(chunk)
            case Completed(message_id=message_id, escalated=escalated):
                pass
            case Failed(message_id=message_id):
                failed = True
    message = await session.get(Message, message_id) if message_id else None
    chunks = (message.retrieved_chunks or []) if message else []
    answer = "".join(text).strip()

    predicted = "error" if failed else classify(answer, escalated, markers)
    missing = [k for k in case.keywords if normalize(k) not in normalize(answer)]
    correct = predicted == case.expected and (case.expected != "answer" or not missing)
    return CaseResult(
        question=case.question,
        expected=case.expected,
        predicted=predicted,
        correct=correct,
        answer=answer,
        missing_keywords=missing if case.expected == "answer" else [],
        unsupported=predicted == "answer" and not chunks,
        chunks=len(chunks),
        top_score=chunks[0]["score"] if chunks else None,
        latency_ms=message.latency_ms if message else None,
        ttft_ms=message.ttft_ms if message else None,
    )


def summarize_results(results: list[CaseResult]) -> dict[str, Any]:
    total = len(results)
    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    ttfts = [r.ttft_ms for r in results if r.ttft_ms is not None]
    return {
        "total": total,
        "correct": sum(r.correct for r in results),
        "accuracy": round(sum(r.correct for r in results) / total, 4) if total else 0.0,
        "by_expected": {
            behavior: {
                "total": sum(r.expected == behavior for r in results),
                "correct": sum(r.expected == behavior and r.correct for r in results),
            }
            for behavior in BEHAVIORS
        },
        "escalations": {
            "correct": sum(r.expected == "escalate" and r.predicted == "escalate" for r in results),
            "unexpected": sum(
                r.expected != "escalate" and r.predicted == "escalate" for r in results
            ),
            "missed": sum(r.expected == "escalate" and r.predicted != "escalate" for r in results),
        },
        "unsupported_answers": sum(r.unsupported for r in results),
        "llm_errors": sum(r.predicted == "error" for r in results),
        "latency_ms": {
            "avg": round(mean(latencies)) if latencies else None,
            "p95": _percentile(latencies, 0.95),
        },
        "ttft_ms": {"avg": round(mean(ttfts)) if ttfts else None},
    }


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]


def format_report(results: list[CaseResult], summary: dict[str, Any]) -> str:
    lines = []
    for index, r in enumerate(results, start=1):
        mark = "✔" if r.correct else "✘"
        lines.append(f"{mark} {index:>3}. [{r.expected} → {r.predicted}] {r.question}")
        if not r.correct:
            if r.missing_keywords:
                lines.append(f"        faltan palabras clave: {', '.join(r.missing_keywords)}")
            lines.append(f"        respuesta: {r.answer[:200]!r}")
        if r.unsupported:
            lines.append("        ⚠ respuesta sin respaldo en la documentación")
    s = summary
    lines += [
        "",
        f"Aciertos: {s['correct']}/{s['total']} ({s['accuracy']:.0%})",
        *(
            f"  {behavior:<9} {v['correct']}/{v['total']}"
            for behavior, v in s["by_expected"].items()
            if v["total"]
        ),
        f"Escalados correctos: {s['escalations']['correct']} · "
        f"incorrectos: {s['escalations']['unexpected']} · "
        f"no detectados: {s['escalations']['missed']}",
        f"Respuestas sin respaldo: {s['unsupported_answers']}",
        f"Errores del LLM: {s['llm_errors']}",
        f"Latencia media: {s['latency_ms']['avg']} ms (p95 {s['latency_ms']['p95']} ms) · "
        f"primer token: {s['ttft_ms']['avg']} ms",
    ]
    return "\n".join(lines)


def results_as_dicts(results: list[CaseResult]) -> list[dict[str, Any]]:
    return [asdict(r) for r in results]
