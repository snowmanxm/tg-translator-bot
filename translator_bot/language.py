from __future__ import annotations

import re


CHINESE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def contains_chinese(text: str | None) -> bool:
    if not text:
        return False
    return bool(CHINESE_RE.search(text))
