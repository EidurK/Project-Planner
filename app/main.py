import json
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from . import agent, org, questions, settings, storage, subagents  # noqa: E402  (after load_dotenv so MODEL is picked up)

STATIC = Path(__file__).resolve().parent.parent / "static"
@asynccontextmanager
async def lifespan(_app):
    storage.seed_defaults()  # first run: create the starter prompt, agent prompts and empty project list
    yield


app = FastAPI(title="PM Agent", lifespan=lifespan)


class ChatIn(BaseModel):
    session_id: str
    message: str


class PromptIn(BaseModel):
    content: str
    agent: str | None = None  # None = the PM's own prompt


class SettingsIn(BaseModel):
    model: str


class OrgIn(BaseModel):
    content: str


class EmployeeIn(BaseModel):
    name: str
    position: str


class EmployeeUpdate(BaseModel):
    new_name: str | None = None
    position: str | None = None


class ApiKeyIn(BaseModel):
    key: str


class AnswerIn(BaseModel):
    question_id: str
    answer: str


class SessionIn(BaseModel):
    session_id: str


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _friendly(e: Exception) -> str:
    if isinstance(e, anthropic.AuthenticationError) or "Could not resolve authentication" in str(e):
        return "No valid API key. Add one in the Settings tab."
    return f"{type(e).__name__}: {e}"


@app.post("/api/chat")
def chat(body: ChatIn):
    def gen():
        try:
            for event in agent.chat(body.session_id, body.message):
                yield _sse(event)
        except Exception as e:  # surface API errors in the UI
            yield _sse({"type": "error", "text": _friendly(e)})
        yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/answer")
def answer(body: AnswerIn):
    if not questions.answer(body.question_id, body.answer):
        raise HTTPException(404, "No such pending question")
    return {"ok": True}


@app.post("/api/stop")
def stop(body: SessionIn):
    agent.stop(body.session_id)
    return {"ok": True}


@app.post("/api/reset")
def reset(body: SessionIn):
    agent.reset(body.session_id)
    return {"ok": True}


@app.get("/api/projects")
def projects():
    return storage.list_projects()


@app.get("/api/overview")
def overview():
    return org.overview()


@app.get("/api/organization")
def get_organization():
    return {"content": org.read_organization()}


@app.put("/api/organization")
def put_organization(body: OrgIn):
    try:
        return {"backup": org.write_organization(body.content)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/employees")
def add_employee(body: EmployeeIn):
    try:
        return org.add_employee(body.name, body.position)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/employees/{name}")
def update_employee(name: str, body: EmployeeUpdate):
    try:
        return org.update_employee(name, body.new_name, body.position)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/employees/{name}")
def remove_employee(name: str):
    try:
        return org.remove_employee(name)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/settings")
def get_settings():
    return settings.public()


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    try:
        if settings.set_model(body.model):
            agent.strip_thinking()
    except ValueError as e:
        raise HTTPException(400, str(e))
    return settings.public()


@app.put("/api/settings/api-key")
def put_api_key(body: ApiKeyIn):
    try:
        warning = settings.set_api_key(body.key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": settings.api_key_status(), "warning": warning}


@app.delete("/api/settings/api-key")
def delete_api_key():
    settings.clear_api_key()
    return {"status": settings.api_key_status()}


@app.get("/api/agents")
def list_agents():
    return subagents.list_agents()


@app.get("/api/prompt")
def get_prompt(agent: str | None = None):
    try:
        if agent:
            return {"content": subagents.read_prompt(agent), "versions": subagents.list_versions(agent)}
        return {"content": storage.read_prompt(), "versions": storage.list_prompt_versions()}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.put("/api/prompt")
def put_prompt(body: PromptIn):
    try:
        if body.agent:
            return {"backup": subagents.write_prompt(body.agent, body.content)}
        return {"backup": storage.write_prompt(body.content)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/prompt/versions/{name}")
def get_version(name: str, agent: str | None = None):
    try:
        if agent:
            return {"content": subagents.read_version(agent, name)}
        return {"content": storage.read_prompt_version(name)}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
