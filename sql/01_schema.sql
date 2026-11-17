-- 检验数仓报告表与索引构建轮次表
-- 幂等脚本，容器首次启动时由 PostgreSQL 自动执行（/docker-entrypoint-initdb.d）。

BEGIN;

-- 1) 报告主表
CREATE TABLE IF NOT EXISTS reports (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- 主键流水号
    lab_no       TEXT NOT NULL,                                     -- 实验室号（非空）
    report_no    TEXT NOT NULL,                                     -- 实验室内报告号（非空）
    measured     INTEGER NOT NULL,                                  -- 整数测值
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  reports            IS '持续补录的检验报告表';
COMMENT ON COLUMN reports.id         IS '主键流水号';
COMMENT ON COLUMN reports.lab_no     IS '实验室号';
COMMENT ON COLUMN reports.report_no  IS '实验室内报告号';
COMMENT ON COLUMN reports.measured   IS '整数测值';

-- 补录期间的普通辅助索引（非唯一），仅用于按实验室号/报告号查询与去重；
-- 唯一索引构建成功前，唯一性由补录 SQL 自行保证。
CREATE INDEX IF NOT EXISTS idx_reports_lab_report
    ON reports (lab_no, report_no);

-- 2) 唯一索引构建轮次表（状态持久化，API 重启后据此接续跟踪）
CREATE TABLE IF NOT EXISTS index_build_rounds (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_id    UUID NOT NULL UNIQUE,
    status      TEXT NOT NULL CHECK (status IN (
                    'running',        -- 构建中（含等待长事务阶段）
                    'complete',       -- 索引已有效
                    'failed',         -- 构建失败（如重复键），原数据保留，无效索引已清理
                    'cancel_requested', -- 已受理取消，等待后端 SQL 真正停止
                    'cancelled'       -- 后端 SQL 已确认停止且索引无效
                )),
    backend_pid INTEGER,                       -- 执行 CREATE INDEX 的 PostgreSQL 后端 PID
    index_name  TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    error       TEXT,                         -- 失败原因（如重复键明细）
    failure_type TEXT,                        -- duplicate_key / lock_timeout / other / cancelled
    restarted   BOOLEAN NOT NULL DEFAULT FALSE -- 是否为 API 重启后接续跟踪的轮次
);

COMMENT ON TABLE index_build_rounds IS 'CREATE UNIQUE INDEX CONCURRENTLY 构建轮次';

-- 同一时刻只允许一轮活跃构建（running 或 cancel_requested 各最多一条）。
-- 该约束在数据库层面兜底，即使多个 API 实例并发发起也不会出现两轮并发构建。
CREATE UNIQUE INDEX IF NOT EXISTS uq_index_rounds_one_active
    ON index_build_rounds ((1))
    WHERE status IN ('running', 'cancel_requested');

-- 3) 差分隐私日汇总：结果台账（幂等键 + 已发布加噪结果固化）
--    同一 (lab_no, day_utc, request_id) 只落一行：重试原样返回，不重复扣预算。
CREATE TABLE IF NOT EXISTS dp_summary_ledger (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lab_no      TEXT NOT NULL,                  -- 查询范围：实验室号
    day_utc     DATE NOT NULL,                  -- 查询范围：reports.created_at 的 UTC 日期
    request_id  TEXT NOT NULL,                  -- 调用方请求号（幂等键）
    epsilon     DOUBLE PRECISION NOT NULL,      -- 本次查询消耗的 ε
    noisy_sum   DOUBLE PRECISION NOT NULL,      -- 已发布的加噪合计（重试原样返回）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (lab_no, day_utc, request_id)
);

COMMENT ON TABLE  dp_summary_ledger             IS '差分隐私日汇总台账：同范围同请求号返回原加噪值';
COMMENT ON COLUMN dp_summary_ledger.day_utc     IS 'reports.created_at 按 UTC 截取的日期';
COMMENT ON COLUMN dp_summary_ledger.request_id  IS '调用方请求号（幂等键）';
COMMENT ON COLUMN dp_summary_ledger.noisy_sum   IS '已发布的加噪合计；真实合计与行数不落库、不出接口';

-- 4) 差分隐私预算表：每个 (实验室, UTC 日期) 总预算 ε=1，单次查询 ε=0.5，
--    即最多 2 次成功查询（不同请求号）。CHECK 为数据库层兜底；
--    应用层用条件 upsert（spent_queries < 2 才 +1）在并发下也不会超限。
CREATE TABLE IF NOT EXISTS dp_summary_budget (
    lab_no        TEXT NOT NULL,
    day_utc       DATE NOT NULL,
    spent_queries INTEGER NOT NULL DEFAULT 0
                  CHECK (spent_queries >= 0 AND spent_queries <= 2),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lab_no, day_utc)
);

COMMENT ON TABLE dp_summary_budget IS '差分隐私预算：每 (实验室, UTC日期) ε=1，每次成功查询扣 0.5';

COMMIT;
