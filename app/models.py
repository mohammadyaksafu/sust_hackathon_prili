from __future__ import annotations
from typing import Any, List, Optional
from pydantic import BaseModel, Field


class TransactionEntry(BaseModel):
    transaction_id: str
    timestamp: str
    type: str
    amount: float
    counterparty: str
    status: str


class TicketRequest(BaseModel):
    ticket_id: str
    complaint: str
    language: Optional[str] = "en"
    channel: Optional[str] = "in_app_chat"
    user_type: Optional[str] = "customer"
    campaign_context: Optional[str] = None
    transaction_history: Optional[List[TransactionEntry]] = Field(default_factory=list)
    metadata: Optional[dict[str, Any]] = None


class TicketResponse(BaseModel):
    ticket_id: str
    relevant_transaction_id: Optional[str] = None
    evidence_verdict: str                        # consistent | inconsistent | insufficient_data
    case_type: str
    severity: str                                # low | medium | high | critical
    department: str
    agent_summary: str
    recommended_next_action: str
    customer_reply: str
    human_review_required: bool
    confidence: Optional[float] = None
    reason_codes: Optional[List[str]] = Field(default_factory=list)
