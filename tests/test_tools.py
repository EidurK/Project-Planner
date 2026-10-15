import json

import pytest

from app import storage, tools


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    (tmp_path / "prompt.md").write_text("original prompt")
    (tmp_path / "projects.json").write_text('{"projects": []}')
    return tmp_path


def test_project_crud():
    p = storage.add_project({"name": "Conveyor", "estimated_hours": 120, "assignees": ["Sam"], "status": "in_progress"})
    assert p["id"] == "p1"
    storage.update_project("p1", {"hours_logged": 10})
    assert storage.get_project("p1")["hours_logged"] == 10
    assert len(storage.list_projects(assignee="sam")) == 1
    assert storage.list_projects(status="done") == []
    storage.delete_project("p1")
    assert storage.list_projects() == []


def test_ids_not_reused():
    storage.add_project({"name": "a"}); storage.add_project({"name": "b"})
    storage.delete_project("p1")
    assert storage.add_project({"name": "c"})["id"] == "p3"


@pytest.mark.parametrize("bad", [{"name": "x", "status": "weird"}, {"name": "x", "estimated_hours": -1},
                                 {"name": "x", "due_date": "tomorrow"}, {"name": ""}, {}])
def test_validation(bad):
    with pytest.raises(ValueError):
        storage.add_project(bad)


def test_prompt_backup(data_dir):
    backup = storage.write_prompt("new prompt")
    assert storage.read_prompt() == "new prompt"
    assert (data_dir / "history" / backup).read_text() == "original prompt"
    assert storage.list_prompt_versions() == [backup]
    with pytest.raises(ValueError):
        storage.write_prompt("  ")


def test_no_path_traversal():
    with pytest.raises(ValueError):
        storage.read_prompt_version("../projects.json")


def test_run_tool_errors_and_dispatch():
    out, err = tools.run_tool("add_project", {"name": "X"})
    assert not err and json.loads(out)["name"] == "X"
    assert tools.run_tool("update_project", {"id": "nope", "status": "done"})[1]
    assert tools.run_tool("bogus", {})[1]
    assert tools.run_tool("read_prompt", {})[0] == "original prompt"


def test_dependencies_and_hierarchy():
    a = storage.add_project({"name": "Power", "estimated_hours": 10})["id"]
    b = storage.add_project({"name": "Factory", "estimated_hours": 20, "depends_on": [a]})["id"]
    t = storage.add_project({"name": "Belt", "parent_id": b, "estimated_hours": 5})["id"]
    with pytest.raises(ValueError):
        storage.update_project(a, {"depends_on": [b]})  # cycle
    with pytest.raises(ValueError):
        storage.update_project(a, {"depends_on": [a]})
    with pytest.raises(ValueError):
        storage.add_project({"name": "x", "depends_on": ["p99"]})
    with pytest.raises(ValueError):
        storage.update_project(b, {"parent_id": t})  # parent cycle
    storage.delete_project(a)
    assert storage.get_project(b)["depends_on"] == []
    storage.delete_project(b)
    assert storage.get_project(t)["parent_id"] is None


def test_check_plan():
    p = storage.add_project({"name": "Big", "estimated_hours": 100})["id"]
    storage.add_project({"name": "Small", "parent_id": p, "estimated_hours": 10, "assignees": ["Sam"]})
    problems = storage.check_plan([p])
    assert any("nobody assigned" in x for x in problems)
    assert any("subtasks sum to 10" in x for x in problems)


def test_delete_with_replacement_and_backup(data_dir):
    keep = storage.add_project({"name": "Keep"})["id"]
    dup = storage.add_project({"name": "Dup"})["id"]
    dep = storage.add_project({"name": "Needs dup", "depends_on": [dup]})["id"]
    kid = storage.add_project({"name": "Kid", "parent_id": dup})["id"]
    out, err = tools.run_tool("delete_project", {"id": dup, "reason": "duplicate", "replaced_by": keep})
    assert not err
    assert storage.get_project(dep)["depends_on"] == [keep]
    assert storage.get_project(kid)["parent_id"] == keep
    assert len(list((data_dir / "history").glob("projects-*.json"))) == 1
    assert tools.run_tool("delete_project", {"id": keep, "reason": "x", "replaced_by": keep})[1]
    assert tools.run_tool("delete_project", {"id": "p99", "reason": "x"})[1]
