"""SQLite 连接管理与数据库初始化（建表）。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def get_db_path() -> Path:
    return Path(os.environ.get("DB_PATH", "/data/film.db"))


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=float(os.environ.get("DB_TIMEOUT", "30")),
        check_same_thread=False,
        isolation_level=None,  # 显式 BEGIN/COMMIT，避免隐式事务
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              TEXT PRIMARY KEY,
    roll_no         TEXT NOT NULL,
    expected_start  INTEGER NOT NULL,
    expected_end    INTEGER NOT NULL,
    frames_json     TEXT NOT NULL,
    raw_frames_json TEXT,
    frames_merged_json TEXT NOT NULL DEFAULT '[]',
    duplicates_json TEXT NOT NULL DEFAULT '[]',
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (roll_no, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_batches_roll ON batches (roll_no);

CREATE TABLE IF NOT EXISTS roll_frames (
    roll_no  TEXT NOT NULL,
    frame_no INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    PRIMARY KEY (roll_no, frame_no),
    FOREIGN KEY (batch_id) REFERENCES batches (id)
);

CREATE TABLE IF NOT EXISTS roll_expected (
    roll_no  TEXT NOT NULL,
    start_no INTEGER NOT NULL,
    end_no   INTEGER NOT NULL,
    PRIMARY KEY (roll_no, start_no, end_no)
);

CREATE TABLE IF NOT EXISTS rescan_claims (
    id           TEXT PRIMARY KEY,
    roll_no      TEXT NOT NULL,
    claimant     TEXT NOT NULL,
    frame_start  INTEGER NOT NULL,
    frame_end    INTEGER NOT NULL,
    max_frames   INTEGER NOT NULL,
    ttl_seconds  INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    released_at  TEXT,
    completed_at TEXT,
    CHECK (frame_end >= frame_start),
    CHECK (max_frames  > 0),
    CHECK (ttl_seconds > 0)
);

CREATE INDEX IF NOT EXISTS idx_claims_roll ON rescan_claims (roll_no);

-- 补扫完成回执：一个认领至多一条回执（结案），一个补扫批次号至多绑定一条回执
CREATE TABLE IF NOT EXISTS rescan_receipts (
    id                TEXT PRIMARY KEY,
    claim_id          TEXT NOT NULL,
    roll_no           TEXT NOT NULL,
    batch_id          TEXT NOT NULL,
    raw_frames_json   TEXT NOT NULL,
    added_frames_json TEXT NOT NULL,
    duplicates_json   TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_claim
    ON rescan_receipts (claim_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_batch
    ON rescan_receipts (batch_id);

-- 交付快照：创建时固化该胶卷的预期闭区间、实扫帧（保序去重集合）与缺帧闭区间。
-- 此后批次导入、撤销或补扫均不修改快照内容；最后一批被撤销后快照仍可查询，
-- 此时以空状态与快照做差异对比。
CREATE TABLE IF NOT EXISTS roll_snapshots (
    id              TEXT PRIMARY KEY,
    roll_no         TEXT NOT NULL,
    expected_json   TEXT NOT NULL,
    frames_json     TEXT NOT NULL,
    missing_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_snapshots_roll ON roll_snapshots (roll_no);

-- 批次导入预检单：提交与 /batches 相同的数据，只生成持久化预检单而不写入批次。
-- raw_frames_json 保留原始帧顺序（含批内重复）；state_fingerprint 为创建时胶卷
-- 状态（合并后预期范围 + 实扫帧集合）的指纹，确认时在同一写事务内复核，
-- 不一致则拒绝导入（409）。确认成功后 result_json 冻结首次导入结果，
-- 同一预检单再次确认原样返回。
CREATE TABLE IF NOT EXISTS import_prechecks (
    id                    TEXT PRIMARY KEY,
    roll_no               TEXT NOT NULL,
    expected_start        INTEGER NOT NULL,
    expected_end          INTEGER NOT NULL,
    raw_frames_json       TEXT NOT NULL,
    batch_id              TEXT,
    state_fingerprint     TEXT NOT NULL,
    current_expected_json TEXT NOT NULL,
    added_frames_json     TEXT NOT NULL,
    duplicates_json       TEXT NOT NULL,
    post_expected_json    TEXT NOT NULL,
    post_missing_json     TEXT NOT NULL,
    confirmed_at          TEXT,
    result_batch_id       TEXT,
    result_json           TEXT,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_prechecks_roll ON import_prechecks (roll_no);

-- 扫描瑕疵复核：按批次**原始帧列表**的 0 起算位置登记瑕疵（模糊、划痕、曝光异常）。
-- 每个位置至多一条标记；PUT 接口整体替换（先全删再插）。批次撤销时在同一写事务内
-- 显式清理（外键 ON DELETE CASCADE 兜底）；瑕疵队列只按存活批次实时重算。
-- 旧批次没有 raw_frames_json，不允许登记（PUT 返回 409），其去重帧在队列中视为
-- 未标记扫描出现。
CREATE TABLE IF NOT EXISTS batch_defects (
    batch_id    TEXT NOT NULL,
    position    INTEGER NOT NULL CHECK (position >= 0),
    defect_type TEXT NOT NULL CHECK (defect_type IN ('模糊', '划痕', '曝光异常')),
    PRIMARY KEY (batch_id, position),
    FOREIGN KEY (batch_id) REFERENCES batches (id) ON DELETE CASCADE
);

-- 胶卷交付核验凭证：仅当三类阻断项（预期内缺帧区间、预期外帧、全部扫描出现均被
-- 标记瑕疵的帧）全空时签发。签发时冻结核验依据（预期覆盖、实扫帧、存活批次身份、
-- 瑕疵标记）与状态指纹（存活批次身份 + 瑕疵位置与类型的 SHA-256）。
-- (roll_no, state_fingerprint) 唯一：同一状态重复申请直接返回原凭证，不重复签发。
-- 凭证只新增、从不改写；GET 时按当前批次与瑕疵标记重算指纹，不一致即失效。
CREATE TABLE IF NOT EXISTS delivery_checks (
    id                TEXT PRIMARY KEY,
    roll_no           TEXT NOT NULL,
    expected_json     TEXT NOT NULL,
    frames_json       TEXT NOT NULL,
    batches_json      TEXT NOT NULL,
    defects_json      TEXT NOT NULL,
    state_fingerprint TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_delivery_checks_roll ON delivery_checks (roll_no);

CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_checks_state
    ON delivery_checks (roll_no, state_fingerprint);
"""


def init_db() -> None:
    """幂等建表：每次启动执行，已存在的库不受影响。

    旧版库（没有 raw_frames_json 列）通过 ALTER TABLE 平滑升级；
    旧批次该列为 NULL，撤销时据此判定能否安全重建（详见 services.delete_batch）。
    旧版 rescan_claims 没有 completed_at 列，同样幂等补齐（补扫回执结案标记）。
    """
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(batches)").fetchall()}
        if "raw_frames_json" not in cols:
            conn.execute("ALTER TABLE batches ADD COLUMN raw_frames_json TEXT")
        claim_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(rescan_claims)").fetchall()
        }
        if "completed_at" not in claim_cols:
            conn.execute("ALTER TABLE rescan_claims ADD COLUMN completed_at TEXT")
    finally:
        conn.close()


@contextmanager
def get_conn_ctx():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def get_db():
    """FastAPI 依赖：每请求一个连接。"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()
