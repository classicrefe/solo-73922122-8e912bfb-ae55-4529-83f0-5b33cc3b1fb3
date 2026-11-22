"""SQLite 连接与初始化。

设计要点：
- WAL 模式：换版写事务进行期间，查询仍可读到已提交快照；
- foreign_keys=ON：所有外键约束生效；
- busy_timeout：写锁冲突时等待而非立即报错；
- 查询接口在单条连接、单个隐式只读事务内完成全部读取，
  SQLite 保证该事务看到的是同一已提交状态（快照隔离）。
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

DB_PATH = os.environ.get("DET_DB_PATH", "/data/determinations.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS checklist_versions (
    version_code     TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    parent_version   TEXT REFERENCES checklist_versions(version_code),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    is_current       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS species (
    version_code   TEXT NOT NULL REFERENCES checklist_versions(version_code),
    species_code   TEXT NOT NULL,
    scientific_name TEXT NOT NULL,
    PRIMARY KEY (version_code, species_code)
);

CREATE TABLE IF NOT EXISTS identifications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    specimen_no       TEXT NOT NULL,
    version_code      TEXT NOT NULL,
    species_code      TEXT NOT NULL,
    recorded_at       TEXT NOT NULL DEFAULT (datetime('now')),
    withdrawn_at      TEXT,
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'withdrawn')),
    FOREIGN KEY (version_code, species_code)
        REFERENCES species(version_code, species_code)
);

CREATE INDEX IF NOT EXISTS idx_id_specimen
    ON identifications(specimen_no, status);

CREATE TABLE IF NOT EXISTS code_mappings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    from_version      TEXT NOT NULL,
    to_version        TEXT NOT NULL,
    from_code         TEXT NOT NULL,
    to_code           TEXT NOT NULL,
    FOREIGN KEY (from_version) REFERENCES checklist_versions(version_code),
    FOREIGN KEY (to_version) REFERENCES checklist_versions(version_code),
    FOREIGN KEY (from_version, from_code)
        REFERENCES species(version_code, species_code),
    FOREIGN KEY (to_version, to_code)
        REFERENCES species(version_code, species_code),
    UNIQUE (from_version, to_version, from_code, to_code)
);

CREATE INDEX IF NOT EXISTS idx_map_lookup
    ON code_mappings(from_version, to_version, from_code);
"""


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path or DB_PATH,
        timeout=30,
        isolation_level=None,  # 手工管理事务，显式 BEGIN
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """请求级连接：只读/普通接口共用，自动提交/回滚并关闭。"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    """建库建表，并确保仅保留一个现行版。"""
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = _connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()
