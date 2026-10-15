"""Subagents the PM can run, edit and create.

Built in: information gatherer, project planner, reviewer. The PM can also create custom subagents
(stored in data/agents/registry.json). Every subagent has a prompt file in data/agents/ and runs its own
Claude loop with a restricted tool set.
"""
import json
import re
from datetime import date

from . import org, questions, settings, storage
from .tools import TOOLS

WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 8}
# Haiku 4.5 only supports the basic web search tool (no dynamic filtering)
WEB_SEARCH_BASIC = {**WEB_SEARCH, "type": "web_search_20250305"}

ASK_USER = {
    "name": "ask_user",
    "description": "Ask the user questions in the chat UI and wait for their reply. Group related questions "
    "into one call. Returns the user's answer text.",
    "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
}

# Tools a subagent may be given. Deliberately excludes deleting projects, editing prompts and running subagents.
ALLOWED_TOOLS = ["list_projects", "get_project", "add_project", "update_project", "check_plan", "web_search", "ask_user"]

BUILTIN = {
    "information_gatherer": {
        "label": "information gatherer", "file": "gatherer.md", "max_turns": 12,
        "tools": ["list_projects", "get_project", "ask_user", "web_search"],
        "description": "Researches the web and asks the user questions about uncertain matters before planning.",
    },
    "project_planner": {
        "label": "project planner", "file": "planner.md", "max_turns": 40,
        "tools": ["list_projects", "get_project", "add_project", "update_project", "check_plan"],
        "description": "Splits a project into subtasks, plans prerequisite projects, writes them to the project file.",
    },
    "reviewer": {
        "label": "reviewer", "file": "reviewer.md", "max_turns": 12,
        "tools": ["list_projects", "get_project", "check_plan"],
        "description": "Read-only review of the planner's output; returns VERDICT: APPROVE or REVISE.",
    },
}
RUN_TOOLS = {f"run_{k}": k for k in BUILTIN}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,30}$")
RESERVED = set(BUILTIN) | {"pm", "atlas", "main"}
REGISTRY = "agents/registry.json"


# ---- registry ----

def _custom() -> dict:
    raw = storage.read_text(REGISTRY)
    return json.loads(raw).get("agents", {}) if raw else {}


def _save_custom(agents: dict) -> None:
    storage.write_text(REGISTRY, json.dumps({"agents": agents}, indent=2) + "\n")


def agents() -> dict[str, dict]:
    """All subagents by id, built-in first. Re-read on every call so new/edited agents apply at once."""
    out = {k: {**v, "id": k, "builtin": True} for k, v in BUILTIN.items()}
    for k, v in _custom().items():
        out[k] = {"label": k.replace("_", " "), "file": f"{k}.md", "max_turns": 15, **v, "id": k, "builtin": False}
    return out


def _spec(agent_id: str) -> dict:
    spec = agents().get(agent_id)
    if not spec:
        raise ValueError(f"Unknown subagent '{agent_id}'. Known: {', '.join(agents())}")
    return spec


def _rel(spec: dict) -> str:
    return f"agents/{spec['file']}"


def _prefix(agent_id: str) -> str:
    return f"agent-{agent_id}"


def _resolve_tools(names: list[str]) -> list[dict]:
    search = WEB_SEARCH_BASIC if settings.get_model().startswith("claude-haiku") else WEB_SEARCH
    by_name = {t["name"]: t for t in TOOLS} | {"ask_user": ASK_USER, "web_search": search}
    return [by_name[n] for n in names if n in by_name]


# ---- prompt access (shared with the web API) ----

def list_agents() -> list[dict]:
    return [{k: a[k] for k in ("id", "label", "builtin", "description", "tools")} for a in agents().values()]


def read_prompt(agent_id: str) -> str:
    return storage.read_text(_rel(_spec(agent_id)))


def write_prompt(agent_id: str, content: str) -> str:
    return storage.write_text_with_backup(_rel(_spec(agent_id)), content, _prefix(agent_id))


def list_versions(agent_id: str) -> list[str]:
    _spec(agent_id)
    return storage.list_backups(_prefix(agent_id))


def read_version(agent_id: str, name: str) -> str:
    _spec(agent_id)
    return storage.read_backup(_prefix(agent_id), name)


# ---- tools the PM sees ----

