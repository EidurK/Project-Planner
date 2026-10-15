"""Lets a subagent block until the user answers a question in the web UI."""
import threading
import uuid

TIMEOUT_S = 15 * 60

_pending: dict[str, dict] = {}
_lock = threading.Lock()


def register(session_id: str) -> str:
    qid = uuid.uuid4().hex[:8]
    with _lock:
        _pending[qid] = {"session": session_id, "event": threading.Event(), "answer": None}
    return qid


def wait(qid: str) -> str:
    entry = _pending[qid]
    got = entry["event"].wait(TIMEOUT_S)
    with _lock:
        _pending.pop(qid, None)
    if not got:
        return "(The user did not answer in time. Proceed with stated assumptions.)"
    return entry["answer"]


def answer(qid: str, text: str) -> bool:
    with _lock:
        entry = _pending.get(qid)
    if not entry:
        return False
    entry["answer"] = text
    entry["event"].set()
    return True


def cancel_session(session_id: str) -> None:
    with _lock:
        entries = [e for e in _pending.values() if e["session"] == session_id]
    for e in entries:
        e["answer"] = "(Conversation was reset; stop and give your best report.)"
        e["event"].set()
