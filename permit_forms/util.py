"""小型共享工具：稳定文本与校验。"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def non_empty(value: Any, name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(name + " must be non-empty")
    return text


def stable_text(value: Any) -> str:
    """稳定、可复现的 JSON 文本，用于摘要和比对。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_text(value: Any) -> str:
    return hashlib.sha256(stable_text(value).encode("utf-8")).hexdigest()
