# PromptWars 🏆

> A production-ready prompt quality scoring API powered by Google Cloud — built for the Antigravity Challenge.

---

## Chosen Vertical

**Smart AI Assistant / AI Competition Platform**

PromptWars is a competition platform that evaluates the quality of participant-submitted prompts using Vertex AI Gemini 1.5 Flash. It scores prompts across five expert-defined dimensions, caches results in Memorystore Redis for sub-millisecond repeat lookups, and exposes a clean REST API deployable to Cloud Run in a single command.

---

## Approach and Logic

### Cache Key Strategy — SHA-256 Hashing

Every request (`prompt` + `task` + `rubric`) is deterministically hashed using **SHA-256**, truncated to 24 hex characters:

```
cache_key = "score:" + SHA-256(f"{prompt}|{task}|{sorted(rubric)}")[:24]
```

- The rubric is **sorted before hashing**, so `["Clarity", "Specificity"]` and `["Specificity", "Clarity"]` produce the same key — rubric order is irrelevant.
- The rubric is **included in the cache key** so that changing the rubric automatically invalidates all old cached results for the same prompt — no manual cache flushing needed.
- Truncating to 24 chars keeps Redis keys compact while maintaining collision resistance across realistic competition loads.

### Gemini Scoring — 5-Dimension Rubric

Each prompt is evaluated by Gemini 1.5 Flash across five dimensions at `temperature=0.1` for high consistency:

| # | Dimension | What is measured |
|---|---|---|
| 1 | **Clarity** | Is the intent unambiguous and easy to understand? |
| 2 | **Specificity** | Does the prompt narrow down scope and context sufficiently? |
| 3 | **Task Alignment** | Does the prompt match the declared task goal? |
| 4 | **Output Format** | Does the prompt specify or imply the expected response format? |
| 5 | **Conciseness** | Is the prompt free of unnecessary verbosity? |

Each dimension is scored **0–10**. The overall score is calculated as:

```
overall_score = round(mean(dimension_scores) * 10, 1)
```

This scales the 0–10 mean to a **0–100 leaderboard-friendly score**.

### Graceful Redis Fallback

If Memorystore Redis is unavailable (connection timeout, network error, or cold start), the API:
1. Logs a warning with structured JSON
2. Proceeds to call Gemini directly
3. Returns a valid response to the client without error

Redis errors on `SET` (write) are similarly swallowed — the API never fails because of cache unavailability.

---

## How the Solution Works

### Architecture

```
Participant
    │
    │  POST /score
    ▼
┌──────────────────────────────┐
│         Cloud Run            │
│  (PromptWars FastAPI app)    │
└──────────┬───────────────────┘
           │
     Cache lookup
           │
           ▼
┌─────────────────────┐      HIT ──► Return cached JSON
│  Memorystore Redis  │
└─────────┬───────────┘
          │ MISS
          ▼
┌──────────────────────────────┐
│  Vertex AI Gemini 1.5 Flash  │
│  (5-dimension rubric scoring)│
└──────────┬───────────────────┘
           │
     Write result + TTL
           │
           ▼
┌─────────────────────┐
│  Memorystore Redis  │  ◄── Cache for future hits
└─────────────────────┘
           │
     Persist score
           │
           ▼
┌─────────────────────┐
│      Firestore      │  ◄── Permanent score record per participant
└─────────────────────┘
           │
     Publish event
           │
           ▼
┌─────────────────────┐
│  Cloud Pub/Sub      │  ◄── Downstream: leaderboard, notifications
└─────────────────────┘
```

### GCP Services

| GCP Service | Role | Justification |
|---|---|---|
| **Cloud Run** | Hosts the FastAPI scoring API | Serverless, scales to zero, no VMs to manage |
| **Vertex AI Gemini 1.5 Flash** | Scores prompts across 5 dimensions | Fastest Gemini variant; `temperature=0.1` for consistent scoring |
| **Memorystore Redis** | Caches scoring results by SHA-256 hash | Sub-millisecond cache hits for repeat prompt submissions |
| **Firestore** | Permanent storage of all scores and participant history | Serverless NoSQL; ideal for per-participant leaderboard queries |
| **Cloud Pub/Sub** | Publishes score events for downstream consumers | Decouples scoring API from leaderboard, notifications, and analytics |
| **Cloud Build** | Builds and pushes Docker image to Container Registry | Fully managed CI; triggered by `gcloud builds submit` in `deploy.sh` |

---

## Sample Request and Response

### Request

```bash
curl -X POST https://<SERVICE_URL>/score \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Summarise the following article in three bullet points, each under 20 words, targeting a non-technical audience.",
    "task": "text summarisation",
    "rubric": ["Clarity", "Specificity", "Task alignment", "Output format", "Conciseness"]
  }'
```

### Response

