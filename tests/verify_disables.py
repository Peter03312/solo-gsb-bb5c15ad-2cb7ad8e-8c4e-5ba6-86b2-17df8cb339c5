"""禁用区间功能与并发正确性验证（直连数据库，不走 HTTP）。

覆盖：
 1. 命中未撤销禁用区间 -> 422 INSTRUMENT_DISABLED（即使校准通过），返回禁用依据
 2. 左闭右开边界：起点命中、终点放行
 3. 重叠登记拒绝（409 DISABLE_INTERVAL_OVERLAP），边界相接允许
 4. 事件号重复 -> 409 DISABLE_EVENT_EXISTS；无效区间 -> 422 INVALID_INTERVAL
 5. 撤销：未知事件 404 / 重复撤销 409；记录保留可查；撤销后原区间可重新登记
 6. 开放区间（终点为空）持续禁用
 7. 受影响已发布报告查询：登记后出现、撤销后消失；快照不改写
 8. 并发：登记禁用与发布竞跑 —— 禁用生效后不会在区间内误放行
 9. 并发：8 个重叠禁用登记 —— 恰好 1 个成功
10. 并发：撤销与发布竞跑 —— 结果可串行
11. 并发：禁用登记 + 校准更正 + 发布 三方竞跑 —— 不在已生效禁用区间误放行
"""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import HTTPException

import app.db as db
from app.models import (CalibrationCreate, CalibrationCorrect, DisableCreate,
                        ReportSubmit)
from app.repository import (compute_disable_affected, correct_calibration,
                            create_calibration, create_disable, publish_report,
                            revoke_disable)
from app.transactions import run_tx

failures = []

db.open_pool()


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name, detail)
    if not cond:
        failures.append(name)


def reset():
    with db.pool.connection() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db.init_db()


def http_call(fn, *args):
    """返回 (status, payload-or-detail)。返回元组的接口取第一个元素。"""
    try:
        result = run_tx(lambda conn: fn(conn, *args))
        if isinstance(result, tuple):
            result = result[0]
        return 200, result
    except HTTPException as e:
        return e.status_code, e.detail


def dt(month, day, **kw):
    return datetime(2026, month, day, tzinfo=timezone.utc, **kw)


def mk_disable(event_no, instrument, start, end, reason="故障检修"):
    return DisableCreate(event_no=event_no, instrument=instrument,
                         disabled_from=start, disabled_to=end, reason=reason)


def mk_cal(instrument):
    return CalibrationCreate(instrument=instrument, valid_from=dt(1, 1),
                             valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
                             passed=True)


# ---------- 1~2. 命中禁用即阻断（校准通过也不放行）+ 边界 ----------
reset()
s, cal = http_call(create_calibration, mk_cal("D1"))
check("准备：校准登记成功", s == 200, str(cal))

s, d1 = http_call(create_disable, mk_disable("D-1", "D1", dt(3, 1), dt(4, 1)))
check("登记禁用 [3/1,4/1) 成功", s == 200 and d1["revoked_at"] is None, str(d1))

s, d = http_call(publish_report, ReportSubmit(
    instrument="D1", tested_at=dt(3, 15), reference="D1-hit"))
check("命中禁用区间 -> 422 INSTRUMENT_DISABLED（校准通过仍阻断）",
      s == 422 and d["code"] == "INSTRUMENT_DISABLED", str(d))
check("阻断响应携带禁用依据（事件号/区间/原因）",
      d.get("disable", {}).get("event_no") == "D-1"
      and d["disable"].get("reason") == "故障检修"
      and d["disable"].get("disabled_to", "").startswith("2026-04-01"), str(d))

s, d = http_call(publish_report, ReportSubmit(instrument="D1", tested_at=dt(3, 1)))
check("起点 3/1 00:00 命中（左闭）", s == 422 and d["code"] == "INSTRUMENT_DISABLED")
s, r = http_call(publish_report, ReportSubmit(instrument="D1", tested_at=dt(4, 1), reference="D1-edge"))
check("终点 4/1 00:00 放行（右开）", s == 200 and r["released"] is True, str(r))
s, r = http_call(publish_report, ReportSubmit(instrument="D1", tested_at=dt(2, 28), reference="D1-before"))
check("区间前 2/28 放行", s == 200)
with db.pool.connection() as c:
    snap = c.execute("SELECT snapshot FROM reports WHERE reference='D1-edge'").fetchone()["snapshot"]
check("未命中时快照格式沿用校准判定（含 basis_calibration）",
      snap["reason_code"] == "PASSED_CALIBRATION_COVERAGE" and "basis_calibration" in snap, str(snap))

