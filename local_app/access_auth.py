"""ASGI authentication context and four slim access-session endpoints."""
import contextvars
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import Response

from local_app import access_session_service as session_service


ACCESS_COOKIE_NAME = "dl_access_session"
_LEGACY_COOKIE_NAMES = ("dl_session", "dental_auth")
_POLLING_PATHS = frozenset(
    {
        "/api/sync/align/status",
        "/api/sync/full-now/status",
        "/api/sync/reconcile-now/status",
        "/api/sync/run-now/status",
    }
)
_STATIC_SUFFIXES = (".js", ".css", ".png", ".svg", ".webmanifest")

_current_access_user = contextvars.ContextVar("current_access_user", default=None)
_current_access_session = contextvars.ContextVar(
    "current_access_session", default=None
)
_current_access_request_time = contextvars.ContextVar(
    "current_access_request_time", default=None
)


def current_access_user():
    return _current_access_user.get()


def current_access_session():
    return _current_access_session.get()


def current_access_request_time():
    return _current_access_request_time.get()


def _utc_now():
    return datetime.now(timezone.utc)


def _cookie_values(scope, wanted_names):
    found = {name: [] for name in wanted_names}
    for raw_name, raw_value in scope.get("headers", ()):
        if raw_name.lower() != b"cookie":
            continue
        try:
            header = raw_value.decode("latin-1")
        except BaseException:
            continue
        for part in header.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name in found:
                found[name].append(value.strip())
    return found


def _is_static_passthrough(method, path):
    if method not in ("GET", "HEAD") or path.startswith("/api/"):
        return False
    if path in ("/", "/index.html", "/favicon.ico", "/__login", "/login"):
        return True
    return path.endswith(_STATIC_SUFFIXES)


def _is_public_request(method, path):
    if (method, path) in (
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/logout"),
        ("GET", "/api/health"),
    ):
        return True
    return _is_static_passthrough(method, path)


def _skip_resolution(method, path):
    if method not in ("GET", "HEAD") or path.startswith("/api/"):
        return False
    if path in ("/__login", "/login", "/favicon.ico"):
        return True
    return path.endswith(_STATIC_SUFFIXES)


def _should_renew(method, path):
    if (method, path) in (
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/logout"),
    ):
        return False
    if method == "GET" and path in _POLLING_PATHS:
        return False
    if method == "GET" and path == "/api/health":
        return False
    return True


def _generic_500():
    return JSONResponse({"detail": "服务暂时不可用"}, status_code=500)


def _authentication_required():
    return JSONResponse({"detail": "登录已失效"}, status_code=401)


def _cookie_secure(scope_or_request):
    if isinstance(scope_or_request, dict):
        scheme = scope_or_request.get("scheme", "http")
    else:
        scheme = scope_or_request.url.scheme
    return scheme == "https"


def _expire_cookie(response, name, scope_or_request):
    response.delete_cookie(
        name,
        path="/",
        secure=_cookie_secure(scope_or_request),
        httponly=True,
        samesite="lax",
    )


def _set_cookie_names(headers):
    names = set()
    for name, value in headers:
        if name.lower() != b"set-cookie":
            continue
        try:
            cookie_name = value.decode("latin-1").split("=", 1)[0].strip()
        except BaseException:
            continue
        if cookie_name:
            names.add(cookie_name)
    return names


def _cleanup_headers(scope, names):
    if not names:
        return []
    holder = Response()
    for name in names:
        _expire_cookie(holder, name, scope)
    return [header for header in holder.raw_headers if header[0].lower() == b"set-cookie"]


def _cleanup_sender(send, scope, names, *, names_on_401=()):
    cleanup = tuple(dict.fromkeys(names))
    conditional = tuple(dict.fromkeys(names_on_401))

    async def wrapped(message):
        if message.get("type") == "http.response.start":
            active_cleanup = cleanup
            if message.get("status") == 401:
                active_cleanup += conditional
            if not active_cleanup:
                return await send(message)
            headers = list(message.get("headers", ()))
            already_set = _set_cookie_names(headers)
            pending = [
                name for name in active_cleanup if name not in already_set
            ]
            if pending:
                headers.extend(_cleanup_headers(scope, pending))
                message = dict(message)
                message["headers"] = headers
        await send(message)

    return wrapped


