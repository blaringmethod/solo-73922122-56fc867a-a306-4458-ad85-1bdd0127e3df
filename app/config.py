"""运行配置：全部通过环境变量注入，容器内由 docker-compose 提供默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@db:5432/report_warehouse",
    )
    # CREATE INDEX CONCURRENTLY 等待锁/长事务的上限（毫秒），0 表示不限制。
    lock_timeout_ms: int = int(os.getenv("BUILD_LOCK_TIMEOUT_MS", "0"))
    # 后台对账轮询间隔（秒）：确认取消、重启接续。
    reconcile_interval_s: float = float(os.getenv("RECONCILE_INTERVAL_S", "1.5"))
    index_name: str = os.getenv("UNIQUE_INDEX_NAME", "uq_reports_lab_no_report_no")
    table_name: str = "reports"
    # ---- 差分隐私日汇总参数 ----
    # 单次查询 ε=0.5；敏感度 100（每条 measured 先截到 [0,100]，单条报告对总和的
    # 最大影响为 100）；拉普拉斯噪声尺度 = 敏感度/ε = 200。
    dp_epsilon_per_query: float = 0.5
    dp_sensitivity: float = 100.0
    # 每个 (实验室, UTC 日期) 总预算 ε=1.0 → 最多 2 次成功查询（不同请求号）。
    # 与 sql/01_schema.sql 中 dp_summary_budget.spent_queries 的 CHECK 上限一致。
    dp_budget_epsilon: float = 1.0
    dp_max_queries_per_scope: int = 2


settings = Settings()
