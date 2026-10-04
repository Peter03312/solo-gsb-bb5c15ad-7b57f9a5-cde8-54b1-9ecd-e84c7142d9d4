"""请求体模型。strict 模式拒绝布尔冒充整数、数字字符串等。"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BoxIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    x: int
    y: int
    width: int
    height: int
    reason: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("x", "y", "width", "height")
    @classmethod
    def _not_bool(cls, v: int) -> int:
        if isinstance(v, bool):
            raise ValueError("必须是整数，不能是布尔值")
        return v


class AnnotationsUpdate(BaseModel):
    model_config = ConfigDict(strict=True)

    expected_revision: Annotated[int, Field(ge=0)]
    boxes: Annotated[list[BoxIn], Field(max_length=1000)]
