import datetime as dt
import importlib.util
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("local_app")

_DB_PATH_UNSET = object()

from local_app.db import APP_DIR, DEFAULT_DB_PATH
from local_app.routes.ai_drafts import create_ai_drafts_router
from local_app.routes.master_data import create_master_data_router
from local_app.routes.patient_assets import create_patient_assets_router
from local_app.routes.audit_logs import create_audit_logs_router  # === 审计日志查询 ===
from local_app.routes.stats import create_stats_router  # === 首页统计 ===
from local_app.routes.today_work import create_today_work_router  # === 今日工作台 ===
from local_app.routes.today_inspection import create_today_inspection_router  # === 今日全量工作检查 ===
from local_app.routes.versions import create_versions_router  # === 版本历史 ===
from local_app.routes.templates import create_templates_router
from local_app.routes.treatment_plans import create_treatment_plans_router  # === 治疗计划 ===
from local_app.routes.oral_exams import create_oral_exams_router  # === 口腔检查 ===
from local_app.routes.consults import create_consults_router  # === 咨询沟通 ===
from local_app.routes.calls import create_calls_router  # === 通话录音 ===
from local_app.routes.communications import create_communications_router  # === 沟通记录 ===
from local_app.routes.customer_hub import create_customer_hub_router  # === 客户通今日总览 ===
from local_app.routes.return_visit_ops import create_return_visit_ops_router  # === 回访工作流 ===
from local_app.routes.return_visits import create_return_visits_router  # === 回访列表/更新 ===
from local_app.routes.surgeries import create_surgeries_router  # === 手术记录 ===
from local_app.routes.patient_create import create_patient_create_router  # === 患者建档 ===
from local_app.routes.referral_sources import create_referral_sources_router  # === 患者来源三级树 ===
from local_app.routes.follow_ups import create_follow_ups_router  # === 今日待跟进(派生) ===
from local_app.routes.patient_tags import create_patient_tags_router  # === 患者标签(大客户经营) ===
from local_app.routes.reports import create_reports_router  # === 财务报表 ===
from local_app.routes.store_stats import create_store_stats_router  # === 店内统计 ===
from local_app.routes.medical_records import create_medical_records_router  # === 病历新建/更新 ===
from local_app.routes.record_items import create_record_items_router  # === 牙位结构化病历条目 ===
from local_app.routes.treatment_orders import create_treatment_orders_router  # === 直接处置开单 ===
from local_app.routes.lab_orders import create_lab_orders_router  # === 技工单 ===
from local_app.routes.backup import create_backup_router  # === 数据备份 ===
from local_app.routes.billing_ops import create_billing_ops_router  # === 收款确认 ===
from local_app.routes.bills import create_bills_router  # === 账单列表 ===
from local_app.routes.staff import create_staff_router  # === 配台人员 ===
from local_app.routes.consent_forms import create_consent_forms_router  # === 知情同意书 ===
from local_app.routes.warehouse_items import create_warehouse_items_router
from local_app.routes.warehouse_stock import create_warehouse_stock_router
from local_app.routes.warehouse_stock_in import create_warehouse_stock_in_router
from local_app.routes.warehouse_stock_out import create_warehouse_stock_out_router
from local_app.routes.warehouse_stock_take import create_warehouse_stock_take_router
from local_app.routes.warehouse_suppliers import create_warehouse_suppliers_router
from local_app.routes.sterilization import create_sterilization_router  # === 消毒/灭菌追溯 ===
from local_app.routes.sterilize_dispatch import create_sterilize_dispatch_router  # === 消毒·送消单 ===
from local_app.routes.app_settings import create_app_settings_router  # === 应用设置 ===
from local_app.routes.users import create_users_router  # === 账号管理 ===
from local_app.routes.recycle_bin import create_recycle_bin_router  # === 回收站 ===
from local_app.routes.appointments import create_appointments_router  # === 预约CRUD ===
# 同步中心/预约对账待核对属可选迁移扩展配套:扩展在场才 import+挂载,见 migration_available()
from local_app.routes.user_settings import create_user_settings_router  # === 用户个性化 ===
from local_app.routes.patient_groups import create_patient_groups_router  # === 患者分类 ===
from local_app.routes.patients import create_patients_router  # === 患者列表/导出/详情/更新/备注 ===
from local_app.routes.patient_merge import create_patient_merge_router  # === 患者合并 ===
from local_app.routes.membership import create_membership_router  # === 会员储值 ===
from local_app.routes.role_permissions import create_role_permissions_router  # === 角色权限矩阵 ===
from local_app.routes.ocr import create_ocr_router  # === 拍照建档 OCR ===


