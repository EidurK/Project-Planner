"""Claude tool-use loop shared by the PM agent and its subagents. Yields UI events as dicts."""
import threading
from typing import Callable, Generator, Iterator

import anthropic

from . import org, questions, settings, storage, subagents
from .tools import TOOLS, WRITE_TOOLS, run_tool

_client: anthropic.Anthropic | None = None
_client_key: str | None = None  # the app-saved key the client was built with
_sessions: dict[str, list] = {}

_cancel: dict[str, threading.Event] = {}


class Cancelled(Exception):
    """The user pressed Stop."""


def _cancel_event(session_id: str) -> threading.Event:
    return _cancel.setdefault(session_id, threading.Event())


def stop(session_id: str) -> None:
    _cancel_event(session_id).set()
    questions.cancel_session(session_id)  # unblock a subagent waiting for an answer


def _repair(messages: list) -> None:
    """Make history valid again: every tool_use must be followed by a tool_result."""
    if not messages or messages[-1]["role"] != "assistant":
        return
    ids = [b["id"] for b in messages[-1]["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
    if ids:
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": i, "content": "Interrupted before this ran.", "is_error": True}
            for i in ids]})


Dispatch = Callable[[str, dict], Generator[dict, None, tuple[str, bool]]]


def _get_client() -> anthropic.Anthropic:
    global _client, _client_key
    key = settings.get_api_key()
    if _client is None or key != _client_key:  # rebuilt when the key is changed in Settings
        _client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        _client_key = key
    return _client


def reset(session_id: str) -> None:
    stop(session_id)
    _sessions.pop(session_id, None)


def run_loop(system: "str | Callable[[], str]", tools: "list | Callable[[], list]", messages: list, dispatch: Dispatch, agent: str,
             max_turns: int = 10, stream_text: bool = True,
             session_id: str = "") -> Generator[dict, None, str]:
    """Run until the model stops calling tools. Returns the final text. Raises Cancelled on Stop."""
    client = _get_client()
    cancel = _cancel_event(session_id)
    for _ in range(max_turns):
        if cancel.is_set():
            raise Cancelled()
        # A callable is re-evaluated every model call, so prompt edits apply immediately
        current_system = system() if callable(system) else system
        with client.messages.stream(model=settings.get_model(), max_tokens=8192, system=current_system,
                                    tools=tools() if callable(tools) else tools, messages=messages) as stream:
            # Iterate raw events (not just text) so Stop also works while the model is thinking silently;
            # leaving the `with` block closes the connection and ends generation.
            for event in stream:
                if cancel.is_set():
                    raise Cancelled()
                if (stream_text and event.type == "content_block_delta"
                        and event.delta.type == "text_delta"):
                    yield {"type": "text", "text": event.delta.text}
            final = stream.get_final_message()

        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in final.content]})
        for block in final.content:  # server-side tools (web search) already ran
            if block.type == "server_tool_use":
                yield {"type": "tool", "agent": agent, "name": block.name, "input": block.input,
                       "error": False, "writes": False}

        if final.stop_reason == "pause_turn":  # server tool needs more time; just resume
            continue
        if final.stop_reason != "tool_use":
            return "".join(b.text for b in final.content if b.type == "text")

        # Whatever happens below (error, Stop, client disconnect), every tool_use gets a tool_result.
        calls = [b for b in final.content if b.type == "tool_use"]
        results: dict[str, dict] = {}
        try:
            for block in calls:
                if cancel.is_set():
                    raise Cancelled()
                output, is_error = yield from dispatch(block.name, block.input)
                results[block.id] = {"type": "tool_result", "tool_use_id": block.id,
                                     "content": output, "is_error": is_error}
        finally:
            messages.append({"role": "user", "content": [
                results.get(b.id) or {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                      "content": "Not run: the request was stopped or failed."}
                for b in calls]})
    return "(stopped: turn limit reached)"


def local_dispatch(agent: str) -> Dispatch:
    """Dispatch for plain file tools."""
    def dispatch(name, args):
        output, is_error = run_tool(name, args)
        yield {"type": "tool", "agent": agent, "name": name, "input": args, "error": is_error,
               "writes": name in WRITE_TOOLS and not is_error}
        return output, is_error
    return dispatch


def chat(session_id: str, user_text: str) -> Iterator[dict]:
    messages = _sessions.setdefault(session_id, [])
    _repair(messages)  # self-heal a history left broken by an earlier failure
    _cancel_event(session_id).clear()
    messages.append({"role": "user", "content": user_text})
    local = local_dispatch("pm")

    def dispatch(name, args):
        if name in subagents.PM_TOOL_NAMES:
            return (yield from subagents.handle(name, args, session_id))
        return (yield from local(name, args))

    # Pass the reader itself: the newest prompt is loaded before every model call
    try:
        yield from run_loop(lambda: storage.read_prompt() + org.context_text(), lambda: TOOLS + subagents.pm_tools(), messages, dispatch, "pm",
                            max_turns=30, session_id=session_id)
    except Cancelled:
        yield {"type": "stopped"}


def strip_thinking() -> None:
    """After switching models, drop thinking blocks from saved chats: they only belong to the model that wrote them."""
    for messages in _sessions.values():
        for m in messages:
            if m["role"] == "assistant":
                m["content"] = [b for b in m["content"] if b.get("type") not in ("thinking", "redacted_thinking")]