# ---------- 3~4. 重叠/相接/重复事件号/无效区间 ----------
s, d = http_call(create_disable, mk_disable("D-2", "D1", dt(3, 15), dt(5, 1)))
check("与未撤销区间重叠 -> 409 DISABLE_INTERVAL_OVERLAP",
      s == 409 and d["code"] == "DISABLE_INTERVAL_OVERLAP", str(d))
s, d = http_call(create_disable, mk_disable("D-3", "D1", dt(4, 1), dt(5, 1)))
check("边界相接 [4/1,5/1) 允许", s == 200, str(d))
s, d = http_call(create_disable, mk_disable("D-1", "D1", dt(6, 1), dt(7, 1)))
check("事件号重复 -> 409 DISABLE_EVENT_EXISTS", s == 409 and d["code"] == "DISABLE_EVENT_EXISTS", str(d))
s, d = http_call(create_disable, mk_disable("D-9", "D1", dt(7, 1), dt(6, 1)))
check("无效区间（终点<=起点）-> 422 INVALID_INTERVAL", s == 422 and d["code"] == "INVALID_INTERVAL", str(d))
s, d = http_call(create_disable, mk_disable("D-8", "OTHER", dt(3, 1), dt(4, 1)))
check("不同仪器同区间不冲突", s == 200, str(d))

# ---------- 5. 撤销语义 ----------
s, d = http_call(revoke_disable, "NO-SUCH-EVENT")
check("撤销未知事件 -> 404 NOT_FOUND", s == 404 and d["code"] == "NOT_FOUND", str(d))
s, row = http_call(revoke_disable, "D-1")
check("撤销 D-1 成功并记录 revoked_at", s == 200 and row["revoked_at"] is not None, str(row))
s, d = http_call(revoke_disable, "D-1")
check("重复撤销 -> 409 DISABLE_ALREADY_REVOKED", s == 409 and d["code"] == "DISABLE_ALREADY_REVOKED", str(d))
with db.pool.connection() as c:
    kept = c.execute("SELECT count(*) n FROM instrument_disables WHERE event_no='D-1'").fetchone()["n"]
check("撤销后记录保留（可追溯）", kept == 1)
s, r = http_call(publish_report, ReportSubmit(instrument="D1", tested_at=dt(3, 15), reference="D1-after-revoke"))
check("撤销后原命中时点放行", s == 200, str(r))
s, row = http_call(create_disable, mk_disable("D-1R", "D1", dt(3, 1), dt(4, 1)))
check("撤销后原区间可用新事件号重新登记", s == 200, str(row))
http_call(revoke_disable, "D-1R")
http_call(revoke_disable, "D-3")
http_call(revoke_disable, "D-8")

# ---------- 6. 开放区间 ----------
reset()
http_call(create_calibration, mk_cal("D6"))
s, row = http_call(create_disable, mk_disable("D6-OPEN", "D6", dt(6, 1), None))
check("开放禁用区间（终点为空）登记成功", s == 200 and row["disabled_to"] is None, str(row))
s, d = http_call(publish_report, ReportSubmit(instrument="D6", tested_at=datetime(2030, 1, 1, tzinfo=timezone.utc)))
check("开放区间覆盖遥远未来时点 -> 阻断", s == 422 and d["code"] == "INSTRUMENT_DISABLED", str(d))
s, d = http_call(create_disable, mk_disable("D6-X", "D6", datetime(2031, 1, 1, tzinfo=timezone.utc), None))
check("与开放区间重叠 -> 409", s == 409 and d["code"] == "DISABLE_INTERVAL_OVERLAP")

# ---------- 7. 受影响报告查询 + 快照不改写 ----------
reset()
http_call(create_calibration, mk_cal("D7"))
s, r1 = http_call(publish_report, ReportSubmit(instrument="D7", tested_at=dt(6, 1), reference="D7-R1"))
s, r2 = http_call(publish_report, ReportSubmit(instrument="D7", tested_at=dt(8, 1), reference="D7-R2"))
check("准备：两份报告放行", s == 200)

def affected():
    return run_tx(lambda conn: compute_disable_affected(conn, "D7"), readonly=True)

check("登记前受影响列表为空", affected() == [])
row, aff = run_tx(lambda conn: create_disable(
    conn, mk_disable("D7-1", "D7", dt(5, 1), dt(7, 1), "泵故障")))
check("登记响应顺带返回受影响报告", len(aff) == 1 and aff[0]["reference"] == "D7-R1", str(aff))
items = affected()
check("登记后命中区间的报告出现在受影响列表（含禁用依据）",
      len(items) == 1 and items[0]["reference"] == "D7-R1"
      and items[0]["disable"]["event_no"] == "D7-1"
      and items[0]["disable"]["reason"] == "泵故障", str(items))
