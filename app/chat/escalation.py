"""Detección de la etiqueta `[ESCALAR: motivo]` en la salida del modelo.

El filtro de streaming retiene el texto desde un `[` que podría iniciar la etiqueta hasta
saber si lo es. Así la etiqueta nunca llega al usuario, aunque venga partida entre tokens.
"""

import re

_OPEN = "[ESCALAR:"
_TAG = re.compile(r"\[ESCALAR:\s*([^\]\n]*)\]", re.IGNORECASE)
# Si una "etiqueta" abierta crece más de esto sin cerrarse, no es una etiqueta.
_MAX_TAG_LENGTH = 300


class EscalationFilter:
    def __init__(self) -> None:
        self._buffer = ""
        self.reason: str | None = None

    @property
    def escalated(self) -> bool:
        return self.reason is not None

    def feed(self, delta: str) -> str:
        """Recibe un fragmento del modelo y devuelve el texto que ya se puede mostrar."""
        self._buffer += delta
        output: list[str] = []
        while self._buffer:
            start = self._buffer.find("[")
            if start == -1:
                output.append(self._buffer)
                self._buffer = ""
                break
            output.append(self._buffer[:start])
            self._buffer = self._buffer[start:]
            if match := _TAG.match(self._buffer):
                self._record(match.group(1))
                self._buffer = self._buffer[match.end() :]
            elif _may_become_tag(self._buffer):
                break  # esperar a más texto
            else:
                output.append("[")
                self._buffer = self._buffer[1:]
        return "".join(output)

    def finish(self) -> str:
        """Vacía el buffer al terminar. Una etiqueta sin `]` final también cuenta."""
        rest, self._buffer = self._buffer, ""
        if rest.upper().startswith(_OPEN):
            self._record(rest[len(_OPEN) :])
            return ""
        return rest

    def _record(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason.strip() or "Sin motivo indicado"


def strip_escalation(text: str) -> tuple[str, str | None]:
    """Versión no incremental: devuelve el texto limpio y el motivo, si lo hay."""
    escalation = EscalationFilter()
    clean = escalation.feed(text) + escalation.finish()
    return clean.strip(), escalation.reason


def _may_become_tag(buffer: str) -> bool:
    upper = buffer.upper()
    if len(upper) <= len(_OPEN):
        return _OPEN.startswith(upper)
    return upper.startswith(_OPEN) and "\n" not in buffer and len(buffer) < _MAX_TAG_LENGTH