_TASK = {"type": "string", "description": "Self-contained description; the subagent sees nothing else of the chat"}
_STATIC_PM_TOOLS = [
    {
        "name": "run_information_gatherer",
        "description": "Subagent that researches the web and asks the user questions about uncertain matters. "
        "Run BEFORE planning. Returns a report of facts, assumptions and open risks.",
        "input_schema": {"type": "object", "properties": {"task": _TASK}, "required": ["task"]},
    },
    {
        "name": "run_project_planner",
        "description": "Subagent that splits a project into subtasks, plans missing prerequisite projects, and "
        "writes everything into the project file. Returns a summary of what it wrote.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": _TASK,
                "project_id": {"type": "string", "description": "Existing project to plan; omit to create a new one"},
                "context": {"type": "string", "description": "Gatherer report and/or reviewer feedback to address"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "run_reviewer",
        "description": "Read-only subagent that reviews the planner's output. Returns 'VERDICT: APPROVE' or "
        "'VERDICT: REVISE' followed by numbered issues.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_ids": {"type": "array", "items": {"type": "string"},
                                "description": "The project and any prerequisite/subtask ids to review"},
                "focus": {"type": "string"},
            },
            "required": ["project_ids"],
        },
    },
    {
        "name": "list_subagents",
        "description": "List all subagents (built-in and custom) with their descriptions and tools.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_subagent_prompt",
        "description": "Read a subagent's prompt.",
        "input_schema": {"type": "object", "properties": {"agent": {"type": "string"}}, "required": ["agent"]},
    },
    {
        "name": "update_subagent_prompt",
        "description": "Replace a subagent's prompt with new full content (previous version is backed up). Takes effect "
        "the next time that subagent calls the model. Read it first, make targeted changes, and tell the user what "
        "you changed.",
        "input_schema": {
            "type": "object",
            "properties": {"agent": {"type": "string"}, "new_content": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["agent", "new_content", "reason"],
        },
    },
    {
        "name": "create_subagent",
        "description": "Create a new custom subagent. ONLY when the user asks for one. It gets only the tools you list "
        f"(allowed: {', '.join(ALLOWED_TOOLS)}); it cannot delete projects, edit prompts or run other subagents. "
        "Run it afterwards with run_subagent. Tell the user what you created.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "lowercase letters/digits/underscores, 3-31 chars, e.g. risk_analyst"},
                "description": {"type": "string", "description": "One line: what it does and when to use it"},
                "prompt": {"type": "string", "description": "Its system prompt: role, method, and output format"},
                "tools": {"type": "array", "items": {"type": "string", "enum": ALLOWED_TOOLS}},
                "max_turns": {"type": "integer", "description": "Model-call limit per run (default 15, max 40)"},
            },
            "required": ["name", "description", "prompt"],
        },
    },
    {
        "name": "delete_subagent",
        "description": "Delete a custom subagent (built-in ones can't be deleted). Its prompt is kept in history. "
        "Only when the user asks.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
]
MANAGE_TOOLS = {"list_subagents", "read_subagent_prompt", "update_subagent_prompt", "create_subagent", "delete_subagent"}
PM_TOOL_NAMES = set(RUN_TOOLS) | MANAGE_TOOLS | {"run_subagent"}


def pm_tools() -> list[dict]:
    """Built fresh before each model call so newly created subagents are immediately runnable."""
    custom = {k: v for k, v in agents().items() if not v["builtin"]}
    tools = list(_STATIC_PM_TOOLS)
    if custom:
        listing = "; ".join(f"{k}: {v.get('description', '')}" for k, v in custom.items())
        tools.append({
            "name": "run_subagent",
            "description": f"Run a custom subagent. Available: {listing}",
            "input_schema": {
                "type": "object",
                "properties": {"agent": {"type": "string", "enum": list(custom)}, "task": _TASK},
                "required": ["agent", "task"],
            },
        })
    return tools


