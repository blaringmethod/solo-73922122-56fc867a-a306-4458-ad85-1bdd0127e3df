"""FastAPI 入口：补录、唯一索引构建的发起/查询/取消、差分隐私日汇总。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date as date_type

import psycopg
from fastapi import FastAPI, HTTPException, status

from . import db, dp
from .config import settings
from .responses import AppJSONResponse
from .schemas import (
    BatchReportIn,
    DpDailySummaryIn,
    DpDailySummaryOut,
    ReportIn,
    ReportOut,
)
from .service import service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_pool()
    service.reconcile_on_startup()
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(
    title="检验报告唯一性保障与差分隐私汇总 API",
    description=(
        "对持续补录的 reports 表在线构建 (lab_no, report_no) 唯一索引；"
        "支持发起、查询、取消 CREATE UNIQUE INDEX CONCURRENTLY，"
        "构建期间补录不阻塞，API 重启后接续跟踪。"
        "另提供报告级差分隐私日汇总：按 UTC 日期对截断后的测值求和并加拉普拉斯噪声，"
        "每实验室每日 ε=1 预算、请求号幂等。"
    ),
    version="1.1.0",
    default_response_class=AppJSONResponse,
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    with db.pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


# ------------------------------ 补录 ------------------------------------

@app.post("/reports", response_model=ReportOut, status_code=201)
def insert_report(payload: ReportIn):
    """单条补录。构建期间（CONCURRENTLY）可正常提交；唯一索引生效后重复键返回 409。"""
    try:
        with db.pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO reports (lab_no, report_no, measured) "
                "VALUES (%s, %s, %s) RETURNING id, lab_no, report_no, measured",
                (payload.lab_no, payload.report_no, payload.measured),
            ).fetchone()
            conn.commit()
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "duplicate_report",
                "message": "同一实验室下报告号已存在，本条补录被拒绝，原有数据保持不变",
                "lab_no": payload.lab_no,
                "report_no": payload.report_no,
                "pg_error": str(exc).strip(),
            },
        )
    return dict(row)


@app.post("/reports/batch")
def insert_reports_batch(payload: BatchReportIn):
    """批量补录：逐条保存，重复键跳过且不影响其它行，返回成功/跳过明细。"""
    inserted, skipped = [], []
    with db.pool().connection() as conn:
        for item in payload.items:
            try:
                row = conn.execute(
                    "INSERT INTO reports (lab_no, report_no, measured) "
                    "VALUES (%s, %s, %s) RETURNING id, lab_no, report_no, measured",
                    (item.lab_no, item.report_no, item.measured),
                ).fetchone()
                inserted.append(dict(row))
            except psycopg.errors.UniqueViolation:
                conn.rollback()
                skipped.append(
                    {"lab_no": item.lab_no, "report_no": item.report_no,
                     "reason": "duplicate_report"}
                )
        conn.commit()
    return {"inserted_count": len(inserted), "skipped_count": len(skipped),
            "inserted": inserted, "skipped": skipped}


@app.get("/reports")
def list_reports(limit: int = 50, offset: int = 0):
    with db.pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, lab_no, report_no, measured FROM reports "
            "ORDER BY id DESC LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT count(*) AS n FROM reports").fetchone()["n"]
    return {"total": total, "items": [dict(r) for r in rows]}


# ----------------------- 差分隐私日汇总 -----------------------------------

@app.post("/stats/daily-summary", response_model=DpDailySummaryOut)
def dp_daily_summary(payload: DpDailySummaryIn):
    """报告级差分隐私日汇总。

    * 取数：reports.created_at 的 UTC 日期当日，单条聚合 SELECT（同一已提交快照）；
    * 每条 measured 先截到 [0,100] 再求和（敏感度 100），加 Laplace(0, 200) 噪声，单次 ε=0.5；
    * 每个 (实验室, UTC 日期) 总预算 ε=1：不同请求号最多 2 次成功查询，超额返回 429；
    * 同范围同请求号重试：返回首次落库的加噪值（replayed=true），不重复扣预算，
      即使报告随后补录或更正结果也不变；
    * 预算扣减与结果落库在同一事务提交：计算或存储失败不消耗预算。
    响应不包含真实合计与行数。
    """
    try:
        result = dp.query_daily_summary(
            payload.lab_no, payload.date, payload.request_id
        )
    except dp.BudgetExhausted:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "dp_budget_exhausted",
                "message": "该实验室当日差分隐私预算已耗尽（ε=1，最多 2 次成功查询），"
                           "本次未返回任何数据",
                "lab_no": payload.lab_no,
                "date": payload.date.isoformat(),
            },
        )
    except Exception:
        # 计算/存储失败：事务已回滚，不消耗预算；如实返回 500。
        log.exception("dp daily summary failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "dp_query_failed",
                    "message": "差分隐私汇总计算或存储失败，未消耗预算"},
        )
    return DpDailySummaryOut(
        lab_no=payload.lab_no,
        date=payload.date,
        request_id=payload.request_id,
        epsilon=settings.dp_epsilon_per_query,
        noisy_sum=result["noisy_sum"],
        replayed=result["replayed"],
    )


@app.get("/stats/budget")
def dp_budget_status(lab_no: str, date: str):
    """查询某 (实验室, UTC 日期) 的差分隐私预算消耗（仅查询次数元数据，不含报告数据）。"""
    try:
        day = date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_date",
                    "message": "date 须为 YYYY-MM-DD 格式的 UTC 日期"},
        )
    return dp.budget_status(lab_no, day)


# --------------------------- 唯一索引构建 --------------------------------

@app.post("/index/build", status_code=202)
def start_build():
    """发起（或复用）CREATE UNIQUE INDEX CONCURRENTLY。

    * 已有活跃轮次：复用同一轮，不新开；
    * 索引已有效：直接返回 complete；
    * 失败后的无效残留索引会先安全清理，绝不删除有效索引。
    """
    view, code = service.start_build()
    return AppJSONResponse(view, status_code=code)


@app.get("/index/status")
def index_status(round_id: str | None = None):
    """查询构建轮次状态，并附带 pg_index 中的真实索引有效性。"""
    return service.get_status(round_id)


@app.post("/index/cancel", status_code=202)
def cancel_build(round_id: str | None = None):
    """取消构建：先受理（cancel_requested），后端 SQL 确认停止后才转 cancelled。

    只向记录在案的构建后端 PID 发 pg_cancel_backend，不影响补录连接；
    若索引已有效则报 complete。
    """
    view, code = service.request_cancel(round_id)
    return AppJSONResponse(view, status_code=code)
