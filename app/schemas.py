"""请求/响应数据模型。"""
import json
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

Severity = Literal["low", "medium", "high"]


# ---------- 试剂 ----------

class ReagentBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    cas: Optional[str] = None
    formula: Optional[str] = None
    concentration: Optional[str] = None
    state: Literal["liquid", "solid", "gas"] = "liquid"
    categories: list[str] = Field(default_factory=list)
    note: Optional[str] = None


class ReagentCreate(ReagentBase):
    pass


class ReagentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    cas: Optional[str] = None
    formula: Optional[str] = None
    concentration: Optional[str] = None
    state: Optional[Literal["liquid", "solid", "gas"]] = None
    categories: Optional[list[str]] = None
    note: Optional[str] = None


class ReagentOut(ReagentBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    builtin: bool
    created_at: datetime

    @field_validator("categories", mode="before")
    @classmethod
    def _parse_categories(cls, value):
        if isinstance(value, str):
            return json.loads(value or "[]")
        return value or []


# ---------- 步骤 / 方案 ----------

class StepIn(BaseModel):
    reagent_id: Optional[int] = None
    reagent_name: Optional[str] = None  # 未建档的临时试剂
    categories: Optional[list[str]] = None  # 临时试剂类别
    order_index: int = Field(ge=0)
    time_offset_min: Optional[float] = Field(default=None, ge=0)
    amount_ml: Optional[float] = Field(default=None, ge=0)
    note: Optional[str] = None


class PlanBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: Optional[str] = None


class PlanCreate(PlanBase):
    steps: list[StepIn] = Field(default_factory=list)


class PlanUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    steps: Optional[list[StepIn]] = None


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_index: int
    time_offset_min: Optional[float]
    reagent_id: Optional[int]
    reagent_name: str
    amount_ml: Optional[float]
    note: Optional[str]
    categories: list[str] = []  # 由路由填充


class PlanOut(PlanBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
    steps: list[StepOut]
    effective_rule_version: Optional[dict] = None  # 当前生效规则版本摘要（None=使用全局规则图）


# ---------- 规则 ----------

class RuleCreate(BaseModel):
    id: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    directional: bool = False
    severity: Severity = "medium"
    title: str = ""
    reaction: str = ""
    rationale: str = ""
    references: list[str] = Field(default_factory=list)


class RuleUpdate(BaseModel):
    active: Optional[bool] = None
    severity: Optional[Severity] = None
    title: Optional[str] = None
    rationale: Optional[str] = None


class RuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    target: str
    directional: bool
    severity: Severity
    title: str
    reaction: str
    rationale: str
    references: list[str]
    builtin: bool
    active: bool


# ---------- 推演记录 ----------

class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    plan_id: Optional[int]
    plan_name: str
    rule_revision: str
    status: str
    created_at: datetime
    report: dict


# ---------- 方案规则版本 ----------

class VersionRuleIn(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    directional: bool = False
    severity: Severity = "medium"
    title: str = ""
    reaction: str = ""
    rationale: str = ""
    references: list[str] = Field(default_factory=list)
    builtin: bool = False


class RuleVersionCreate(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)
    base_version_id: Optional[int] = None  # 不传则克隆当前生效版本 / 全局规则图
    rules: Optional[list[VersionRuleIn]] = None  # 不传则按克隆来源生成草稿


class RuleVersionUpdate(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)
    rules: Optional[list[VersionRuleIn]] = None
