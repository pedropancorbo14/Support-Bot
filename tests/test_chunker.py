from app.llm import estimate_tokens
from app.rag.chunker import chunk_document, chunk_hash

DOC = """# Guía de facturación

Introducción a la facturación.

## Métodos de pago

Aceptamos tarjeta y transferencia.

### Tarjeta

Visa y Mastercard.

## Reembolsos

Se tramitan en 5 días.
"""


def test_sections_follow_heading_hierarchy_and_drop_title_h1() -> None:
    chunks = chunk_document("Guía de facturación", DOC, max_tokens=500, overlap_tokens=50)

    assert [(c.section, c.content) for c in chunks] == [
        ("", "Introducción a la facturación."),
        ("Métodos de pago", "Aceptamos tarjeta y transferencia."),
        ("Métodos de pago > Tarjeta", "Visa y Mastercard."),
        ("Reembolsos", "Se tramitan en 5 días."),
    ]
    assert [c.position for c in chunks] == [0, 1, 2, 3]


def test_h1_different_from_title_is_kept_in_section() -> None:
    chunks = chunk_document("Otro título", "# Inicio\n\nHola", max_tokens=500, overlap_tokens=0)

    assert chunks[0].section == "Inicio"


def test_headings_inside_code_blocks_are_not_sections() -> None:
    text = "## Script\n\n```bash\n# no es un título\n\necho hola\n```\n"

    [chunk] = chunk_document("Doc", text, max_tokens=500, overlap_tokens=0)

    assert chunk.section == "Script"
    assert chunk.content == "```bash\n# no es un título\n\necho hola\n```"


def test_plain_text_without_headings() -> None:
    [chunk] = chunk_document("Notas", "Línea uno.\nLínea dos.", max_tokens=500, overlap_tokens=0)

    assert chunk.section == ""
    assert chunk.content == "Línea uno.\nLínea dos."


def test_long_sections_are_split_within_limit_with_overlap() -> None:
    sentences = " ".join(f"Frase número {i} sobre configuración avanzada." for i in range(200))

    chunks = chunk_document("Doc", f"## Larga\n\n{sentences}", max_tokens=100, overlap_tokens=20)

    assert len(chunks) > 5
    assert all(c.section == "Larga" for c in chunks)
    assert all(c.token_count <= 100 for c in chunks)
    for previous, current in zip(chunks, chunks[1:], strict=False):
        overlap = _shared_overlap(previous.content.split(), current.content.split())
        assert overlap, "cada chunk debe empezar con el final del anterior"
        assert estimate_tokens(" ".join(overlap)) <= 20


def _shared_overlap(previous: list[str], current: list[str]) -> list[str]:
    """Mayor sufijo de `previous` que es prefijo de `current`."""
    for size in range(min(len(previous), len(current)), 0, -1):
        if previous[-size:] == current[:size]:
            return current[:size]
    return []


def test_overlap_never_crosses_sections() -> None:
    text = "## A\n\n" + "alfa " * 300 + "\n\n## B\n\nbeta"

    chunks = chunk_document("Doc", text, max_tokens=100, overlap_tokens=20)

    assert chunks[-1].section == "B"
    assert chunks[-1].content == "beta"


def test_giant_word_is_split() -> None:
    chunks = chunk_document("Doc", "x" * 2000, max_tokens=100, overlap_tokens=10)

    assert all(estimate_tokens(c.content) <= 100 for c in chunks)
    assert "".join(c.content.replace(" ", "") for c in chunks).count("x") >= 2000


def test_hash_depends_on_title_section_and_content() -> None:
    base = chunk_hash("Doc", "Sección", "contenido")

    assert base == chunk_hash("Doc", "Sección", "contenido")
    assert base != chunk_hash("Otro", "Sección", "contenido")
    assert base != chunk_hash("Doc", "Otra", "contenido")
    assert base != chunk_hash("Doc", "Sección", "otro")
