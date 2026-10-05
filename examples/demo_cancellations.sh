#!/usr/bin/env bash
# 已核准场次取消调用示例。启动服务后执行：bash examples/demo_cancellations.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

FILM="film-cancel-demo"

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 登记共享池（上限 3）并录入绑池授权（上限 3） =="
curl -s -X POST "$BASE/pools" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"max_sessions\": 3, \"note\": \"取消演示池\"
}" | pretty
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2031-01-01T00:00:00Z\",
  \"max_sessions\": 3, \"pool_id\": 1, \"note\": \"取消演示授权\"
}" | pretty

echo
echo "== 2. 核准两个未来场次（2030 年）：授权与池各消耗至 used=2 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2030-06-01T19:30:00Z\", \"idempotency_key\": \"cancel-demo-1\"
}" | pretty
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2030-06-02T19:30:00Z\", \"idempotency_key\": \"cancel-demo-2\"
}" | pretty

echo
echo "== 3. 按原幂等键取消 cancel-demo-1：返回取消时间、原因及关联授权和共享池 =="
curl -s -X POST "$BASE/verifications/cancel-demo-1/cancel" \
  -H 'Content-Type: application/json' -d '{"reason": "放映设备故障，场次取消"}' | pretty

echo
echo "== 4. 授权与池的实时已用次数各回退一次（used 2 -> 1） =="
curl -s "$BASE/licenses?film_id=$FILM" | pretty
curl -s "$BASE/pools/1" | pretty

echo
echo "== 5. 重复提交相同取消：返回首次记录（replay=true），不重复释放额度 =="
curl -s -X POST "$BASE/verifications/cancel-demo-1/cancel" \
  -H 'Content-Type: application/json' -d '{"reason": "放映设备故障，场次取消"}' | pretty

echo
echo "== 6. 同一键但取消原因不同：409 冲突，existing 附首次取消记录 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/cancel-demo-1/cancel" \
  -H 'Content-Type: application/json' -d '{"reason": "换个理由"}'

echo
echo "== 7. 释放出的额度可被后续核验使用：新场次 approved =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2030-06-03T19:30:00Z\", \"idempotency_key\": \"cancel-demo-3\"
}" | pretty

echo
echo "== 8. 核验查询可见取消状态；原决定与扣减快照保持原样 =="
curl -s "$BASE/verifications/cancel-demo-1" | pretty
echo "-- 同键重放核验：仍返回首次 approved 结果（replay=true），不重复扣减 --"
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2030-06-01T19:30:00Z\", \"idempotency_key\": \"cancel-demo-1\"
}" | pretty

echo
echo "== 9. 额度再占满（过去场次也算一次），为拒绝场景铺垫 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2020-06-01T19:30:00Z\", \"idempotency_key\": \"cancel-demo-past\"
}" | pretty
echo "-- 额度已尽：新核验被拒绝 QUOTA_EXHAUSTED --"
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2030-06-04T19:30:00Z\", \"idempotency_key\": \"cancel-demo-4\"
}" | pretty

echo
echo "== 10. 已拒绝的流水不可取消：422 VERIFICATION_NOT_APPROVED =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/cancel-demo-4/cancel" \
  -H 'Content-Type: application/json' -d '{"reason": "尝试取消被拒绝的流水"}'

echo
echo "== 11. 已开场的场次不可取消：422 SESSION_ALREADY_STARTED =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/cancel-demo-past/cancel" \
  -H 'Content-Type: application/json' -d '{"reason": "场次已开场"}'

echo
echo "== 12. 未知幂等键：404 NOT_FOUND =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/no-such-key/cancel" \
  -H 'Content-Type: application/json' -d '{"reason": "键不存在"}'

echo
echo "== 13. 原因为空白：422，不留任何记录 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/verifications/cancel-demo-2/cancel" \
  -H 'Content-Type: application/json' -d '{"reason": "   "}'

echo
echo "== 14. 核验流水列表：已取消场次 cancelled=true 且附取消详情 =="
curl -s "$BASE/verifications?film_id=$FILM" | pretty
