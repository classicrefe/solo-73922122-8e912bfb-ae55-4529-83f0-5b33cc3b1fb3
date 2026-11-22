"""端到端测试：TestClient + 临时 SQLite 文件，覆盖全部业务分支。"""
from __future__ import annotations

import os
import tempfile

import pytest

# 必须在导入 app 之前指定独立数据库
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DET_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()  # 显式建库；TestClient 作为上下文管理器时 lifespan 也会再执行一次（幂等）
client = TestClient(app)


V1_SPECIES = [
    {"species_code": "PIN01", "scientific_name": "Pinus massoniana"},
    {"species_code": "PIN02", "scientific_name": "Pinus tabuliformis"},
    {"species_code": "QRC01", "scientific_name": "Quercus variabilis"},
    {"species_code": "QRC02", "scientific_name": "Quercus acutissima"},
    {"species_code": "CUN01", "scientific_name": "Cunninghamia lanceolata"},
]

V2 = {
    "version_code": "v2",
    "name": "第二版",
    "species": [
        {"species_code": "PIN01A", "scientific_name": "Pinus massoniana A"},
        {"species_code": "PIN01B", "scientific_name": "Pinus massoniana B"},
        {"species_code": "PIN02", "scientific_name": "Pinus tabuliformis"},
        {"species_code": "QRC99", "scientific_name": "Quercus sl"},
        {"species_code": "CUN01", "scientific_name": "Cunninghamia lanceolata"},
    ],
    "mappings": {
        "PIN01": ["PIN01A", "PIN01B"],
        "PIN02": ["PIN02"],
        "QRC01": ["QRC99"],
        "QRC02": ["QRC99"],
        "CUN01": ["CUN01"],
    },
}


@pytest.fixture(scope="session", autouse=True)
def _bootstrap():
    r = client.post(
        "/versions/bootstrap",
        json={"version_code": "v1", "name": "初始版", "species": V1_SPECIES, "mappings": {}},
    )
    assert r.status_code == 200
    yield


def test_health_and_current():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/versions/current").json() == {"current_version": "v1"}
    versions = client.get("/versions").json()
    assert versions[0]["version_code"] == "v1"
    assert versions[0]["is_current"] is True
    assert versions[0]["species_count"] == 5


def test_bootstrap_twice_conflicts():
    r = client.post(
        "/versions/bootstrap",
        json={"version_code": "v1b", "name": "x", "species": V1_SPECIES[:1], "mappings": {}},
    )
    assert r.status_code == 409


def test_register_validation_errors():
    r = client.post(
        "/identifications",
        json={"specimen_no": "S1", "version_code": "v9", "species_code": "X"},
    )
    assert r.status_code == 404
    r = client.post(
        "/identifications",
        json={"specimen_no": "S1", "version_code": "v1", "species_code": "NOPE"},
    )
    assert r.status_code == 404


def test_full_lifecycle():
    # 登记：S-SPLIT 单份鉴定 -> unique
    r = client.post(
        "/identifications",
        json={"specimen_no": "S-SPLIT", "version_code": "v1", "species_code": "PIN01"},
    )
    split_id = r.json()["id"]
    res = client.get("/specimens/S-SPLIT/resolution").json()
    assert res["status"] == "unique"
    assert res["resolved_species_code"] == "PIN01"
    assert res["current_version"] == "v1"
    assert res["snapshot_consistent"] is True

    # 同标本第二份不同鉴定 -> conflict（无交集）
    client.post(
        "/identifications",
        json={"specimen_no": "S-CONF", "version_code": "v1", "species_code": "QRC01"},
    )
    c2 = client.post(
        "/identifications",
        json={"specimen_no": "S-CONF", "version_code": "v1", "species_code": "QRC02"},
    ).json()["id"]
    res = client.get("/specimens/S-CONF/resolution").json()
    assert res["status"] == "conflict"
    assert res["candidates"] == [] and res["resolved_species_code"] is None

    # 未鉴定
    res = client.get("/specimens/S-EMPTY/resolution").json()
    assert res["status"] == "unidentified"

    # 发布 v2（拆种+并种）
    r = client.post("/versions", json=V2)
    assert r.status_code == 200
    assert client.get("/versions/current").json()["current_version"] == "v2"

    # 拆种 -> review
    res = client.get("/specimens/S-SPLIT/resolution").json()
    assert res["status"] == "review"
    assert set(res["candidates"]) == {"PIN01A", "PIN01B"}
    assert res["active_identifications"][0]["chain"][1]["codes"] == ["PIN01A", "PIN01B"]

    # 并种 -> 原冲突转为 unique
    res = client.get("/specimens/S-CONF/resolution").json()
    assert res["status"] == "unique"
    assert res["resolved_species_code"] == "QRC99"

    # 撤回一份：S-CONF 剩 QRC01 链，结论不变（仍 unique QRC99）
    r = client.post(f"/identifications/{c2}/withdraw")
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"
    res = client.get("/specimens/S-CONF/resolution").json()
    assert res["status"] == "unique"
    assert len(res["active_identifications"]) == 1

    # 重复撤回 409
    assert client.post(f"/identifications/{c2}/withdraw").status_code == 409
    # 撤回不存在的鉴定 404
    assert client.post("/identifications/999999/withdraw").status_code == 404

    # 撤回唯一有效鉴定 -> unidentified
    client.post(f"/identifications/{split_id}/withdraw")
    res = client.get("/specimens/S-SPLIT/resolution").json()
    assert res["status"] == "unidentified"


