"""仪器禁用区间接口。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from .. import db
from ..errors import not_found
from ..models import DisableCreate, serialize_disable
from ..repository import compute_disable_affected, create_disable, revoke_disable
from ..transactions import run_tx

router = APIRouter(prefix="/disables", tags=["disables"])


@router.post("", status_code=201)
def register(payload: DisableCreate):
    """登记禁用区间（左闭右开，终点可空表示持续禁用）。

    - 区间无效（disabled_to <= disabled_from）-> 422 INVALID_INTERVAL
    - 事件号重复 -> 409 DISABLE_EVENT_EXISTS
    - 与同仪器未撤销区间重叠 -> 409 DISABLE_INTERVAL_OVERLAP（边界相接允许）
    - 成功响应顺带返回命中当前禁用区间的已发布报告（快照不改写）
    """
    row, affected = run_tx(lambda conn: create_disable(conn, payload))
    return {"disable": serialize_disable(row), "affected_reports": affected}


@router.post("/{event_no}/revoke")
def revoke(event_no: str):
    """按事件号撤销禁用：仅标记撤销时间，记录保留供追溯。

    - 未知事件号 -> 404 NOT_FOUND
    - 已撤销 -> 409 DISABLE_ALREADY_REVOKED
    """
    row = run_tx(lambda conn: revoke_disable(conn, event_no))
    return {"disable": serialize_disable(row)}


# 注意：静态路由需在 /{event_no} 之前声明
@router.get("/affected-reports")
def affected_reports(instrument: str | None = Query(None)):
    """按当前（未撤销）禁用状态，列出检测时间命中禁用区间的已发布报告及禁用依据。

    只读重算：登记/撤销后结果随之变化；报告原放行记录与快照不改写。
    """
    items = run_tx(lambda conn: compute_disable_affected(conn, instrument), readonly=True)
    return {"items": items, "count": len(items)}


@router.get("")
def list_disables(
    instrument: str | None = Query(None),
    active_only: bool = Query(False, description="仅返回未撤销（生效中）的禁用区间"),
):
    """禁用记录列表（含已撤销，供追溯）。"""
    assert db.pool is not None
    sql = "SELECT * FROM instrument_disables"
    where, params = [], []
    if instrument:
        where.append("instrument = %s")
        params.append(instrument)
    if active_only:
        where.append("revoked_at IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY instrument, disabled_from, id"
    with db.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {"items": [serialize_disable(r) for r in rows]}


@router.get("/{event_no}")
def get_disable(event_no: str):
    """按事件号查询单条禁用记录（含已撤销）。"""
    assert db.pool is not None
    with db.pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM instrument_disables WHERE event_no = %s", (event_no,)
        ).fetchone()
    if row is None:
        raise not_found("禁用事件", event_no)
    return {"disable": serialize_disable(row)}
