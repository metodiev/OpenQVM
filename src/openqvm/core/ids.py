"""Identifier helpers."""

from __future__ import annotations

import re
import uuid

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def new_id() -> str:
    """Return a random RFC 4122 version 4 UUID string."""

    return str(uuid.uuid4())


def is_uuid(value: object) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))