def _manage(name: str, args: dict) -> object:
    if name == "list_subagents":
        return list_agents()
    if name == "read_subagent_prompt":
        return read_prompt(args["agent"])
    if name == "update_subagent_prompt":
        return {"ok": True, "backup": write_prompt(args["agent"], args["new_content"]), "reason": args.get("reason", "")}
    if name == "create_subagent":
        agent_id = str(args["name"]).strip()
        if not NAME_RE.match(agent_id):
            raise ValueError("name must match [a-z][a-z0-9_]{2,30}")
        if agent_id in RESERVED or agent_id in agents():
            raise ValueError(f"A subagent named '{agent_id}' already exists or the name is reserved")
        desc = str(args.get("description", "")).strip()
        if not desc:
            raise ValueError("description is required")
        tools = args.get("tools") or ["list_projects", "get_project"]
        bad = [t for t in tools if t not in ALLOWED_TOOLS]
        if bad:
            raise ValueError(f"Tools not allowed for subagents: {bad}. Allowed: {ALLOWED_TOOLS}")
        turns = args.get("max_turns", 15)
        if isinstance(turns, bool) or not isinstance(turns, int) or not 1 <= turns <= 40:
            raise ValueError("max_turns must be an integer 1-40")
        storage.write_text_with_backup(f"agents/{agent_id}.md", args.get("prompt", ""), _prefix(agent_id))
        custom = _custom()
        custom[agent_id] = {"description": desc, "tools": list(dict.fromkeys(tools)), "max_turns": turns}
        _save_custom(custom)
        return {"created": agent_id, "tools": custom[agent_id]["tools"]}
    if name == "delete_subagent":
        agent_id = args["name"]
        custom = _custom()
        if agent_id not in custom:
            raise ValueError("Only custom subagents can be deleted" if agent_id in BUILTIN else f"No custom subagent '{agent_id}'")
        stamp = date.today().isoformat()
        storage.write_text(f"history/{_prefix(agent_id)}-deleted-{stamp}.md", storage.read_text(f"agents/{agent_id}.md"))
        storage.delete_text(f"agents/{agent_id}.md")
        del custom[agent_id]
        _save_custom(custom)
        return {"deleted": agent_id}
    raise ValueError(f"Unknown tool {name}")


# ---- running ----

def _task_text(args: dict) -> str:
    parts = []
    if args.get("task"):
        parts.append(f"Task:\n{args['task']}")
    if args.get("project_id"):
        parts.append(f"Project id: {args['project_id']}")
    if args.get("project_ids"):
        parts.append("Project ids to review: " + ", ".join(args["project_ids"]))
    if args.get("context"):
        parts.append(f"Context:\n{args['context']}")
    if args.get("focus"):
        parts.append(f"Focus: {args['focus']}")
    return "\n\n".join(parts) or "(no details given)"


def handle(name: str, args: dict, session_id: str):
    """Generator for every PM tool that concerns subagents. Yields UI events, returns (text, is_error)."""
    if name in RUN_TOOLS:
        return (yield from run(RUN_TOOLS[name], args, session_id))
    if name == "run_subagent":
        return (yield from run(args.get("agent", ""), args, session_id))
    try:
        result = _manage(name, args)
        out = (result if isinstance(result, str) else json.dumps(result, indent=2)), False
    except (ValueError, KeyError) as e:
        out = f"Error: {e}", True
    yield {"type": "tool", "agent": "pm", "name": name, "input": args, "error": out[1],
           "writes": name in MANAGE_TOOLS - {"list_subagents", "read_subagent_prompt"} and not out[1]}
    return out


def run(agent_id: str, args: dict, session_id: str):
    """Generator: yields UI events, returns (final_text, is_error)."""
    from . import agent  # late import: agent imports this module

    try:
        spec = _spec(agent_id)
    except ValueError as e:
        return f"Error: {e}", True
    label = spec["label"]

    def system():  # re-read every model call so prompt edits apply immediately
        return read_prompt(agent_id) + org.context_text() + f"\n\nToday's date: {date.today().isoformat()}"

    messages = [{"role": "user", "content": _task_text(args)}]
    local = agent.local_dispatch(label)

    def dispatch(tool, targs):
        if tool == "ask_user":
            qid = questions.register(session_id)
            yield {"type": "question", "id": qid, "agent": label, "question": targs.get("question", "")}
            return questions.wait(qid), False
        return (yield from local(tool, targs))

    yield {"type": "subagent", "name": label, "status": "start"}
    try:
        text = yield from agent.run_loop(system, lambda: _resolve_tools(spec["tools"]), messages, dispatch, label,
                                         max_turns=spec["max_turns"], stream_text=False, session_id=session_id)
        result = (text or "(no output)", False)
    except agent.Cancelled:
        raise  # Stop aborts the whole chat turn, not just this subagent
    except Exception as e:  # report to the PM instead of killing the whole chat
        result = (f"Subagent failed: {type(e).__name__}: {e}", True)
    yield {"type": "subagent", "name": label, "status": "end", "writes": "add_project" in spec["tools"]}
    return result
