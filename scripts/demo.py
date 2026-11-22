"""端到端演示：拆种、并种、撤回如何改变鉴定结论。

用法（服务启动后）：
    python scripts/demo.py
    BASE_URL=http://localhost:8000 python scripts/demo.py

脚本只调用 HTTP 接口，不直接读库；每步都打印请求与结论。
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
# 加时间戳后缀，重复运行不会与历史标本冲突
RUN = time.strftime("%H%M%S")

SPLIT = f"SPLIT-{RUN}"
MERGE = f"MERGE-{RUN}"
CONFL = f"CONFL-{RUN}"


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def show(resp: httpx.Response) -> dict:
    data = resp.json()
    print(f"-> HTTP {resp.status_code}: {data}")
    return data


def resolve(client: httpx.Client, specimen: str) -> dict:
    r = client.get(f"{BASE_URL}/specimens/{specimen}/resolution")
    return show(r)


def main() -> int:
    client = httpx.Client(timeout=10)
    banner("0. 健康检查 / 现行版本")
    show(client.get(f"{BASE_URL}/health"))
    cur = show(client.get(f"{BASE_URL}/versions/current"))

    if cur["current_version"] is None:
        print("未初始化，先 bootstrap v1 ...")
        show(
            client.post(
                f"{BASE_URL}/versions/bootstrap",
                json={
                    "version_code": "v1",
                    "name": "第一版树木名录（初始版）",
                    "species": [
                        {"species_code": "PIN01", "scientific_name": "Pinus massoniana"},
                        {"species_code": "PIN02", "scientific_name": "Pinus tabuliformis"},
                        {"species_code": "QRC01", "scientific_name": "Quercus variabilis"},
                        {"species_code": "QRC02", "scientific_name": "Quercus acutissima"},
                        {"species_code": "CUN01", "scientific_name": "Cunninghamia lanceolata"},
                    ],
                    "mappings": {},
                },
            )
        )

    banner("1. 在 v1 登记三份标本的鉴定")
    id_split = show(
        client.post(
            f"{BASE_URL}/identifications",
            json={"specimen_no": SPLIT, "version_code": "v1", "species_code": "PIN01"},
        )
    )["id"]
    id_merge = show(
        client.post(
            f"{BASE_URL}/identifications",
            json={"specimen_no": MERGE, "version_code": "v1", "species_code": "PIN02"},
        )
    )["id"]
    show(
        client.post(
            f"{BASE_URL}/identifications",
            json={"specimen_no": CONFL, "version_code": "v1", "species_code": "QRC01"},
        )
    )
    id_confl_qrc02 = show(
        client.post(
            f"{BASE_URL}/identifications",
            json={"specimen_no": CONFL, "version_code": "v1", "species_code": "QRC02"},
        )
    )["id"]

    print("\n-- 初始结论（v1，均应为候选唯一/待复核等预期状态）--")
    r1 = resolve(client, SPLIT)
    assert r1["status"] == "unique" and r1["resolved_species_code"] == "PIN01"
    r2 = resolve(client, MERGE)
    assert r2["status"] == "unique" and r2["resolved_species_code"] == "PIN02"
    r3 = resolve(client, CONFL)
    print(f"   {CONFL} 两份不同鉴定 -> 状态 {r3['status']}（{r3['status_label']}）")
    assert r3["status"] == "conflict"

    banner("2. 发布 v2：PIN01 拆种，QRC01+QRC02 并种，逐代码给出去向")
    v2 = {
        "version_code": "v2",
        "name": "第二版（松类拆种、栎类并种）",
        "species": [
            {"species_code": "PIN01A", "scientific_name": "Pinus massoniana var. A"},
            {"species_code": "PIN01B", "scientific_name": "Pinus massoniana var. B"},
            {"species_code": "PIN02", "scientific_name": "Pinus tabuliformis"},
            {"species_code": "QRC99", "scientific_name": "Quercus sensu lato"},
            {"species_code": "CUN01", "scientific_name": "Cunninghamia lanceolata"},
        ],
        "mappings": {
            "PIN01": ["PIN01A", "PIN01B"],  # 拆种：一源多去向
            "PIN02": ["PIN02"],
            "QRC01": ["QRC99"],             # 并种：两源同去向
            "QRC02": ["QRC99"],
            "CUN01": ["CUN01"],
        },
    }
    show(client.post(f"{BASE_URL}/versions", json=v2))
    print("\n-- 换版后重新查询 --")
    r1 = resolve(client, SPLIT)
    print(f"   {SPLIT}: {r1['status']}（{r1['status_label']}）候选={r1['candidates']}")
    assert r1["status"] == "review" and set(r1["candidates"]) == {"PIN01A", "PIN01B"}
    r2 = resolve(client, MERGE)
    print(f"   {MERGE}: {r2['status']}（{r2['status_label']}）入库代码={r2['resolved_species_code']}")
    assert r2["status"] == "unique" and r2["resolved_species_code"] == "PIN02"
    r3 = resolve(client, CONFL)
    print(f"   {CONFL}: {r3['status']}（{r3['status_label']}）候选={r3['candidates']}")
    assert r3["status"] == "unique" and r3["resolved_species_code"] == "QRC99", "并种应消除原冲突"

    banner("3. 换版失败演练：故意漏掉前版代码 CUN01 的去向")
    bad = dict(v2)
    bad["version_code"] = "v2-bad"
    bad["mappings"] = {k: v for k, v in v2["mappings"].items() if k != "CUN01"}
    show(client.post(f"{BASE_URL}/versions", json=bad))
    cur = show(client.get(f"{BASE_URL}/versions/current"))
    assert cur["current_version"] == "v2", "失败后必须仍按原名录 v2 供数"
    print("   现行版仍为 v2，查询不受影响：")
    assert resolve(client, SPLIT)["status"] == "review"

    banner("4. 发布 v3（多跳转换）：QRC99 再拆、PIN01A+PIN02 再并")
    v3 = {
        "version_code": "v3",
        "name": "第三版（再次拆分与合并）",
        "species": [
            {"species_code": "PIN01A", "scientific_name": "Pinus massoniana var. A"},
            {"species_code": "PIN01B", "scientific_name": "Pinus massoniana var. B"},
            {"species_code": "PIN99", "scientific_name": "Pinus sensu lato"},
            {"species_code": "QRC99X", "scientific_name": "Quercus sl. subsp. X"},
            {"species_code": "QRC99Y", "scientific_name": "Quercus sl. subsp. Y"},
            {"species_code": "CUN01", "scientific_name": "Cunninghamia lanceolata"},
        ],
        "mappings": {
            "PIN01A": ["PIN01A"],
            "PIN01B": ["PIN01B"],
            "PIN02": ["PIN99"],
            "QRC99": ["QRC99X", "QRC99Y"],  # 再拆
            "CUN01": ["CUN01"],
        },
    }
    show(client.post(f"{BASE_URL}/versions", json=v3))
    r3c = resolve(client, CONFL)
    print(f"   {CONFL} 经 v1→v2→v3 两跳：{r3c['status']} 候选={r3c['candidates']}")
    assert r3c["status"] == "review"
    assert set(r3c["candidates"]) == {"QRC99X", "QRC99Y"}
    print("   转换链明细：")
    for tr in r3c["active_identifications"]:
        print("    ", tr["identification_id"], tr["chain"])

    banner("5. 撤回鉴定，观察结论变化")
    show(client.post(f"{BASE_URL}/identifications/{id_confl_qrc02}/withdraw"))
    r3c = resolve(client, CONFL)
    print(f"   撤回一份后 {CONFL}: {r3c['status']}（{r3c['status_label']}）候选={r3c['candidates']}")
    assert r3c["status"] == "review"  # 剩 QRC01 -> QRC99 -> 两个候选
    print("   重复撤回应报错：")
    show(client.post(f"{BASE_URL}/identifications/{id_confl_qrc02}/withdraw"))

    show(client.post(f"{BASE_URL}/identifications/{id_split}/withdraw"))
    r1 = resolve(client, SPLIT)
    print(f"   撤回唯一鉴定后 {SPLIT}: {r1['status']}（{r1['status_label']}）")
    assert r1["status"] == "unidentified" and r1["candidates"] == []

    banner("6. 换版期间登记：用现行版 v3 代码登记，立即查询")
    new_id = show(
        client.post(
            f"{BASE_URL}/identifications",
            json={"specimen_no": SPLIT, "version_code": "v3", "species_code": "PIN99"},
        )
    )["id"]
    r1 = resolve(client, SPLIT)
    assert r1["status"] == "unique" and r1["resolved_species_code"] == "PIN99"
    print(f"   {SPLIT} 重新鉴定为 PIN99 -> {r1['status']}，入库代码 {r1['resolved_species_code']}")

    banner("7. 非法登记校验（版本/代码不存在）")
    show(client.post(
        f"{BASE_URL}/identifications",
        json={"specimen_no": SPLIT, "version_code": "v9", "species_code": "X"},
    ))
    show(client.post(
        f"{BASE_URL}/identifications",
        json={"specimen_no": SPLIT, "version_code": "v3", "species_code": "NOPE"},
    ))

    banner("全部断言通过 ✔  演示标本号前缀：SPLIT/MERGE/CONFL-" + RUN)
    print(f"可在 {BASE_URL}/docs 继续手工试验；鉴定编号示例：{id_merge}, {new_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
