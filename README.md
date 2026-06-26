# QueueStorm Investigator
**bKash SUST CSE Carnival 2026 — Codex Community Hackathon**
AI/API SupportOps Challenge for Digital Finance

---

## Tech Stack
- Python 3.11
- FastAPI + Uvicorn
- httpx (async LLM API calls)
- Pydantic v2 (validation + settings)
- Multi-provider LLM: Gemini (default) / Anthropic Claude / OpenAI

---

## Quick Start (local)

```bash
git clone <your-repo-url>
cd queuestorm

cp .env.example .env
# Edit .env — add your GEMINI_API_KEY (or ANTHROPIC/OPENAI) and set ACTIVE_PROVIDER

pip install -r requirements.txt

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Test:
```bash
python test_local.py
```

Health check:
```bash
curl http://localhost:8000/health
# → {"status":"ok"}
```

---

## Deploy on Railway

1. Push this repo to GitHub.
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub.
3. Select your repo.
4. In Railway dashboard → Variables, add:
   - `ACTIVE_PROVIDER` = `gemini`
   - `GEMINI_API_KEY` = your key
   - (optional) `MODEL_NAME`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`
5. Railway auto-detects `railway.toml` and deploys.
6. Your live URL will be `https://<project>.up.railway.app`.

---

## API Reference

### GET /health
```json
{"status": "ok"}
```

### POST /analyze-ticket
Request body per the QueueStorm problem statement schema.

Example:
```bash
curl -X POST https://<your-url>/analyze-ticket \
  -H "Content-Type: application/json" \
  -d @sample_output.json
```

---

## Architecture

```
FastAPI + Pydantic validation (400 on bad schema)
  │
Input injection scan  ← catches prompt injection in complaint text
  │
Deterministic evidence pre-match  ← fast, no LLM, amount+time+counterparty scoring
  │
LLM Pass 1: Evidence reasoning  ← focused prompt: which txn? consistent or not?
  │
LLM Pass 2: Classification + routing  ← case_type, department, severity, summaries
  │
Confidence arbitration  ← cross-checks verdict vs classification, forces human_review
  │
Safety guardrails (pre-generation)  ← blocks PIN/OTP/refund-confirm patterns
  │
Output injection scan  ← second pass over all output fields
  │
Enum enforcer  ← hard-enforces exact enum values, fallbacks on mismatch
  │
POST /analyze-ticket → 200 JSON
```

---

## AI Approach

Two focused LLM passes rather than one monolithic prompt:

**Pass 1 — Evidence reasoning** receives only the complaint and transaction history. It identifies which transaction the complaint refers to and returns `relevant_transaction_id` + `evidence_verdict`. A deterministic pre-match (amount, time, counterparty scoring) seeds the LLM with a candidate, reducing hallucination.

**Pass 2 — Classification + routing** receives the complaint, metadata, and the evidence verdict from pass 1. It outputs `case_type`, `department`, `severity`, `agent_summary`, `recommended_next_action`, `customer_reply`, and `human_review_required`.

---

## Safety Logic

Three layers:

1. **Input scan**: Regex patterns detect prompt injection in the complaint before it reaches the LLM. Injected complaints are flagged, complaint text is sanitized, and the ticket is routed as `phishing_or_social_engineering` with `human_review_required=true`.

2. **Pre-generation constraints**: The Pass 2 system prompt hard-codes all safety rules as LLM instructions — never request credentials, never confirm refunds, official channels only.

3. **Output scan**: After generation, all output fields are scanned for credential requests, unauthorized refund promises, and third-party redirects. Violations trigger field replacement with the safe fallback reply and set `human_review_required=true`.

`human_review_required` is forced `true` by Python rules (not LLM judgment) when:
- `evidence_verdict = inconsistent`
- No transaction match found
- Combined confidence < 0.55
- Amount ≥ 5000 BDT
- Fraud/phishing case type
- Injection detected

---

## Models Used

| Provider | Model | Why |
|---|---|---|
| Gemini (default) | gemini-2.0-flash | Fast, cheap, strong JSON output, good multilingual (Bengali support) |
| Anthropic | claude-haiku-4-5-20251001 | Fast and reliable, strong safety alignment |
| OpenAI | gpt-4o-mini | Fallback option, reliable JSON mode |

Switch provider via `ACTIVE_PROVIDER` env var. No code changes needed.

---

## Cost Reasoning

Each ticket uses 2 LLM calls. At Gemini Flash pricing (~$0.075/1M input tokens):
- Pass 1: ~400 tokens in + ~150 out ≈ $0.000040
- Pass 2: ~500 tokens in + ~300 out ≈ $0.000059
- Total per ticket: ~$0.0001

For 40,000 tickets: ~$4 total.

---

## Known Limitations

- Bengali/Banglish complaint parsing relies on LLM multilingual capability; deterministic amount extraction only handles numeric BDT patterns.
- Deterministic matching uses timestamps relative to current time, which may score historical cases differently.
- No persistent state — each ticket is processed independently.
- Rate limits of the chosen LLM provider apply; under very high load, queue externally.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ACTIVE_PROVIDER` | Yes | `gemini` \| `anthropic` \| `openai` |
| `GEMINI_API_KEY` | If Gemini | Google AI Studio API key |
| `ANTHROPIC_API_KEY` | If Anthropic | Anthropic API key |
| `OPENAI_API_KEY` | If OpenAI | OpenAI API key |
| `MODEL_NAME` | No | Override model name |
| `PORT` | No | Server port (default 8000) |
| `DEBUG` | No | Enable debug logging |
