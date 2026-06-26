# QueueStorm Investigator
**bKash SUST CSE Carnival 2026 — Codex Community Hackathon**  
AI/API SupportOps Challenge for Digital Finance

---

## Quick Start (local)

```bash
git clone <your-repo-url>
cd queuestorm

cp .env.example .env
# Edit .env — set GEMINI_API_KEY and MODEL_NAME=gemini-2.0-flash-lite

pip install -r requirements.txt

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Health check:
```bash
curl http://localhost:8000/health
# → {"status":"ok"}
```

Run local smoke tests:
```bash
python test_local.py
```

---

## Deploy on Railway

1. Push this repo to GitHub (make sure `.env` is in `.gitignore`).
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo.
3. In Railway dashboard → **Variables** tab, add:

| Variable | Value |
|---|---|
| `ACTIVE_PROVIDER` | `gemini` |
| `GEMINI_API_KEY` | your key from [aistudio.google.com](https://aistudio.google.com/app/apikey) |
| `MODEL_NAME` | `gemini-2.0-flash-lite` |
| `PORT` | `8000` |

4. Railway auto-detects `railway.toml` and deploys. Live URL: `https://<project>.up.railway.app`

---

## API Reference

### GET /health
```json
{"status": "ok"}
```
Must respond within 60 seconds of service start.

### POST /analyze-ticket
Accepts one ticket per request. Must respond within 30 seconds.

```bash
curl -X POST https://<your-url>/analyze-ticket \
  -H "Content-Type: application/json" \
  -d '{
    "ticket_id": "TKT-001",
    "complaint": "I sent 5000 taka to a wrong number around 2pm today.",
    "language": "en",
    "channel": "in_app_chat",
    "user_type": "customer",
    "transaction_history": [
      {
        "transaction_id": "TXN-9101",
        "timestamp": "2026-04-14T14:08:22Z",
        "type": "transfer",
        "amount": 5000,
        "counterparty": "+8801719876543",
        "status": "completed"
      }
    ]
  }'
```

Sample response:
```json
{
  "ticket_id": "TKT-001",
  "relevant_transaction_id": "TXN-9101",
  "evidence_verdict": "consistent",
  "case_type": "wrong_transfer",
  "severity": "high",
  "department": "dispute_resolution",
  "agent_summary": "Customer reports sending 5000 BDT to wrong recipient via TXN-9101.",
  "recommended_next_action": "Initiate wrong-transfer dispute process per internal SOP.",
  "customer_reply": "We have received your complaint regarding TXN-9101. Our dispute resolution team will investigate and contact you within 24-48 hours through official bKash channels.",
  "human_review_required": true,
  "confidence": 0.88,
  "reason_codes": ["wrong_transfer", "transaction_match", "high_value_transaction"]
}
```

---

## Architecture

The service is a **complaint investigator**, not a classifier. Every request includes both the complaint text and a transaction history snippet. The pipeline compares them and decides what actually happened before classifying or routing anything.

```
POST /analyze-ticket (HTTP request in)
         │
         ▼
┌─────────────────────────────────────┐
│  FastAPI + Pydantic validation      │  ← 400 on bad schema, 422 on empty complaint
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐  ╔══════════════╗
│  Input injection scan          [🛡] │  ║ SAFETY GATE  ║
│  Detect + sanitize prompt injection │  ║   LAYER 1    ║
└─────────────────────────────────────┘  ╚══════════════╝
         │
         ▼
┌─────────────────────────────────────┐
│  Deterministic evidence match       │  ← No LLM cost
│  Amount + counterparty + timestamp  │  ← Scores every transaction
│  scoring → candidate transaction ID │  ← Seeds LLM to reduce hallucination
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│  Rate limit guard                   │  ← Proactive spacing, prevents 429
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│  LLM Pass 1 — Evidence reasoning    │  ← Gemini Flash Lite
│  · Which transaction matches?       │
│  · Is complaint consistent with it? │
│  → relevant_transaction_id          │
│  → evidence_verdict                 │
└─────────────────────────────────────┘
         │  (if LLM fails → deterministic fallback, always returns valid response)
         ▼
┌─────────────────────────────────────┐
│  Rate limit guard                   │  ← Second spacing before Pass 2
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│  LLM Pass 2 — Classify + route      │  ← Gemini Flash Lite
│  · case_type                        │
│  · department                       │
│  · severity                         │
│  · agent_summary                    │
│  · recommended_next_action          │
│  · customer_reply                   │
└─────────────────────────────────────┘
         │  (if LLM fails → keyword-based fallback)
         ▼
┌─────────────────────────────────────┐
│  Confidence arbitration             │  ← Python rules, not LLM judgment
│  Force human_review_required = true │
│  when ANY of:                       │
│  · evidence_verdict = inconsistent  │
│  · No transaction match found       │
│  · Combined confidence < 0.55       │
│  · Amount ≥ 5000 BDT               │
│  · Fraud / phishing case type       │
│  · Injection detected in input      │
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐  ╔══════════════╗
│  Safety guardrails             [🛡] │  ║ SAFETY GATE  ║
│  Strip PIN/OTP/refund-confirm       │  ║   LAYER 2    ║
│  patterns from customer_reply       │  ╚══════════════╝
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐  ╔══════════════╗
│  Output field scan             [🛡] │  ║ SAFETY GATE  ║
│  Scan all output fields for leaks   │  ║   LAYER 3    ║
│  Replace violations with fallback   │  ╚══════════════╝
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│  Enum enforcer                      │  ← Hard-enforce exact enum values
│  case_type · department · severity  │  ← Fallback to safe defaults on mismatch
│  evidence_verdict                   │
└─────────────────────────────────────┘
         │
         ▼
HTTP 200 JSON response  (<30s · valid schema · safe reply)
```

