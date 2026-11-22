"""请求/响应模型，字段说明同时显示在 /docs 上。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# 鉴定结论状态
STATUS_UNIQUE = "unique"          # 候选唯一，可入库
STATUS_REVIEW = "review"          # 多个候选，待复核
STATUS_CONFLICT = "conflict"      # 无交集，鉴定冲突
STATUS_UNIDENTIFIED = "unidentified"  # 无有效鉴定

STATUS_LABELS = {
    STATUS_UNIQUE: "候选唯一",
    STATUS_REVIEW: "待复核",
    STATUS_CONFLICT: "鉴定冲突",
    STATUS_UNIDENTIFIED: "未鉴定",
}


class SpeciesIn(BaseModel):
    species_code: str = Field(..., description="物种代码")
    scientific_name: str = Field(..., description="学名")


class VersionCreate(BaseModel):
    version_code: str = Field(..., description="新版名录版号")
    name: str = Field(..., description="名录名称/说明")
    species: list[SpeciesIn] = Field(
        ..., min_length=1, description="新版包含的全部物种"
    )
    mappings: dict[str, list[str]] = Field(
        ...,
        description=(
            "前版每个代码在新版的一个或多个去向。"
            "键=前版物种代码，值=新版代码列表（拆种给多个，并种与其他键指向同一代码）"
        ),
    )


class VersionOut(BaseModel):
    version_code: str
    name: str
    parent_version: str | None
    is_current: bool
    created_at: datetime
    species_count: int


class IdentificationCreate(BaseModel):
    specimen_no: str = Field(..., description="标本号")
    version_code: str = Field(..., description="名录版号（须已发布）")
    species_code: str = Field(..., description="该版名录中的物种代码")


class IdentificationOut(BaseModel):
    id: int
    specimen_no: str
    version_code: str
    species_code: str
    status: Literal["active", "withdrawn"]
    recorded_at: datetime
    withdrawn_at: datetime | None


class WithdrawOut(BaseModel):
    id: int
    specimen_no: str
    status: Literal["withdrawn"]
    withdrawn_at: datetime


class MappingOut(BaseModel):
    from_version: str
    to_version: str
    from_code: str
    to_codes: list[str]


class ChainStep(BaseModel):
    version: str = Field(..., description="名录版号")
    codes: list[str] = Field(..., description="该版下的候选代码（拆种后可能多个）")


class IdTrace(BaseModel):
    identification_id: int
    registered_version: str
    registered_code: str
    # 逐版转换链，如 [{version:"v1",codes:["PIN01"]},{version:"v2",codes:["PIN01A","PIN01B"]}]
    chain: list[ChainStep]
    candidates_at_current: list[str]


class ResolutionOut(BaseModel):
    specimen_no: str
    current_version: str
    status: Literal["unique", "review", "conflict", "unidentified"]
    status_label: str
    # 候选唯一时给出可入库的物种代码，否则为 None
    resolved_species_code: str | None
    candidates: list[str]
    active_identifications: list[IdTrace]
    snapshot_consistent: bool = Field(
        ..., description="本次查询是否全部基于同一已提交快照（恒为 true）"
    )
