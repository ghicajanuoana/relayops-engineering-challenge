import json
from typing import Any

from pydantic import BaseModel, Field, field_validator


class EventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any]

    @field_validator("event_id", "type")
    @classmethod
    def reject_null_characters(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("null characters are not supported by PostgreSQL")
        return value

    @field_validator("payload")
    @classmethod
    def validate_json_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        # PostgreSQL JSONB rejects non-finite numbers and Unicode nulls.
        raw = json.dumps(value, allow_nan=False, ensure_ascii=False)
        try:
            raw.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("payload strings must contain valid Unicode scalar values") from None
        if "\\u0000" in raw.lower():
            # Inspect decoded strings so a literal backslash-u0000 is allowed.
            def has_null(item):
                if isinstance(item, str):
                    return "\x00" in item
                if isinstance(item, dict):
                    return any(has_null(k) or has_null(v) for k, v in item.items())
                if isinstance(item, list):
                    return any(has_null(v) for v in item)
                return False

            if has_null(value):
                raise ValueError("null characters are not supported by PostgreSQL JSONB")
        return value
