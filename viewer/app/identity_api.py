"""Modular authenticated HTTP surface for the manual people directory."""
from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
_context = None
_PERSON_RE = re.compile(r"p_[a-f0-9]{24}\Z")
_SPEAKER_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_RECORDING_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def configure(*, csrf_ok, json_body, client_factory, recording_exists):
    global _context
    _context = (csrf_ok, json_body, client_factory, recording_exists)


def _bad(message="Не удалось изменить спикера.", status=400):
    return JSONResponse({"error_ru": message}, status_code=status)


def _client():
    return _context[2]()


def _errors(call):
    try:
        return call()
    except Exception as exc:
        name = type(exc).__name__
        return _bad(status=400 if name == "ControlRejected" else 503)


@router.get("/api/people")
def list_people():
    result = _errors(lambda: _client().people("list_people"))
    return result if isinstance(result, JSONResponse) else {"people": result.get("people", [])}


@router.post("/api/people")
async def create_person(request: Request):
    csrf_ok, json_body, _, _ = _context
    if not csrf_ok(request): return _bad("Сессия устарела. Обновите страницу и повторите.", 403)
    body = await json_body(request)
    if not isinstance(body, dict) or set(body) not in ({"display_name"}, {"display_name", "this_is_me"}):
        return _bad("Введите имя человека.")
    name, this_is_me = body.get("display_name"), body.get("this_is_me", False)
    if not isinstance(name, str) or type(this_is_me) is not bool:
        return _bad("Введите имя человека.")
    fields = {"display_name": name}
    if this_is_me:
        fields.update(is_self=True, consent_status="self_confirmed")
    result = _errors(lambda: _client().people("create_person", **fields))
    return result if isinstance(result, JSONResponse) else {"person": result["person"]}


async def _target(request, rec_id, speaker_id):
    csrf_ok, json_body, _, recording_exists = _context
    if not csrf_ok(request): return None, _bad("Сессия устарела. Обновите страницу и повторите.", 403)
    if not _RECORDING_RE.fullmatch(rec_id) or not _SPEAKER_RE.fullmatch(speaker_id):
        return None, _bad()
    if not recording_exists(rec_id): return None, _bad("Запись не найдена.", 404)
    return await json_body(request), None


@router.post("/api/recordings/{rec_id}/speakers/{speaker_id}/identity")
async def assign_identity(rec_id: str, speaker_id: str, request: Request):
    body, error = await _target(request, rec_id, speaker_id)
    if error: return error
    if not isinstance(body, dict): return _bad()
    if set(body) == {"person_id"} and isinstance(body["person_id"], str) and _PERSON_RE.fullmatch(body["person_id"]):
        action, fields = "assign_identity", {"person_id": body["person_id"]}
    elif body == {"unknown": True}:
        action, fields = "set_unknown", {}
    else:
        return _bad()
    result = _errors(lambda: _client().people(action, recording_id=rec_id,
                                                speaker_id=speaker_id, **fields))
    return result if isinstance(result, JSONResponse) else {"assignment": result["assignment"]}


@router.post("/api/recordings/{rec_id}/speakers/{speaker_id}/identity/undo")
async def undo_identity(rec_id: str, speaker_id: str, request: Request):
    body, error = await _target(request, rec_id, speaker_id)
    if error: return error
    if body != {}: return _bad()
    result = _errors(lambda: _client().people("undo_identity", recording_id=rec_id,
                                                speaker_id=speaker_id))
    return result if isinstance(result, JSONResponse) else {"assignment": result["assignment"]}
