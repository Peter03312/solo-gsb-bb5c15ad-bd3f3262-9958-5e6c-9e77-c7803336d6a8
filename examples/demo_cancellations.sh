#!/usr/bin/env bash
# 已核准场次临时取消调用示例。启动服务后执行：bash examples/demo_cancellations.sh
#
# 流程：录入授权（含绑池）→ 核验核准 → 按原幂等键登记取消（开场前、非空原因）
#       → 授权与共享池实时已用各回退一次 → 释放额度可被新核验使用
#       → 同键重复取消（同原因返回首次 / 不同原因 409）→ 原核验重放快照不变
#       → 未知键 404 / 已拒绝 422 / 已开场 422 → 查询取消状态与取消列表
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }
# 从 JSON 中取某个字段（优先 jq，退回 python3，再退回 grep 兜底）
json_field() { # $1=field, stdin=json
  if command -v jq >/dev/null 2>&1; then jq -r ".$1";
  elif command -v python3 >/dev/null 2>&1; then python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])";
  else grep -o "\"$1\": *\(-\?[0-9]\+\)" | grep -o '[0-9]\+' | head -1; fi
}
# 全部场次时间安排在 2030 年，保证脚本在任意日期执行时都处于"开场前"可取消窗口。
FILM="film-cancel-demo"

post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 登记共享池（上限 2）并录入绑池授权 hall-1（自身上限 5） =="
POOL_ID=$(post "/pools" "{
  \"film_id\": \"$FILM\", \"max_sessions\": 2,
  \"note\": \"取消后池已用次数同样回退\"
}" | tee /dev/stderr | json_field id)
LIC_ID=$(post "/licenses" "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"valid_from\": \"2019-01-01T00:00:00Z\", \"valid_to\": \"2031-12-31T23:59:59Z\",
  \"max_sessions\": 5, \"pool_id\": $POOL_ID
}" | tee /dev/stderr | json_field id)
echo "(pool_id=$POOL_ID, license_id=$LIC_ID)"

echo
echo "== 2. 核验核准 screening-1001：授权与池各消耗一次 =="
post "/verifications" "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2030-10-05T19:30:00Z\", \"idempotency_key\": \"screening-1001\"
}" | pretty

echo
echo "== 3. 按原核验幂等键登记取消（非空原因），返回取消时间/原因/关联授权与池 =="
post "/verifications/screening-1001/cancellation" '{
  "reason": "台风预警，场馆通知该场次临时取消"
}' | pretty

echo
echo "== 4. 授权与共享池实时已用各回退一次（均回到 0，剩余恢复） =="
curl -s "$BASE/licenses/$LIC_ID" | pretty
curl -s "$BASE/pools/$POOL_ID" | pretty

echo
echo "== 5. 释放出的额度可被后续新核验使用（池上限 2，现可再核 2 场） =="
post "/verifications" "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2030-10-06T19:30:00Z\", \"idempotency_key\": \"screening-1002\"
}" | pretty

echo
echo "== 6. 同键同原因重复取消：返回首次记录（同一 id/时间，不重复释放额度） =="
post "/verifications/screening-1001/cancellation" '{
  "reason": "台风预警，场馆通知该场次临时取消"
}' | pretty

echo
echo "== 7. 同键不同原因重复取消：409 冲突，existing 附首次取消记录 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST \
  "$BASE/verifications/screening-1001/cancellation" \
  -H 'Content-Type: application/json' -d '{"reason": "想换一个取消原因"}'

echo
echo "== 8. 原核验同键重放：原决定 approved 与扣减时余额快照不变（replay=true），"
echo "      仅额外可见 cancelled=true 与 cancellation 取消登记 =="
post "/verifications" "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2030-10-05T19:30:00Z\", \"idempotency_key\": \"screening-1001\"
}" | pretty

echo
echo "== 9. 错误情形 =="
echo "-- 9a. 未知幂等键取消 → 404 NOT_FOUND"
curl -s -w '\nHTTP %{http_code}\n' -X POST \
  "$BASE/verifications/no-such-screening/cancellation" \
  -H 'Content-Type: application/json' -d '{"reason": "x"}'

echo "-- 9b. 已拒绝流水取消 → 422 VERIFICATION_NOT_APPROVED（先造一条无授权拒绝流水）"
post "/verifications" "{
  \"film_id\": \"no-such-film\", \"venue_id\": \"hall-9\",
  \"start_time\": \"2030-10-05T19:30:00Z\", \"idempotency_key\": \"screening-rej\"
}" >/dev/null
curl -s -w '\nHTTP %{http_code}\n' -X POST \
  "$BASE/verifications/screening-rej/cancellation" \
  -H 'Content-Type: application/json' -d '{"reason": "x"}'

echo "-- 9c. 已开场流水取消 → 422 SCREENING_ALREADY_STARTED（场次时间在过去）"
post "/verifications" "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2020-10-05T19:30:00Z\", \"idempotency_key\": \"screening-old\"
}" >/dev/null
curl -s -w '\nHTTP %{http_code}\n' -X POST \
  "$BASE/verifications/screening-old/cancellation" \
  -H 'Content-Type: application/json' -d '{"reason": "x"}'

echo "-- 9d. 空白原因 → 422（不产生取消记录、不释放额度）"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST \
  "$BASE/verifications/screening-1002/cancellation" \
  -H 'Content-Type: application/json' -d '{"reason": "   "}'

echo
echo "== 10. 查询：核验详情中的取消状态、取消登记，以及取消列表/过滤 =="
echo "-- 10a. 按幂等键查核验（cancelled / cancellation 字段）"
curl -s "$BASE/verifications/screening-1001" | pretty
echo "-- 10b. 单独取该核验的取消登记（取消时间、原因、授权与池）"
curl -s "$BASE/verifications/screening-1001/cancellation" | pretty
echo "-- 10c. 仅看已取消 / 未取消的核验流水"
curl -s "$BASE/verifications?cancelled=true" | pretty
echo "-- 10d. 全部取消登记，可按授权/池/影片/场馆过滤"
curl -s "$BASE/cancellations" | pretty
curl -s "$BASE/cancellations?license_id=$LIC_ID" | pretty