def test_failed_publish_rolls_back():
    bad = {
        "version_code": "v2-bad",
        "name": "缺映射的坏版本",
        "species": V2["species"],
        "mappings": {k: v for k, v in V2["mappings"].items() if k != "CUN01"},
    }
    r = client.post("/versions", json=bad)
    assert r.status_code == 400
    # 现行版仍是 v2
    assert client.get("/versions/current").json()["current_version"] == "v2"
    # 坏版本及其物种均未落库
    versions = [v["version_code"] for v in client.get("/versions").json()]
    assert "v2-bad" not in versions
    # 去向指向不存在的新版代码也应失败
    bad2 = {
        "version_code": "v2-bad2",
        "name": "坏去向",
        "species": V2["species"],
        "mappings": dict(V2["mappings"], PIN02=["GHOST"]),
    }
    r = client.post("/versions", json=bad2)
    assert r.status_code == 400
    assert client.get("/versions/current").json()["current_version"] == "v2"


def test_duplicate_version_code():
    r = client.post("/versions", json={**V2, "name": "重复版号"})
    assert r.status_code == 409


def test_multi_hop_v3():
    v3 = {
        "version_code": "v3",
        "name": "第三版",
        "species": [
            {"species_code": "PIN01A", "scientific_name": "a"},
            {"species_code": "PIN01B", "scientific_name": "b"},
            {"species_code": "PIN99", "scientific_name": "p"},
            {"species_code": "QRC99X", "scientific_name": "qx"},
            {"species_code": "QRC99Y", "scientific_name": "qy"},
            {"species_code": "CUN01", "scientific_name": "c"},
        ],
        "mappings": {
            "PIN01A": ["PIN01A"],
            "PIN01B": ["PIN01B"],
            "PIN02": ["PIN99"],
            "QRC99": ["QRC99X", "QRC99Y"],
            "CUN01": ["CUN01"],
        },
    }
    assert client.post("/versions", json=v3).status_code == 200
    assert client.get("/versions/current").json()["current_version"] == "v3"

    # v1 登记的标本经两跳转换
    res = client.get("/specimens/S-CONF/resolution").json()
    assert res["status"] == "review"
    assert set(res["candidates"]) == {"QRC99X", "QRC99Y"}
    chain = res["active_identifications"][0]["chain"]
    assert [s["version"] for s in chain] == ["v1", "v2", "v3"]

    # 用旧版 v1 代码在 v3 时期登记，同样可追溯
    client.post(
        "/identifications",
        json={"specimen_no": "S-OLD", "version_code": "v1", "species_code": "PIN02"},
    )
    res = client.get("/specimens/S-OLD/resolution").json()
    assert res["status"] == "unique"
    assert res["resolved_species_code"] == "PIN99"

    # 用现行版代码登记
    client.post(
        "/identifications",
        json={"specimen_no": "S-NEW", "version_code": "v3", "species_code": "CUN01"},
    )
    res = client.get("/specimens/S-NEW/resolution").json()
    assert res["status"] == "unique"
    assert res["resolved_species_code"] == "CUN01"

    # 映射查询接口
    maps = client.get("/versions/v2/mappings/outgoing").json()
    qrc = next(m for m in maps if m["from_code"] == "QRC99")
    assert qrc["to_codes"] == ["QRC99X", "QRC99Y"]


def test_snapshot_consistency_under_concurrent_writes():
    """换版期间反复查询，单次查询内部必须始终自洽（同一快照）。"""
    import threading

    errors: list[Exception] = []

    def worker():
        try:
            for i in range(20):
                client.post(
                    "/identifications",
                    json={
                        "specimen_no": f"S-CONC-{i % 4}",
                        "version_code": "v3",
                        "species_code": "CUN01",
                    },
                )
                res = client.get(f"/specimens/S-CONC-{i % 4}/resolution").json()
                assert res["current_version"] == "v3"
                assert res["snapshot_consistent"] is True
                assert res["status"] in {"unique", "review", "conflict", "unidentified"}
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
