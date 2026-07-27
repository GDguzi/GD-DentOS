"""v3 目标模式下旧栈守卫不得静默退化成"只判登录"。

- require_admin:患者合并/储值退现等"仅管理员"硬闸,v3 下必须仍只放行系统管理员或 admin 角色;
  普通角色即便勾了对应权限点也 403(路由策略层是第一道门,这里是第二道硬闸,双门都要过)。
- require_perm:能映射到 v3 权限的动作必须真查权限(如 回收站的 patient.view 二次门禁);
  无 v3 对应物的动作交路由策略层把关(放行不属兜底——策略层每路由必有一条策略,有守卫测试锁定)。
"""

import unittest
from contextlib import contextmanager

from fastapi import HTTPException

from local_app import access_authorization as authorization
from local_app import auth
from local_app.access_policy import ALL_PERMISSIONS


def principal(*, system_admin=False, roles=("front_desk",), permissions=frozenset(), username="u1"):
    return authorization.AccessPrincipal(
        user_id="user-" + username,
        username=username,
        display_name=username,
        staff_id="staff-" + username,
        is_system_admin=system_admin,
        primary_role=roles[0],
        role_keys=tuple(roles),
        permissions=frozenset(permissions),
        role_permissions=frozenset(permissions),
    )


@contextmanager
def target_request(actor):
    active_token = authorization._access_authorizer_active.set(True)
    principal_token = authorization._current_access_principal.set(actor)
    try:
        yield
    finally:
        authorization._current_access_principal.reset(principal_token)
        authorization._access_authorizer_active.reset(active_token)


class RequireAdminV3Test(unittest.TestCase):
    def test_ordinary_role_with_perm_still_403(self):
        # 前台勾了 membership.refund 权限点 ≠ 管理员:require_admin 硬闸必须仍然 403
        actor = principal(permissions={"membership.refund", "patient.profile.merge"})
        with target_request(actor), self.assertRaises(HTTPException) as caught:
            auth.require_admin()
        self.assertEqual(caught.exception.status_code, 403)

    def test_system_admin_passes(self):
        with target_request(principal(system_admin=True, username="boss")):
            u = auth.require_admin()
        self.assertEqual(u["username"], "boss")

    def test_admin_role_alone_does_not_pass(self):
        """v3 只认 is_system_admin：光有角色名 "admin" 不再是管理员。

        空库 v3 会种出零权限的遗留 admin 角色；若继续认它，任何拿到 staff.edit
        的账号都能给自己勾上该角色越过本守卫（提权已实测复现）。
        """
        with target_request(principal(roles=("admin",), username="mgr")), \
             self.assertRaises(HTTPException) as caught:
            auth.require_admin()
        self.assertEqual(caught.exception.status_code, 403)

    def test_unauthenticated_401(self):
        with target_request(None), self.assertRaises(HTTPException) as caught:
            auth.require_admin()
        self.assertEqual(caught.exception.status_code, 401)

    def test_fake_admin_values_rejected(self):
        # 伪值不得冒充管理员——is_system_admin 只认内建 True,角色名精确匹配 "admin"
        for actor in (
            principal(system_admin=1, username="int1"),        # int 1 ≠ True(经兼容层转 False)
            principal(system_admin="true", username="strtrue"),
            principal(roles=("ADMIN",), username="upper"),
            principal(roles=("admin ",), username="trailing"),
            principal(roles=("admin",), username="exactrole"),   # 精确角色名同样不放行
        ):
            with self.subTest(actor=actor.username), target_request(actor), \
                 self.assertRaises(HTTPException) as caught:
                auth.require_admin()
            self.assertEqual(caught.exception.status_code, 403)


class RequirePermV3Test(unittest.TestCase):
    def test_mapped_action_missing_perm_403(self):
        # 回收站场景:只有 recycle.view、没有 patient.profile.view 的角色,
        # require_perm("patient.view") 必须 403,不得看到患者名+金额
        actor = principal(permissions={"recycle.view"})
        with target_request(actor), self.assertRaises(HTTPException) as caught:
            auth.require_perm("patient.view")
        self.assertEqual(caught.exception.status_code, 403)

    def test_mapped_action_with_perm_passes(self):
        actor = principal(permissions={"recycle.view", "patient.profile.view"})
        with target_request(actor):
            u = auth.require_perm("patient.view")
        self.assertIn("patient.profile.view", u["permissions"])

    def test_legacy_key_coinciding_with_v3_enforced(self):
        # legacy 动作名本身就是合法 v3 权限键(如 master_data.manage):同样要真查
        self.assertIn("master_data.manage", ALL_PERMISSIONS)   # 前提自检
        actor = principal(permissions={"recycle.view"})
        with target_request(actor), self.assertRaises(HTTPException) as caught:
            auth.require_perm("master_data.manage")
        self.assertEqual(caught.exception.status_code, 403)

    def test_legacy_key_coinciding_with_v3_passes_when_granted(self):
        # 持权时同名键动作必须通过,不得误伤
        actor = principal(permissions={"master_data.manage"})
        with target_request(actor):
            u = auth.require_perm("master_data.manage")
        self.assertIn("master_data.manage", u["permissions"])

    def test_unmapped_action_delegates_to_policy_layer(self):
        # 无 v3 对应物的动作(如 sterilize.manage)交路由策略层把关,这里放行
        self.assertNotIn("sterilize.manage", ALL_PERMISSIONS)  # 前提自检
        self.assertNotIn("sterilize.manage", auth._TARGET_HAS_PERM_ALIASES)
        actor = principal(permissions={"recycle.view"})
        with target_request(actor):
            u = auth.require_perm("sterilize.manage")
        self.assertEqual(u["username"], "u1")

    def test_system_admin_bypasses(self):
        with target_request(principal(system_admin=True)):
            auth.require_perm("patient.view")   # 不抛即通过


if __name__ == "__main__":
    unittest.main()
