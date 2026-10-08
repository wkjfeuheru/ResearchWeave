"""FastAPI app for a local, single-user financial research workspace."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from openharness.api.client import ApiMessageCompleteEvent, ApiMessageRequest
from openharness.auth.manager import AuthManager
from openharness.engine.messages import ConversationMessage
from openharness.research.store import ResearchError, ResearchStore
from openharness.runtime import _resolve_api_client_from_settings
from openharness.web.catalog import (
    models_list,
    profile_settings,
    save_model,
    skills_list,
    toggle_skill,
)
from openharness.web.models import ModelInput, SessionInput, SkillToggle, SocketRequest
from openharness.web.runtime import BrowserConnection, Redactor, session_view
from openharness.web.storage import WebSessionBackend
from openharness.utils.research_documents import MAX_DOCUMENT_BYTES
from openharness.utils.session_files import SessionFiles

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class AttachmentBodyLimit:
    """Bound multipart body bytes before Starlette spools uploads to disk."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or not scope["path"].endswith("/attachments"):
            return await self.app(scope, receive, send)
        limit = 10 * MAX_DOCUMENT_BYTES + 1024 * 1024
        seen = 0

        async def bounded_receive():
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > limit:
                raise HTTPException(413, "附件请求超过大小限制")
            return message

        return await self.app(scope, bounded_receive, send)


def local_host(value: str) -> bool:
    try:
        return urlsplit(f"//{value}").hostname in LOCAL_HOSTS
    except ValueError:
        return False


def allowed_origin(origin: str | None, host: str) -> bool:
    if origin is None:
        return True  # Non-browser HTTP clients; WebSocket requires Origin below.
    try:
        url = urlsplit(origin)
        return (
            url.scheme in {"http", "https"}
            and url.hostname in LOCAL_HOSTS
            and (url.netloc == host or url.port == 5173)
        )
    except ValueError:
        return False


class Workspace:
    def __init__(self, cwd: str):
        self.cwd = str(Path(cwd).resolve())
        self.store = WebSessionBackend(self.cwd)
        self.lock = asyncio.Lock()
        self.connections: dict[str, BrowserConnection] = {}
        self.deleting: set[str] = set()
        self.file_operations: set[str] = set()

    def record(self, session_id: str) -> dict:
        try:
            if session_id in self.deleting or not self.store._path(session_id).is_file():
                raise HTTPException(404, "会话不存在")
            connection = self.connections.get(session_id)
            idle = connection is None or connection.task is None or connection.task.done()
            if idle:
                ResearchStore(self.cwd, session_id).recover_pending_steers()
                ResearchStore(self.cwd, session_id).recover_investigations()
            record = self.store.load_by_id(self.cwd, session_id)
            if record is not None:
                # Acceptance is authoritative even if the process stopped
                # before the connection could save its display projection.
                pending = ResearchStore(self.cwd, session_id).load().pending_steers
                rows = list(session_view(record)["messages"])
                recovered = False
                if idle:
                    # A persisted running projection can survive a process crash.
                    # Opening history must not claim it is still executing.
                    for row in rows:
                        if row.get("turn_status") == "running":
                            row["turn_status"] = "stopped"
                            if row.get("phase") == "pending":
                                row["phase"] = "progress"
                            if row.get("status") == "running":
                                row["status"] = "interrupted"
                            recovered = True
                known = {row["id"] for row in rows}
                missing = [{"id": key, "role": "user", "text": item["text"]}
                           for key, item in pending.items() if key not in known]
                if missing or recovered:
                    record["display_messages"] = rows + missing
                    self.store.write(record)
        except ResearchError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError:
            record = None
        if record is None:
            raise HTTPException(404, "会话不存在")
        return record


