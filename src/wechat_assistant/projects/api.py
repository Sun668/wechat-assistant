"""Loopback operator API for project registration and reconciliation."""

import secrets
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Registration(Request):
    project_id: str
    name: str
    source_path: str


class ProjectName(Request):
    name: str


class Reconciliation(Request):
    outcome: Literal["not_started", "resume_bound", "discard"]


class SendResolution(Request):
    outcome: Literal["sent", "not_sent"]


def create_app(runtime, background=True):
    @asynccontextmanager
    async def lifespan(app):
        if background:
            await runtime.start()
        try:
            yield
        finally:
            if background:
                await runtime.stop()

    auth = HTTPBearer(auto_error=False)

    def operator(credentials: HTTPAuthorizationCredentials | None = Depends(auth)):
        if not credentials or not secrets.compare_digest(
            credentials.credentials, runtime.api_token
        ):
            raise HTTPException(401, "需要操作者令牌")

    app = FastAPI(
        title="ClawBot Projects", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/health", dependencies=[Depends(operator)])
    def health():
        return runtime.health()

    @app.post("/schedule-fire/{plan_id}")
    def schedule_fire(plan_id: str, trigger_token: str | None = Header(None, alias="X-Schedule-Token")):
        try:
            return {"status": runtime.schedules.fire_authorized(plan_id, trigger_token)}
        except ValueError:
            raise HTTPException(401, "定时触发器凭据无效") from None

    @app.get("/projects", dependencies=[Depends(operator)])
    def projects():
        return runtime.router.available_projects()

    @app.post("/projects", dependencies=[Depends(operator)])
    def register(request: Registration):
        try:
            uuid.UUID(request.project_id)
        except ValueError:
            raise ValueError("仅接受客户端本地项目 ID") from None
        runtime.store.register(request.project_id, request.name, request.source_path)
        return {"status": "registered"}

    @app.post("/projects/{project_id}/name", dependencies=[Depends(operator)])
    def rename(project_id: str, request: ProjectName):
        runtime.store.rename(project_id, request.name)
        return {"status": "renamed"}

    @app.get("/projects/{project_id}/jobs", dependencies=[Depends(operator)])
    def jobs(project_id: str):
        return runtime.store.rows(
            "SELECT id,task_id,kind,state,error FROM jobs WHERE project_id=? ORDER BY seq",
            (project_id,),
        )

    @app.post("/projects/{project_id}/jobs/{job_id}/reconcile", dependencies=[Depends(operator)])
    def reconcile(project_id: str, job_id: str, request: Reconciliation):
        runtime.bridge.reconcile(project_id, job_id, request.outcome)
        return {"status": "reconciled"}

    @app.post("/outbox/{outbox_id}/resolve", dependencies=[Depends(operator)])
    def reconcile_send(outbox_id: int, request: SendResolution):
        runtime.reconcile_outbox(outbox_id, request.outcome)
        return {"status": "reconciled"}

    @app.get("/outbox", dependencies=[Depends(operator)])
    def outbox():
        return runtime.store.rows(
            "SELECT id,project_id,kind,state FROM outbox ORDER BY id DESC LIMIT 100"
        )

    return app
