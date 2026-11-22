"""并发一致性验证（直连 SQLite，不走 HTTP）：

读事务进行到一半时，另一连接提交换版写入，
读事务在整个生命周期内必须始终看到同一个已提交版本（快照隔离 + WAL）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import _connect, init_db
from app.schemas import SpeciesIn, VersionCreate
from app.services import bootstrap_version, publish_version

path = tempfile.mktemp(suffix=".db")
os.environ["DET_DB_PATH"] = path
init_db(path)

writer = _connect(path)
# v1 是初始版本，用 bootstrap
bootstrap_version(
    writer,
    VersionCreate(
        version_code="v1",
        name="init",
        species=[SpeciesIn(species_code="A", scientific_name="sp A")],
        mappings={},
    ),
)
writer.close()

reader = _connect(path)
reader.execute("BEGIN")  # 建立快照
seen_before = reader.execute(
    "SELECT version_code FROM checklist_versions WHERE is_current=1"
).fetchone()["version_code"]
assert seen_before == "v1"

ready = threading.Event()


def do_publish():
    w = _connect(path)
    ready.set()
    publish_version(
        w,
        VersionCreate(
            version_code="v2",
            name="split",
            species=[
                SpeciesIn(species_code="A1", scientific_name="sp A1"),
                SpeciesIn(species_code="A2", scientific_name="sp A2"),
            ],
            mappings={"A": ["A1", "A2"]},
        ),
    )
    w.close()


t = threading.Thread(target=do_publish)
t.start()
ready.wait()
t.join()

# 写已提交，但读事务快照不变
seen_after = reader.execute(
    "SELECT version_code FROM checklist_versions WHERE is_current=1"
).fetchone()["version_code"]
print("读事务快照内：换版前 =", seen_before, " 换版提交后 =", seen_after)
assert seen_before == seen_after == "v1", "快照被换版写入破坏！"
reader.execute("COMMIT")

# 新事务能看到 v2
fresh = _connect(path)
new_cur = fresh.execute(
    "SELECT version_code FROM checklist_versions WHERE is_current=1"
).fetchone()["version_code"]
print("新事务读到现行版 =", new_cur)
assert new_cur == "v2"

# 写锁竞争：BEGIN IMMEDIATE 期间第二个写者等待而非立即失败
w1 = _connect(path)
w1.execute("BEGIN IMMEDIATE")
timed_out = []


def second_writer():
    # SQLite 连接只能在创建它的线程内使用，故连接在线程内创建
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        timed_out.append(str(exc))
    finally:
        conn.close()


t2 = threading.Thread(target=second_writer)
t2.start()
t2.join(timeout=2)
assert not timed_out, f"写锁未等待而是直接报错: {timed_out}"
w1.execute("COMMIT")
t2.join(timeout=5)
print("写锁竞争：第二个写者成功等待并完成（busy_timeout 生效）")
w1.close()
fresh.close()
reader.close()
print("并发快照一致性验证通过 ✔")
