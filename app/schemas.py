"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import date as date_type

from pydantic import BaseModel, Field


class ReportIn(BaseModel):
    lab_no: str = Field(..., min_length=1, description="实验室号（非空）")
    report_no: str = Field(..., min_length=1, description="实验室内报告号（非空）")
    measured: int = Field(..., description="整数测值")


class ReportOut(ReportIn):
    id: int


class BatchReportIn(BaseModel):
    items: list[ReportIn] = Field(..., min_length=1)


class DpDailySummaryIn(BaseModel):
    """差分隐私日汇总请求。date 为 reports.created_at 的 UTC 日期。"""

    lab_no: str = Field(..., min_length=1, max_length=128, description="实验室号")
    date: date_type = Field(..., description="UTC 日期，YYYY-MM-DD")
    request_id: str = Field(
        ..., min_length=1, max_length=128,
        description="调用方请求号（幂等键）：同范围同号重试返回原加噪值且不重复扣预算",
    )


class DpDailySummaryOut(BaseModel):
    """差分隐私日汇总响应。刻意不包含真实合计与行数。"""

    lab_no: str
    date: date_type
    request_id: str
    epsilon: float = Field(..., description="本次查询消耗的 ε")
    noisy_sum: float = Field(..., description="加噪后的当日测值合计（Laplace 噪声）")
    replayed: bool = Field(
        ..., description="true=幂等命中，返回首次落库的加噪值且未再扣预算",
    )
