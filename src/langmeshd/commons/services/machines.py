"""The other machines this one knows how to reach, kept where the desktop can read them."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from langmeshd.commons import state
from langmeshd.commons.database import MachineRecord

# The scheme a paired door prints, accepted with or without it.
PAIRING_PREFIX = "langmesh://pair#"


class PairingLinkError(ValueError):
    """A pairing link that could not be read, with a sentence saying which way."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_pairing_link(link: str) -> dict[str, str]:
    """Turn a pairing link into the machine it describes, from its padding-stripped base64url payload."""
    fragment = link.strip()
    if fragment.startswith(PAIRING_PREFIX):
        fragment = fragment[len(PAIRING_PREFIX) :]
    elif "#" in fragment:
        fragment = fragment.split("#", 1)[1]
    if not fragment:
        raise PairingLinkError("That is not a LangMesh pairing link.")
    try:
        padded = fragment + "=" * (-len(fragment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PairingLinkError("That is not a LangMesh pairing link.") from error
    if not isinstance(payload, dict):
        raise PairingLinkError("That is not a LangMesh pairing link.")
    if payload.get("version") != 1:
        raise PairingLinkError("That is not a current LangMesh pairing link.")
    name = payload.get("name")
    endpoint = payload.get("endpoint")
    token = payload.get("token")
    if not (isinstance(name, str) and isinstance(endpoint, str) and isinstance(token, str)):
        raise PairingLinkError("That link is missing its address or its token.")
    if not (name.strip() and endpoint.strip() and token.strip()):
        raise PairingLinkError("That link is missing its address or its token.")
    return {"name": name.strip(), "endpoint": endpoint.rstrip("/"), "token": token}


def _serialize(record: MachineRecord) -> dict[str, Any]:
    """A machine as the interface sees it, deliberately without its token."""
    return {
        "id": record.id,
        "name": record.name,
        "endpoint": record.endpoint,
        "created_at": record.created_at,
    }


def machines_payload() -> dict[str, list[dict[str, Any]]]:
    assert state.session_factory is not None
    database = state.session_factory()
    try:
        records = database.query(MachineRecord).order_by(MachineRecord.created_at).all()
        return {"machines": [_serialize(record) for record in records]}
    finally:
        database.close()


def remember_machine(link: str) -> dict[str, Any]:
    """Add a machine, or refresh the token of one already known, keyed on its address."""
    described = read_pairing_link(link)
    assert state.session_factory is not None
    database = state.session_factory()
    try:
        record = (
            database.query(MachineRecord)
            .filter(MachineRecord.endpoint == described["endpoint"])
            .first()
        )
        if record is None:
            record = MachineRecord(
                id=str(uuid.uuid4()),
                name=described["name"],
                endpoint=described["endpoint"],
                token=described["token"],
                created_at=_now(),
            )
            database.add(record)
        else:
            # The name is left alone, since a re-pair is about the token and the name may have been edited.
            record.token = described["token"]
        database.commit()
        return _serialize(record)
    finally:
        database.close()


def machine_door(machine_id: str) -> dict[str, str] | None:
    """Endpoint and token for in-page calls, asked for at the moment somebody chooses to talk to it."""
    assert state.session_factory is not None
    database = state.session_factory()
    try:
        record = database.get(MachineRecord, machine_id)
        if record is None:
            return None
        return {
            "endpoint": record.endpoint.rstrip("/"),
            "token": record.token,
        }
    finally:
        database.close()


def rename_machine(machine_id: str, name: str) -> dict[str, Any] | None:
    trimmed = name.strip()
    if not trimmed:
        return None
    assert state.session_factory is not None
    database = state.session_factory()
    try:
        record = database.get(MachineRecord, machine_id)
        if record is None:
            return None
        record.name = trimmed
        database.commit()
        return _serialize(record)
    finally:
        database.close()


def forget_machine(machine_id: str) -> bool:
    assert state.session_factory is not None
    database = state.session_factory()
    try:
        record = database.get(MachineRecord, machine_id)
        if record is None:
            return False
        database.delete(record)
        database.commit()
        return True
    finally:
        database.close()


__all__ = [
    "PairingLinkError",
    "forget_machine",
    "machine_door",
    "machines_payload",
    "read_pairing_link",
    "remember_machine",
    "rename_machine",
]
