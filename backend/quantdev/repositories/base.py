"""Repository 公共叶子模块：共享 id/时间/JSON 辅助函数。

放在最底层，被 quantdev.store 与各 repository 复用，避免与 store 形成循环导入。
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return "{}_{}".format(prefix, uuid.uuid4().hex[:16])


def decode_json(value: str) -> Any:
    return json.loads(value)
