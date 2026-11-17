"""差分隐私日汇总关键行为自测：

* 裁剪求和与 UTC 日期边界（用零噪声桩精确断言）；
* 响应不暴露真实合计与行数；
* 同范围同请求号幂等重试：补录/更正后仍返回原加噪值且不重复扣预算；
* 预算上限：不同请求号最多 2 次成功，第 3 次 429；
* 并发：异号并发最多 2 次成功；同号并发全部返回同一值且只扣一次预算；
* 存储失败不消耗预算；
* 未提交数据不进入汇总（已提交快照）。
"""
from __future__ import annotations

import threading
from datetime import date

import psycopg
import pytest

from app import db as dbmod
from app import dp
from app.config import settings

DAY = "2026-11-17"


def _insert(client, lab_no, report_no, measured, created_at=None):
    """补录并（可选）回写 created_at，便于构造 UTC 日期边界。"""
    r = client.post("/reports",
                    json={"lab_no": lab_no, "report_no": report_no,
                          "measured": measured})
    assert r.status_code == 201
    if created_at is not None:
        with dbmod.pool().connection() as conn:
            conn.execute("UPDATE reports SET created_at = %s WHERE id = %s",
                         (created_at, r.json()["id"]))
            conn.commit()
    return r.json()["id"]


def _summary(client, lab_no, day, request_id):
    return client.post("/stats/daily-summary",
                       json={"lab_no": lab_no, "date": day,
                             "request_id": request_id})


def _spent(lab_no, day):
    with dbmod.pool().connection() as conn:
        row = conn.execute(
            "SELECT spent_queries FROM dp_summary_budget "
            "WHERE lab_no = %s AND day_utc = %s",
            (lab_no, date.fromisoformat(day)),
        ).fetchone()
    return 0 if row is None else row["spent_queries"]


@pytest.fixture()
def no_noise(monkeypatch):
    """把噪声钉为 0，便于精确断言裁剪后的真实合计。"""
    monkeypatch.setattr(dp, "laplace_sample", lambda scale: 0.0)


def test_clamp_sum_and_utc_boundary(client, no_noise):
    # 当日：负数截到 0、>100 截到 100、正常值原样 → 0 + 100 + 7 = 107
    _insert(client, "L1", "R1", -50, "2026-11-17T01:00:00+00:00")
    _insert(client, "L1", "R2", 9999, "2026-11-17T23:59:59+00:00")
    _insert(client, "L1", "R3", 7, "2026-11-17T12:00:00+00:00")
    # 次日（UTC）与前一日的报告不应计入
    _insert(client, "L1", "R4", 55, "2026-11-18T00:00:00+00:00")
    _insert(client, "L1", "R5", 66, "2026-11-16T23:59:59+00:00")
    # 带时区偏移的时间按 UTC 换算：+08:00 的 08:30 即 UTC 00:30，属于当日
    _insert(client, "L1", "R6", 5, "2026-11-17T08:30:00+08:00")
    # 别的实验室不计入
    _insert(client, "L2", "R1", 42, "2026-11-17T12:00:00+00:00")

    r = _summary(client, "L1", DAY, "req-boundary")
    assert r.status_code == 200
    body = r.json()
    assert body["noisy_sum"] == pytest.approx(112.0)  # 0+100+7+5
    assert body["epsilon"] == 0.5
    assert body["replayed"] is False
    # 响应不暴露真实合计与行数：字段集合固定且无 count/total/raw 字段
    assert set(body) == {"lab_no", "date", "request_id", "epsilon",
                         "noisy_sum", "replayed"}


def test_empty_day_returns_pure_noise_without_count(client):
    r = _summary(client, "NO-SUCH-LAB", DAY, "req-empty")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["noisy_sum"], float)
    assert "count" not in body and "total" not in body


def test_idempotent_replay_immune_to_backfill_and_correction(client):
    _insert(client, "L1", "R1", 10, "2026-11-17T01:00:00+00:00")
    first = _summary(client, "L1", DAY, "req-1")
    assert first.status_code == 200
    v1 = first.json()["noisy_sum"]
    assert first.json()["replayed"] is False
    assert _spent("L1", DAY) == 1

    # 随后补录新报告并更正已有报告
    _insert(client, "L1", "R2", 20, "2026-11-17T02:00:00+00:00")
    with dbmod.pool().connection() as conn:
        conn.execute("UPDATE reports SET measured = 99 WHERE lab_no='L1' "
                     "AND report_no='R1'")
        conn.commit()

    # 同范围同号重试：原加噪值原样返回，预算不再扣
    second = _summary(client, "L1", DAY, "req-1")
    assert second.status_code == 200
    assert second.json()["noisy_sum"] == v1
    assert second.json()["replayed"] is True
    assert _spent("L1", DAY) == 1