---

## AI Approach

**Two focused LLM passes** instead of one monolithic prompt:

**Pass 1 — Evidence reasoning** receives only the complaint and transaction history. It returns `relevant_transaction_id` and `evidence_verdict`. A deterministic pre-match runs first — this seeds the LLM with a candidate transaction ID, dramatically reducing hallucination on the most critical output field.

**Pass 2 — Classification and routing** receives the complaint, metadata, and the verdict from Pass 1. It returns `case_type`, `department`, `severity`, `agent_summary`, `recommended_next_action`, and `customer_reply`.

Separating these two concerns means each LLM call gets a tightly focused prompt, producing more accurate and consistent outputs than a single large prompt trying to do everything at once.

---

## Safety Logic

Three independent layers protect every response. A violation caught by any layer does not depend on the others.

**Layer 1 — Input scan**: Regex patterns detect prompt injection in the complaint before it reaches the LLM. Injected complaints are sanitized and routed as `phishing_or_social_engineering` with `human_review_required=true`.

**Layer 2 — Pre-generation constraints**: The Pass 2 system prompt hard-codes all safety rules as LLM instructions: never request credentials, never confirm refunds, direct customers to official channels only.

**Layer 3 — Output scan**: After generation, all output fields are scanned for credential requests, unauthorized refund confirmations, and third-party redirects. Violations replace `customer_reply` with the safe fallback and set `human_review_required=true`.

`human_review_required` is additionally forced `true` by Python rules — independent of the LLM — when:
- `evidence_verdict = inconsistent`
- No transaction match found in history
- Combined confidence < 0.55
- Amount ≥ 5000 BDT
- Case type is phishing or social engineering
- Prompt injection detected in input

---

## Rate Limit Handling

Gemini free tier allows 30 requests/minute for `gemini-2.0-flash-lite`. The service handles this at three levels:

1. **Proactive rate limiter** in `pipeline.py` tracks calls in a rolling 60-second window and waits before sending if approaching 25 RPM — preventing 429s before they happen.
2. **Retry logic** in `llm_client.py` retries up to 3 times on 429/5xx with short waits (2s, 4s, 6s) — total max 12s, safely fits inside the 30-second judge deadline.
3. **Deterministic fallback** activates if LLM is still unavailable — returns a valid structured response using keyword matching and the pre-matched transaction rather than failing with 500.

---

## MODELS

| Provider | Model | Where it runs | Why chosen |
|---|---|---|---|
| Gemini | `gemini-2.0-flash-lite` | Google AI API (remote) | 30 RPM free tier (double standard Flash), fast JSON output, strong Bengali/Banglish support, lowest latency for two-pass pipeline |

Model is set via `MODEL_NAME` environment variable. Default: `gemini-2.0-flash-lite`.

**Cost per ticket**: ~2 LLM calls × ~450 tokens average = ~900 tokens.  
At Gemini Flash Lite pricing (~$0.0375/1M input tokens): **~$0.000034 per ticket**.  
For 40,000 tickets: **~$1.36 total**.

---

## Known Limitations

- Bengali/Banglish numeric amount extraction uses regex; non-numeric amounts (e.g. "পাঁচ হাজার টাকা") rely on LLM.
- Deterministic timestamp matching scores against current time — historical test cases score lower on time proximity signal.
- No persistent state: each ticket is independent, no session memory.
- In-memory rate limiter resets on restart and does not coordinate across multiple instances.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ACTIVE_PROVIDER` | Yes | `gemini` |
| `GEMINI_API_KEY` | Yes | Google AI Studio API key |
| `MODEL_NAME` | Recommended | `gemini-2.0-flash-lite` for higher free tier quota |
| `PORT` | No | Server port (default 8000, set automatically by Railway) |
| `DEBUG` | No | Enable debug logging (`true` / `false`) |

---

## Submission Checklist

- [x] `GET /health` returns `{"status":"ok"}` within 60s of start
- [x] `POST /analyze-ticket` responds within 30s
- [x] All required response fields present with correct enum values
- [x] `customer_reply` never requests PIN, OTP, or password
- [x] `customer_reply` never confirms refund or reversal
- [x] Prompt injection in complaint does not override output fields
- [x] `human_review_required=true` on disputes, fraud, inconsistent evidence, high-value amounts
- [x] Returns 400 on malformed JSON, 422 on empty complaint
- [x] No secrets or stack traces in any response or log output
- [x] `.env` excluded from version control via `.gitignore`
- [x] `README.md`, `requirements.txt`, `.env.example`, `sample_output.json` all present
- [x] `MODELS` section present with model name, location, and justification