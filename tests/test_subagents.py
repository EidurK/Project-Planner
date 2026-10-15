"""Drives the PM -> subagents flow with a scripted fake Anthropic client."""
import threading
from types import SimpleNamespace as NS

import pytest

from app import agent, questions, storage


class Block(NS):
    def model_dump(self, exclude_none=True):
        return dict(self.__dict__)


def text(t):
    return Block(type="text", text=t)


def tool_use(id, tool, **inp):
    return Block(type="tool_use", id=id, name=tool, input=inp)


class FakeClient:
    """Each stream() call pops the next scripted reply for the given system-prompt marker."""
    def __init__(self, script):
        self.script = script  # {marker: [ (blocks, stop_reason), ... ]}
        self.messages = self
        self.calls = []
        self.on_event = lambda: None

    def stream(self, *, system, tools, messages, **kw):
        marker = next(m for m in self.script if m in system)
        self.calls.append((marker, [t["name"] for t in tools]))
        blocks, stop = self.script[marker].pop(0)
        fake = NS(content=blocks, stop_reason=stop)
        return _Ctx(_Stream(blocks, fake, self.on_event))


class _Stream:
    def __init__(self, blocks, final, on_event):
        self.blocks, self.final, self.on_event = blocks, final, on_event

    def __iter__(self):
        yield NS(type="message_start")  # non-text events must be tolerated
        for b in self.blocks:
            self.on_event()
            if b.type == "text":
                yield NS(type="content_block_delta", delta=NS(type="text_delta", text=b.text))

    def get_final_message(self):
        return self.final


class _Ctx:
    def __init__(self, v): self.v = v
    def __enter__(self): return self.v
    def __exit__(self, *a): return False


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    (tmp_path / "agents").mkdir()
    (tmp_path / "prompt.md").write_text("PM-PROMPT")
    (tmp_path / "projects.json").write_text('{"projects": []}')
    for n, marker in [("planner", "PLANNER-PROMPT"), ("gatherer", "GATHERER-PROMPT"), ("reviewer", "REVIEWER-PROMPT")]:
        (tmp_path / "agents" / f"{n}.md").write_text(marker)
    agent._sessions.clear()


def test_planner_and_reviewer_flow(monkeypatch):
    fake = FakeClient({
        "PM-PROMPT": [
            ([tool_use("a", "run_project_planner", task="plan it")], "tool_use"),
            ([tool_use("b", "run_reviewer", project_ids=["p1"])], "tool_use"),
            ([text("All done")], "end_turn"),
        ],
        "PLANNER-PROMPT": [
            ([tool_use("c", "add_project", name="Top", estimated_hours=10)], "tool_use"),
            ([tool_use("d", "add_project", name="Sub", parent_id="p1", estimated_hours=10, depends_on=[])], "tool_use"),
            ([text("created p1,p2")], "end_turn"),
        ],
        "REVIEWER-PROMPT": [([text("VERDICT: APPROVE")], "end_turn")],
    })
    monkeypatch.setattr(agent, "_client", fake)
    events = list(agent.chat("s", "plan a thing"))
    assert [p["name"] for p in storage.list_projects()] == ["Top", "Sub"]
    assert [e["name"] for e in events if e["type"] == "subagent" and e["status"] == "start"] == [
        "project planner", "reviewer"]
    assert events[-1] == {"type": "text", "text": "All done"}
    # reviewer is read-only
    reviewer_tools = next(t for m, t in fake.calls if m == "REVIEWER-PROMPT")
    assert "add_project" not in reviewer_tools and "check_plan" in reviewer_tools
    # subagent results fed back to the PM
    pm_msgs = agent._sessions["s"]
    assert "VERDICT: APPROVE" in str(pm_msgs)