check("区间外报告不受影响", all(i["reference"] != "D7-R2" for i in items))
check("受影响查询返回原快照（放行判定不变）",
      items and items[0]["snapshot"]["released"] is True
      and items[0]["snapshot"]["reason_code"] == "PASSED_CALIBRATION_COVERAGE", str(items))
with db.pool.connection() as c:
    snap = c.execute("SELECT snapshot FROM reports WHERE reference='D7-R1'").fetchone()["snapshot"]
check("数据库内快照未被改写", snap["released"] is True and snap["reason_code"] == "PASSED_CALIBRATION_COVERAGE")
http_call(revoke_disable, "D7-1")
check("撤销后受影响列表随之清空", affected() == [])

# ---------- 8. 并发：登记禁用 vs 发布 ----------
reset()
http_call(create_calibration, mk_cal("D8"))
N = 12
barrier = threading.Barrier(N + 1)

def race_publish(i):
    barrier.wait()
    try:
        r = run_tx(lambda conn: publish_report(conn, ReportSubmit(
            instrument="D8", tested_at=dt(6, 1), reference=f"D8-race-{i}")))
        return ("released", r["report_id"])
    except HTTPException as e:
        return (e.detail["code"], None)

def race_register():
    barrier.wait()
    try:
        run_tx(lambda conn: create_disable(conn, mk_disable("D8-1", "D8", dt(5, 1), dt(7, 1))))
        return "registered"
    except HTTPException as e:
        return e.detail["code"]

with ThreadPoolExecutor(N + 1) as ex:
    futs = [ex.submit(race_publish, i) for i in range(N)]
    rf = ex.submit(race_register)
    pub_results = [f.result() for f in futs]
    reg_result = rf.result()

check("并发登记最终生效", reg_result == "registered", str(reg_result))
with db.pool.connection() as c:
    # 竞跑结束（禁用已提交）后的服务器时间：此后创建的报告绝不可能命中区间仍放行
    t_after = c.execute("SELECT now() AS t").fetchone()["t"]
    bad = c.execute(
        """
        SELECT r.id FROM reports r
        JOIN instrument_disables d
          ON d.instrument = r.instrument AND d.revoked_at IS NULL
         AND d.disabled_from <= r.tested_at
         AND (d.disabled_to IS NULL OR r.tested_at < d.disabled_to)
        WHERE r.created_at > %s
        """,
        (t_after,),
    ).fetchall()
check("禁用生效后无误放行（竞跑结束后无命中区间的新报告）", not bad, str(bad))
codes = {code for code, _ in pub_results}
check("竞跑结果仅含 released / INSTRUMENT_DISABLED",
      codes <= {"released", "INSTRUMENT_DISABLED"}, str(codes))
s, d = http_call(publish_report, ReportSubmit(instrument="D8", tested_at=dt(6, 1), reference="D8-after"))
check("禁用生效后同点发布 -> 422 INSTRUMENT_DISABLED", s == 422 and d["code"] == "INSTRUMENT_DISABLED", str(d))

# ---------- 9. 并发：重叠禁用登记 ----------
reset()
http_call(create_calibration, mk_cal("D9"))
barrier = threading.Barrier(8)

def race_register_overlap(i):
    barrier.wait()
    try:
        row, _ = run_tx(lambda conn: create_disable(conn, mk_disable(
            f"D9-{i}", "D9", dt(3, 1), dt(9, 1))))
        return ("ok", row["event_no"])
    except HTTPException as e:
        return (e.detail["code"], None)

with ThreadPoolExecutor(8) as ex:
    results = list(ex.map(race_register_overlap, range(8)))
ok = [r for r in results if r[0] == "ok"]
overlaps = [r for r in results if r[0] == "DISABLE_INTERVAL_OVERLAP"]
others = [r for r in results if r[0] not in ("ok", "DISABLE_INTERVAL_OVERLAP")]
check("并发8个重叠登记：恰好1个成功", len(ok) == 1, f"ok={len(ok)} overlap={len(overlaps)} other={others}")
check("其余全部区间冲突", len(overlaps) == 7 and not others, str(others))
with db.pool.connection() as c:
    n = c.execute("SELECT count(*) n FROM instrument_disables WHERE instrument='D9' AND revoked_at IS NULL").fetchone()["n"]
check("生效禁用区间仅1条", n == 1)