class NoStoreHTMLStatic(StaticFiles):
    """HTML 文档禁缓存(登出后浏览器启发式缓存回放登录期骨架);js/css 不动,仍走 ?v= 版本缓存。"""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        if resp.headers.get("content-type", "").startswith("text/html"):
            resp.headers["Cache-Control"] = "no-store"
        return resp


def migration_available():
    """可选迁移扩展(migration_layer 包)是否在场。不在场 →
    同步中心/预约对账路由不挂载、前端对应入口不显示。
    探测包本身而非子模块:父包缺席时 find_spec(子模块) 抛 ModuleNotFoundError 而非返回 None。"""
    return importlib.util.find_spec("local_app.migration_layer") is not None


def create_app(
    db_path=_DB_PATH_UNSET,
    images_dir=None,
    access_password="",
    require_login=False,
    access_v3=False,
):
    explicit_db_path = db_path is not _DB_PATH_UNSET
    if not explicit_db_path:
        if access_v3:
            raise ValueError("access_v3 requires an explicit db_path")
        db_path = DEFAULT_DB_PATH
    if access_v3 and access_password:
        raise ValueError("access_v3 cannot use access_password")

    db_path = Path(db_path)
    if access_v3:
        from local_app.access_authorization import build_access_authorizer

        app = FastAPI(
            title="本地牙科门诊系统",
            dependencies=[Depends(build_access_authorizer(db_path))],
        )
    else:
        app = FastAPI(title="本地牙科门诊系统")
    images_dir = Path(images_dir) if images_dir else APP_DIR / "data" / "images"
    if access_v3:
        from local_app.access_auth import (
            register_access_auth_routes,
            setup_access_auth_context,
            setup_access_page_gate,
        )
        from local_app.auth import OperationAuditMiddleware

        register_access_auth_routes(app, db_path)
        # add_middleware 后加者在外：目标顺序为认证上下文 → 页面网关 → 操作审计。
        app.add_middleware(OperationAuditMiddleware, db_path=db_path)
        setup_access_page_gate(app, db_path)
        setup_access_auth_context(app, db_path)
    else:
        # 局域网门禁(#B,旧)：仅当显式传访问口令时挂；账号系统上线后由 require_login 取代，默认不挂。
        if access_password:
            from local_app.lan_auth import setup_lan_auth
            setup_lan_auth(app, access_password, db_path)
        # 账号系统：登录态 contextvar 中间件始终挂(无害,可测)；require_login 时拦截未登录。
        from local_app.auth import setup_auth
        setup_auth(app, db_path, require_login=require_login)
    app.include_router(create_stats_router(db_path))  # === 首页统计 ===
    app.include_router(create_today_work_router(db_path))  # === 今日工作台 ===
    app.include_router(create_today_inspection_router(db_path))  # === 今日全量工作检查 ===
    app.include_router(create_audit_logs_router(db_path))  # === 审计日志查询 ===
    app.include_router(create_versions_router(db_path))  # === 版本历史 ===
    has_migration = migration_available()
    if has_migration:
        from local_app.migration_layer.routes.sync import create_sync_router
        app.include_router(create_sync_router(db_path))
        from local_app.migration_layer.routes.ct_studies import create_ct_studies_router
        app.include_router(create_ct_studies_router(db_path))  # === CT/CBCT 云端档案+阅片跳转 ===
    app.include_router(create_patient_assets_router(db_path, images_dir))
    app.include_router(create_ai_drafts_router(db_path))
    app.include_router(create_templates_router(db_path))
    app.include_router(create_master_data_router(db_path, access_v3=access_v3))
    app.include_router(create_treatment_plans_router(db_path))  # === 治疗计划 ===
    app.include_router(create_oral_exams_router(db_path))  # === 口腔检查 ===
    app.include_router(create_consults_router(db_path))  # === 咨询沟通 ===
    app.include_router(create_calls_router(db_path, images_dir))  # === 通话录音 ===
    app.include_router(create_communications_router(db_path, images_dir))  # === 沟通记录 ===
    app.include_router(create_customer_hub_router(db_path))  # === 客户通今日总览 ===
    app.include_router(create_return_visit_ops_router(db_path, images_dir))  # === 回访工作流 ===
    app.include_router(create_return_visits_router(db_path))  # === 回访列表/更新 ===
    app.include_router(create_surgeries_router(db_path))  # === 手术记录 ===
    app.include_router(create_patient_create_router(db_path))  # === 患者建档 ===
    app.include_router(create_referral_sources_router(db_path))  # === 患者来源三级树 ===
    app.include_router(create_follow_ups_router(db_path))  # === 今日待跟进(派生) ===
    app.include_router(create_patient_tags_router(db_path))  # === 患者标签(大客户经营) ===
    app.include_router(create_reports_router(db_path))  # === 财务报表 ===
    app.include_router(create_medical_records_router(db_path))  # === 病历新建/更新 ===
    app.include_router(create_record_items_router(db_path))  # === 牙位结构化病历条目 ===
    app.include_router(create_treatment_orders_router(db_path))  # === 直接处置开单 ===
    app.include_router(create_lab_orders_router(db_path, images_dir))  # === 技工单 ===
    app.include_router(create_backup_router(db_path, images_dir))  # === 数据备份 ===
    app.include_router(create_billing_ops_router(db_path))  # === 收款确认 ===
    app.include_router(create_bills_router(db_path))  # === 账单列表 ===
    app.include_router(
        create_staff_router(db_path, access_v3=access_v3)
    )  # === 配台人员 ===
    app.include_router(create_consent_forms_router(db_path))  # === 知情同意书 ===
    # === 库房 ===
    app.include_router(create_warehouse_items_router(db_path))
    app.include_router(create_warehouse_suppliers_router(db_path))
    app.include_router(create_warehouse_stock_in_router(db_path))
    app.include_router(create_warehouse_stock_router(db_path))
    app.include_router(create_warehouse_stock_out_router(db_path))
    app.include_router(create_warehouse_stock_take_router(db_path))
    # === 消毒/灭菌追溯 ===
    app.include_router(create_sterilization_router(db_path))
    app.include_router(create_sterilize_dispatch_router(db_path))
    # === 应用设置 ===
    app.include_router(create_app_settings_router(db_path))
    # === 账号管理 ===
    app.include_router(create_users_router(db_path, access_v3=access_v3))
    # === 回收站 ===
    app.include_router(
        create_recycle_bin_router(db_path, access_v3=access_v3)
    )
    if has_migration:
        from local_app.migration_layer.routes.appointment_reconcile import create_appointment_reconcile_router
        app.include_router(create_appointment_reconcile_router(db_path))  # === 预约对账待核对(迁移扩展配套) ===
    # 预约CRUD必须挂在reconcile之后:/api/appointments/{id}会吞掉先注册才轮不到的/api/appointments/suspect-cancelled
    app.include_router(create_appointments_router(db_path))  # === 预约CRUD ===

    @app.get("/api/capabilities")
    def capabilities():
        """前端按能力显隐迁移扩展入口(同步中心/影像资料运维);扩展不在场 → false。"""
        return {"sync": has_migration}
    # === 用户个性化 ===
    app.include_router(create_user_settings_router(db_path))
    # === 患者分类 ===
    app.include_router(create_patient_groups_router(db_path))
    # === 患者合并 ===
    app.include_router(create_patient_merge_router(db_path))
    # === 会员储值 ===
    app.include_router(create_membership_router(db_path))
    app.include_router(
        create_role_permissions_router(db_path, access_v3=access_v3)
    )  # === 角色权限矩阵 ===
    app.include_router(create_ocr_router())  # === 拍照建档 OCR ===
    app.include_router(create_store_stats_router(db_path))  # === 店内统计(只读聚合) ===
    # 患者CRUD挂在最后:保持原闭包时代的路由匹配顺序(/api/patients/{id}不得吞掉先注册子路径)
    app.include_router(create_patients_router(db_path))  # === 患者列表/导出/详情/更新/备注 ===

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception):
        # 兜底任何未预期异常：落日志（含栈），但只回结构化 500，绝不把
        # 堆栈/SQL/患者数据漏给前端。HTTPException 由 FastAPI 自行处理，不进这里。
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请稍后重试"})

    @app.get("/api/health")
    def health():
        return {"ok": True}

    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount("/", NoStoreHTMLStatic(directory=static_dir, html=True), name="static")

    return app
