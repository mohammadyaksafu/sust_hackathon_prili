"""
Safety guardrails module.
Applied TWICE:
  1. Before LLM generation  - to detect prompt injection in complaint text
  2. After LLM generation   - to scan all output fields for policy violations
"""

import re
from typing import Tuple

# Patterns that must NEVER appear in customer_reply or recommended_next_action
CREDENTIAL_PATTERNS = [
    r"\bpin\b",
    r"\botp\b",
    r"\bpassword\b",
    r"\bcard\s*number\b",
    r"\bfull\s*card\b",
    r"\bsecret\b",
    r"\bshare\s+your\b",
    r"\bprovide\s+your\b",
    r"\bverif(?:y|ication)\s+(?:your\s+)?(?:pin|otp|password)\b",
]

UNAUTHORIZED_REFUND_PATTERNS = [
    r"\bwill\s+refund\b",
    r"\brefund\s+(?:has\s+been|will\s+be|is\s+being)\s+process",
    r"\byour\s+(?:money|amount|balance)\s+(?:has\s+been|will\s+be)\s+(?:returned|credited|reversed)",
    r"\bwe\s+(?:guarantee|confirm|promise)\s+(?:a\s+)?refund\b",
    r"\brefund\s+confirmed\b",
    r"\baccount\s+(?:will\s+be\s+)?unblock",
    r"\bwe\s+have\s+(?:initiated|processed|completed)\s+(?:the\s+)?refund\b",
]

THIRD_PARTY_PATTERNS = [
    r"\bcontact\s+(?!our\s+official|bkash\s+official)[a-z]+\s+(?:at|on)\s+[\d+]",
    r"\bcall\s+this\s+number\b",
    r"\bwhatsapp\s+(?:us|me|them)\b",
]

INJECTION_PATTERNS = [
    r"ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions",
    r"forget\s+(?:all\s+)?(?:previous|above|prior)\s+instructions",
    r"you\s+are\s+now\s+(?:a\s+)?(?:different|new|unrestricted)",
    r"disregard\s+(?:your\s+)?(?:instructions|rules|guidelines)",
    r"act\s+as\s+(?:if\s+you\s+(?:are|were)|an?\s+)",
    r"jailbreak",
    r"dan\s+mode",
    r"developer\s+mode",
    r"system\s*:\s*you",
    r"<\s*system\s*>",
    r"\[INST\]",
    r"\{\{.*\}\}",
]


def _check_patterns(text: str, patterns: list[str]) -> list[str]:
    text_lower = text.lower()
    hits = []
    for p in patterns:
        if re.search(p, text_lower, re.IGNORECASE):
            hits.append(p)
    return hits


def scan_input_for_injection(complaint: str) -> Tuple[bool, str]:
    """Returns (is_safe, reason)."""
    hits = _check_patterns(complaint, INJECTION_PATTERNS)
    if hits:
        return False, f"Potential prompt injection detected in complaint"
    return True, ""


def sanitize_customer_reply(reply: str) -> Tuple[str, list[str]]:
    """
    Removes or replaces policy-violating phrases.
    Returns (sanitized_reply, list_of_violations_found).
    """
    violations = []
    sanitized = reply

    for p in CREDENTIAL_PATTERNS:
        if re.search(p, sanitized, re.IGNORECASE):
            violations.append("credential_request")
            sanitized = re.sub(
                r"[^.!?]*\b(?:pin|otp|password|card\s*number)\b[^.!?]*[.!?]?",
                "",
                sanitized,
                flags=re.IGNORECASE,
            ).strip()

    for p in UNAUTHORIZED_REFUND_PATTERNS:
        if re.search(p, sanitized, re.IGNORECASE):
            violations.append("unauthorized_refund_promise")
            sanitized = re.sub(
                r"we\s+(?:will|have|shall)\s+(?:refund|process|return|credit)[^.!?]*[.!?]?",
                "Any eligible amount will be returned through official channels if verified. ",
                sanitized,
                flags=re.IGNORECASE,
            ).strip()

    return sanitized, violations


def validate_output_fields(agent_summary: str, recommended_next_action: str, customer_reply: str) -> list[str]:
    """Scan all output text fields for safety violations. Returns list of violation codes."""
    violations = []

    for field_name, text in [
        ("agent_summary", agent_summary),
        ("recommended_next_action", recommended_next_action),
        ("customer_reply", customer_reply),
    ]:
        if _check_patterns(text, CREDENTIAL_PATTERNS):
            violations.append(f"{field_name}:credential_leak")
        if _check_patterns(text, UNAUTHORIZED_REFUND_PATTERNS):
            violations.append(f"{field_name}:unauthorized_refund")
        if _check_patterns(text, THIRD_PARTY_PATTERNS):
            violations.append(f"{field_name}:suspicious_third_party")

    return violations


SAFE_REPLY_FALLBACK = (
    "Thank you for reaching out to bKash Support. "
    "We have received your complaint and it has been logged for review by our team. "
    "Our support team will investigate and respond within 24-48 hours. "
    "For urgent matters, please contact us through official bKash channels only. "
    "Please keep your account credentials confidential."
)
