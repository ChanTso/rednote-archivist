from __future__ import annotations

import re

from .utils import safe_filename

COMPANY_HINTS = [
    "字节",
    "腾讯",
    "阿里",
    "美团",
    "百度",
    "快手",
    "小红书",
    "拼多多",
    "京东",
    "华为",
    "微软",
    "Google",
    "Amazon",
    "Meta",
]

ROLE_HINTS = ["后端", "前端", "算法", "客户端", "数据", "测试", "产品", "运营", "Java", "Go", "Python", "C++"]
ROUND_HINTS = ["一面", "二面", "三面", "hr", "HR", "终面", "笔试", "oc", "OC"]


def _meaningful(text: str | None) -> bool:
    if not text:
        return False
    stripped = re.sub(r"[\W_]+", "", text, flags=re.UNICODE)
    return len(stripped) >= 4


def generate_summary_title(original_title: str | None, body_text: str | None, ocr_text: str | None, note_id: str) -> str:
    if _meaningful(original_title):
        return safe_filename(original_title, max_chars=30, fallback=f"无标题面经_{note_id[:8]}")
    source = "\n".join(part for part in [body_text, ocr_text] if part)
    hints: list[str] = []
    for group in (COMPANY_HINTS, ROLE_HINTS, ROUND_HINTS):
        for hint in group:
            if hint in source and hint not in hints:
                hints.append(hint)
            if len(hints) >= 4:
                break
        if len(hints) >= 4:
            break
    if hints:
        return safe_filename("".join(hints) + "面经", max_chars=30)
    first_line = next((line.strip() for line in source.splitlines() if _meaningful(line.strip())), "")
    if first_line:
        return safe_filename(first_line, max_chars=30, fallback=f"无标题面经_{note_id[:8]}")
    return f"无标题面经_{note_id[:8]}"


def extract_light_structured(text: str) -> dict[str, str]:
    data = {
        "company": "",
        "position": "",
        "city": "",
        "interview_round": "",
        "question_type": "",
        "result": "",
    }
    for company in COMPANY_HINTS:
        if company in text:
            data["company"] = company
            break
    for role in ROLE_HINTS:
        if role in text:
            data["position"] = role
            break
    for round_name in ROUND_HINTS:
        if round_name in text:
            data["interview_round"] = round_name.upper() if round_name.lower() == "hr" else round_name
            break
    if "offer" in text.lower() or "oc" in text.lower() or "已录" in text:
        data["result"] = "疑似通过"
    elif "挂" in text or "拒" in text or "凉" in text:
        data["result"] = "疑似未通过"
    return data
