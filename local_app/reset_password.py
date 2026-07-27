"""忘记密码救援：在这台电脑上直接重置某账号密码（不需要旧密码）。

用法：
    python3 -m local_app.reset_password <用户名> <新密码>
例：
    python3 -m local_app.reset_password admin 新密码123

会顺带启用该账号(若被停用)并清掉其旧会话(强制重登)。仅本机执行,安全。
"""
import sys

from local_app import auth
from local_app.db import DEFAULT_DB_PATH, connect, init_db
from local_app.timeutil import now_str


def reset(db_path, username, new_password):
    """重置 username 的密码为 new_password。返回 (ok, message)。"""
    if not auth.password_is_nonblank(new_password):
        return False, "新密码不能为空"
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("select id from users where username = ?", (username,)).fetchone()
        if not row:
            return False, f"用户不存在：{username}"
        conn.execute(
            "update users set password_hash = ?, is_active = 1, updated_at = ? where id = ?",
            (auth.hash_password(new_password), now_str(), row["id"]),
        )
        conn.execute("delete from sessions where user_id = ?", (row["id"],))
        conn.commit()
    return True, f"已重置「{username}」的密码（并启用该账号、清除旧会话，需重新登录）。"


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 2:
        print("用法: python3 -m local_app.reset_password <用户名> <新密码>")
        return 1
    ok, msg = reset(DEFAULT_DB_PATH, argv[0], argv[1])
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
