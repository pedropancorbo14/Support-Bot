import pytest

from app.chat.escalation import EscalationFilter, strip_escalation


def stream(text: str, size: int) -> tuple[str, str | None]:
    escalation = EscalationFilter()
    shown = [escalation.feed(text[i : i + size]) for i in range(0, len(text), size)]
    shown.append(escalation.finish())
    return "".join(shown), escalation.reason


def test_strip_tag_at_the_end() -> None:
    text = "No puedo ayudarte con eso. [ESCALAR: reembolso fuera de plazo]"

    assert strip_escalation(text) == ("No puedo ayudarte con eso.", "reembolso fuera de plazo")


def test_no_tag() -> None:
    assert strip_escalation("Hola, ¿en qué te ayudo?") == ("Hola, ¿en qué te ayudo?", None)


@pytest.mark.parametrize("size", [1, 2, 3, 5, 8, 1000])
def test_tag_split_across_stream_chunks_never_reaches_the_user(size: int) -> None:
    text = "Te paso con el equipo. [ESCALAR: cliente enfadado]"

    shown, reason = stream(text, size)

    assert shown == "Te paso con el equipo. "
    assert reason == "cliente enfadado"


@pytest.mark.parametrize("size", [1, 4, 1000])
def test_other_brackets_are_shown(size: int) -> None:
    text = "Pulsa [Guardar] y luego [ESC] o [E]."

    assert stream(text, size) == (text, None)


def test_case_insensitive_and_whitespace() -> None:
    assert strip_escalation("Vale.\n[escalar:   pide factura  ]\n") == ("Vale.", "pide factura")


def test_unterminated_tag_at_end_of_stream_counts() -> None:
    assert stream("Un momento. [ESCALAR: error de pago", 3) == ("Un momento. ", "error de pago")


def test_empty_reason() -> None:
    assert strip_escalation("[ESCALAR:]") == ("", "Sin motivo indicado")


def test_only_first_reason_is_kept_and_all_tags_removed() -> None:
    assert strip_escalation("A [ESCALAR: uno] B [ESCALAR: dos]") == ("A  B", "uno")


def test_text_after_partial_match_is_flushed() -> None:
    assert stream("Mira [ESCA", 2) == ("Mira [ESCA", None)
    assert stream("Precio [ESCALADO] 3€", 2) == ("Precio [ESCALADO] 3€", None)