class AccessAuthContextMiddleware:
    def __init__(self, app, db_path, now_provider):
        self.app = app
        self.db_path = db_path
        self.now_provider = now_provider

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        try:
            request_now = session_service.normalize_server_now(self.now_provider())
        except BaseException:
            return await _generic_500()(scope, receive, send)

        method = scope.get("method", "")
        path = scope.get("path", "")
        cookie_values = _cookie_values(
            scope, (ACCESS_COOKIE_NAME,) + _LEGACY_COOKIE_NAMES
        )
        access_values = cookie_values[ACCESS_COOKIE_NAME]
        raw_token = access_values[0] if len(access_values) == 1 else None
        bad_access_cookie = len(access_values) > 1
        cleanup_names = [
            name for name in _LEGACY_COOKIE_NAMES if cookie_values[name]
        ]
        cleanup_names_on_401 = []

        context = None
        if raw_token is not None and not _skip_resolution(method, path):
            try:
                context = session_service.resolve_session(
                    self.db_path,
                    raw_token,
                    now=request_now,
                    renew_activity=_should_renew(method, path),
                )
            except session_service.AccessSessionError as exc:
                if exc.code == "authentication_required":
                    context = None
                    bad_access_cookie = True
                else:
                    response = _generic_500()
                    return await response(
                        scope,
                        receive,
                        _cleanup_sender(send, scope, cleanup_names),
                    )
            except BaseException:
                response = _generic_500()
                return await response(
                    scope,
                    receive,
                    _cleanup_sender(send, scope, cleanup_names),
                )
            if context is None:
                bad_access_cookie = True

        user = context.get("user") if type(context) is dict else None
        session = context.get("session") if type(context) is dict else None
        if context is not None and (type(user) is not dict or type(session) is not dict):
            response = _generic_500()
            return await response(
                scope,
                receive,
                _cleanup_sender(send, scope, cleanup_names),
            )

        if bad_access_cookie:
            if (method, path) == ("POST", "/api/auth/logout"):
                cleanup_names_on_401.append(ACCESS_COOKIE_NAME)
            else:
                cleanup_names.append(ACCESS_COOKIE_NAME)
        if (method, path) == ("POST", "/api/auth/logout") and user is None:
            cleanup_names_on_401.append(ACCESS_COOKIE_NAME)

        is_protected_api = path.startswith("/api/") and not _is_public_request(
            method, path
        )
        if is_protected_api and user is None:
            response = _authentication_required()
            return await response(
                scope,
                receive,
                _cleanup_sender(send, scope, cleanup_names),
            )

        user_token = _current_access_user.set(user)
        session_token = _current_access_session.set(session)
        time_token = _current_access_request_time.set(request_now)
        try:
            return await self.app(
                scope,
                receive,
                _cleanup_sender(
                    send,
                    scope,
                    cleanup_names,
                    names_on_401=cleanup_names_on_401,
                ),
            )
        finally:
            _current_access_request_time.reset(time_token)
            _current_access_session.reset(session_token)
            _current_access_user.reset(user_token)


def setup_access_auth_context(app, db_path, *, now_provider=_utc_now):
    app.add_middleware(
        AccessAuthContextMiddleware,
        db_path=db_path,
        now_provider=now_provider,
    )


class AccessPageGateMiddleware:
    """Serve the v3 login page while keeping the existing static app shell."""

    def __init__(self, app, db_path):
        self.app = app
        self.db_path = db_path

    def _login_response(self):
        from local_app.access_login import render_access_login_html
        from local_app.db import DEFAULT_CLINIC_NAME, clinic_display_name

        try:
            database_exists = Path(self.db_path).is_file()
        except (OSError, TypeError, ValueError):
            database_exists = False
        clinic_name = (
            clinic_display_name(self.db_path)
            if database_exists
            else DEFAULT_CLINIC_NAME
        )

        return HTMLResponse(
            render_access_login_html(clinic_name),
            headers={"Cache-Control": "no-store"},
        )

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        method = scope.get("method", "")
        path = scope.get("path", "")
        if method in ("GET", "HEAD"):
            fixed_login = path in ("/__login", "/login")
            protected_shell = path in ("/", "/index.html")
            if fixed_login or (
                protected_shell and current_access_user() is None
            ):
                return await self._login_response()(scope, receive, send)
        return await self.app(scope, receive, send)


def setup_access_page_gate(app, db_path):
    app.add_middleware(AccessPageGateMiddleware, db_path=db_path)