def test_gatherer_asks_user(monkeypatch):
    fake = FakeClient({
        "PM-PROMPT": [([tool_use("a", "run_information_gatherer", task="x")], "tool_use"),
                      ([text("ok")], "end_turn")],
        "GATHERER-PROMPT": [([tool_use("q", "ask_user", question="How many people?")], "tool_use"),
                            ([text("report: 3 people")], "end_turn")],
    })
    monkeypatch.setattr(agent, "_client", fake)
    gen = agent.chat("s2", "go")
    seen = []
    for ev in gen:
        seen.append(ev)
        if ev["type"] == "question":
            threading.Timer(0.05, questions.answer, args=(ev["id"], "three")).start()
    assert any(e["type"] == "question" and e["question"] == "How many people?" for e in seen)
    assert "report: 3 people" in str(agent._sessions["s2"])


def test_subagent_error_does_not_kill_chat(monkeypatch):
    fake = FakeClient({
        "PM-PROMPT": [([tool_use("a", "run_reviewer", project_ids=["p1"])], "tool_use"), ([text("fine")], "end_turn")],
        "REVIEWER-PROMPT": [],  # pop from empty list -> IndexError inside the subagent
    })
    monkeypatch.setattr(agent, "_client", fake)
    events = list(agent.chat("s3", "go"))
    assert events[-1]["text"] == "fine"
    assert "Subagent failed" in str(agent._sessions["s3"])


def test_prompt_edit_applies_mid_turn(monkeypatch):
    fake = FakeClient({
        "PM-PROMPT": [([tool_use("a", "update_prompt", new_content="FRESH-INSTRUCTIONS", reason="r")], "tool_use")],
        "FRESH-INSTRUCTIONS": [([text("done")], "end_turn")],
    })
    monkeypatch.setattr(agent, "_client", fake)
    list(agent.chat("s4", "improve yourself"))
    assert [m for m, _ in fake.calls] == ["PM-PROMPT", "FRESH-INSTRUCTIONS"]


