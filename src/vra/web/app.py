"""FastAPI application: JSON API plus the single-page UI in ./static."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from . import exports
from .auth import Auth, AuthError
from .config import Settings, foundry_configured
from .db import Database
from .runner import Runner
from .service import ApiError, Service

STATIC = Path(__file__).parent / "static"
COOKIE = "vra_session"
INLINE_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
TEXT_TYPES = {".txt", ".md", ".log", ".csv", ".tsv", ".eml"}
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
       "frame-src 'self'; object-src 'self'; base-uri 'none'; form-action 'self'")
log = logging.getLogger("vra.web")


class SetupBody(BaseModel):
    username: str
    display_name: str = ""
    password: str


class LoginBody(BaseModel):
    username: str
    password: str


class NewUser(BaseModel):
    username: str
    display_name: str = ""
    password: str
    role: str = "preparer"


class UserPatch(BaseModel):
    role: str | None = None
    active: bool | None = None
    password: str | None = None


class PasswordBody(BaseModel):
    current: str
    new: str


class EngagementBody(BaseModel):
    name: str
    client: str = ""
    period_start: str | None = None
    period_end: str | None = None
    default_type: str = "purchase"


class EngagementPatch(BaseModel):
    name: str | None = None
    client: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    default_type: str | None = None
    status: str | None = None


class DecisionBody(BaseModel):
    txn_id: str
    action: str
    note: str = ""


class SignoffBody(BaseModel):
    note: str = ""
    acknowledge_issues: bool = False


class VoucherOverride(BaseModel):
    voucher_id: str
    fields: dict
    note: str = ""


class LinkOverride(BaseModel):
    txn_id: str
    voucher_id: str | None = None
    note: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    auth = Auth(db, settings)
    svc = Service(db, settings, auth)
    runner = Runner(svc)
    runner.recover()

    app = FastAPI(title="Vouching & Reconciliation Agent", version=__version__, docs_url=None, redoc_url=None)
    app.state.svc, app.state.runner, app.state.settings = svc, runner, settings

    # ---------------------------------------------------------------- plumbing
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        return JSONResponse({"error": e.message}, status_code=e.status)

    @app.exception_handler(AuthError)
    async def _auth_error(_: Request, e: AuthError):
        return JSONResponse({"error": str(e)}, status_code=e.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        first = e.errors()[0] if e.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        return JSONResponse({"error": f"Invalid request: {where} {first.get('msg', '')}".strip()}, status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, e: Exception):
        log.exception("unhandled error")
        return JSONResponse({"error": "Unexpected server error. Details are in the server log."}, status_code=500)

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.endswith("/document"):
            resp.headers.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    def csrf(request: Request) -> None:
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-vra") != "1":
            raise ApiError(403, "Missing X-VRA header.")

    def current_user(request: Request) -> dict:
        user = auth.user_for_token(request.cookies.get(COOKIE))
        if not user:
            raise ApiError(401, "Please sign in.")
        return user

    def admin(user: dict = Depends(current_user)) -> dict:
        if user["role"] != "admin":
            raise ApiError(403, "Administrators only.")
        return user

    def set_cookie(resp: Response, token: str) -> None:
        resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=settings.cookie_secure,
                        max_age=settings.session_hours * 3600, path="/")

    api = {"dependencies": [Depends(csrf)]}

    # ---------------------------------------------------------------- session
    @app.get("/api/system")
    def system(request: Request):
        user = auth.user_for_token(request.cookies.get(COOKIE))
        return {"version": __version__, "setup_required": auth.user_count() == 0, "solo": settings.solo,
                "foundry_configured": foundry_configured(), "user": user,
                "max_upload_mb": settings.max_upload_mb}

    @app.post("/api/setup", **api)
    def setup(body: SetupBody):
        if auth.user_count() > 0:
            raise ApiError(409, "Setup has already been completed.")
        auth.create_user(body.username, body.display_name, body.password, "admin", only_if_empty=True)
        token, user = auth.login(body.username, body.password)
        resp = JSONResponse({"user": user})
        set_cookie(resp, token)
        return resp

    @app.post("/api/login", **api)
    def login(body: LoginBody):
        token, user = auth.login(body.username, body.password)
        resp = JSONResponse({"user": user})
        set_cookie(resp, token)
        return resp

    @app.post("/api/logout", **api)
    def logout(request: Request):
        auth.logout(request.cookies.get(COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/api/me")
    def me(user: dict = Depends(current_user)):
        return user

    @app.post("/api/me/password", **api)
    def change_password(body: PasswordBody, user: dict = Depends(current_user)):
        from .auth import verify_password

        row = db.one("SELECT pw_hash FROM users WHERE id=?", (user["id"],))
        if not verify_password(body.current, row["pw_hash"]):
            raise ApiError(400, "Current password is wrong.")
        svc.update_user(user["id"], user, None, None, body.new)
        return {"ok": True}

    # ---------------------------------------------------------------- admin
    @app.get("/api/users")
    def users(_: dict = Depends(admin)):
        return svc.list_users()

    @app.post("/api/users", **api)
    def new_user(body: NewUser, actor: dict = Depends(admin)):
        uid = auth.create_user(body.username, body.display_name, body.password, body.role)
        svc.log(actor, "user_created", f"{body.username} ({body.role})")
        return {"id": uid}

    @app.patch("/api/users/{uid}", **api)
    def patch_user(uid: int, body: UserPatch, actor: dict = Depends(admin)):
        svc.update_user(uid, actor, body.role, body.active, body.password)
        return {"ok": True}

    @app.post("/api/system/test-foundry", **api)
    def test_foundry(_: dict = Depends(admin)):
        if not foundry_configured():
            raise ApiError(400, "Foundry is not configured. Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT.")
        try:
            from ..llm import FoundryChat

            chat = FoundryChat()
            reply = chat.chat("Reply with the single word OK.", "ping")
            return {"ok": True, "deployment": chat.name, "reply": reply[:80]}
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {str(e)[:400]}"}, status_code=200)

    @app.get("/api/programmes")
    def programmes(_: dict = Depends(current_user)):
        return svc.programmes()

    # ---------------------------------------------------------------- engagements
    @app.get("/api/engagements")
    def list_engagements(include_archived: bool = False, _: dict = Depends(current_user)):
        return svc.list_engagements(include_archived)

    @app.post("/api/engagements", **api)
    def create_engagement(body: EngagementBody, user: dict = Depends(current_user)):
        return {"id": svc.create_engagement(user, body.name, body.client, body.period_start, body.period_end,
                                            body.default_type)}

    @app.get("/api/engagements/{eid}")
    def get_engagement(eid: int, _: dict = Depends(current_user)):
        return {"engagement": svc.engagement(eid), "files": svc.list_files(eid), "runs": svc.list_runs(eid),
                "overrides": svc.overrides(eid)}

    @app.patch("/api/engagements/{eid}", **api)
    def patch_engagement(eid: int, body: EngagementPatch, user: dict = Depends(current_user)):
        svc.update_engagement(user, eid, body.model_dump(exclude_unset=True))
        return {"ok": True}

    @app.get("/api/engagements/{eid}/activity")
    def activity(eid: int, _: dict = Depends(current_user)):
        svc.engagement(eid)
        return svc.activity(eid)

    # ---------------------------------------------------------------- files
    @app.post("/api/engagements/{eid}/files", **api)
    def upload(eid: int, kind: str = Form(...), files: list[UploadFile] = File(...),
               paths: list[str] = Form(default=[]), user: dict = Depends(current_user)):
        stored, skipped = [], []
        for i, f in enumerate(files):
            name = paths[i] if i < len(paths) and paths[i] else (f.filename or "")
            try:
                got = svc.add_file(user, eid, kind, name, f.file)
            except ApiError as e:
                if len(files) == 1:
                    raise
                skipped.append({"filename": name, "reason": e.message})
                continue
            if got:
                stored.extend(got)
            else:
                skipped.append({"filename": name, "reason": "ignored (hidden or temporary file)"})
        return {"stored": stored, "skipped": skipped}

    @app.delete("/api/files/{fid}", **api)
    def delete_file(fid: int, user: dict = Depends(current_user)):
        svc.remove_file(user, fid)
        return {"ok": True}

    # ---------------------------------------------------------------- runs
    @app.post("/api/engagements/{eid}/runs", **api)
    def start_run(eid: int, options: dict, user: dict = Depends(current_user)):
        rid = svc.create_run(user, eid, options)
        runner.submit(rid)
        return {"id": rid}

    @app.get("/api/runs/{rid}")
    def get_run(rid: int, _: dict = Depends(current_user)):
        r = svc.run_public(rid)
        r["outstanding"] = svc.outstanding(rid) if r["status"] == "completed" else 0
        r["files"] = svc.run_files(rid)
        return r

    @app.get("/api/runs/{rid}/items")
    def items(rid: int, disposition: str | None = None, status: str | None = None, code: str | None = None,
              severity: str | None = None, q: str | None = None, limit: int = Query(50, le=500), offset: int = 0,
              _: dict = Depends(current_user)):
        svc.run_row(rid)
        return svc.list_items(rid, disposition, status, code, severity, q, limit, offset)

    @app.get("/api/runs/{rid}/item")
    def item(rid: int, id: str, _: dict = Depends(current_user)):
        return svc.item_detail(rid, id)

    @app.post("/api/runs/{rid}/decision", **api)
    def decision(rid: int, body: DecisionBody, user: dict = Depends(current_user)):
        svc.add_decision(user, rid, body.txn_id, body.action, body.note)
        return svc.item_detail(rid, body.txn_id)

    @app.post("/api/runs/{rid}/signoff", **api)
    def signoff(rid: int, body: SignoffBody, user: dict = Depends(current_user)):
        svc.sign_off(user, rid, body.note, body.acknowledge_issues)
        return svc.run_public(rid)

    @app.get("/api/runs/{rid}/documents")
    def run_documents(rid: int, _: dict = Depends(current_user)):
        return svc.voucher_documents(rid)

    def _doc_blob(rid: int, name: str):
        row = db.one("SELECT sha256 FROM run_files WHERE run_id=? AND kind='document' AND filename=?", (rid, name))
        if not row:
            raise ApiError(404, "Document not found in this run.")
        eid = svc.run_row(rid)["engagement_id"]
        path = svc.blob_path(eid, row["sha256"])
        if not path.is_file():
            raise ApiError(404, "The stored file is missing.")
        return path

    @app.get("/api/runs/{rid}/document")
    def document(rid: int, name: str, download: bool = False, _: dict = Depends(current_user)):
        path = _doc_blob(rid, name)
        ext = Path(name).suffix.lower()
        base = quote(Path(name).name)
        if download or (ext not in INLINE_TYPES and ext not in TEXT_TYPES):
            return FileResponse(path, media_type="application/octet-stream",
                                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{base}"})
        mt = INLINE_TYPES.get(ext, "text/plain; charset=utf-8")
        return FileResponse(path, media_type=mt, headers={"Content-Disposition": f"inline; filename*=UTF-8''{base}"})

    @app.get("/api/runs/{rid}/document-text")
    def document_text(rid: int, name: str, _: dict = Depends(current_user)):
        from ..ingest import IngestError, ingest_path

        path = _doc_blob(rid, name)
        tmp = settings.runs_dir / str(rid) / "docs" / name
        try:
            doc = ingest_path(tmp if tmp.is_file() else path)
        except IngestError as e:
            return {"text": "", "note": str(e)}
        note = "Scanned image: no text layer (read by OCR if configured)." if doc.needs_ocr else ""
        return {"text": doc.text[:200000], "note": note, "kind": doc.kind}

    @app.get("/api/runs/{rid}/export/{fmt}")
    def export(rid: int, fmt: str, _: dict = Depends(current_user)):
        wd = settings.runs_dir / str(rid)
        svc.run_row(rid)
        stem = f"vouching_run_{rid}"
        if fmt == "md":
            body, mt, ext = exports.build_markdown(svc, rid).encode(), "text/markdown; charset=utf-8", "md"
        elif fmt == "docx":
            body, mt, ext = (exports.build_docx(svc, rid),
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx")
        elif fmt == "xlsx":
            body, mt, ext = (exports.build_xlsx(svc, rid),
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx")
        elif fmt in ("audit", "report", "manifest"):
            fname = {"audit": "audit_log.jsonl", "report": "report.json", "manifest": "manifest.json"}[fmt]
            p = wd / fname
            if not p.is_file():
                raise ApiError(404, "Not available for this run.")
            body, mt, ext = p.read_bytes(), "application/json", fname.split(".")[-1]
            stem += "_" + fname.split(".")[0]
        else:
            raise ApiError(404, "Unknown export format.")
        return Response(body, media_type=mt, headers={"Content-Disposition": f'attachment; filename="{stem}.{ext}"'})

    # ---------------------------------------------------------------- corrections
    @app.get("/api/engagements/{eid}/overrides")
    def get_overrides(eid: int, _: dict = Depends(current_user)):
        return svc.overrides(eid)

    @app.put("/api/engagements/{eid}/voucher-override", **api)
    def put_voucher_override(eid: int, body: VoucherOverride, user: dict = Depends(current_user)):
        svc.set_voucher_override(user, eid, body.voucher_id, body.fields, body.note)
        return {"ok": True}

    @app.delete("/api/engagements/{eid}/voucher-override", **api)
    def delete_voucher_override(eid: int, voucher_id: str, user: dict = Depends(current_user)):
        svc.clear_voucher_override(user, eid, voucher_id)
        return {"ok": True}

    @app.put("/api/engagements/{eid}/link-override", **api)
    def put_link_override(eid: int, body: LinkOverride, user: dict = Depends(current_user)):
        svc.set_link_override(user, eid, body.txn_id, body.voucher_id, body.note)
        return {"ok": True}

    @app.delete("/api/engagements/{eid}/link-override", **api)
    def delete_link_override(eid: int, txn_id: str, user: dict = Depends(current_user)):
        svc.clear_link_override(user, eid, txn_id)
        return {"ok": True}

    # ---------------------------------------------------------------- UI
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
