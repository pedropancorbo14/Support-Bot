import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import cli
from app.evaluation import CaseResult, classify, load_cases, normalize, summarize_results
from app.models import Conversation, WebhookDelivery
from app.rag.ingest import upsert_document
from app.worker import embed_pending_chunks
from tests.fakes import FakeLlmClient

pytestmark = pytest.mark.anyio

CASES = """
unknown_markers = ["no lo sé"]

[[case]]
question = "aceptamos tarjeta de crédito"
expected = "answer"
keywords = ["tarjeta", "credito"]

[[case]]
question = "¿Capital de Australia?"
expected = "unknown"

[[case]]
question = "Quiero una persona"
expected = "escalate"

[[case]]
question = "¿Hacéis envíos?"
expected = "answer"
keywords = ["24 horas"]
"""


def result(**overrides) -> CaseResult:
    base = dict(
        question="q",
        expected="answer",
        predicted="answer",
        correct=True,
        answer="",
        missing_keywords=[],
        unsupported=False,
        chunks=1,
        top_score=0.9,
        latency_ms=100,
        ttft_ms=10,
    )
    return CaseResult(**(base | overrides))


async def test_classify_and_normalize() -> None:
    markers = [normalize("No lo sé")]

    assert classify("Lo siento, NO LO SE.", False, markers) == "unknown"
    assert classify("Aceptamos tarjeta", False, markers) == "answer"
    assert classify("No lo sé", True, markers) == "escalate"
    assert normalize("Crédito Ñandú") == "credito nandu"


async def test_load_cases_validates(tmp_path: Path) -> None:
    good = tmp_path / "good.toml"
    good.write_text(CASES)
    bad = tmp_path / "bad.toml"
    bad.write_text('[[case]]\nquestion = "x"\nexpected = "quizás"\n')

    cases, markers = load_cases(good)

    assert [c.expected for c in cases] == ["answer", "unknown", "escalate", "answer"]
    assert markers == ["no lo se"]
    with pytest.raises(ValueError, match="expected"):
        load_cases(bad)


async def test_summary_counts() -> None:
    summary = summarize_results(
        [
            result(),
            result(expected="escalate", predicted="escalate"),
            result(expected="unknown", predicted="escalate", correct=False),
            result(expected="escalate", predicted="answer", correct=False, unsupported=True),
            result(predicted="error", correct=False, latency_ms=300),
        ]
    )

    assert (summary["total"], summary["correct"], summary["accuracy"]) == (5, 2, 0.4)
    assert summary["escalations"] == {"correct": 1, "unexpected": 1, "missed": 1}
    assert summary["unsupported_answers"] == 1
    assert summary["llm_errors"] == 1
    assert summary["latency_ms"] == {"avg": 140, "p95": 300}


async def test_evaluate_command_runs_cases_without_leaving_traces(
    session, sessionmaker, demo_app, settings, tmp_path: Path, capsys
) -> None:
    client_app, _ = demo_app
    await upsert_document(
        session,
        client_app,
        external_id="faq",
        title="FAQ",
        content="## Pagos\n\nAceptamos tarjeta de crédito.",
        settings=settings,
    )
    await session.commit()
    await embed_pending_chunks(sessionmaker, FakeLlmClient(), settings)
    cases = tmp_path / "cases.toml"
    cases.write_text(CASES)
    report = tmp_path / "report.json"
    llm = FakeLlmClient(
        [
            "Sí, aceptamos tarjeta de crédito.",
            "No lo sé, lo siento.",
            "Te paso con alguien. [ESCALAR: lo pide]",
            "Hacemos envíos.",  # sin la palabra clave y sin documentación
        ]
    )

    code = await cli.run(
        ["evaluate", "--app", "demo", "--cases", str(cases), "--output", str(report)],
        settings.model_copy(update={"rag_min_score": 0.3}),
        llm=llm,
    )

    output = capsys.readouterr().out
    data = json.loads(report.read_text())
    assert code == 0
    assert data["summary"]["correct"] == 3
    assert data["summary"]["escalations"] == {"correct": 1, "unexpected": 0, "missed": 0}
    assert data["summary"]["unsupported_answers"] == 1
    last = data["results"][-1]
    assert (last["correct"], last["missing_keywords"], last["unsupported"]) == (
        False,
        ["24 horas"],
        True,
    )
    assert data["results"][0]["chunks"] == 1
    assert "Aciertos: 3/4 (75%)" in output
    assert "sin respaldo" in output

    # Nada persiste: ni conversaciones ni webhooks de escalado hacia la app.
    async with sessionmaker() as check:
        assert await check.scalar(select(func.count()).select_from(Conversation)) == 0
        assert await check.scalar(select(func.count()).select_from(WebhookDelivery)) == 0


async def test_min_accuracy_exit_code(sessionmaker, demo_app, settings, tmp_path: Path) -> None:
    cases = tmp_path / "cases.toml"
    cases.write_text('[[case]]\nquestion = "x"\nexpected = "escalate"\n')

    code = await cli.run(
        ["evaluate", "--app", "demo", "--cases", str(cases), "--min-accuracy", "0.9"],
        settings,
        llm=FakeLlmClient(["Respuesta normal."]),
    )

    assert code == 2


SHIPPED_CASES = Path(__file__).resolve().parents[1] / "eval" / "cases.toml"


def test_shipped_cases_file_is_valid() -> None:
    cases, markers = load_cases(SHIPPED_CASES)

    assert cases and markers