```json
{
  "overall_score": 88.0,
  "dimensions": [
    {
      "dimension": "Clarity",
      "score": 9,
      "reason": "The instruction is unambiguous and easy to parse."
    },
    {
      "dimension": "Specificity",
      "score": 9,
      "reason": "Three bullet points and a 20-word cap give precise constraints."
    },
    {
      "dimension": "Task alignment",
      "score": 9,
      "reason": "Directly targets summarisation with clear scope."
    },
    {
      "dimension": "Output format",
      "score": 8,
      "reason": "Bullet format is specified but no example provided."
    },
    {
      "dimension": "Conciseness",
      "score": 9,
      "reason": "No unnecessary words; every clause adds constraint."
    }
  ],
  "strengths": [
    "Precise output constraints (3 bullets, 20 words)",
    "Audience-aware framing",
    "Format explicitly stated"
  ],
  "improvements": [
    "Provide a sample bullet to anchor style",
    "Specify language or tone (formal vs casual)"
  ],
  "cache_hit": false,
  "prompt_hash": "a3f8c21b0e947d6f2c81a04b"
}
```

---

## Running Locally

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set environment variables

```bash
export GCP_PROJECT=your-gcp-project-id
export GCP_REGION=us-central1
export REDIS_HOST=127.0.0.1
export REDIS_PORT=6379
export CACHE_TTL_SEC=3600
export GEMINI_MODEL=gemini-1.5-flash-001
```

> **Note:** Authenticate with GCP before starting the server:
> ```bash
> gcloud auth application-default login
> ```

### 3. Start the server

```bash
uvicorn main:app --reload --port 8080
```

### 4. Test the health endpoint

```bash
curl http://localhost:8080/health
# {"status":"ok","model":"gemini-1.5-flash-001"}
```

### 5. Score a prompt

```bash
curl -X POST http://localhost:8080/score \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Explain quantum entanglement to a 10-year-old.", "task": "explanation"}'
```

---

## Deploying to GCP

```bash
chmod +x deploy.sh
./deploy.sh YOUR_PROJECT_ID REDIS_IP
```

**Example:**

```bash
./deploy.sh my-gcp-project 10.128.0.5
```

The script will:
1. Build and push the Docker image using Cloud Build
2. Deploy to Cloud Run in `us-central1` with a VPC connector and service account
3. Print the live service URL on completion

---

## Running Tests

```bash
pip install pytest
pytest tests/ -v
```

No GCP credentials required — all external services are mocked. Expected output:

```
tests/test_scoring.py::TestHashFunction::test_same_inputs_produce_same_hash PASSED
tests/test_scoring.py::TestHashFunction::test_hash_length_is_exactly_24_chars PASSED
...
47 passed in 0.42s
```

---

## Assumptions Made

- **Authentication:** Cloud Run is deployed with `--no-allow-unauthenticated`. Callers must provide a valid GCP identity token; no API key scheme is implemented.
- **Redis is co-located:** Memorystore Redis is reachable via a VPC connector from Cloud Run. Direct public Redis exposure is not assumed.
- **Gemini returns valid JSON:** The model is instructed with `response_mime_type="application/json"`. If it violates this (502), the API surfaces the error rather than guessing.
- **Rubric is optional:** If no rubric is provided, the five default dimensions are used. This ensures backward compatibility if the rubric schema changes.
- **Firestore and Pub/Sub are future-wired:** The architecture diagram includes them as the intended next integration layer; they are not wired in `main.py` in this submission.
- **Temperature = 0.1 is sufficient for consistency:** Slightly above zero to avoid deterministic repetition while keeping scoring stable across identical prompts.
- **Single region deployment:** `us-central1` is hardcoded in `deploy.sh`. Multi-region failover is a future scope item.

---

## Future Scope

| Feature | GCP Service |
|---|---|
| **Persistent leaderboard** — store every score, rank participants | Firestore + Cloud Run |
| **Real-time score events** — trigger leaderboard refresh on each score | Cloud Pub/Sub + Eventarc |
| **Multi-model scoring** — compare Gemini Flash vs Pro vs Claude | Vertex AI Model Garden |
| **Rate limiting** — prevent prompt-flooding abuse | Cloud Armor + API Gateway |
| **CI/CD pipeline** — auto-deploy on push to `main` | Cloud Build Triggers + Artifact Registry |

---

## Project Structure

```
promptwars/
├── main.py              # FastAPI application (scoring API, cache logic, Gemini integration)
├── requirements.txt     # Pinned Python dependencies
├── Dockerfile           # Production Docker image (python:3.12-slim)
├── deploy.sh            # One-command GCP Cloud Run deployment script
├── .gitignore           # Python + GCP + Terraform ignores
├── README.md            # This file
└── tests/
    ├── __init__.py
    └── test_scoring.py  # 47 unit tests (no GCP credentials required)
```
