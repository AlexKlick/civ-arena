"""Lossless live entity identity; historical numeric IDs are read-only."""

import re

QUALIFIED_ID = re.compile(r"([uc])(0|[1-9][0-9]*):(0|[1-9][0-9]*)\Z")
MAX_EXACT_INTEGER = 2**53 - 1


def decode(entity_id: str, kind: str) -> tuple[int, int]:
    match = QUALIFIED_ID.fullmatch(entity_id) if isinstance(entity_id, str) else None
    if match is None or match[1] != kind:
        raise ValueError("live entity ID requires explicit owner and raw ID")
    owner, raw = int(match[2]), int(match[3])
    if max(owner, raw) > MAX_EXACT_INTEGER:
        raise ValueError("entity ID exceeds exact Lua integer range")
    return owner, raw


def observed(value: str, owner: int, kind: str, *, qualified: bool = False) -> str:
    """Validate owner binding; only offline reads may retain old numeric IDs."""
    if type(owner) is not int or owner < 0:
        raise ValueError("invalid entity owner")
    if ':' in value or qualified:
        actual, _ = decode(value, kind)
        if actual != owner:
            raise ValueError("entity ID owner disagrees with observation")
        return value
    if not re.fullmatch(r'0|[1-9][0-9]*', value):
        raise ValueError("invalid historical entity ID")
    return kind + value


def sort_key(entity_id: str) -> tuple[int, int]:
    if ':' in entity_id:
        return decode(entity_id, entity_id[0])
    return -1, int(entity_id[1:])