def test_budget_exhaustion_and_scope_isolation(client):
    _insert(client, "L1", "R1", 10, "2026-11-17T01:00:00+00:00")
    assert _summary(client, "L1", DAY, "req-a").status_code == 200
    assert _summary(client, "L1", DAY, "req-b").status_code == 200
    # 第 3 个不同请求号：预算耗尽 → 429，且不返回 noisy_sum
    third = _summary(client, "L1", DAY, "req-c")
    assert third.status_code == 429
    assert third.json()["detail"]["code"] == "dp_budget_exhausted"
    assert _spent("L1", DAY) == 2
    # 已耗尽的请求号重试仍 429（req-c 从未成功，无台账可幂等）
    assert _summary(client, "L1", DAY, "req-c").status_code == 429
    # 预算按范围隔离：其它日期 / 其它实验室不受影响
    assert _summary(client, "L1", "2026-11-18", "req-c").status_code == 200
    assert _summary(client, "L2", DAY, "req-c").status_code == 200
    # 预算接口只暴露查询次数元数据
    st = client.get("/stats/budget", params={"lab_no": "L1", "date": DAY}).json()
    assert st["spent_queries"] == 2 and st["remaining_queries"] == 0
    assert st["max_queries"] == 2


def test_concurrent_distinct_request_ids_at_most_two_succeed(client):
    for i in range(30):
        _insert(client, "L1", f"R{i}", 10, "2026-11-17T01:00:00+00:00")
    n = 8
    results = [None] * n

    def worker(i):
        results[i] = _summary(client, "L1", DAY, f"req-conc-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = [r for r in results if r.status_code == 200]
    rejected = [r for r in results if r.status_code == 429]
    assert len(ok) == 2, f"expected exactly 2 successes, got {len(ok)}"
    assert len(rejected) == n - 2
    assert _spent("L1", DAY) == 2
    # 两个成功值都落库可幂等重放
    for r in ok:
        again = _summary(client, "L1", DAY, r.json()["request_id"])
        assert again.json()["noisy_sum"] == r.json()["noisy_sum"]


def test_concurrent_same_request_id_single_charge_same_value(client):
    _insert(client, "L1", "R1", 10, "2026-11-17T01:00:00+00:00")
    n = 6
    results = [None] * n

    def worker(i):
        results[i] = _summary(client, "L1", DAY, "req-same")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.status_code == 200 for r in results)
    values = {r.json()["noisy_sum"] for r in results}
    assert len(values) == 1, "同请求号并发必须返回同一加噪值"
    assert _spent("L1", DAY) == 1  # 只扣一次预算
    assert sum(1 for r in results if r.json()["replayed"] is False) == 1


def test_storage_failure_does_not_consume_budget(client):
    _insert(client, "L1", "R1", 10, "2026-11-17T01:00:00+00:00")
    # 拆掉台账表模拟存储失败：接口应 500，且预算不被消耗
    with dbmod.pool().connection() as conn:
        conn.execute("DROP TABLE dp_summary_ledger")
        conn.commit()
    r = _summary(client, "L1", DAY, "req-fail")
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "dp_query_failed"
    assert _spent("L1", DAY) == 0
    # 恢复 schema 后同一请求号可正常成功（此前没有留下任何痕迹）
    dbmod.ensure_schema()
    ok = _summary(client, "L1", DAY, "req-fail")
    assert ok.status_code == 200
    assert ok.json()["replayed"] is False
    assert _spent("L1", DAY) == 1


def test_uncommitted_rows_not_visible_in_snapshot(client, no_noise):
    _insert(client, "L1", "R1", 10, "2026-11-17T01:00:00+00:00")
    # 未提交的写事务：当日再插 50，但汇总只能看到已提交的 10
    blocker = psycopg.connect(settings.database_url)
    try:
        blocker.execute("BEGIN")
        blocker.execute(
            "INSERT INTO reports(lab_no, report_no, measured, created_at) "
            "VALUES ('L1', 'R-PENDING', 50, '2026-11-17T02:00:00+00:00')")
        r = _summary(client, "L1", DAY, "req-snap")
        assert r.status_code == 200
        assert r.json()["noisy_sum"] == pytest.approx(10.0)
    finally:
        blocker.rollback()
        blocker.close()


def test_validation_errors(client):
    # 缺请求号 / 空请求号 / 非法日期 → 422，且不消耗预算
    assert client.post("/stats/daily-summary",
                       json={"lab_no": "L1", "date": DAY}).status_code == 422
    assert _summary(client, "L1", DAY, "").status_code == 422
    assert _summary(client, "L1", "not-a-date", "r1").status_code == 422
    assert _summary(client, "", DAY, "r1").status_code == 422
    assert _spent("L1", DAY) == 0
    # 预算接口的非法日期
    assert client.get("/stats/budget",
                      params={"lab_no": "L1", "date": "bad"}).status_code == 422
