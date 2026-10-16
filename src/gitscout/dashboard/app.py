"""The GitScout dashboard: log in, add repositories, watch runs, browse and export records.

Server-rendered pages, no client-side framework and no inline script, so the
Content-Security-Policy can forbid scripts entirely. Every request:

1. reads the signed session cookie, refreshes the Auth tokens when they are about to
   expire, and validates them with Supabase Auth;
2. loads the viewer's role and memberships from ``gitscout.app_users`` (an account
   without a row there, or a disabled one, gets nothing);
3. scopes every query to the repositories the viewer may see (``data.py``).

Run it with ``gitscout web`` (uvicorn). Configuration:

==============================  ================================================
``SUPABASE_URL``                the project URL (Auth and Storage)
``SUPABASE_SERVICE_KEY``        secret key, server side only
``GITSCOUT_APP_DB_URL``         Postgres DSN for the dashboard role gitscout_app
                                (falls back to SUPABASE_DB_URL)
``GITSCOUT_SESSION_SECRET``     32+ random characters that sign the cookie
``GITSCOUT_PUBLIC_URL``         e.g. https://gitscout.onrender.com (links in emails)
``RESEND_API_KEY``              invitation and reset emails (optional)
``GITHUB_TOKENS``               optional, for checking repositories on GitHub
==============================  ================================================
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import exports as ex
from ..config import Settings
from ..jobs import JobStore, sanitize
from . import data
from .auth import AuthError, AuthProvider, SupabaseAuth
from .mail import send as send_mail
from .session import COOKIE, MAX_AGE, Cookie, CookieCodec

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")


class Redirect(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


class Denied(Exception):
    pass


def _ist(value: Any, fmt: str = "%d %b %Y, %H:%M") -> str:
    if not value:
        return "-"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(IST).strftime(fmt) + " IST"
    return str(value)


def _ago(value: Any) -> str:
    if not isinstance(value, datetime):
        return "never"
    seconds = int((datetime.now(timezone.utc) - value).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def _num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


class Pool:
    """psycopg_pool, opened on first use; tests pass a plain DSN and get the same API."""

    def __init__(self, dsn: str, *, max_size: int = 5) -> None:
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(dsn, min_size=0, max_size=max_size, open=False,
                                    kwargs={"prepare_threshold": None, "connect_timeout": 15})
        self._opened = False

    @contextmanager
    def connection(self) -> Iterator[Any]:
        if not self._opened:
            self._pool.open()
            self._opened = True
        with self._pool.connection() as conn:   # commits on success, rolls back on error
            yield conn

    def close(self) -> None:
        if self._opened:
            self._pool.close()


def create_app(settings: Settings, *, auth: AuthProvider | None = None, dsn: str | None = None,
               session_secret: str | None = None, public_url: str | None = None,
               github_transport=None, mail_transport=None, storage_transport=None,
               embedded_worker: bool = False) -> FastAPI:
    secret = session_secret or os.getenv("GITSCOUT_SESSION_SECRET") or ""
    codec = CookieCodec(secret)
    public = (public_url or os.getenv("GITSCOUT_PUBLIC_URL") or "http://localhost:8000").rstrip("/")
    secure = public.startswith("https://")
    dsn = dsn or os.getenv("GITSCOUT_APP_DB_URL") or settings.supabase_db_url
    if not dsn:
        raise ValueError("the dashboard needs GITSCOUT_APP_DB_URL (or SUPABASE_DB_URL)")
    if auth is None:
        if not (settings.supabase_url and settings.supabase_key):
            raise ValueError("the dashboard needs SUPABASE_URL and SUPABASE_SERVICE_KEY for Auth")
        auth = SupabaseAuth(settings.supabase_url, settings.supabase_key)
    pool = Pool(dsn)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runner = None
        if embedded_worker:
            from .embedded import EmbeddedWorker

            runner = EmbeddedWorker(settings, public_url=public)
            runner.start()
            log.info("worker running inside the dashboard process")
        try:
            yield
        finally:
            if runner is not None:
                runner.stop()
            pool.close()

    app = FastAPI(title="GitScout", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.pool = pool
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(ist=_ist, ago=_ago, num=_num)
    templates.env.globals["roles"] = data.ROLES
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ------------------------------------------------------------ plumbing
    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy",
                                    "default-src 'self'; script-src 'none'; style-src 'self'; "
                                    "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'")
        if secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        new = getattr(request.state, "new_cookie", None)
        if new == "":
            response.delete_cookie(COOKIE, path="/")
        elif new:
            response.set_cookie(COOKIE, new, max_age=MAX_AGE, httponly=True, secure=secure,
                                samesite="lax", path="/")
        return response

    @app.exception_handler(Redirect)
    async def _redirect(request: Request, exc: Redirect):
        return RedirectResponse(exc.url, status_code=303)

    @app.exception_handler(Denied)
    @app.exception_handler(data.Forbidden)
    async def _denied(request: Request, exc: Exception):
        return HTMLResponse("<h1>Not allowed</h1><p>Your account cannot do that.</p><p><a href='/'>Back</a></p>",
                            status_code=403)

    def cookie_of(request: Request) -> Cookie | None:
        return codec.loads(request.cookies.get(COOKIE))

    def set_cookie(request: Request, cookie: Cookie | None) -> None:
        request.state.new_cookie = codec.dumps(cookie) if cookie else ""

    def current(request: Request, conn: Any) -> tuple[data.Viewer, Cookie]:
        """The signed-in viewer, or a redirect to the login page."""
        cookie = cookie_of(request)
        if cookie is None:
            raise Redirect("/login?next=" + quote(request.url.path))
        if cookie.expires_at - 60 < time.time():
            try:
                fresh = auth.refresh(cookie.refresh_token)
            except AuthError:
                set_cookie(request, None)
                raise Redirect("/login?err=" + quote("Your session expired. Sign in again."))
            cookie = Cookie.from_session(fresh, cookie.csrf)
            set_cookie(request, cookie)
        try:
            user = auth.user(cookie.access_token)
        except AuthError:
            set_cookie(request, None)
            raise Redirect("/login")
        if user.get("id") != cookie.user_id:
            set_cookie(request, None)
            raise Redirect("/login")
        v = data.viewer(conn, cookie.user_id)
        if v is None:
            set_cookie(request, None)
            raise Redirect("/login?err=" + quote("This account has no access. Ask your administrator."))
        return v, cookie

    def check_csrf(request: Request, cookie: Cookie, token: str) -> None:
        if not token or token != cookie.csrf:
            raise Denied()

    def page(request: Request, name: str, ctx: dict[str, Any], status: int = 200) -> HTMLResponse:
        ctx = {"request": request, "ok": request.query_params.get("ok"),
               "err": request.query_params.get("err"), **ctx}
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def back(url: str, **msg: str) -> RedirectResponse:
        sep = "&" if "?" in url else "?"
        return RedirectResponse(url + (sep + urlencode(msg) if msg else ""), status_code=303)

    # --------------------------------------------------------------- auth
    @app.get("/healthz")
    def healthz() -> JSONResponse:
        try:
            with pool.connection() as conn:
                conn.execute("select 1")
            return JSONResponse({"ok": True})
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False}, status_code=503)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return page(request, "login.html", {"next": next if next.startswith("/") else "/"})

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/")):
        try:
            session = auth.sign_in(email.strip().lower(), password)
        except AuthError:
            return back("/login", err="Wrong email or password.")
        with pool.connection() as conn:
            v = data.viewer(conn, session.user_id)
            if v is None:
                return back("/login", err="This account has no access. Ask your administrator.")
            data.audit(conn, v, "auth.login")
        set_cookie(request, Cookie.from_session(session))
        return RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        cookie = cookie_of(request)
        if cookie:
            auth.sign_out(cookie.access_token)
        set_cookie(request, None)
        return RedirectResponse("/login?ok=" + quote("Signed out."), status_code=303)

    @app.get("/auth/confirm", response_class=HTMLResponse)
    def confirm_form(request: Request, token_hash: str = "", type: str = "invite"):
        # one click before the token is used, so a link scanner cannot spend it
        return page(request, "confirm.html", {"token_hash": token_hash,
                                              "kind": type if type in ("invite", "recovery") else "invite"})

    @app.post("/auth/confirm")
    def confirm(request: Request, token_hash: str = Form(...), kind: str = Form("invite")):
        try:
            session = auth.verify(token_hash, kind if kind in ("invite", "recovery") else "invite")
        except AuthError:
            return back("/login", err="That link has expired or was already used. Ask for a new one.")
        set_cookie(request, Cookie.from_session(session))
        return RedirectResponse("/account/password?first=1", status_code=303)

    @app.get("/auth/forgot", response_class=HTMLResponse)
    def forgot_form(request: Request):
        return page(request, "forgot.html", {})

    @app.post("/auth/forgot")
    def forgot(request: Request, email: str = Form(...)):
        email = email.strip().lower()
        with pool.connection() as conn:
            known = data._dicts(conn, "select user_id from gitscout.app_users where email = %s and disabled_at is null", [email])
        if known:
            try:
                _, token, kind = auth.generate_link("recovery", email)
                link = f"{public}/auth/confirm?token_hash={quote(token)}&type={kind}"
                send_mail(settings, email, "Reset your GitScout password",
                          f"Open this link to choose a new password:\n\n{link}\n\nIf you did not ask for this, ignore this email.",
                          transport=mail_transport)
            except AuthError as exc:
                log.warning("recovery link for a known user failed: %s", exc)
        # the same answer either way: the page must not reveal who has an account
        return back("/login", ok="If that address has an account, a reset link is on its way.")

    @app.get("/account/password", response_class=HTMLResponse)
    def password_form(request: Request, first: int = 0):
        with pool.connection() as conn:
            v, cookie = current(request, conn)
        return page(request, "password.html", {"v": v, "csrf": cookie.csrf, "first": first})

    @app.post("/account/password")
    def password(request: Request, password: str = Form(...), confirm: str = Form(...), csrf: str = Form("")):
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            check_csrf(request, cookie, csrf)
            if len(password) < 12:
                return back("/account/password", err="Use at least 12 characters.")
            if password != confirm:
                return back("/account/password", err="The two passwords differ.")
            try:
                auth.set_password(cookie.access_token, password)
            except AuthError as exc:
                return back("/account/password", err=str(exc))
            data.audit(conn, v, "auth.password_set")
        return back("/", ok="Password saved.")

    # ----------------------------------------------------------- overview
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            repos = data.repositories(conn, v)
            recent = data.jobs(conn, v, limit=10) if v.repos else []
            store = JobStore.wrap(conn)
            workers = store.workers() if v.is_super else []
            alerts = store.alerts(5) if v.is_super else []
        return page(request, "overview.html", {"v": v, "csrf": cookie.csrf, "repos": repos,
                                               "totals": data.totals(repos), "jobs": recent,
                                               "workers": workers, "alerts": alerts})

    # -------------------------------------------------------- repositories
    @app.get("/repos", response_class=HTMLResponse)
    def repos_page(request: Request):
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            repos = data.repositories(conn, v)
        return page(request, "repos.html", {"v": v, "csrf": cookie.csrf, "repos": repos})

    @app.post("/repos/add")
    async def repos_add(request: Request, repos: str = Form(""), file: UploadFile | None = None, csrf: str = Form("")):
        text = repos
        if file is not None and file.filename:
            raw = await file.read(512_000)
            text += "\n" + raw.decode("utf-8", errors="replace")
        wanted, bad = data.parse_repo_list(text)
        if not wanted and not bad:
            return back("/repos", err="Paste at least one repository (owner/name or a GitHub link).")
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            check_csrf(request, cookie, csrf)
            limit = int(JobStore.wrap(conn).settings().get("max_repos_per_user") or 10)
            result = data.add_repositories(conn, v, wanted[:50], token=(settings.tokens or [None])[0],
                                           max_per_user=limit, transport=github_transport)
        parts = []
        if result["added"]:
            parts.append(f"Added {', '.join(result['added'])}.")
        if result["joined"]:
            parts.append(f"Already tracked, now visible to you: {', '.join(result['joined'])}.")
        if result["queued"]:
            parts.append("Scraping starts within a minute; after that it runs every night at 00:00 IST.")
        failed = [f"{r} ({why})" for r, why in result["failed"]] + [f"{b} (not a repository)" for b in bad]
        msg = {"ok": " ".join(parts)} if parts else {}
        if failed:
            msg["err"] = "Not added: " + "; ".join(failed)
        return back("/repos", **msg)

    @app.post("/repos/{owner}/{name}/run")
    def repo_run(request: Request, owner: str, name: str, csrf: str = Form(""), kind: str = Form("incremental")):
        repo = f"{owner}/{name}"
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            check_csrf(request, cookie, csrf)
            job_id = data.run_now(conn, v, repo, kind if kind in ("incremental", "full", "sweep", "export") else "incremental")
        if job_id is None:
            return back(f"/repos/{repo}", err="A run for this repository is already queued or running.")
        return back(f"/repos/{repo}", ok=f"Run queued (job {job_id}). It starts within a minute.")

    @app.post("/repos/{owner}/{name}/enabled")
    def repo_enabled(request: Request, owner: str, name: str, enabled: int = Form(...), csrf: str = Form("")):
        repo = f"{owner}/{name}"
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            check_csrf(request, cookie, csrf)
            data.set_enabled(conn, v, repo, bool(enabled))
        return back(f"/repos/{repo}", ok="Nightly runs " + ("resumed." if enabled else "paused."))

    @app.get("/repos/{owner}/{name}", response_class=HTMLResponse)
    def repo_page(request: Request, owner: str, name: str):
        repo = f"{owner}/{name}"
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            if not v.can_see(repo):
                raise Denied()
            row = next((r for r in data.repositories(conn, v) if r["repo"] == repo), None)
            history = data.jobs(conn, v, repo=repo, limit=30)
        if row is None:
            raise Denied()
        return page(request, "repo.html", {"v": v, "csrf": cookie.csrf, "r": row, "jobs": history,
                                           "today": date.today().isoformat()})

    # -------------------------------------------------------------- records
    @app.get("/records", response_class=HTMLResponse)
    def records_page(request: Request, repo: str = "", q: str = "", email: int = 0, page_no: int = 1):
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            rows, total = data.records(conn, v, repo=repo or None, q=q.strip() or None,
                                       with_email=bool(email), page=max(1, page_no))
        pages = max(1, (total + 49) // 50)
        return page(request, "records.html", {"v": v, "csrf": cookie.csrf, "rows": rows, "total": total,
                                              "repo": repo, "q": q, "email": email, "page_no": page_no,
                                              "pages": pages})

    @app.get("/records/{owner}/{name}/{login}", response_class=HTMLResponse)
    def person_page(request: Request, owner: str, name: str, login: str):
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            p = data.person(conn, v, f"{owner}/{name}", login)
        if p is None:
            return page(request, "message.html", {"v": v, "csrf": cookie.csrf, "title": "Not found",
                                                  "text": "No record for that person."}, status=404)
        return page(request, "person.html", {"v": v, "csrf": cookie.csrf, "p": p, "repo": f"{owner}/{name}"})

    # -------------------------------------------------------------- exports
    @app.get("/exports", response_class=HTMLResponse)
    def exports_page(request: Request, repo: str = ""):
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            archives = [a for a in ex.recent(JobStore.wrap(conn), None, 60) if v.can_see(a["repo"] or "")]
        return page(request, "exports.html", {"v": v, "csrf": cookie.csrf, "repo": repo, "archives": archives,
                                              "today": date.today().isoformat()})

    @app.get("/exports/download")
    def export_download(request: Request, repo: str = "", date_from: str = "", date_to: str = "",
                        basis: str = "collected", full: int = 0):
        conn_cm = pool.connection()
        conn = conn_cm.__enter__()
        try:
            v, _ = current(request, conn)
            if repo and not v.can_see(repo):
                raise Denied()
            req = ex.ExportRequest(repo=repo or None, date_from=ex.parse_day(date_from),
                                   date_to=ex.parse_day(date_to), basis=basis, excel_safe=not full,
                                   repos=None if v.is_super else list(v.repos))
            req.bounds()
            data.audit(conn, v, "export.download", repo=repo or None, details=req.filters())
            conn.commit()
        except ex.ExportError as exc:
            conn_cm.__exit__(None, None, None)
            return back("/exports", err=str(exc))
        except BaseException as exc:
            conn_cm.__exit__(type(exc), exc, None)
            raise

        def stream() -> Iterator[bytes]:
            try:
                for chunk in ex.iter_csv(conn, req):
                    yield chunk.encode("utf-8")
            finally:
                conn_cm.__exit__(None, None, None)

        name = req.filename()
        return StreamingResponse(stream(), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"',
                                          "Cache-Control": "no-store"})

    @app.get("/exports/archive/{export_id}")
    def export_archive(request: Request, export_id: int):
        with pool.connection() as conn:
            v, _ = current(request, conn)
            rows = data._dicts(conn, "select repo, storage_path from gitscout.exports where id = %s and deleted_at is null",
                               [export_id])
            if not rows or not v.can_see(rows[0]["repo"] or ""):
                raise Denied()
            data.audit(conn, v, "export.archive", repo=rows[0]["repo"], target_kind="export", target_id=str(export_id))
        if not (settings.supabase_url and settings.supabase_key):
            return back("/exports", err="Storage is not configured.")
        try:
            url = ex.Storage(settings.supabase_url, settings.supabase_key,
                             transport=storage_transport).signed_url(rows[0]["storage_path"], 300)
        except ex.ExportError as exc:
            return back("/exports", err=sanitize(str(exc)))
        return RedirectResponse(url, status_code=303)

    # ----------------------------------------------------------------- jobs
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, repo: str = "", status: str = ""):
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            rows = data.jobs(conn, v, repo=repo or None, status=status or None, limit=100)
        return page(request, "jobs.html", {"v": v, "csrf": cookie.csrf, "jobs": rows, "repo": repo, "status": status})

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int):
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            job = JobStore.wrap(conn).get(job_id)
            if job is None or not v.can_see(job.repo):
                raise Denied()
        return page(request, "job.html", {"v": v, "csrf": cookie.csrf, "j": job, "can_manage": v.can_manage(job.repo)})

    @app.post("/jobs/{job_id}/cancel")
    def job_cancel(request: Request, job_id: int, csrf: str = Form("")):
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            check_csrf(request, cookie, csrf)
            done = data.cancel_job(conn, v, job_id)
        return back(f"/jobs/{job_id}", **({"ok": "Cancelled."} if done else {"err": "That job already finished."}))

    # ---------------------------------------------------------------- users
    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request):
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            rows = data.users(conn, v)
        return page(request, "users.html", {"v": v, "csrf": cookie.csrf, "users": rows,
                                            "link": request.query_params.get("link")})

    @app.post("/users/invite")
    def users_invite(request: Request, email: str = Form(...), role: str = Form("client_viewer"),
                     repos: list[str] = Form([]), csrf: str = Form("")):
        email = email.strip().lower()
        if "@" not in email:
            return back("/users", err="That is not an email address.")
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            check_csrf(request, cookie, csrf)
            if not v.can_manage_users:
                raise Denied()
            if not v.is_super and (role != "client_viewer" or any(r not in v.admin_repos for r in repos)):
                raise Denied()
            try:
                user_id, token, kind = auth.generate_link("invite", email)
            except AuthError as exc:
                return back("/users", err=f"Auth refused the invitation: {exc}")
            data.provision(conn, v, user_id=user_id, email=email, role=role, repos=repos)
        link = f"{public}/auth/confirm?token_hash={quote(token)}&type={kind}"
        problem = send_mail(settings, email, "You are invited to GitScout",
                            f"{v.email} invited you to the GitScout dashboard as {data.ROLES[role]}.\n\n"
                            f"Open this link to choose your password:\n\n{link}\n\nThe link works once.",
                            transport=mail_transport)
        if problem:
            # no mail channel: hand the one-time link to the inviter, once, to pass on
            return page(request, "invite_link.html", {"v": v, "csrf": cookie.csrf, "email": email,
                                                      "link": link, "problem": problem})
        return back("/users", ok=f"Invitation emailed to {email}.")

    @app.post("/users/{user_id}/disabled")
    def users_disable(request: Request, user_id: str, disabled: int = Form(...), csrf: str = Form("")):
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            check_csrf(request, cookie, csrf)
            data.set_disabled(conn, v, user_id, bool(disabled))
        return back("/users", ok="Account " + ("disabled." if disabled else "enabled."))

    @app.post("/users/{user_id}/revoke")
    def users_revoke(request: Request, user_id: str, repo: str = Form(...), csrf: str = Form("")):
        with pool.connection() as conn:
            v, cookie = current(request, conn)
            check_csrf(request, cookie, csrf)
            data.revoke(conn, v, user_id, repo)
        return back("/users", ok=f"Access to {repo} removed.")

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots() -> str:
        return "User-agent: *\nDisallow: /\n"

    return app


def app_from_env() -> FastAPI:
    """uvicorn factory: `uvicorn gitscout.dashboard.app:app_from_env --factory`."""
    from ..config import load_settings
    from ..logs import configure

    settings = load_settings()
    configure(settings.log_format)
    return create_app(settings, embedded_worker=os.getenv("GITSCOUT_EMBEDDED_WORKER", "").lower() in ("1", "true", "yes"))