# ---------- 10. 并发：撤销 vs 发布 ----------
reset()
http_call(create_calibration, mk_cal("D10"))
http_call(create_disable, mk_disable("D10-1", "D10", dt(5, 1), dt(7, 1)))
N = 8
barrier = threading.Barrier(N + 1)

def race_publish_d10(i):
    barrier.wait()
    try:
        r = run_tx(lambda conn: publish_report(conn, ReportSubmit(
            instrument="D10", tested_at=dt(6, 1), reference=f"D10-race-{i}")))
        return ("released", r["report_id"])
    except HTTPException as e:
        return (e.detail["code"], None)

def race_revoke():
    barrier.wait()
    try:
        run_tx(lambda conn: revoke_disable(conn, "D10-1"))
        return "revoked"
    except HTTPException as e:
        return e.detail["code"]

with ThreadPoolExecutor(N + 1) as ex:
    futs = [ex.submit(race_publish_d10, i) for i in range(N)]
    rf = ex.submit(race_revoke)
    pub_results = [f.result() for f in futs]
    rev_result = rf.result()

check("并发撤销最终生效", rev_result == "revoked", str(rev_result))
codes = {code for code, _ in pub_results}
check("撤销竞跑结果仅含 released / INSTRUMENT_DISABLED（可串行）",
      codes <= {"released", "INSTRUMENT_DISABLED"}, str(codes))
s, r = http_call(publish_report, ReportSubmit(instrument="D10", tested_at=dt(6, 1), reference="D10-after"))
check("撤销生效后同点发布 -> 放行", s == 200, str(r))

# ---------- 11. 并发：禁用登记 + 校准更正 + 发布 三方竞跑 ----------
reset()
s, v1 = http_call(create_calibration, mk_cal("D11"))
N = 12
barrier = threading.Barrier(N + 2)

def race_publish_d11(i):
    barrier.wait()
    try:
        r = run_tx(lambda conn: publish_report(conn, ReportSubmit(
            instrument="D11", tested_at=dt(6, 1), reference=f"D11-race-{i}")))
        return ("released", r["basis_calibration"]["passed"])
    except HTTPException as e:
        return (e.detail["code"], None)

def race_register_d11():
    barrier.wait()
    try:
        run_tx(lambda conn: create_disable(conn, mk_disable("D11-1", "D11", dt(5, 1), dt(7, 1))))
        return "registered"
    except HTTPException as e:
        return e.detail["code"]

def race_correct_d11():
    barrier.wait()
    try:
        run_tx(lambda conn: correct_calibration(conn, CalibrationCorrect(
            corrects_id=v1["id"], version=2,
            valid_from=dt(1, 1), valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
            passed=False)))
        return "corrected"
    except HTTPException as e:
        return e.detail["code"]

with ThreadPoolExecutor(N + 2) as ex:
    futs = [ex.submit(race_publish_d11, i) for i in range(N)]
    rf = ex.submit(race_register_d11)
    cf = ex.submit(race_correct_d11)
    pub_results = [f.result() for f in futs]
    reg_result, corr_result = rf.result(), cf.result()

check("三方竞跑：禁用登记生效", reg_result == "registered", str(reg_result))
check("三方竞跑：校准更正生效", corr_result == "corrected", str(corr_result))
for code, passed in pub_results:
    if code == "released" and passed is False:
        failures.append("依据未通过校准放行!")
with db.pool.connection() as c:
    t_after = c.execute("SELECT now() AS t").fetchone()["t"]
    bad = c.execute(
        """
        SELECT r.id FROM reports r
        JOIN instrument_disables d
          ON d.instrument = r.instrument AND d.revoked_at IS NULL
         AND d.disabled_from <= r.tested_at
         AND (d.disabled_to IS NULL OR r.tested_at < d.disabled_to)
        WHERE r.created_at > %s
        """,
        (t_after,),
    ).fetchall()
check("三方竞跑：禁用生效后无误放行", not bad, str(bad))
s, d = http_call(publish_report, ReportSubmit(instrument="D11", tested_at=dt(6, 1), reference="D11-after"))
check("三方竞跑后同点发布被阻断（禁用优先）", s == 422 and d["code"] == "INSTRUMENT_DISABLED", str(d))
http_call(revoke_disable, "D11-1")
s, d = http_call(publish_report, ReportSubmit(instrument="D11", tested_at=dt(6, 1), reference="D11-after2"))
check("撤销禁用后仍按校准判定（更正为未通过 -> 422）",
      s == 422 and d["code"] == "CALIBRATION_NOT_PASSED", str(d))

print()
if failures:
    print(f"{len(failures)} 项失败:", failures)
    sys.exit(1)
print("全部验证通过")
