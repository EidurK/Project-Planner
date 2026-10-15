"""Tool schemas and dispatch for the agent. Only the known data files are reachable (no arbitrary paths)."""
import json

from . import org, storage

_PROJECT_PROPS = {
    "name": {"type": "string"},
    "description": {"type": "string"},
    "status": {"type": "string", "enum": list(storage.STATUSES)},
    "priority": {"type": "string", "enum": list(storage.PRIORITIES)},
    "assignees": {"type": "array", "items": {"type": "string"}, "description": "People working on it"},
    "estimated_hours": {"type": "number", "description": "Estimated total person-hours"},
    "hours_logged": {"type": "number", "description": "Person-hours spent so far"},
    "due_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
    "notes": {"type": "string"},
    "parent_id": {"type": ["string", "null"], "description": "Id of the project this is a subtask of"},
    "depends_on": {"type": "array", "items": {"type": "string"},
                   "description": "Ids of projects/tasks that must finish before this one can start"},
}

TOOLS = [
    {
        "name": "read_prompt",
        "description": "Read your own current system prompt.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_prompt",
        "description": "Replace your own system prompt with new full content (previous version is backed up). "
        "Takes effect on your very next step. Always tell the user what you changed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "new_content": {"type": "string", "description": "The complete new prompt"},
                "reason": {"type": "string", "description": "Why you are changing it"},
            },
            "required": ["new_content", "reason"],
        },
    },
    {
        "name": "read_organization",
        "description": "Read the organization/scope knowledge file (what the organization does, what is in scope for the team).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_organization",
        "description": "Replace the organization/scope file (markdown) with new full content; the previous version is "
        "backed up. Read it first and make targeted changes. Only record what the user told you or confirmed. "
        "Tell the user what you changed.",
        "input_schema": {
            "type": "object",
            "properties": {"new_content": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["new_content", "reason"],
        },
    },
    {
        "name": "list_employees",
        "description": "List employees (name, position).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_employee",
        "description": "Add an employee to the organization. Use the same name when assigning them to projects.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "position": {"type": "string"}},
            "required": ["name", "position"],
        },
    },
    {
        "name": "update_employee",
        "description": "Change an employee's position and/or rename them (a rename also updates their project assignments).",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Current name"},
                           "new_name": {"type": "string"}, "position": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "remove_employee",
        "description": "Remove an employee from the list (e.g. they left). Their project assignments are not changed; "
        "the result lists unfinished work still assigned to them so you can reassign it. Only when the user asks.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "list_projects",
        "description": "List projects, optionally filtered by status or assignee.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": list(storage.STATUSES)},
                "assignee": {"type": "string"},
            },
        },
    },
    {
        "name": "get_project",
        "description": "Get one project by id.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "add_project",
        "description": "Add a new project. Only name is required.",
        "input_schema": {"type": "object", "properties": _PROJECT_PROPS, "required": ["name"]},
    },
    {
        "name": "update_project",
        "description": "Update fields of an existing project; only provided fields change.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, **_PROJECT_PROPS},
            "required": ["id"],
        },
    },
    {
        "name": "check_plan",
        "description": "Run deterministic checks (missing estimates/assignees, dependency cycles, subtask hours vs "
        "parent estimate) on the given project ids, or on everything if none are given.",
        "input_schema": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}}},
    },
    {
        "name": "delete_project",
        "description": "Delete a redundant project (duplicate, superseded, or obsolete) by id. A backup of the "
        "project file is kept. Pass replaced_by (the surviving project's id) when removing a duplicate so "
        "dependencies and subtasks are moved to it. Tell the user what you deleted and why. If the project has "
        "hours logged or you are unsure it is redundant, ask the user first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "reason": {"type": "string", "description": "Why this project is redundant"},
                "replaced_by": {"type": "string", "description": "Id of the project that supersedes it, if any"},
            },
            "required": ["id", "reason"],
        },
    },
]

# Tools that change files (UI refreshes panels after these)
WRITE_TOOLS = {"update_prompt", "add_project", "update_project", "delete_project",
               "update_organization", "add_employee", "update_employee", "remove_employee"}


def run_tool(name: str, args: dict) -> tuple[str, bool]:
    """Execute a tool. Returns (result_text, is_error)."""
    try:
        if name == "read_prompt":
            result = storage.read_prompt()
        elif name == "update_prompt":
            backup = storage.write_prompt(args["new_content"])
            result = {"ok": True, "backup": backup, "reason": args.get("reason", "")}
        elif name == "read_organization":
            result = org.read_organization() or "(empty)"
        elif name == "update_organization":
            result = {"ok": True, "backup": org.write_organization(args["new_content"]), "reason": args.get("reason", "")}
        elif name == "list_employees":
            result = org.list_employees()
        elif name == "add_employee":
            result = org.add_employee(args["name"], args["position"])
        elif name == "update_employee":
            result = org.update_employee(args["name"], args.get("new_name"), args.get("position"))
        elif name == "remove_employee":
            result = org.remove_employee(args["name"])
        elif name == "list_projects":
            result = storage.list_projects(args.get("status"), args.get("assignee"))
        elif name == "get_project":
            result = storage.get_project(args["id"])
        elif name == "add_project":
            result = storage.add_project(args)
        elif name == "update_project":
            fields = {k: v for k, v in args.items() if k != "id"}
            result = storage.update_project(args["id"], fields)
        elif name == "check_plan":
            result = storage.check_plan(args.get("ids")) or "No problems found."
        elif name == "delete_project":
            result = {"deleted": storage.delete_project(args["id"], args.get("replaced_by")),
                      "reason": args.get("reason", "")}
        else:
            return f"Unknown tool: {name}", True
    except (ValueError, KeyError) as e:
        return f"Error: {e}", True
    except Exception as e:  # never let a tool bug break the agent loop
        return f"Unexpected error: {type(e).__name__}: {e}", True
    return (result if isinstance(result, str) else json.dumps(result, indent=2)), False
