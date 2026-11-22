"""FastAPI 入口：树木标本鉴定供数 API。

在 /docs 可直接演示：
1. POST /versions/bootstrap 建立 v1 名录；
2. POST /identifications 登记鉴定；
3. GET /specimens/{specimen_no}/resolution 查看现行候选物种；
4. POST /versions 发布 v2（请求体给出拆种/并种映射），再查结论变化；
5. POST /identifications/{id}/withdraw 撤回鉴定，再查结论变化。
"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .database import get_conn, init_db
from .schemas import (
    IdentificationCreate,
    IdentificationOut,
    MappingOut,
    ResolutionOut,
    SpeciesIn,
    VersionCreate,
    VersionOut,
    WithdrawOut,
)
from .services import (
    get_current_version,
    list_versions,
    publish_version,
    register_identification,
    resolve_specimen,
    bootstrap_version,
    withdraw_identification,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动即建库建表（WAL 模式，数据目录不存在时自动创建）
    init_db()
    yield


app = FastAPI(
    title="树木标本鉴定供数 API",
    description=(
        "同一件树木标本可有多份鉴定；名录换版存在拆种（一个代码→多个代码）"
        "与并种（多个代码→同一代码）。本服务按现行名录对每份有效鉴定逐版转换，"
        "取候选物种交集：唯一→可入库，多个→待复核，无交集→鉴定冲突，"
        "无有效鉴定→未鉴定。换版为原子事务，失败则继续按原名录供数；"
        "查询在单快照内完成，换版期间登记/撤回不影响查询一致性。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_db() -> sqlite3.Connection:
    with get_conn() as conn:
        yield conn


# ---------- 健康检查 ----------

@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


# ---------- 名录版本 ----------

@app.post(
    "/versions/bootstrap",
    tags=["名录版本"],
    summary="初始化第一个名录版本（无前置版本，无需映射）",
    response_model=VersionOut,
)
def create_bootstrap(payload: VersionCreate, conn=Depends(get_db)) -> JSONResponse:
    bootstrap_version(conn, payload)
    row = conn.execute(
        "SELECT v.*, (SELECT COUNT(*) FROM species s "
        "WHERE s.version_code=v.version_code) AS species_count "
        "FROM checklist_versions v WHERE version_code=?",
        (payload.version_code,),
    ).fetchone()
    return _version_json(row)


@app.post(
    "/versions",
    tags=["名录版本"],
    summary="发布新版名录（须给出前版每个代码的一个或多个去向）",
    response_model=VersionOut,
    responses={
        400: {"description": "映射不完整/去向非法，事务已回滚，仍按原名录供数"},
        409: {"description": "版号冲突或尚无初始名录"},
    },
)
def create_version(payload: VersionCreate, conn=Depends(get_db)) -> JSONResponse:
    publish_version(conn, payload)
    row = conn.execute(
        "SELECT v.*, (SELECT COUNT(*) FROM species s "
        "WHERE s.version_code=v.version_code) AS species_count "
        "FROM checklist_versions v WHERE version_code=?",
        (payload.version_code,),
    ).fetchone()
    return _version_json(row)


@app.get("/versions", tags=["名录版本"], summary="名录版本列表", response_model=list[VersionOut])
def get_versions(conn=Depends(get_db)) -> list[dict]:
    return [_version_dict(r) for r in list_versions(conn)]


@app.get("/versions/current", tags=["名录版本"], summary="现行版号")
def current_version(conn=Depends(get_db)) -> dict:
    cur = get_current_version(conn)
    return {"current_version": cur}


@app.get(
    "/versions/{version_code}/species",
    tags=["名录版本"],
    summary="某版名录的物种清单",
    response_model=list[SpeciesIn],
)
def version_species(version_code: str, conn=Depends(get_db)) -> list[dict]:
    if not conn.execute(
        "SELECT 1 FROM checklist_versions WHERE version_code=?", (version_code,)
    ).fetchone():
        raise HTTPException(404, f"版本 {version_code} 不存在")
    rows = conn.execute(
        "SELECT species_code, scientific_name FROM species "
        "WHERE version_code=? ORDER BY species_code",
        (version_code,),
    ).fetchall()
    return [dict(r) for r in rows]


@app.get(
    "/versions/{version_code}/mappings/outgoing",
    tags=["名录版本"],
    summary="某版到其后继版本的代码去向（拆种/并种）",
    response_model=list[MappingOut],
)
def version_mappings(version_code: str, conn=Depends(get_db)) -> list[dict]:
    rows = conn.execute(
        "SELECT from_version, to_version, from_code, to_code FROM code_mappings "
        "WHERE from_version=? ORDER BY from_code, to_code",
        (version_code,),
    ).fetchall()
    # 线性版本链下同一 from_version 只有一个 to_version，按 from_code 聚合
    result: dict[str, dict] = {}
    for r in rows:
        item = result.setdefault(
            r["from_code"],
            {
                "from_version": r["from_version"],
                "to_version": r["to_version"],
                "from_code": r["from_code"],
                "to_codes": [],
            },
        )
        item["to_codes"].append(r["to_code"])
    return list(result.values())


# ---------- 鉴定 ----------

@app.post(
    "/identifications",
    tags=["鉴定登记"],
    summary="登记一份鉴定（标本号 + 名录版号 + 物种代码）",
    response_model=IdentificationOut,
)
def create_identification(
    payload: IdentificationCreate, conn=Depends(get_db)
) -> dict:
    row = register_identification(conn, payload)
    return dict(row)


@app.get(
    "/identifications",
    tags=["鉴定登记"],
    summary="鉴定列表（可按标本号过滤）",
    response_model=list[IdentificationOut],
)
def list_identifications(
    specimen_no: str | None = Query(None, description="可选：按标本号过滤"),
    status_filter: str | None = Query(None, alias="status", description="active / withdrawn"),
    conn=Depends(get_db),
) -> list[dict]:
    sql = "SELECT * FROM identifications"
    clauses, params = [], []
    if specimen_no:
        clauses.append("specimen_no = ?")
        params.append(specimen_no)
    if status_filter:
        clauses.append("status = ?")
        params.append(status_filter)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


@app.post(
    "/identifications/{identification_id}/withdraw",
    tags=["鉴定登记"],
    summary="按鉴定编号撤回一份鉴定",
    response_model=WithdrawOut,
)
def withdraw(identification_id: int, conn=Depends(get_db)) -> dict:
    row = withdraw_identification(conn, identification_id)
    return {
        "id": row["id"],
        "specimen_no": row["specimen_no"],
        "status": row["status"],
        "withdrawn_at": row["withdrawn_at"],
    }


# ---------- 候选物种查询 ----------

@app.get(
    "/specimens/{specimen_no}/resolution",
    tags=["供数查询"],
    summary="查询现行版号与候选物种结论（唯一/待复核/冲突/未鉴定）",
    response_model=ResolutionOut,
)
def get_resolution(specimen_no: str, conn=Depends(get_db)) -> dict:
    return resolve_specimen(conn, specimen_no)


# ---------- 序列化辅助 ----------

def _version_dict(row: sqlite3.Row) -> dict:
    return {
        "version_code": row["version_code"],
        "name": row["name"],
        "parent_version": row["parent_version"],
        "is_current": bool(row["is_current"]),
        "created_at": row["created_at"],
        "species_count": row["species_count"],
    }


def _version_json(row: sqlite3.Row) -> JSONResponse:
    return JSONResponse(_version_dict(row))
