#!/usr/bin/env bash
# 仪器禁用区间端到端示例：登记禁用 -> 命中阻断（校准通过也不放行）-> 受影响报告追溯 -> 撤销 -> 恢复放行
# 依赖：curl（jq 可选，没有时用 python -m json.tool）
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { if command -v jq >/dev/null 2>&1; then jq .; else python3 -m json.tool; fi; }

echo "== 0. 准备：登记仪器 LC-01 的校准（2026 全年，通过） =="
curl -sS -X POST "$BASE/calibrations" -H 'Content-Type: application/json' -d '{
  "instrument": "LC-01",
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_to":   "2027-01-01T00:00:00Z",
  "passed": true
}' | pp

echo "== 1. 先放行两份报告（检测时间 3/15 与 4/15），稍后用禁用追溯它们 =="
curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "LC-01", "tested_at": "2026-03-15T08:00:00Z", "reference": "LC-R1"
}' | pp
curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "LC-01", "tested_at": "2026-04-15T08:00:00Z", "reference": "LC-R2"
}' | pp

echo "== 2. 登记禁用区间 [2026-03-01, 2026-04-01)，事件号 EQ-2026-0007（响应顺带返回受影响报告） =="
curl -sS -X POST "$BASE/disables" -H 'Content-Type: application/json' -d '{
  "event_no": "EQ-2026-0007",
  "instrument": "LC-01",
  "disabled_from": "2026-03-01T00:00:00Z",
  "disabled_to":   "2026-04-01T00:00:00Z",
  "reason": "泵模块故障返厂"
}' | pp

echo "== 3. 检测时间命中禁用区间 -> 422 INSTRUMENT_DISABLED（即使校准通过），detail 内为禁用依据 =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/reports" \
  -H 'Content-Type: application/json' -d '{
    "instrument": "LC-01", "tested_at": "2026-03-20T08:00:00Z", "reference": "LC-R3"
  }'

echo "== 4. 边界语义：恰为终点 2026-04-01T00:00:00Z（右开）-> 放行 =="
curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "LC-01", "tested_at": "2026-04-01T00:00:00Z", "reference": "LC-R4"
}' | pp

echo "== 5. 与未撤销区间重叠的登记 -> 409 DISABLE_INTERVAL_OVERLAP（边界相接则允许） =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/disables" \
  -H 'Content-Type: application/json' -d '{
    "event_no": "EQ-2026-0008", "instrument": "LC-01",
    "disabled_from": "2026-03-15T00:00:00Z", "disabled_to": "2026-05-01T00:00:00Z",
    "reason": "重叠示例"
  }'

echo "== 6. 事件号重复 -> 409 DISABLE_EVENT_EXISTS；无效区间 -> 422 INVALID_INTERVAL =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/disables" \
  -H 'Content-Type: application/json' -d '{
    "event_no": "EQ-2026-0007", "instrument": "LC-01",
    "disabled_from": "2026-06-01T00:00:00Z", "disabled_to": "2026-07-01T00:00:00Z",
    "reason": "重复事件号示例"
  }'
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/disables" \
  -H 'Content-Type: application/json' -d '{
    "event_no": "EQ-2026-0009", "instrument": "LC-01",
    "disabled_from": "2026-07-01T00:00:00Z", "disabled_to": "2026-06-01T00:00:00Z",
    "reason": "无效区间示例"
  }'

echo "== 7. 按当前禁用状态查询受影响的已发布报告及禁用依据（快照不改写） =="
curl -sS "$BASE/disables/affected-reports?instrument=LC-01" | pp

echo "== 8. 撤销未知事件 -> 404；撤销 EQ-2026-0007 -> 200（记录保留）；重复撤销 -> 409 =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/disables/NO-SUCH-EVENT/revoke"
curl -sS -X POST "$BASE/disables/EQ-2026-0007/revoke" | pp
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/disables/EQ-2026-0007/revoke"

echo "== 9. 撤销后：受影响列表随之清空；原命中时点的报告可放行 =="
curl -sS "$BASE/disables/affected-reports?instrument=LC-01" | pp
curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "LC-01", "tested_at": "2026-03-20T08:00:00Z", "reference": "LC-R3"
}' | pp

echo "== 10. 禁用记录保留供追溯（含已撤销；active_only=true 仅看生效中） =="
curl -sS "$BASE/disables?instrument=LC-01" | pp
curl -sS "$BASE/disables/EQ-2026-0007" | pp
