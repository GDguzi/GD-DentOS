"""患者路由公共校验（同目录共享模块，先例见 warehouse_common.py）。

审计R2:此前 require_patient 在各路由里散落多份拷贝,的软删口径只落进了 1 份,
导致被合并(is_deleted=1)的旧档案仍能写入新预约/处置/储值。统一收敛到这一份。
"""
from fastapi import HTTPException


def require_patient(conn, patient_identity):
    # 已软删/已被合并的患者按不存在处理——旧身份不得再挂任何新业务数据
    exists = conn.execute(
        "select 1 from patients where patient_identity = ? and coalesce(is_deleted, 0) = 0",
        (patient_identity,),
    ).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="patient not found")