def _request_metadata(request):
    client = request.scope.get("client")
    ip_address = client[0] if type(client) in (tuple, list) and client else ""
    return {
        "request_id": secrets.token_hex(8),
        "ip_address": ip_address,
        "user_agent": request.headers.get("user-agent", ""),
    }


async def _json_object(request):
    try:
        value = await request.json()
    except BaseException:
        return None
    return value if type(value) is dict else None


def _login_denied():
    return JSONResponse({"detail": "用户名或密码错误"}, status_code=401)


def _password_denied():
    return JSONResponse({"detail": "密码错误"}, status_code=401)


def _request_invalid():
    return JSONResponse({"detail": "请求格式不正确"}, status_code=400)


def _login_input_error(code):
    return code in {
        "invalid_credentials",
        "username_invalid",
        "device_name_invalid",
        "user_agent_invalid",
    }


def register_access_auth_routes(app, db_path):
    @app.post("/api/auth/login")
    async def access_login(request: Request):
        payload = await _json_object(request)
        if payload is None:
            return _login_denied()
        try:
            metadata = _request_metadata(request)
            raw_token, context = session_service.authenticate_and_create_session(
                db_path,
                username=payload.get("username"),
                password=payload.get("password"),
                device_name=payload.get("device_name", ""),
                now=current_access_request_time(),
                **metadata,
            )
            user = context["user"]
            if type(user) is not dict:
                raise TypeError
        except session_service.AccessSessionError as exc:
            if _login_input_error(exc.code):
                return _login_denied()
            return _generic_500()
        except BaseException:
            return _generic_500()

        response = JSONResponse({"user": user})
        response.set_cookie(
            ACCESS_COOKIE_NAME,
            raw_token,
            path="/",
            secure=_cookie_secure(request),
            httponly=True,
            samesite="lax",
        )
        return response

    @app.post("/api/auth/logout")
    async def access_logout(request: Request):
        values = _cookie_values(request.scope, (ACCESS_COOKIE_NAME,))[ACCESS_COOKIE_NAME]
        raw_token = values[0] if len(values) == 1 else None
        if raw_token is None:
            response = _authentication_required()
            _expire_cookie(response, ACCESS_COOKIE_NAME, request)
            return response
        try:
            removed = session_service.logout_session(
                db_path,
                raw_token=raw_token,
                now=current_access_request_time(),
                **_request_metadata(request),
            )
        except session_service.AccessSessionError as exc:
            if exc.code == "user_agent_invalid":
                return _request_invalid()
            return _generic_500()
        except BaseException:
            return _generic_500()
        if removed is not True:
            response = _authentication_required()
            _expire_cookie(response, ACCESS_COOKIE_NAME, request)
            return response
        response = JSONResponse({"ok": True})
        _expire_cookie(response, ACCESS_COOKIE_NAME, request)
        return response

    @app.get("/api/auth/me")
    async def access_me():
        from local_app.access_authorization import (
            AccessPrincipal,
            current_access_principal,
        )

        principal = current_access_principal()
        if not isinstance(principal, AccessPrincipal):
            return _authentication_required()
        return {
            "user": {
                "id": principal.user_id,
                "username": principal.username,
                "display_name": principal.display_name,
                "staff_id": principal.staff_id,
                "is_system_admin": principal.is_system_admin is True,
                "primary_role": principal.primary_role,
                "role_keys": list(principal.role_keys),
                "permissions": sorted(principal.role_permissions),
            }
        }

    @app.post("/api/auth/reauth")
    async def access_reauth(request: Request):
        payload = await _json_object(request)
        if payload is None:
            return _password_denied()
        session = current_access_session()
        if type(session) is not dict:
            response = _authentication_required()
            _expire_cookie(response, ACCESS_COOKIE_NAME, request)
            return response
        try:
            accepted = session_service.reauthenticate_session(
                db_path,
                session_id=session.get("session_id"),
                password=payload.get("password"),
                now=current_access_request_time(),
                **_request_metadata(request),
            )
        except session_service.AccessSessionError as exc:
            if exc.code == "authentication_required":
                response = _authentication_required()
                _expire_cookie(response, ACCESS_COOKIE_NAME, request)
                return response
            if exc.code == "user_agent_invalid":
                return _request_invalid()
            return _generic_500()
        except BaseException:
            return _generic_500()
        if accepted is not True:
            return _password_denied()
        return {"ok": True}
