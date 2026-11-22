"""初始化数据库并写入 v1 名录（幂等：已有名录则跳过）。

用法：
    DET_DB_PATH=./data/det.db python -m scripts.seed
容器启动时 entrypoint 会自动调用。
"""
from __future__ import annotations

import os
import sys

# 允许直接 `python scripts/seed.py` 运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import get_conn, init_db  # noqa: E402
from app.schemas import SpeciesIn, VersionCreate  # noqa: E402
from app.services import bootstrap_version, get_current_version  # noqa: E402

V1_SPECIES = [
    SpeciesIn(species_code="PIN01", scientific_name="Pinus massoniana"),
    SpeciesIn(species_code="PIN02", scientific_name="Pinus tabuliformis"),
    SpeciesIn(species_code="QRC01", scientific_name="Quercus variabilis"),
    SpeciesIn(species_code="QRC02", scientific_name="Quercus acutissima"),
    SpeciesIn(species_code="CUN01", scientific_name="Cunninghamia lanceolata"),
]

V1 = VersionCreate(
    version_code="v1",
    name="第一版树木名录（初始版）",
    species=V1_SPECIES,
    mappings={},  # 初始版无需映射
)


def main() -> None:
    init_db()
    with get_conn() as conn:
        if get_current_version(conn) is not None:
            print("数据库已存在名录，跳过种子数据。")
            return
        bootstrap_version(conn, V1)
        print(f"已写入初始名录 v1，含 {len(V1_SPECIES)} 个物种代码。")


if __name__ == "__main__":
    main()
