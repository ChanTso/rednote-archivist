from __future__ import annotations

CLEARING_MANUAL_STATUSES = {"accepted", "corrected", "cleared", "discarded", "discarded_by_user"}
DISCARDED_MANUAL_STATUSES = {"discarded", "discarded_by_user"}


def manual_review_decision(ocr_item: dict) -> dict | None:
    manual = ocr_item.get("manual_review")
    if not isinstance(manual, dict):
        return None
    status = str(manual.get("status") or "")
    if status not in CLEARING_MANUAL_STATUSES:
        return None
    if manual.get("human_review_required") not in {False, 0, "false", "False"}:
        return None
    return manual


def is_user_discarded_ocr(ocr_item: dict) -> bool:
    manual = manual_review_decision(ocr_item)
    return bool(manual and str(manual.get("status") or "") in DISCARDED_MANUAL_STATUSES)