def create_app(cwd: str | None = None, static_dir: Path | None = None) -> FastAPI:
    workspace = Workspace(cwd or str(Path.cwd()))

    @asynccontextmanager
    async def lifespan(app):
        yield
        for connection in list(workspace.connections.values()):
            await connection.cancel()

    app = FastAPI(title="OpenHarness 投研工作台", lifespan=lifespan)
    app.add_middleware(AttachmentBodyLimit)
    app.state.workspace = workspace

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if not local_host(host) or not allowed_origin(request.headers.get("origin"), host):
            return JSONResponse({"detail": "仅允许本机工作台访问"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = (
            "no-store" if request.url.path.startswith("/api") else "no-cache"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default payload echoes inputs, including API keys.
        return JSONResponse({"detail": "请求字段无效，请检查表单内容"}, status_code=422)

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "busy": workspace.lock.locked()}

    @app.get("/api/models")
    async def get_models():
        return models_list()

    @app.post("/api/models", status_code=201)
    async def add_model(data: ModelInput):
        return {"id": save_model(data)}

    @app.put("/api/models/{profile_id}")
    async def edit_model(profile_id: str, data: ModelInput):
        return {"id": save_model(data, profile_id)}

    @app.post("/api/models/{profile_id}/activate")
    async def activate_model(profile_id: str):
        profile_settings(profile_id)
        AuthManager().use_profile(profile_id)
        return {"ok": True}

    @app.delete("/api/models/{profile_id}/credential")
    async def clear_credential(profile_id: str):
        profile_settings(profile_id)
        AuthManager().clear_profile_credential(profile_id)
        return {"ok": True}

    @app.delete("/api/models/{profile_id}")
    async def delete_model(profile_id: str):
        profile_settings(profile_id)
        if any(
            r["profile_id"] == profile_id
            for r in workspace.store.list_snapshots(workspace.cwd, limit=None)
        ):
            raise HTTPException(409, "此配置正在被会话使用，请先切换会话的模型")
        manager = AuthManager()
        try:
            manager.remove_profile(profile_id)
        except ValueError:
            raise HTTPException(409, "默认配置或内置配置不能删除，请先切换默认配置") from None
        # Custom slots are owned by their profile; legacy shared slots are kept.
        if profile_id.startswith("web-"):
            from openharness.auth.storage import clear_provider_credentials

            clear_provider_credentials(f"profile:{profile_id}")
        return {"ok": True}

    @app.post("/api/models/{profile_id}/test")
    async def test_model(profile_id: str):
        settings = profile_settings(profile_id)
        try:
            settings.resolve_auth()
        except ValueError:
            raise HTTPException(400, "请先配置此模型的凭据") from None
        client = _resolve_api_client_from_settings(settings)
        try:

            async def probe():
                complete = False
                from openharness.services.context_budget import checked_request
                async for event in client.stream_message(
                    checked_request(client, ApiMessageRequest(
                        model=settings.model,
                        max_tokens=64,
                        context_window_tokens=settings.context_window_tokens,
                        messages=[ConversationMessage.from_user_text("Reply with OK.")],
                    ))
                ):
                    if isinstance(event, ApiMessageCompleteEvent):
                        complete = True
                if not complete:
                    raise ValueError("模型未返回完整响应")

            await asyncio.wait_for(probe(), timeout=30)
            return {"ok": True, "message": "连接成功，模型可正常响应"}
        except TimeoutError:
            return {"ok": False, "message": "连接超时，请检查接口地址和网络"}
        except Exception as exc:  # noqa: BLE001 -- redact upstream exception bodies
            from openharness.api.errors import AuthenticationFailure, RateLimitFailure
            from openharness.services.context_budget import ContextBudgetError

            message = "连接失败，请检查接口地址、模型名称及网络"
            if isinstance(exc, AuthenticationFailure):
                message = "认证失败，请检查模型凭据"
            elif isinstance(exc, RateLimitFailure):
                message = "请求受限，请检查服务额度或稍后重试"
            elif isinstance(exc, ContextBudgetError):
                message = "上下文预算配置不可用，请填写真实的上下文窗口（context_window_tokens），并核对输出额度。"
            return {"ok": False, "message": message}
        finally:
            await client.close()

    @app.get("/api/skills")
    async def get_skills():
        return {"items": skills_list(workspace.cwd)}

    @app.patch("/api/skills/{plugin_id}")
    async def patch_skill(plugin_id: str, data: SkillToggle):
        toggle_skill(workspace.cwd, plugin_id, data.enabled)
        return {"ok": True}

    @app.get("/api/sessions")
    async def get_sessions():
        return {
            "items": Redactor().clean(
                [
                    {k: r[k] for k in ("session_id", "summary", "profile_id", "updated_at")}
                    for r in workspace.store.list_snapshots(workspace.cwd)
                ]
            )
        }

    @app.post("/api/sessions", status_code=201)
    async def add_session(data: SessionInput):
        profile_id = data.profile_id or models_list()["active_profile"]
        # Allow an unconfigured API profile so first-use UI can guide the user.
        profile_settings(profile_id)
        return session_view(workspace.store.create(profile_id))

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        return Redactor().clean(session_view(workspace.record(session_id)))

    def session_files(session_id: str) -> SessionFiles:
        workspace.record(session_id)
        return SessionFiles(ResearchStore(workspace.cwd, session_id).directory)

    def require_idle_files(session_id: str):
        connection = workspace.connections.get(session_id)
        if session_id in workspace.file_operations or (connection and connection.task and not connection.task.done()):
            raise HTTPException(409, "请等待文件操作或当前生成完成")

    @app.get("/api/sessions/{session_id}/attachments")
    async def list_attachments(session_id: str):
        return {"items": session_files(session_id).list("attachments")}

    @app.post("/api/sessions/{session_id}/attachments", status_code=201)
    async def upload_attachments(session_id: str, request: Request):
        storage = session_files(session_id)
        require_idle_files(session_id)
        # Multipart is bounded both in file count and in actual streamed bytes.
        workspace.file_operations.add(session_id)
        created = []
        try:
            async with request.form(max_files=10, max_fields=0) as form:
                uploads = [item for key, item in form.multi_items() if key == "files" and hasattr(item, "read")]
                if not uploads or len(uploads) != len(form.multi_items()):
                    raise HTTPException(422, "请通过files字段上传1至10个PDF、TXT或MD文件")
                for upload in uploads:
                    content = bytearray()
                    while chunk := await upload.read(1024 * 1024):
                        content.extend(chunk)
                        if len(content) > MAX_DOCUMENT_BYTES:
                            raise HTTPException(413, "每个文件最多30 MB")
                    try:
                        meta = await asyncio.to_thread(storage.upload, upload.filename or "", bytes(content))
                    except ValueError as exc:
                        raise HTTPException(422, str(exc)) from None
                    created.append(meta)
            return {"items": created}
        except Exception:
            for meta in created:
                storage.delete_attachment(meta["id"])
            raise
        finally:
            workspace.file_operations.discard(session_id)

    @app.delete("/api/sessions/{session_id}/attachments/{file_id}")
    async def delete_attachment(session_id: str, file_id: str):
        storage = session_files(session_id)
        require_idle_files(session_id)
        try:
            storage.delete_attachment(file_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "附件不存在") from None
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/artifacts")
    async def list_artifacts(session_id: str):
        return {"items": session_files(session_id).list("artifacts")}

    @app.get("/api/sessions/{session_id}/artifacts/{file_id}/download")
    async def download_artifact(session_id: str, file_id: str):
        try:
            meta, path = session_files(session_id).artifact(file_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "产物不存在或不属于当前会话") from None
        return FileResponse(path, filename=meta["name"], media_type="application/octet-stream")

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        try:
            path = workspace.store._path(session_id)
        except ValueError:
            raise HTTPException(404, "会话不存在") from None
        if not path.is_file():
            raise HTTPException(404, "会话不存在")
        connection = workspace.connections.get(session_id)
        if session_id in workspace.deleting:
            raise HTTPException(409, "会话正在删除")
        if session_id in workspace.file_operations:
            raise HTTPException(409, "请等待文件操作完成，再删除对话")
        if connection and connection.task and not connection.task.done():
            raise HTTPException(409, "请先停止当前生成，再删除对话")
        workspace.deleting.add(session_id)
        try:
            # No await between checking for a running task and closing submissions.
            workspace.store.delete(session_id)
            if connection:
                with suppress(RuntimeError, OSError, WebSocketDisconnect):
                    await connection.emit("session_deleted")
                    await connection.websocket.close(code=1000, reason="对话已删除")
        except OSError:
            raise HTTPException(500, "删除失败，请检查本地存储后重试") from None
        finally:
            workspace.deleting.discard(session_id)
            if not path.exists() and connection and workspace.connections.get(session_id) is connection:
                workspace.connections.pop(session_id, None)
        return {"ok": True}

    @app.patch("/api/sessions/{session_id}")
    async def change_session_model(session_id: str, data: SessionInput):
        connection = workspace.connections.get(session_id)
        if connection and connection.task and not connection.task.done():
            raise HTTPException(409, "请先停止当前生成，再切换模型")
        if not data.profile_id:
            raise HTTPException(422, "请选择模型配置")
        profile_settings(data.profile_id)
        record = workspace.record(session_id)
        record["profile_id"] = data.profile_id
        workspace.store.write(record)
        return Redactor().clean(session_view(record))

    @app.websocket("/api/sessions/{session_id}/ws")
    async def socket(websocket: WebSocket, session_id: str):
        host = websocket.headers.get("host", "")
        origin = websocket.headers.get("origin")
        if not local_host(host) or not origin or not allowed_origin(origin, host):
            await websocket.close(code=1008)
            return
        if session_id in workspace.deleting:
            await websocket.close(code=1008)
            return
        try:
            workspace.record(session_id)
        except HTTPException:
            await websocket.close(code=1008)
            return
        if session_id in workspace.connections:
            await websocket.close(code=1008, reason="此会话已在另一个窗口打开")
            return
        await websocket.accept()
        if session_id in workspace.deleting or not workspace.store._path(session_id).is_file():
            await websocket.close(code=1008)
            return
        connection = BrowserConnection(websocket, session_id, workspace)
        workspace.connections[session_id] = connection
        try:
            await connection.emit("ready", session=session_view(workspace.record(session_id)))
            while True:
                try:
                    request = SocketRequest.model_validate(await websocket.receive_json())
                except (ValidationError, ValueError):
                    await connection.emit("error", message="请求格式无效")
                    continue
                if session_id in workspace.deleting or not workspace.store._path(session_id).is_file():
                    break
                if request.type == "submit":
                    if connection.task and not connection.task.done():
                        # Leave the active generation and its request id unchanged.
                        await connection.emit("rejected", message="当前会话正在生成")
                    elif not request.text.strip():
                        await connection.emit("rejected", message="请输入消息")
                    else:
                        connection.task = asyncio.create_task(connection.run(request))
                elif request.type == "cancel" and request.request_id == connection.request_id:
                    await connection.cancel()
                elif request.type == "steer":
                    await connection.steer(request)
                elif request.type == "response" and request.request_id == connection.request_id:
                    connection.respond(request.request_id, request.prompt_id, request.answer)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            await connection.cancel()
            if workspace.connections.get(session_id) is connection:
                workspace.connections.pop(session_id, None)

    if static_dir is None:
        packaged = Path(__file__).parents[1] / "_web"
        source = Path(__file__).parents[3] / "frontend" / "web" / "dist"
        static_dir = packaged if packaged.is_dir() else source
    if (static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        if path in {"", "chat", "models", "skills"} and (static_dir / "index.html").is_file():
            return FileResponse(static_dir / "index.html")
        raise HTTPException(404, "请先构建前端：cd frontend/web && npm ci && npm run build")

    return app
