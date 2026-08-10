"""Pick the right parser for a `.BYS` file.

Detection is structural rather than name-based: the device names files
`00100001.BYS` and reuses those names after a reset, and rebadged units carry other
brands in the serial string. Only the byte layout is trustworthy.

Adding a model: write `app/parsers/<model>.py` exposing `MODEL`, `looks_like_*` and
`parse`, then append it to _PARSERS. Nothing downstream changes.
"""

from __future__ import annotations

from typing import Callable

from app.parsers import yh550
from app.parsers.base import InvalidPAPFile, SessionFile

# (model name, structural detector, parser)
_PARSERS: list[tuple[str, Callable[[bytes], bool], Callable[[bytes], SessionFile]]] = [
    (yh550.MODEL, yh550.looks_like_yh550, yh550.parse),
]

SUPPORTED_MODELS = [model for model, _, _ in _PARSERS]


def parse(data: bytes) -> SessionFile:
    """Parse a session file, choosing the parser by layout.

    Raises InvalidPAPFile if no supported layout matches.
    """
    for _model, detector, parser in _PARSERS:
        if detector(data):
            return parser(data)

    raise InvalidPAPFile(
        f"not a recognised session file ({len(data)} bytes). "
        f"Supported models: {', '.join(SUPPORTED_MODELS)}"
    )
