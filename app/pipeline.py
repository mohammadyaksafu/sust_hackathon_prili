"""
Main pipeline orchestrator.
Order:
  1. Input injection scan
  2. Deterministic evidence pre-match (no LLM cost)
  3. Rate limit guard
  4. LLM Pass 1 — evidence reasoning
  5. Rate limit guard
  6. LLM Pass 2 — classification + routing
  7. Confidence arbitration
  8. Safety guardrails (pre customer_reply finalization)
  9. Output field scan
  10. Enum enforcement + JSON assembly
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional

from app.models import TicketRequest, TicketResponse
from app.evidence import deterministic_match, enforce_enums
from app.safety import (
    scan_input_for_injection,
    sanitize_customer_reply,
    validate_output_fields,
    SAFE_REPLY_FALLBACK,
)
from app.llm_client import llm_call, extract_json
from app.prompts import (
    PASS1_SYSTEM, build_pass1_prompt,
    PASS2_SYSTEM, build_pass2_prompt,
)

logger = logging.getLogger(__name__)

# ── Proactive rate limiter ────────────────────────────────────────────────────
# Gemini flash-lite: 30 RPM free tier. We self-limit to 25 RPM to stay safe.
# Each ticket makes 2 LLM calls, so at 25 RPM we can handle ~12 tickets/min.
_request_times: list[float] = []
_RATE_LIMIT_CALLS = 25       # max calls per window
_RATE_LIMIT_WINDOW = 60.0    # seconds


async def _rate_limit_guard() -> None:
    """Wait if we are approaching Gemini free tier limits."""
    global _request_times
    now = time.monotonic()
    # Drop calls outside the rolling window
    _request_times = [t for t in _request_times if now - t < _RATE_LIMIT_WINDOW]

    if len(_request_times) >= _RATE_LIMIT_CALLS:
        # Wait until the oldest slot expires
        wait = _RATE_LIMIT_WINDOW - (now - _request_times[0]) + 0.3
        if wait > 0:
            logger.info(f"Rate limit guard: waiting {wait:.1f}s to avoid 429")
            await asyncio.sleep(wait)
            # Refresh after sleep
            now = time.monotonic()
            _request_times = [t for t in _request_times if now - t < _RATE_LIMIT_WINDOW]

    _request_times.append(time.monotonic())


# ── Deterministic fallbacks (used when LLM is unavailable) ───────────────────

def _extract_complaint_amount(complaint: str) -> Optional[float]:
    from app.evidence import _extract_amount_from_complaint
    return _extract_amount_from_complaint(complaint)


def _fallback_pass1_data(
    complaint: str,
    transactions: list,
    det_id: Optional[str],
    det_confidence: float,
) -> dict:
    complaint_lower = complaint.lower()
    matched_txn = next((txn for txn in transactions if txn.transaction_id == det_id), None)

    if not matched_txn:
        return {
            "relevant_transaction_id": None,
            "evidence_verdict": "insufficient_data",
            "evidence_confidence": max(det_confidence, 0.35),
            "evidence_notes": "No matching transaction found by deterministic checks.",
        }

    if matched_txn.status == "failed" and any(
        w in complaint_lower for w in ["deducted", "charged", "balance"]
    ):
        verdict = "inconsistent"
        notes = "Matched transaction is failed but complaint claims a debit."
    elif det_confidence >= 0.45:
        verdict = "consistent"
        notes = "Matched transaction aligns with complaint signals."
    else:
        verdict = "insufficient_data"
        notes = "Weak match — evidence not conclusive."

    return {
        "relevant_transaction_id": det_id,
        "evidence_verdict": verdict,
        "evidence_confidence": max(det_confidence, 0.45),
        "evidence_notes": notes,
    }


def _fallback_pass2_data(
    ticket_id: str,
    complaint: str,
    evidence_verdict: str,
    injection_detected: bool,
) -> dict:
    complaint_lower = complaint.lower()

    if injection_detected or any(
        w in complaint_lower for w in ["otp", "pin", "called me", "balance is 0"]
    ):
        case_type = "phishing_or_social_engineering"
        department = "fraud_risk"
        severity = "critical"
    elif any(w in complaint_lower for w in ["wrong number", "wrong transfer", "wrong recipient"]):
        case_type = "wrong_transfer"
        department = "dispute_resolution"
        severity = "high"
    elif any(w in complaint_lower for w in ["settlement", "merchant"]):
        case_type = "merchant_settlement_delay"
        department = "merchant_operations"
        severity = "high"
    elif any(w in complaint_lower for w in ["cash in", "deposit", "agent"]):
        case_type = "agent_cash_in_issue"
        department = "agent_operations"
        severity = "medium"
    elif any(w in complaint_lower for w in ["payment", "paid", "deducted", "failed"]):
        case_type = "payment_failed"
        department = "payments_ops"
        severity = "medium"
    elif "refund" in complaint_lower:
        case_type = "refund_request"
        department = "customer_support"
        severity = "medium"
    else:
        case_type = "other"
        department = "customer_support"
        severity = "low"

    return {
        "case_type": case_type,
        "severity": severity,
        "department": department,
        "agent_summary": (
            f"Ticket {ticket_id} processed via deterministic fallback. "
            f"LLM was temporarily unavailable. Manual review required."
        ),
        "recommended_next_action": (
            "Review matched transaction evidence and contact the customer "
            "through official bKash support channels only."
        ),
        "customer_reply": SAFE_REPLY_FALLBACK,
        "human_review_required": True,
        "confidence": 0.45 if evidence_verdict == "insufficient_data" else 0.55,
        "reason_codes": ["llm_unavailable"],
    }


# ── Main pipeline ─────────────────────────────────────────────────────────────

async def analyze_ticket_pipeline(request: TicketRequest) -> TicketResponse:
    ticket_id = request.ticket_id
    complaint = request.complaint.strip()

    # ── STAGE 1: Input injection scan ────────────────────────────────────────
    is_safe, injection_reason = scan_input_for_injection(complaint)
    if not is_safe:
        logger.warning(f"[{ticket_id}] Injection detected: {injection_reason}")
        complaint = "[SANITIZED: potential injection attempt]"

    # ── STAGE 2: Deterministic evidence pre-match ─────────────────────────────
    det_id, det_confidence = deterministic_match(complaint, request.transaction_history or [])
    logger.info(f"[{ticket_id}] Deterministic match: {det_id} (conf={det_confidence:.2f})")

    # ── STAGE 3: LLM Pass 1 — evidence reasoning ─────────────────────────────
    pass1_prompt = build_pass1_prompt(
        complaint,
        request.transaction_history or [],
        det_id,
        det_confidence,
    )
    llm_unavailable = False

    await _rate_limit_guard()
    try:
        pass1_raw = await llm_call(PASS1_SYSTEM, pass1_prompt)
        pass1_data = extract_json(pass1_raw) or {}
    except Exception as exc:
        llm_unavailable = True
        logger.warning(f"[{ticket_id}] LLM pass 1 failed, using fallback: {exc}")
        pass1_data = _fallback_pass1_data(
            complaint,
            request.transaction_history or [],
            det_id,
            det_confidence,
        )

    llm_txn_id: Optional[str] = pass1_data.get("relevant_transaction_id")
    evidence_verdict: str = pass1_data.get("evidence_verdict", "insufficient_data")
    evidence_confidence: float = float(pass1_data.get("evidence_confidence", 0.5))
    evidence_notes: str = pass1_data.get("evidence_notes", "")

    relevant_transaction_id = llm_txn_id or det_id
    if evidence_verdict not in {"consistent", "inconsistent", "insufficient_data"}:
        evidence_verdict = "insufficient_data"

    logger.info(
        f"[{ticket_id}] Evidence: txn={relevant_transaction_id} "
        f"verdict={evidence_verdict} conf={evidence_confidence:.2f}"
    )

    # ── STAGE 4: LLM Pass 2 — classification + routing ───────────────────────
    complaint_amount = _extract_complaint_amount(complaint)
    pass2_prompt = build_pass2_prompt(
        complaint=complaint,
        language=request.language or "en",
        channel=request.channel or "in_app_chat",
        user_type=request.user_type or "customer",
        campaign_context=request.campaign_context,
        relevant_transaction_id=relevant_transaction_id,
        evidence_verdict=evidence_verdict,
        evidence_notes=evidence_notes,
        amount=complaint_amount,
    )

    await _rate_limit_guard()
    try:
        pass2_raw = await llm_call(PASS2_SYSTEM, pass2_prompt)
        pass2_data = extract_json(pass2_raw) or {}
    except Exception as exc:
        llm_unavailable = True
        logger.warning(f"[{ticket_id}] LLM pass 2 failed, using fallback: {exc}")
        pass2_data = _fallback_pass2_data(
            ticket_id=ticket_id,
            complaint=complaint,
            evidence_verdict=evidence_verdict,
            injection_detected=not is_safe,
        )

    case_type: str = pass2_data.get("case_type", "other")
    severity: str = pass2_data.get("severity", "medium")
    department: str = pass2_data.get("department", "customer_support")
    agent_summary: str = pass2_data.get(
        "agent_summary", f"Customer complaint received for ticket {ticket_id}."
    )
    recommended_next_action: str = pass2_data.get(
        "recommended_next_action",
        "Review ticket manually and contact customer via official channel.",
    )
    customer_reply: str = pass2_data.get("customer_reply", SAFE_REPLY_FALLBACK)
    human_review_required: bool = bool(pass2_data.get("human_review_required", True))
    classification_confidence: float = float(pass2_data.get("confidence", 0.7))
    reason_codes: list = pass2_data.get("reason_codes", [])

    # ── STAGE 5: Confidence arbitration ──────────────────────────────────────
    combined_confidence = (evidence_confidence + classification_confidence) / 2

    if evidence_verdict == "inconsistent":
        human_review_required = True
        reason_codes.append("evidence_inconsistent")
    if evidence_verdict == "insufficient_data" and relevant_transaction_id is None:
        human_review_required = True
        reason_codes.append("no_transaction_match")
    if combined_confidence < 0.55:
        human_review_required = True
        reason_codes.append("low_confidence")
    if complaint_amount and complaint_amount >= 5000:
        human_review_required = True
        reason_codes.append("high_value_transaction")
    if case_type == "phishing_or_social_engineering":
        human_review_required = True
        department = "fraud_risk"
        severity = "critical"
        reason_codes.append("fraud_escalation")
    if not is_safe:
        human_review_required = True
        case_type = "phishing_or_social_engineering"
        department = "fraud_risk"
        severity = "critical"
        reason_codes.append("injection_attempt")
    if llm_unavailable:
        human_review_required = True
        if "llm_unavailable" not in reason_codes:
            reason_codes.append("llm_unavailable")

    # ── STAGE 6: Safety guardrails on customer_reply ──────────────────────────
    customer_reply, reply_violations = sanitize_customer_reply(customer_reply)
    if reply_violations:
        logger.warning(f"[{ticket_id}] Reply violations sanitized: {reply_violations}")
        reason_codes.extend(reply_violations)
        if not customer_reply.strip():
            customer_reply = SAFE_REPLY_FALLBACK

    # ── STAGE 7: Output field scan ────────────────────────────────────────────
    output_violations = validate_output_fields(agent_summary, recommended_next_action, customer_reply)
    if output_violations:
        logger.warning(f"[{ticket_id}] Output violations: {output_violations}")
        reason_codes.extend(output_violations)
        if any("customer_reply" in v for v in output_violations):
            customer_reply = SAFE_REPLY_FALLBACK
        human_review_required = True

    # ── STAGE 8: Enum enforcement + final assembly ────────────────────────────
    final = enforce_enums({
        "evidence_verdict": evidence_verdict,
        "case_type": case_type,
        "severity": severity,
        "department": department,
    })

    return TicketResponse(
        ticket_id=ticket_id,
        relevant_transaction_id=relevant_transaction_id,
        evidence_verdict=final["evidence_verdict"],
        case_type=final["case_type"],
        severity=final["severity"],
        department=final["department"],
        agent_summary=agent_summary[:500],
        recommended_next_action=recommended_next_action[:500],
        customer_reply=customer_reply[:800],
        human_review_required=human_review_required,
        confidence=round(combined_confidence, 3),
        reason_codes=list(set(reason_codes)),
    )