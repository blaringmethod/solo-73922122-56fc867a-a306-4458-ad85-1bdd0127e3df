#!/usr/bin/env bash
# 演示 5：报告级差分隐私日汇总。
#   1) 按 UTC 日期取数，每条 measured 先截到 [0,100] 再求和，加 Laplace(0,200) 噪声（ε=0.5）；
#   2) 同范围同请求号重试：补录/更正后仍返回原加噪值，且不重复扣预算；
#   3) 每 (实验室, UTC 日期) 预算 ε=1：不同请求号最多 2 次成功，第 3 次 429；
#   4) 响应不含真实合计与行数；预算接口只暴露查询次数元数据。
set -euo pipefail
cd "$(dirname "$0")"
source ./common.sh

DAY="2026-11-17"

echo "== 0. 清理环境（含差分隐私台账与预算表） =="
psql_exec -c "DROP INDEX IF EXISTS uq_reports_lab_no_report_no;" \
          -c "TRUNCATE reports RESTART IDENTITY;" \
          -c "DELETE FROM index_build_rounds;" \
          -c "TRUNCATE dp_summary_ledger, dp_summary_budget;" >/dev/null

echo "== 1. 补录当日数据：-50（截到 0）、9999（截到 100）、7、5 → 真实合计 112（不出接口） =="
c -X POST "$API_BASE/reports" -d '{"lab_no":"LAB-001","report_no":"R-1","measured":-50}'  >/dev/null
c -X POST "$API_BASE/reports" -d '{"lab_no":"LAB-001","report_no":"R-2","measured":9999}' >/dev/null
c -X POST "$API_BASE/reports" -d '{"lab_no":"LAB-001","report_no":"R-3","measured":7}'    >/dev/null
c -X POST "$API_BASE/reports" -d '{"lab_no":"LAB-001","report_no":"R-4","measured":5}'    >/dev/null
psql_exec -c "SELECT sum(LEAST(GREATEST(measured,0),100)) AS true_clamped_sum, count(*) AS true_rows
              FROM reports WHERE lab_no='LAB-001'
                AND created_at >= DATE '$DAY' AT TIME ZONE 'UTC'
                AND created_at <  (DATE '$DAY' + 1) AT TIME ZONE 'UTC';"
echo "（↑ 仅演示核对用，API 响应中不会出现真实合计与行数）"

echo "== 2. 第一次查询（请求号 req-A）：扣 ε=0.5，返回加噪合计 =="
c -X POST "$API_BASE/stats/daily-summary" \
  -d "{\"lab_no\":\"LAB-001\",\"date\":\"$DAY\",\"request_id\":\"req-A\"}"

echo "== 3. 补录 + 更正后，同范围同号（req-A）重试：返回原加噪值，预算不再扣 =="
c -X POST "$API_BASE/reports" -d '{"lab_no":"LAB-001","report_no":"R-5","measured":88}' >/dev/null
psql_exec -c "UPDATE reports SET measured=1 WHERE lab_no='LAB-001' AND report_no='R-3';" >/dev/null
c -X POST "$API_BASE/stats/daily-summary" \
  -d "{\"lab_no\":\"LAB-001\",\"date\":\"$DAY\",\"request_id\":\"req-A\"}"
echo "-- 预算（应仍为 spent=1）："
c "$API_BASE/stats/budget?lab_no=LAB-001&date=$DAY"

echo "== 4. 换请求号 req-B：第二次成功（预算耗尽）；req-C：429 拒绝 =="
c -X POST "$API_BASE/stats/daily-summary" \
  -d "{\"lab_no\":\"LAB-001\",\"date\":\"$DAY\",\"request_id\":\"req-B\"}"
c -X POST "$API_BASE/stats/daily-summary" \
  -d "{\"lab_no\":\"LAB-001\",\"date\":\"$DAY\",\"request_id\":\"req-C\"}"
echo "-- 预算（spent=2，remaining=0）："
c "$API_BASE/stats/budget?lab_no=LAB-001&date=$DAY"

echo "== 5. 预算按范围隔离：其它日期 / 其它实验室仍可查询 =="
c -X POST "$API_BASE/stats/daily-summary" \
  -d '{"lab_no":"LAB-001","date":"2026-11-18","request_id":"req-C"}'
c -X POST "$API_BASE/stats/daily-summary" \
  -d "{\"lab_no\":\"LAB-002\",\"date\":\"$DAY\",\"request_id\":\"req-C\"}"

echo "== 6. 台账核对（数据库层）：每个请求号只落一行，加噪值固化 =="
psql_exec -c "SELECT lab_no, day_utc, request_id, epsilon, round(noisy_sum::numeric, 4) AS noisy_sum
              FROM dp_summary_ledger ORDER BY id;"
psql_exec -c "SELECT lab_no, day_utc, spent_queries FROM dp_summary_budget ORDER BY 1, 2;"
echo "DEMO-5 OK"
