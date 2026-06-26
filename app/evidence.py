"""
Evidence reasoning engine.
Step 1: Deterministic matching (fast, no LLM cost).
Step 2: LLM confirmation pass with focused prompt.
"""

from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple

from app.models import TransactionEntry


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_amount_from_complaint(text: str) -> Optional[float]:
    patterns = [
        r"(?:tk|taka|bdt)[\s.]*([0-9,]+(?:\.[0-9]+)?)",
        r"([0-9,]+(?:\.[0-9]+)?)\s*(?:tk|taka|bdt)",
        r"(?:sent|transferred|paid|deducted|charged|amount of)\s+([0-9,]+(?:\.[0-9]+)?)",
        r"([0-9]{3,}(?:[,][0-9]{3})*(?:\.[0-9]+)?)",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(",", "")
            try:
                return float(raw)
            except ValueError:
                pass
    return None


def _extract_time_keywords(text: str) -> Optional[str]:
    """Return rough time hint: 'recent', 'today', 'morning', 'afternoon', 'yesterday', or None."""
    text_lower = text.lower()
    if "yesterday" in text_lower:
        return "yesterday"
    if any(w in text_lower for w in ["this morning", "in the morning", "morning"]):
        return "morning"
    if any(w in text_lower for w in ["afternoon", "2pm", "3pm", "4pm"]):
        return "afternoon"
    if any(w in text_lower for w in ["today", "just now", "a few minutes", "recently", "just"]):
        return "today"
    return None


def deterministic_match(
    complaint: str,
    transaction_history: List[TransactionEntry],
) -> Tuple[Optional[str], float]:
    """
    Returns (best_transaction_id, confidence_score).
    confidence_score 0–1 based on how many signals matched.
    """
    if not transaction_history:
        return None, 0.0

    complaint_amount = _extract_amount_from_complaint(complaint)
    time_hint = _extract_time_keywords(complaint)
    complaint_lower = complaint.lower()

    scores: dict[str, float] = {}

    for txn in transaction_history:
        score = 0.0

        # Amount match
        if complaint_amount and abs(txn.amount - complaint_amount) < 0.01:
            score += 0.45

        # Counterparty mention in complaint
        counterparty_clean = re.sub(r"[^0-9]", "", txn.counterparty)
        if len(counterparty_clean) >= 6 and counterparty_clean[-6:] in re.sub(r"[^0-9]", "", complaint):
            score += 0.25
        elif txn.counterparty.lower() in complaint_lower:
            score += 0.25

        # Time proximity
        txn_ts = _parse_iso(txn.timestamp)
        if txn_ts:
            now_utc = datetime.now(timezone.utc)
            age = now_utc - txn_ts
            if age < timedelta(hours=2):
                if time_hint in ("today", "just now", None):
                    score += 0.15
            elif age < timedelta(hours=12):
                if time_hint in ("today", "morning", "afternoon"):
                    score += 0.10
            elif age < timedelta(days=1):
                if time_hint == "yesterday":
                    score += 0.10

        # Transaction type keyword hints
        type_hints = {
            "transfer": ["sent", "transferred", "wrong number", "wrong transfer", "wrong recipient"],
            "payment": ["payment", "paid", "merchant", "shop"],
            "cash_in": ["cash in", "deposit", "agent"],
            "cash_out": ["cash out", "withdraw"],
            "refund": ["refund", "return"],
        }
        for txn_type, keywords in type_hints.items():
            if txn.type == txn_type and any(kw in complaint_lower for kw in keywords):
                score += 0.15

        # Failed/pending status bonus
        if txn.status in ("failed", "pending"):
            if any(w in complaint_lower for w in ["failed", "deducted", "not received", "not credited", "balance"]):
                score += 0.10

        scores[txn.transaction_id] = score

    if not scores:
        return None, 0.0

    best_id = max(scores, key=lambda k: scores[k])
    best_score = scores[best_id]

    if best_score < 0.15:
        return None, best_score

    return best_id, min(best_score, 1.0)


VALID_VERDICTS = {"consistent", "inconsistent", "insufficient_data"}
VALID_CASE_TYPES = {
    "wrong_transfer",
    "payment_failed",
    "refund_request",
    "duplicate_payment",
    "merchant_settlement_delay",
    "agent_cash_in_issue",
    "phishing_or_social_engineering",
    "other",
}
VALID_DEPARTMENTS = {
    "customer_support",
    "dispute_resolution",
    "payments_ops",
    "merchant_operations",
    "agent_operations",
    "fraud_risk",
}
VALID_SEVERITIES = {"low", "medium", "high", "critical"}


def enforce_enums(data: dict) -> dict:
    """Hard-enforce all enum fields, falling back to safe defaults."""
    if data.get("evidence_verdict") not in VALID_VERDICTS:
        data["evidence_verdict"] = "insufficient_data"
    if data.get("case_type") not in VALID_CASE_TYPES:
        data["case_type"] = "other"
    if data.get("department") not in VALID_DEPARTMENTS:
        data["department"] = "customer_support"
    if data.get("severity") not in VALID_SEVERITIES:
        data["severity"] = "medium"
    return data
