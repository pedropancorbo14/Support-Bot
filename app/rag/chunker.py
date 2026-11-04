"""Troceado de documentos Markdown o texto plano.

Respeta la jerarquía de títulos: un chunk nunca mezcla dos secciones y lleva la ruta de
títulos (`Instalación > Requisitos`). Dentro de una sección, agrupa bloques (párrafos,
listas, bloques de código) hasta `max_tokens`, con un solape de `overlap_tokens` entre
chunks consecutivos de la misma sección.
"""

import hashlib
import re
from dataclasses import dataclass

from app.llm import estimate_tokens

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    position: int
    section: str
    content: str
    content_hash: str
    token_count: int


@dataclass(frozen=True, slots=True)
class _Piece:
    text: str
    separator: str  # cómo se une al fragmento anterior dentro del mismo chunk


def chunk_document(
    title: str, text: str, *, max_tokens: int, overlap_tokens: int
) -> list[ChunkDraft]:
    if overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens debe ser menor que max_tokens")
    drafts: list[ChunkDraft] = []
    for section, body in _split_sections(title, text):
        for content in _pack(
            _pieces(body, max_tokens - overlap_tokens), max_tokens, overlap_tokens
        ):
            drafts.append(
                ChunkDraft(
                    position=len(drafts),
                    section=section,
                    content=content,
                    content_hash=chunk_hash(title, section, content),
                    token_count=estimate_tokens(content),
                )
            )
    return drafts


def chunk_hash(title: str, section: str, content: str) -> str:
    """El hash cubre todo lo que se embebe: si no cambia, el embedding sigue siendo válido."""
    return hashlib.sha256(f"{title}\n{section}\n{content}".encode()).hexdigest()


def _split_sections(title: str, text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    lines: list[str] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(lines).strip()
        if body:
            sections.append((" > ".join(name for _, name in stack), body))
        lines.clear()

    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        if heading is None:
            lines.append(line)
            continue
        flush()
        level, name = len(heading.group(1)), heading.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        # Un H1 igual al título del documento no aporta nada a la ruta de sección.
        if not (level == 1 and name.casefold() == title.strip().casefold()):
            stack.append((level, name))
    flush()
    return sections


def _blocks(body: str) -> list[str]:
    """Separa por líneas en blanco sin partir nunca un bloque de código."""
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        if not line.strip() and not in_fence:
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _pieces(body: str, limit: int) -> list[_Piece]:
    """Bloques de como mucho `limit` tokens: primero por frases y, si no basta, por palabras."""
    pieces: list[_Piece] = []
    for block in _blocks(body):
        if estimate_tokens(block) <= limit:
            pieces.append(_Piece(block, "\n\n"))
            continue
        parts = [p for s in _SENTENCE_END.split(block) for p in _split_words(s, limit)]
        pieces.append(_Piece(parts[0], "\n\n"))
        pieces.extend(_Piece(part, " ") for part in parts[1:])
    return pieces


def _split_words(text: str, limit: int) -> list[str]:
    if estimate_tokens(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for word in text.split():
        while estimate_tokens(word) > limit:  # palabra gigantesca (URL, base64…)
            head, word = word[: limit * 4], word[limit * 4 :]
            if current:
                parts.append(current)
                current = ""
            parts.append(head)
        candidate = f"{current} {word}" if current else word
        if current and estimate_tokens(candidate) > limit:
            parts.append(current)
            current = word
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _pack(pieces: list[_Piece], max_tokens: int, overlap_tokens: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = current + piece.separator + piece.text if current else piece.text
        if current and estimate_tokens(candidate) > max_tokens:
            chunks.append(current)
            # Un token de margen para el espacio que une solape y fragmento.
            overlap = _tail(current, overlap_tokens - 1)
            current = f"{overlap} {piece.text}" if overlap else piece.text
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _tail(text: str, tokens: int) -> str:
    """Últimas palabras de `text` que caben en `tokens`."""
    if tokens <= 0:
        return ""
    words: list[str] = []
    for word in reversed(text.split()):
        if estimate_tokens(" ".join([word, *words])) > tokens:
            break
        words.insert(0, word)
    return " ".join(words)
