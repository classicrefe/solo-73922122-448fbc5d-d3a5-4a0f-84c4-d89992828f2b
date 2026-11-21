"""SQLite 存取层（标准库 sqlite3，按请求开连接）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalogs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 0,
    -- 1 = “加载演示数据”专用目录；普通目录（含历史同名目录）恒为 0
    is_demo     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS partitions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id       INTEGER NOT NULL REFERENCES catalogs(id),
    partition_hour   TEXT NOT NULL,   -- UTC 整点 ISO8601
    planned_deadline TEXT NOT NULL,
    submitted_at     TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_partitions_catalog_hour
    ON partitions(catalog_id, partition_hour);

CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id    INTEGER NOT NULL REFERENCES catalogs(id),
    name          TEXT NOT NULL,
    offset_days   INTEGER NOT NULL DEFAULT 1,
    grace_minutes INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id      INTEGER NOT NULL REFERENCES catalogs(id),
    catalog_version INTEGER NOT NULL,   -- 核查时的目录版本；与当前版本不一致即失效
    business_date   TEXT NOT NULL,      -- YYYY-MM-DD
    as_of           TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checks_catalog ON checks(catalog_id);

CREATE TABLE IF NOT EXISTS check_results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id       INTEGER NOT NULL REFERENCES checks(id),
    report_name    TEXT NOT NULL,       -- 快照：报表定义可能随后变化
    offset_days    INTEGER NOT NULL,
    grace_minutes  INTEGER NOT NULL,
    window_start   TEXT NOT NULL,
    window_end     TEXT NOT NULL,
    watermark_hour TEXT,                -- 安全水位（窗口内最后一个连续可用分区）
    status         TEXT NOT NULL,       -- READY | BLOCKED
    block_hour     TEXT,
    block_reason   TEXT,                -- MISSING | PENDING | DUPLICATE | LATE
    block_message  TEXT,
    counts_json    TEXT NOT NULL,
    detail_json    TEXT NOT NULL        -- 24 个分区逐小时判定明细
);
CREATE INDEX IF NOT EXISTS idx_check_results_check ON check_results(check_id);
"""

# 部分唯一索引：数据库层保证全库最多只有一个专用演示目录。
# 必须在迁移之后创建，因为旧库的 catalogs 表在迁移前没有 is_demo 列。
DEMO_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_catalogs_single_demo
    ON catalogs(is_demo) WHERE is_demo = 1;
"""


def connect(path: str) -> sqlite3.Connection:
    # timeout/busy_timeout：并发首次加载演示数据时，后到的写事务等待前一个提交，
    # 而不是立刻报 database is locked。
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """就地升级旧库：只新增列与索引，不改写任何已有数据行。

    旧库里可能已有一个叫“演示目录”的目录，但无法确认它是否由演示按钮创建，
    因此一律保留为普通目录（is_demo=0），其分区、日报依赖和核查记录原样保留。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(catalogs)")}
    if "is_demo" not in columns:
        try:
            conn.execute(
                "ALTER TABLE catalogs ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError as exc:
            # 多个 uvicorn worker 同时启动时，另一个进程可能刚加过该列。
            if "duplicate column name" not in str(exc).lower():
                raise
    # 防御性归一：若历史版本因异常留下了多个专用目录，保留最早的，
    # 其余降级为普通目录（只改标记，不改名称、分区、依赖或核查）。
    conn.execute(
        "UPDATE catalogs SET is_demo = 0"
        " WHERE is_demo = 1 AND id NOT IN (SELECT MIN(id) FROM catalogs WHERE is_demo = 1)"
    )
    conn.execute(DEMO_UNIQUE_INDEX)


def init_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        try:
            # WAL 提升“读取历史核查/分区”与“写入演示数据”并发时的可用性。
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            # 某些容器/网络卷不支持 WAL；退回默认日志模式即可正常使用。
            pass
        conn.executescript(SCHEMA)
        # 串行化迁移，避免两个进程同时 ALTER TABLE。
        conn.execute("BEGIN IMMEDIATE")
        try:
            _migrate(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
