"""局域网访问门禁 (B 联机签字)。

服务监听局域网时，非本机设备(iPad/手机)须过一道访问口令；本机(127.0.0.1)免密直连。
登录一次写 cookie，设备记住 30 天。单口令、单 token，诊所场景够用，不搞账号体系。
口令为空(默认/只本机)时不挂门禁——create_app 只在显式传口令时才调本模块。
"""
import secrets

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_OPEN_PATHS = {"/__login", "/api/login"}

# 抬头显示配置的诊所名(占位符渲染时替换,开源壳阶段1去硬编码)
LOGIN_HTML_TMPL = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>访问口令</title>
<style>body{font-family:-apple-system,"PingFang SC",sans-serif;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0;background:#f1f4f7}.box{background:#fff;padding:28px 24px;
border-radius:14px;box-shadow:0 6px 24px rgba(0,0,0,.08);width:min(320px,86vw);text-align:center}
h2{margin:0 0 4px;font-size:18px}p{color:#888;font-size:13px;margin:0 0 18px}
input{width:100%;box-sizing:border-box;padding:11px 12px;font-size:16px;border:1px solid #d6dde3;
border-radius:8px;margin-bottom:12px}button{width:100%;padding:11px;font-size:16px;border:0;
border-radius:8px;background:#2c8c6f;color:#fff;cursor:pointer}#msg{color:#c0392b;font-size:13px;
margin-top:10px;min-height:18px}</style></head><body>
<div class="box"><h2>__CLINIC_NAME__ · 本地系统</h2><p>请输入访问口令</p>
<input id="pw" type="password" placeholder="访问口令" autofocus>
<button onclick="go()">进入</button><div id="msg"></div></div>
<script>
async function go(){var pw=document.getElementById('pw').value;
var r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({password:pw})});
if(r.ok){location.href='/';}else{document.getElementById('msg').textContent='口令错误，请重试';}}
document.getElementById('pw').addEventListener('keydown',function(e){if(e.key==='Enter')go();});
</script></body></html>"""


def setup_lan_auth(app, password, db_path):
    """给 app 挂局域网门禁。返回本次会话 token(测试用)。db_path 用于口令页显示配置的诊所名。"""
    token = secrets.token_hex(16)

    def _login_html():
        import html as _html
        from local_app.db import clinic_display_name
        return LOGIN_HTML_TMPL.replace("__CLINIC_NAME__", _html.escape(clinic_display_name(db_path)))

    @app.post("/api/login")
    async def _login(payload: dict):
        # 原值直比,不 str() 强转：JSON 数字 1 不得命中字符串口令 "1"
        if (payload or {}).get("password") != password:
            raise HTTPException(status_code=401, detail="访问口令错误")
        resp = JSONResponse({"ok": True})
        resp.set_cookie("dental_auth", token, httponly=True, samesite="lax", max_age=2592000)
        return resp

    @app.get("/__login")
    async def _login_page():
        return HTMLResponse(_login_html())

    @app.middleware("http")
    async def _gate(request: Request, call_next):
        host = request.client.host if request.client else ""
        path = request.url.path
        if host in _LOCAL_HOSTS or path in _OPEN_PATHS or request.cookies.get("dental_auth") == token:
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "需要访问口令"}, status_code=401)
        return HTMLResponse(_login_html())   # 页面请求 → 给登录页

    return token
