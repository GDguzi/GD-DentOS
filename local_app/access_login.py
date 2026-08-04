"""Pure renderer for the access-v3 login page."""

import html


def render_access_login_html(clinic_name):
    """Return the standalone Chinese login page for ``clinic_name``."""
    if type(clinic_name) is not str or not clinic_name.strip():
        raise ValueError("clinic_name_invalid")
    try:
        clinic_name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("clinic_name_invalid") from exc

    escaped_clinic_name = html.escape(clinic_name, quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_clinic_name} · 登录</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      color: #17343b;
      background: #eef7f7;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
    }}
    .login-card {{
      width: min(100%, 400px);
      padding: 34px;
      border: 1px solid #d7e7e8;
      border-radius: 18px;
      background: #fff;
      box-shadow: 0 18px 48px rgba(28, 76, 84, .12);
    }}
    .eyebrow {{ margin: 0 0 8px; color: #43858d; font-size: 14px; font-weight: 700; }}
    h1 {{ margin: 0; font-size: 26px; line-height: 1.3; }}
    .subtitle {{ margin: 8px 0 28px; color: #688087; font-size: 15px; }}
    label {{ display: block; margin: 16px 0 7px; font-weight: 650; }}
    input[type="text"], input[type="password"] {{
      width: 100%;
      height: 46px;
      padding: 0 13px;
      border: 1px solid #c9dcde;
      border-radius: 10px;
      color: #17343b;
      background: #fbfefe;
      font: inherit;
    }}
    input:focus {{ outline: 3px solid rgba(61, 155, 165, .18); border-color: #3d9ba5; }}
    .remember {{ display: flex; align-items: center; gap: 8px; margin: 17px 0; font-weight: 500; }}
    .remember input {{ width: 17px; height: 17px; accent-color: #328b95; }}
    button {{
      width: 100%;
      height: 46px;
      border: 0;
      border-radius: 10px;
      color: #fff;
      background: #328b95;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: #287c85; }}
    button:disabled {{ cursor: wait; opacity: .65; }}
    .error {{ min-height: 22px; margin: 12px 0 0; color: #b42318; font-size: 14px; }}
  </style>
</head>
<body>
  <main class="login-card">
    <p class="eyebrow">诊所管理系统</p>
    <h1>{escaped_clinic_name}</h1>
    <p class="subtitle">请输入你的账号和密码</p>
    <form id="loginForm" action="/api/auth/login" method="post">
      <label for="username">账号</label>
      <input id="username" name="username" type="text" autocomplete="username" required autofocus>
      <label for="password">密码</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <label class="remember" for="rememberUsername">
        <input id="rememberUsername" name="rememberUsername" type="checkbox">
        <span>记住账号</span>
      </label>
      <button id="loginButton" type="submit">登录</button>
      <p id="loginError" class="error" role="status" aria-live="polite"></p>
    </form>
  </main>
  <script>
  (() => {{
    "use strict";
    const form = document.getElementById("loginForm");
    const usernameInput = document.getElementById("username");
    const passwordInput = document.getElementById("password");
    const rememberUsernameInput = document.getElementById("rememberUsername");
    const loginButton = document.getElementById("loginButton");
    const loginError = document.getElementById("loginError");

    try {{
      const rememberedUsername = localStorage.getItem("dl.rememberedUsername.v1");
      if (typeof rememberedUsername === "string" && rememberedUsername.trim() !== "") {{
        usernameInput.value = rememberedUsername;
        rememberUsernameInput.checked = true;
      }}
    }} catch (error) {{
      // Storage can be unavailable in private or restricted browser contexts.
    }}

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      loginError.textContent = "";
      loginButton.disabled = true;
      const payload = {{
        username: usernameInput.value,
        password: passwordInput.value,
      }};

      try {{
        const response = await fetch("/api/auth/login", {{
          method: "POST",
          credentials: "same-origin",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify(payload),
        }});
        let data = {{}};
        try {{ data = await response.json(); }} catch (error) {{}}
        if (!response.ok) {{
          loginError.textContent = data.detail || "账号或密码不正确";
          return;
        }}

        try {{
          if (rememberUsernameInput.checked) {{
            localStorage.setItem("dl.rememberedUsername.v1", usernameInput.value);
          }} else {{
            localStorage.removeItem("dl.rememberedUsername.v1");
          }}
        }} catch (error) {{}}
        if (payload.password === "admin") {{
          window.alert("当前是默认密码 admin，请进系统后到「配置管理 → 人员与权限 → 账号管理」尽快修改。");
        }}
        location.assign("/");
      }} catch (error) {{
        loginError.textContent = "暂时无法登录，请稍后重试";
      }} finally {{
        loginButton.disabled = false;
      }}
    }});
  }})();
  </script>
</body>
</html>
"""
