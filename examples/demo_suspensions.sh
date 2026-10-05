#!/usr/bin/env bash
# 授权限时暂停调用示例。启动服务后执行：bash examples/demo_suspensions.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

FILM="film-suspense-demo"

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 录入两份同影片同场馆授权（早到期 license-early / 兜底 license-late） =="
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"valid_from\": \"2026-10-01T00:00:00Z\", \"valid_to\": \"2026-10-15T23:59:59Z\",
  \"max_sessions\": 3, \"note\": \"早到期授权\"
}" | pretty
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"valid_from\": \"2026-10-01T00:00:00Z\", \"valid_to\": \"2026-10-31T23:59:59Z\",
  \"max_sessions\": 5, \"note\": \"兜底授权\"
}" | pretty

echo
echo "== 2. 暂停前先核准一场（10-02 19:30Z），命中早到期授权，剩余 2 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2026-10-02T19:30:00Z\", \"idempotency_key\": \"susp-demo-1\"
}" | pretty

echo
echo "== 3. 为授权 1 登记限时暂停：2026-10-05T00:00+08:00 至 2026-10-07T00:00+08:00（北京时间，左闭右开） =="
curl -s -X POST "$BASE/licenses/1/suspensions" -H 'Content-Type: application/json' -d '{
  "suspend_from": "2026-10-05T00:00:00+08:00",
  "suspend_to":   "2026-10-07T00:00:00+08:00",
  "reason": "版权方临时通知暂停两日"
}' | pretty

echo
echo "== 4. 非法登记示例：结束不晚于开始 -> 422，且不留任何记录 =="
curl -s -w 'HTTP %{http_code}\n' -X POST "$BASE/licenses/1/suspensions" \
  -H 'Content-Type: application/json' -d '{
  "suspend_from": "2026-10-07T00:00:00+08:00",
  "suspend_to":   "2026-10-05T00:00:00+08:00",
  "reason": "区间颠倒"
}'

echo
echo "== 5. 未知授权登记暂停 -> 404 =="
curl -s -w 'HTTP %{http_code}\n' -X POST "$BASE/licenses/999/suspensions" \
  -H 'Content-Type: application/json' -d '{
  "suspend_from": "2026-10-05T00:00:00Z",
  "suspend_to":   "2026-10-06T00:00:00Z",
  "reason": "授权不存在"
}'

echo
echo "== 6. 暂停区间内核验（10-05 19:30Z = 10-06 03:30+08:00）：跳过暂停的授权 1，改用授权 2 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2026-10-05T19:30:00Z\", \"idempotency_key\": \"susp-demo-2\"
}" | pretty

echo
echo "== 7. 左闭右开边界（单独影片、仅一份授权）：起点含、止点不含 =="
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"film-edge-demo\", \"venue_id\": \"hall-9\",
  \"valid_from\": \"2026-10-01T00:00:00Z\", \"valid_to\": \"2026-10-31T23:59:59Z\",
  \"max_sessions\": 5, \"note\": \"边界演示授权\"
}" | pretty
# 暂停区间 [2026-10-04T16:00Z, 2026-10-06T16:00Z)，即北京时间 10-05 全天
curl -s -X POST "$BASE/licenses/3/suspensions" -H 'Content-Type: application/json' -d '{
  "suspend_from": "2026-10-04T16:00:00Z",
  "suspend_to":   "2026-10-06T16:00:00Z",
  "reason": "边界演示：暂停起含、止不含"
}' | pretty
echo "-- 暂停起前一刻 10-04T15:59Z：approved --"
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d '{
  "film_id": "film-edge-demo", "venue_id": "hall-9",
  "start_time": "2026-10-04T15:59:00Z", "idempotency_key": "susp-demo-edge-1"
}' | pretty
echo "-- 恰好暂停起 10-04T16:00Z：LICENSE_SUSPENDED --"
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d '{
  "film_id": "film-edge-demo", "venue_id": "hall-9",
  "start_time": "2026-10-04T16:00:00Z", "idempotency_key": "susp-demo-edge-2"
}' | pretty
echo "-- 恰好暂停止 10-06T16:00Z（右开，不含）：approved --"
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d '{
  "film_id": "film-edge-demo", "venue_id": "hall-9",
  "start_time": "2026-10-06T16:00:00Z", "idempotency_key": "susp-demo-edge-3"
}' | pretty

echo
echo "== 8. 再暂停授权 2，使覆盖该时段且未撤销的授权全部暂停：LICENSE_SUSPENDED =="
curl -s -X POST "$BASE/licenses/2/suspensions" -H 'Content-Type: application/json' -d '{
  "suspend_from": "2026-10-01T00:00:00Z",
  "suspend_to":   "2026-11-01T00:00:00Z",
  "reason": "兜底授权同步暂停"
}' | pretty
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2026-10-05T20:00:00Z\", \"idempotency_key\": \"susp-demo-4\"
}" | pretty

echo
echo "== 9. 暂停不改变历史：susp-demo-1 重放仍为首次核准结果 replay=true，额度不重复扣 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2026-10-02T19:30:00Z\", \"idempotency_key\": \"susp-demo-1\"
}" | pretty

echo
echo "== 10. 查询暂停记录：按授权 / 全局列表 / 单条 =="
curl -s "$BASE/licenses/1/suspensions" | pretty
curl -s "$BASE/suspensions?license_id=2" | pretty
curl -s "$BASE/suspensions/1" | pretty

echo
echo "== 11. 暂停登记后不可改：PUT/PATCH/DELETE 均不被允许（405） =="
curl -s -o /dev/null -w 'PUT    HTTP %{http_code}\n' -X PUT \
  "$BASE/suspensions/1" -H 'Content-Type: application/json' -d '{"reason":"x"}'
curl -s -o /dev/null -w 'PATCH  HTTP %{http_code}\n' -X PATCH \
  "$BASE/suspensions/1" -H 'Content-Type: application/json' -d '{"reason":"x"}'
curl -s -o /dev/null -w 'DELETE HTTP %{http_code}\n' -X DELETE "$BASE/suspensions/1"

echo
echo "== 12. 授权视图：既有已用额度不因暂停而变化（授权 1 used=1 / 授权 2 used=1） =="
curl -s "$BASE/licenses?film_id=$FILM" | pretty
