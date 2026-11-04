import os
from pathlib import Path

from app.chat.prompt import EndUser, HistoryMessage, SystemPrompt, build_messages, split_history

USER = EndUser(user_id="u1", name="Ana", plan="pro", locale="es-ES")


def test_message_order_system_user_docs_summary_history_question() -> None:
    history = [HistoryMessage(1, "user", "Hola"), HistoryMessage(2, "assistant", "¡Hola!")]

    messages = build_messages(
        system_prompt="Eres el asistente de Tienda.",
        user=USER,
        context="[FAQ > Pagos] Tarjeta.",
        summary="El usuario preguntó por envíos.",
        history=history,
        question="¿Aceptáis tarjeta?",
    )

    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    system = messages[0]["content"]
    assert system.startswith("Eres el asistente de Tienda.")  # prefijo estable para la caché
    positions = [
        system.index(marker)
        for marker in (
            "## Datos del usuario",
            "- Nombre: Ana",
            "## Documentación\n[FAQ > Pagos] Tarjeta.",
            "## Resumen de la conversación anterior\nEl usuario preguntó por envíos.",
        )
    ]
    assert positions == sorted(positions)
    assert "- Plan: pro" in system and "- Idioma: es-ES" in system
    assert messages[-1] == {"role": "user", "content": "¿Aceptáis tarjeta?"}


def test_without_summary_and_unknown_user_data() -> None:
    [system, question] = build_messages(
        system_prompt="S",
        user=EndUser(user_id="u1"),
        context="ctx",
        summary=None,
        history=[],
        question="q",
    )

    assert "Resumen" not in system["content"]
    assert "- Nombre: desconocido" in system["content"]
    assert question["content"] == "q"


def test_system_prompt_placeholders_comments_and_other_braces(tmp_path: Path) -> None:
    path = tmp_path / "system.md"
    path.write_text(
        "<!-- nota interna {app_name} -->\n"
        "Eres el asistente de {app_name}. Habla a {user_name} ({user_plan}, {user_locale}).\n"
        'Formato JSON: {"ok": true} y {otro}.'
    )

    rendered = SystemPrompt(path).render(app_name="Tienda", user=USER)

    assert rendered == (
        "Eres el asistente de Tienda. Habla a Ana (pro, es-ES).\n"
        'Formato JSON: {"ok": true} y {otro}.'
    )


def test_system_prompt_reloads_when_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "system.md"
    path.write_text("Versión 1")
    prompt = SystemPrompt(path)
    assert prompt.template == "Versión 1"

    path.write_text("Versión 2")
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))

    assert prompt.template == "Versión 2"


def test_missing_or_comment_only_prompt_renders_empty(tmp_path: Path) -> None:
    assert SystemPrompt(tmp_path / "no-existe.md").template == ""
    path = tmp_path / "system.md"
    path.write_text("<!-- pendiente -->")
    assert SystemPrompt(path).template == ""


def test_split_history_by_message_count() -> None:
    history = [HistoryMessage(i, "user", f"m{i}") for i in range(1, 6)]

    older, recent = split_history(history, max_messages=2, max_tokens=1000)

    assert [m.id for m in older] == [1, 2, 3]
    assert [m.id for m in recent] == [4, 5]


def test_split_history_by_token_budget() -> None:
    history = [
        HistoryMessage(1, "user", "x" * 400),  # 100 tokens
        HistoryMessage(2, "assistant", "y" * 400),
        HistoryMessage(3, "user", "z" * 400),
    ]

    older, recent = split_history(history, max_messages=10, max_tokens=250)

    assert [m.id for m in older] == [1]
    assert [m.id for m in recent] == [2, 3]
