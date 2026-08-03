"""#617第3/4步守卫:配置管理新增「全店设置」「数据备份」子tab。
全店设置=V3 clinic_settings.manage / 旧版 settings.manage 显隐，集中编辑诊所名/营业时间/最小时段/医生列/诊室/会员开关，
复用既有4个 PUT 接口(不新造后端);数据备份=仅管理员(后端 require_admin 是闸门,前端同口径),
立即备份(可选打包影像)+列表+下载。源码级断言防丢。"""
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent.parent / "local_app" / "static" / "config_center.js").read_text(encoding="utf-8")


class ConfigShopBackup617Test(unittest.TestCase):
    def test_shop_tab_accepts_target_and_legacy_settings_permissions(self):
        self.assertIn(
            'hasAny("clinic_settings.manage", "settings.manage")', SRC
        )

    def test_backup_tab_admin_only(self):
        self.assertIn('window.__userRole === "admin"', SRC)
        self.assertIn('{key: "backup"', SRC)

    def test_shop_saves_via_existing_puts(self):
        for ep in ("/api/settings/clinic", "/api/settings/appointment",
                   "/api/settings/rooms", "/api/settings/features"):
            self.assertIn(f'fetch("{ep}", {{method: "PUT"', SRC, ep)

    def test_backup_uses_existing_endpoints(self):
        self.assertIn('fetch("/api/backup/list")', SRC)
        self.assertIn('fetch("/api/backup", {method: "POST"', SRC)
        self.assertIn("include_images", SRC)


if __name__ == "__main__":
    unittest.main()
