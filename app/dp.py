"""报告级差分隐私日汇总：裁剪-求和-拉普拉斯加噪，预算与结果台账同事务生效。

隐私语义：
    * 取数：reports.created_at 的 UTC 日期当日 [00:00Z, 次日00:00Z)；
    * 每条 measured 先截到 [0, 100] 再求和 → 单条报告对总和的影响 ≤ 100（敏感度）；
    * 加 Laplace(0, 敏感度/ε) = Laplace(0, 200) 噪声，单次查询 ε=0.5；
    * 每个 (实验室, UTC 日期) 总预算 ε=1.0 → 不同请求号最多 2 次成功查询；
    * 同范围同请求号重试：返回首次落库的加噪值，不再扣预算（即使报告随后补录/更正）。

一致性语义：
    * 聚合是单条 SELECT，天然基于同一已提交快照（READ COMMITTED 语句级快照）；
    * 预算扣减与结果台账在同一个事务里提交：任一失败整体回滚，不消耗预算；
    * 预算用条件 upsert（spent_queries < 上限才 +1），行锁序列化并发，超限拒绝；
      数据库再以 CHECK (spent_queries <= 2) 兜底。
"""
from __future__ import annotations

import logging
import math
import secrets
from datetime import date, datetime, time, timedelta, timezone

import psycopg

from . import db
from .config import settings

log = logging.getLogger("dp")

# SystemRandom 走 OS urandom，避免可预测噪声。
_rng = secrets.SystemRandom()


def laplace_sample(scale: float) -> float:
    """Laplace(0, scale) 采样（逆变换法）。

    u ∈ [-0.5, 0.5)：X = -scale · sign(u) · ln(1 - 2|u|)。
    显式避开使对数无定义的端点（random() 恰好返回 0.0 时 u=-0.5）。
    """
    while True:
        u = _rng.random() - 0.5
        if u == 0.0:
            return 0.0
        guard = 1.0 - 2.0 * abs(u)
        if guard > 0.0:
            return -scale * math.copysign(1.0, u) * math.log(guard)


class BudgetExhausted(Exception):
    """该 (实验室, UTC 日期) 的差分隐私预算已耗尽（≥ 上限次成功查询）。"""

    def __init__(self, lab_no: str, day: date) -> None:
        super().__init__(f"dp budget exhausted for lab={lab_no} day={day}")
        self.lab_no = lab_no
        self.day = day


def _ledger_lookup(conn: psycopg.Connection, lab_no: str, day: date,
                   request_id: str) -> float | None:
    row = conn.execute(
        "SELECT noisy_sum FROM dp_summary_ledger "
        "WHERE lab_no = %s AND day_utc = %s AND request_id = %s",
        (lab_no, day, request_id),
    ).fetchone()
    return None if row is None else float(row["noisy_sum"])


def query_daily_summary(lab_no: str, day: date, request_id: str) -> dict:
    """执行一次差分隐私日汇总查询。

    返回 {"noisy_sum": float, "replayed": bool}；预算耗尽抛 BudgetExhausted。
    单事务完成：幂等命中直接返回；否则 裁剪求和 → 加噪 → 扣预算 → 落台账 → 提交。
    任何一步失败整体回滚：不消耗预算，也不留下半成品台账。
    """
    # UTC 当日 [start, end)：与会话时区无关的绝对时间界。
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    scale = settings.dp_sensitivity / settings.dp_epsilon_per_query

    with db.pool().connection() as conn:
        # 1) 幂等命中：同范围同请求号 → 返回原加噪值，不动预算、不重算。
        hit = _ledger_lookup(conn, lab_no, day, request_id)
        if hit is not None:
            conn.commit()
            return {"noisy_sum": hit, "replayed": True}

        # 2) 单条聚合 SELECT：同一已提交快照；每条 measured 先截到 [0,100] 再求和。
        #    真实合计只存在于本事务内存中，不落库、不出接口。
        true_sum = conn.execute(
            "SELECT COALESCE(SUM(LEAST(GREATEST(measured, 0), 100)), 0) AS s "
            "FROM reports "
            "WHERE lab_no = %s AND created_at >= %s AND created_at < %s",
            (lab_no, start, end),
        ).fetchone()["s"]

        # 3) 加噪。
        noisy_sum = float(true_sum) + laplace_sample(scale)

        # 4) 预算扣减：条件 upsert，行锁序列化并发；超限则本次不生效。
        budget_row = conn.execute(
            "INSERT INTO dp_summary_budget (lab_no, day_utc, spent_queries) "
            "VALUES (%s, %s, 1) "
            "ON CONFLICT (lab_no, day_utc) DO UPDATE "
            "  SET spent_queries = dp_summary_budget.spent_queries + 1, "
            "      updated_at = now() "
            "  WHERE dp_summary_budget.spent_queries < %s "
            "RETURNING spent_queries",
            (lab_no, day, settings.dp_max_queries_per_scope),
        ).fetchone()
        if budget_row is None:
            conn.rollback()
            log.info("dp budget exhausted: lab=%s day=%s", lab_no, day)
            raise BudgetExhausted(lab_no, day)

        # 5) 台账落库。并发下同请求号只会有一个事务先提交；
        #    后提交者唯一冲突 → 整体回滚（预算扣减随之撤销）→ 返回先入库的值。
        try:
            conn.execute(
                "INSERT INTO dp_summary_ledger "
                "  (lab_no, day_utc, request_id, epsilon, noisy_sum) "
                "VALUES (%s, %s, %s, %s, %s)",
                (lab_no, day, request_id,
                 settings.dp_epsilon_per_query, noisy_sum),
            )
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            with db.pool().connection() as conn2:
                existing = _ledger_lookup(conn2, lab_no, day, request_id)
            # 唯一冲突意味着对方已提交台账，此处必然能读到。
            assert existing is not None
            return {"noisy_sum": existing, "replayed": True}

        # 6) 预算与结果台账一同提交生效。
        conn.commit()
        return {"noisy_sum": noisy_sum, "replayed": False}


def budget_status(lab_no: str, day: date) -> dict:
    """查询某 (实验室, UTC 日期) 的预算消耗（只含查询次数元数据，不含任何报告数据）。"""
    with db.pool().connection() as conn:
        row = conn.execute(
            "SELECT spent_queries FROM dp_summary_budget "
            "WHERE lab_no = %s AND day_utc = %s",
            (lab_no, day),
        ).fetchone()
    spent = int(row["spent_queries"]) if row else 0
    max_q = settings.dp_max_queries_per_scope
    return {
        "lab_no": lab_no,
        "date": day.isoformat(),
        "epsilon_per_query": settings.dp_epsilon_per_query,
        "budget_epsilon": settings.dp_budget_epsilon,
        "max_queries": max_q,
        "spent_queries": spent,
        "remaining_queries": max(0, max_q - spent),
    }
