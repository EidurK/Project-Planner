import pytest
from fastapi.testclient import TestClient

from app import org, storage, tools
from app.main import app


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    (tmp_path / "projects.json").write_text('{"projects": []}')
    return tmp_path


def test_employee_crud_and_validation():
    org.add_employee("Sam", "Logistics lead")
    with pytest.raises(ValueError):
        org.add_employee("sam", "dup")
    with pytest.raises(ValueError):
        org.add_employee("", "x")
    org.update_employee("SAM", position="Factory lead")
    assert org.list_employees() == [{"name": "Sam", "position": "Factory lead"}]
    with pytest.raises(ValueError):
        org.update_employee("nobody", position="x")


def test_rename_updates_assignments_and_remove_reports_work():
    org.add_employee("Sam", "Lead")
    org.add_employee("Alex", "Builder")
    storage.add_project({"name": "Mine", "assignees": ["Sam", "Alex"], "status": "in_progress"})
    storage.add_project({"name": "Old", "assignees": ["Sam"], "status": "done"})
    org.update_employee("Sam", new_name="Samuel")
    assert storage.get_project("p1")["assignees"] == ["Samuel", "Alex"]
    with pytest.raises(ValueError):
        org.update_employee("Samuel", new_name="alex")
    out = org.remove_employee("Samuel")
    assert out["still_assigned_to_unfinished"] == ["Mine"]  # done work not reported
    assert storage.get_project("p1")["assignees"] == ["Samuel", "Alex"]  # assignments untouched


def test_overview():
    org.add_employee("Sam", "Lead")
    org.add_employee("Idle", "Intern")
    storage.add_project({"name": "Parent"})
    storage.add_project({"name": "Task", "parent_id": "p1", "assignees": ["sam"], "estimated_hours": 10,
                         "hours_logged": 4, "status": "in_progress"})
    storage.add_project({"name": "Done one", "assignees": ["Sam"], "estimated_hours": 5, "status": "done"})
    storage.add_project({"name": "Ghost work", "assignees": ["Stranger"], "estimated_hours": 3})
    o = org.overview()
    sam = next(e for e in o["employees"] if e["name"] == "Sam")
    assert [p["name"] for p in sam["projects"]] == ["Task", "Done one"]  # active first
    assert sam["projects"][0]["parent"] == "Parent" and sam["remaining_hours"] == 6
    assert next(e for e in o["employees"] if e["name"] == "Idle")["projects"] == []
    assert [u["name"] for u in o["unlisted"]] == ["Stranger"]


def test_organization_file_backup_and_context(data):
    assert "not filled in" in org.context_text()
    org.write_organization("We build factories in Satisfactory.")
    backup = org.write_organization("We build big factories.")
    assert (data / "history" / backup).read_text() == "We build factories in Satisfactory."
    org.add_employee("Sam", "Lead")
    ctx = org.context_text()
    assert "We build big factories." in ctx and "- Sam — Lead" in ctx
    with pytest.raises(ValueError):
        org.write_organization("   ")


def test_tools_and_api(data):
    out, err = tools.run_tool("add_employee", {"name": "Sam", "position": "Lead"})
    assert not err
    assert tools.run_tool("add_employee", {"name": "Sam", "position": "x"})[1]
    assert not tools.run_tool("update_organization", {"new_content": "# Org", "reason": "r"})[1]
    assert tools.run_tool("read_organization", {})[0] == "# Org"
    c = TestClient(app)
    assert c.post("/api/employees", json={"name": "Alex", "position": "Builder"}).status_code == 200
    assert c.post("/api/employees", json={"name": "alex", "position": "x"}).status_code == 400
    assert [e["name"] for e in c.get("/api/overview").json()["employees"]] == ["Sam", "Alex"]
    assert c.delete("/api/employees/Alex").status_code == 200
    assert c.delete("/api/employees/Alex").status_code == 404
    assert c.put("/api/organization", json={"content": "x"}).status_code == 200


def test_context_reaches_pm_and_subagents(monkeypatch, data):
    from app import agent
    seen = []

    class C:
        def __init__(self): self.messages = self
        def stream(self, *, system, **kw):
            seen.append(system)
            raise RuntimeError("stop")

    (data / "prompt.md").write_text("PM")
    (data / "agents").mkdir()
    (data / "agents" / "reviewer.md").write_text("REV")
    org.add_employee("Sam", "Lead")
    monkeypatch.setattr(agent, "_client", C())
    with pytest.raises(RuntimeError):
        list(agent.chat("o", "hi"))
    from app import subagents
    list(subagents.run("reviewer", {"task": "x"}, "o"))  # subagent failure is reported, not raised
    assert all("- Sam — Lead" in s for s in seen) and len(seen) == 2