def _unmatched(messages):
    """Return tool_use ids lacking a tool_result in the following message."""
    bad = []
    for i, m in enumerate(messages):
        if m["role"] != "assistant":
            continue
        ids = {b["id"] for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"}
        nxt = messages[i + 1]["content"] if i + 1 < len(messages) else []
        got = {b["tool_use_id"] for b in nxt if isinstance(b, dict) and b.get("type") == "tool_result"}
        bad += sorted(ids - got)
    return bad


def test_stop_mid_tools_keeps_history_valid(monkeypatch):
    calls = [tool_use(str(i), "add_project", name=f"P{i}") for i in range(5)]
    fake = FakeClient({"PM-PROMPT": [(calls, "tool_use"), ([text("after")], "end_turn")]})
    monkeypatch.setattr(agent, "_client", fake)
    real = storage.add_project
    n = {"c": 0}

    def add_then_stop(fields):
        n["c"] += 1
        if n["c"] == 2:
            agent.stop("s5")  # user presses Stop while tools are running
        return real(fields)

    monkeypatch.setattr(storage, "add_project", add_then_stop)
    events = list(agent.chat("s5", "go"))
    assert events[-1] == {"type": "stopped"}
    assert len(storage.list_projects()) == 2  # remaining calls did not run
    assert _unmatched(agent._sessions["s5"]) == []
    # the session still works afterwards
    monkeypatch.setattr(storage, "add_project", real)
    assert list(agent.chat("s5", "continue"))[-1] == {"type": "text", "text": "after"}


def test_stop_while_streaming(monkeypatch):
    fake = FakeClient({"PM-PROMPT": [([text("thinking...")], "end_turn")]})
    fake.on_event = lambda: agent.stop("s6")  # stop arrives mid-stream
    monkeypatch.setattr(agent, "_client", fake)
    assert list(agent.chat("s6", "go"))[-1] == {"type": "stopped"}


def test_crash_in_dispatch_keeps_history_valid(monkeypatch):
    fake = FakeClient({"PM-PROMPT": [([tool_use("a", "list_projects"), tool_use("b", "list_projects")], "tool_use")]})
    monkeypatch.setattr(agent, "_client", fake)
    boom = iter([RuntimeError("boom")])

    def explode(*a, **k):
        raise next(boom)

    monkeypatch.setattr(agent, "local_dispatch", lambda who: explode)
    with pytest.raises(RuntimeError):
        list(agent.chat("s7", "go"))
    assert _unmatched(agent._sessions["s7"]) == []


def test_poisoned_history_self_heals(monkeypatch):
    agent._sessions["s8"] = [{"role": "user", "content": "x"},
                             {"role": "assistant", "content": [{"type": "tool_use", "id": "z", "name": "t", "input": {}}]}]
    fake = FakeClient({"PM-PROMPT": [([text("ok")], "end_turn")]})
    monkeypatch.setattr(agent, "_client", fake)
    list(agent.chat("s8", "hi"))
    assert _unmatched(agent._sessions["s8"]) == []


from app import subagents  # noqa: E402


def test_edit_builtin_subagent_prompt_applies_immediately(monkeypatch, data):
    fake = FakeClient({
        "PM-PROMPT": [
            ([tool_use("a", "update_subagent_prompt", agent="reviewer", new_content="STRICTER-REVIEWER", reason="r")], "tool_use"),
            ([tool_use("b", "run_reviewer", project_ids=["p1"])], "tool_use"),
            ([text("ok")], "end_turn"),
        ],
        "STRICTER-REVIEWER": [([text("VERDICT: APPROVE")], "end_turn")],
    })
    monkeypatch.setattr(agent, "_client", fake)
    events = list(agent.chat("e1", "tune reviewer"))
    assert ("STRICTER-REVIEWER", ["list_projects", "get_project", "check_plan"]) in fake.calls
    assert any(e.get("name") == "update_subagent_prompt" and e["writes"] for e in events if e["type"] == "tool")
    assert len(subagents.list_versions("reviewer")) == 1  # old prompt backed up
    assert subagents.read_prompt("reviewer") == "STRICTER-REVIEWER"


def test_create_run_and_delete_custom_subagent(monkeypatch, data):
    fake = FakeClient({
        "PM-PROMPT": [
            ([tool_use("a", "create_subagent", name="risk_analyst", description="finds risks",
                       prompt="RISK-PROMPT", tools=["list_projects", "check_plan"])], "tool_use"),
            ([tool_use("b", "run_subagent", agent="risk_analyst", task="look")], "tool_use"),
            ([tool_use("c", "delete_subagent", name="risk_analyst")], "tool_use"),
            ([text("done")], "end_turn"),
        ],
        "RISK-PROMPT": [([text("risks: none")], "end_turn")],
    })
    monkeypatch.setattr(agent, "_client", fake)
    events = list(agent.chat("e2", "make a risk analyst"))
    assert ("RISK-PROMPT", ["list_projects", "check_plan"]) in fake.calls
    # run_subagent only appears in the PM's tools once a custom agent exists
    pm_calls = [t for m, t in fake.calls if m == "PM-PROMPT"]
    assert "run_subagent" not in pm_calls[0] and "run_subagent" in pm_calls[1]
    assert "risks: none" in str(agent._sessions["e2"])
    assert "risk_analyst" not in subagents.agents()
    assert not (storage.DATA_DIR / "agents" / "risk_analyst.md").exists()
    assert events[-1] == {"type": "text", "text": "done"}


@pytest.mark.parametrize("args", [
    {"name": "BAD NAME", "description": "d", "prompt": "p"},
    {"name": "reviewer", "description": "d", "prompt": "p"},
    {"name": "sneaky", "description": "d", "prompt": "p", "tools": ["delete_project"]},
    {"name": "sneaky", "description": "d", "prompt": "p", "tools": ["update_prompt"]},
    {"name": "sneaky", "description": "", "prompt": "p"},
    {"name": "sneaky", "description": "d", "prompt": "  "},
    {"name": "sneaky", "description": "d", "prompt": "p", "max_turns": 500},
])
def test_create_subagent_validation(args, data):
    with pytest.raises(ValueError):
        subagents._manage("create_subagent", args)
    assert "sneaky" not in subagents.agents()


def test_cannot_delete_builtin_or_edit_unknown(data):
    with pytest.raises(ValueError):
        subagents._manage("delete_subagent", {"name": "reviewer"})
    with pytest.raises(ValueError):
        subagents._manage("update_subagent_prompt", {"agent": "../prompt", "new_content": "x", "reason": "r"})
