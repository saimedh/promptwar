"""
PromptWars — Prompt Quality Scoring API
FastAPI + Vertex AI Gemini 1.5 Flash + Memorystore Redis + Firestore + Pub/Sub

Local dev mode: set GEMINI_API_KEY to use google-generativeai (no GCP creds needed).
Production mode: set GCP_PROJECT to use Vertex AI.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from statistics import mean
from typing import Optional

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level":   record.levelname,
            "logger":  record.name,
            "message": record.getMessage(),
            "time":    self.formatTime(record, self.datefmt),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _configure_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    return logging.getLogger("promptwars")


log = _configure_logging()

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

GCP_PROJECT:    str = os.environ.get("GCP_PROJECT",    "my-gcp-project")
GCP_REGION:     str = os.environ.get("GCP_REGION",     "us-central1")
REDIS_HOST:     str = os.environ.get("REDIS_HOST",     "127.0.0.1")
REDIS_PORT:     int = int(os.environ.get("REDIS_PORT", "6379"))
CACHE_TTL_SEC:  int = int(os.environ.get("CACHE_TTL_SEC", "3600"))
GEMINI_MODEL:   str = os.environ.get("GEMINI_MODEL",   "gemini-1.5-flash-001")
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")   # local dev
PUBSUB_TOPIC:   str = os.environ.get("PUBSUB_TOPIC",   "")   # optional
FS_COLLECTION:  str = os.environ.get("FS_COLLECTION",  "scores")

USE_VERTEX: bool = not bool(GEMINI_API_KEY)   # True in production

# ---------------------------------------------------------------------------
# Default rubric dimensions
# ---------------------------------------------------------------------------

DEFAULT_DIMENSIONS = [
    "Clarity",
    "Specificity",
    "Task alignment",
    "Output format",
    "Conciseness",
]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an expert prompt engineer evaluating prompt quality.\n"
    "Score the prompt on each rubric dimension from 0 to 10.\n"
    "Return ONLY valid JSON in exactly this structure:\n"
    "{\n"
    '  "dimensions": [{"dimension": "string", "score": 0, "reason": "one sentence"}],\n'
    '  "strengths": ["short phrases"],\n'
    '  "improvements": ["short actionable phrases"]\n'
    "}"
)

# ---------------------------------------------------------------------------
# Pydantic v2 models
# ---------------------------------------------------------------------------


class ScoreRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="The prompt to evaluate.")
    task:   str = Field(..., min_length=1, description="The target task for the prompt.")
    rubric: Optional[list[str]] = Field(
        default=None,
        description="Custom rubric dimensions. Defaults to the 5 standard ones.",
    )


class DimensionScore(BaseModel):
    dimension: str
    score:     int = Field(..., ge=0, le=10)
    reason:    str


class ScoreResponse(BaseModel):
    overall_score: float
    dimensions:    list[DimensionScore]
    strengths:     list[str]
    improvements:  list[str]
    cache_hit:     bool
    prompt_hash:   str


class LeaderboardEntry(BaseModel):
    prompt_hash:   str
    task:          str
    overall_score: float
    timestamp:     str
    dimensions:    list[DimensionScore]


class HealthResponse(BaseModel):
    status:    str
    model:     str
    mode:      str
    redis:     str
    firestore: str


class FeedbackRequest(BaseModel):
    prompt_hash: str = Field(..., min_length=24, max_length=24)
    helpful:     bool


# ---------------------------------------------------------------------------
# App state singleton
# ---------------------------------------------------------------------------

class _AppState:
    redis: Optional[aioredis.Redis] = None
    fs_client = None          # google.cloud.firestore.Client (sync, threaded)
    pubsub_client = None      # google.cloud.pubsub_v1.PublisherClient
    redis_ok:     bool = False
    firestore_ok: bool = False
    pubsub_ok:    bool = False


app_state = _AppState()

# ---------------------------------------------------------------------------
# Lifespan — startup / shutdown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- Gemini client init ----
    if USE_VERTEX:
        log.info("Initialising Vertex AI", extra={"project": GCP_PROJECT, "region": GCP_REGION})
        try:
            import vertexai
            vertexai.init(project=GCP_PROJECT, location=GCP_REGION)
            log.info("Vertex AI ready")
        except Exception as exc:
            log.warning("Vertex AI init failed", extra={"error": str(exc)})
    else:
        log.info("Using google-generativeai (API key mode)")
        try:
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            log.info("google-generativeai configured")
        except Exception as exc:
            log.warning("google-generativeai init failed", extra={"error": str(exc)})

    # ---- Redis ----
    log.info("Connecting to Redis", extra={"host": REDIS_HOST, "port": REDIS_PORT})
    try:
        app_state.redis = aioredis.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        await app_state.redis.ping()
        app_state.redis_ok = True
        log.info("Redis connection established")
    except Exception as exc:
        log.warning("Redis unavailable — cache disabled", extra={"error": str(exc)})
        app_state.redis = None

    # ---- Firestore ----
    try:
        from google.cloud import firestore as _fs
        app_state.fs_client = _fs.Client(project=GCP_PROJECT)
        app_state.firestore_ok = True
        log.info("Firestore client ready")
    except Exception as exc:
        log.warning("Firestore unavailable — persistence disabled", extra={"error": str(exc)})

    # ---- Pub/Sub ----
    if PUBSUB_TOPIC:
        try:
            from google.cloud import pubsub_v1
            app_state.pubsub_client = pubsub_v1.PublisherClient()
            app_state.pubsub_ok = True
            log.info("Pub/Sub publisher ready")
        except Exception as exc:
            log.warning("Pub/Sub unavailable", extra={"error": str(exc)})

    yield

    if app_state.redis:
        await app_state.redis.aclose()
        log.info("Redis connection closed")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="PromptWars",
    description="Prompt quality scoring API powered by Vertex AI Gemini.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _build_cache_key(prompt: str, task: str, rubric: Optional[list[str]]) -> tuple[str, str]:
    rubric_str = json.dumps(sorted(rubric or DEFAULT_DIMENSIONS))
    raw        = f"{prompt}|{task}|{rubric_str}"
    digest     = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"score:{digest}", digest


async def _get_cached(key: str) -> Optional[dict]:
    if not app_state.redis_ok or app_state.redis is None:
        return None
    try:
        raw = await app_state.redis.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:
        log.warning("Redis GET failed", extra={"key": key, "error": str(exc)})
        return None


async def _set_cached(key: str, value: dict) -> None:
    if not app_state.redis_ok or app_state.redis is None:
        return
    try:
        await app_state.redis.set(key, json.dumps(value), ex=CACHE_TTL_SEC)
    except Exception as exc:
        log.warning("Redis SET failed", extra={"key": key, "error": str(exc)})

# ---------------------------------------------------------------------------
# Firestore helpers (sync wrapped in thread pool)
# ---------------------------------------------------------------------------


def _fs_save_sync(doc_id: str, payload: dict) -> None:
    coll = app_state.fs_client.collection(FS_COLLECTION)
    coll.document(doc_id).set(payload, merge=True)


def _fs_get_sync(doc_id: str) -> Optional[dict]:
    coll = app_state.fs_client.collection(FS_COLLECTION)
    doc  = coll.document(doc_id).get()
    return doc.to_dict() if doc.exists else None


def _fs_leaderboard_sync(limit: int) -> list[dict]:
    coll = app_state.fs_client.collection(FS_COLLECTION)
    docs = (
        coll
        .order_by("overall_score", direction="DESCENDING")
        .limit(limit)
        .stream()
    )
    return [d.to_dict() for d in docs]


async def _save_to_firestore(doc_id: str, payload: dict) -> None:
    if not app_state.firestore_ok:
        return
    try:
        await asyncio.to_thread(_fs_save_sync, doc_id, payload)
        log.info("Firestore: score saved", extra={"doc_id": doc_id})
    except Exception as exc:
        log.warning("Firestore save failed", extra={"error": str(exc)})


async def _get_from_firestore(doc_id: str) -> Optional[dict]:
    if not app_state.firestore_ok:
        return None
    try:
        return await asyncio.to_thread(_fs_get_sync, doc_id)
    except Exception as exc:
        log.warning("Firestore get failed", extra={"error": str(exc)})
        return None


async def _leaderboard_from_firestore(limit: int) -> list[dict]:
    if not app_state.firestore_ok:
        return []
    try:
        return await asyncio.to_thread(_fs_leaderboard_sync, limit)
    except Exception as exc:
        log.warning("Firestore leaderboard query failed", extra={"error": str(exc)})
        return []

# ---------------------------------------------------------------------------
# Pub/Sub helper
# ---------------------------------------------------------------------------


async def _publish_event(payload: dict) -> None:
    if not app_state.pubsub_ok or not PUBSUB_TOPIC:
        return
    try:
        data = json.dumps(payload).encode()
        await asyncio.to_thread(
            lambda: app_state.pubsub_client.publish(PUBSUB_TOPIC, data).result()
        )
        log.info("Pub/Sub: score event published")
    except Exception as exc:
        log.warning("Pub/Sub publish failed", extra={"error": str(exc)})

# ---------------------------------------------------------------------------
# Gemini call — supports both API key and Vertex AI modes
# ---------------------------------------------------------------------------


def _build_user_message(prompt: str, task: str, dimensions: list[str]) -> str:
    dims = "\n".join(f"- {d}" for d in dimensions)
    return (
        f"Task: {task}\n\n"
        f"Prompt to evaluate:\n{prompt}\n\n"
        f"Rubric dimensions to score:\n{dims}"
    )


async def _call_gemini_api_key(prompt: str, task: str, dimensions: list[str]) -> dict:
    """Use google-generativeai (API key mode) — no GCP credentials needed."""
    import google.generativeai as genai
    from google.generativeai.types import GenerationConfig as GConfig

    user_message = _build_user_message(prompt, task, dimensions)

    def _sync_call() -> str:
        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=SYSTEM_PROMPT,
        )
        resp = model.generate_content(
            user_message,
            generation_config=GConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )
        return resp.text

    try:
        raw_text = await asyncio.to_thread(_sync_call)
    except Exception as exc:
        log.error("Gemini API key call failed", extra={"error": str(exc)})
        raise HTTPException(status_code=503, detail=f"Gemini unreachable: {exc}") from exc

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        log.error("Gemini returned non-JSON", extra={"raw": raw_text[:500]})
        raise HTTPException(status_code=502, detail="Gemini returned non-JSON response") from exc


async def _call_gemini_vertex(prompt: str, task: str, dimensions: list[str]) -> dict:
    """Use Vertex AI (production mode — requires GCP credentials)."""
    from google.api_core.exceptions import GoogleAPIError
    from vertexai.generative_models import GenerationConfig, GenerativeModel, Part

    user_message = _build_user_message(prompt, task, dimensions)

    def _sync_call() -> str:
        model = GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=SYSTEM_PROMPT,
        )
        resp = model.generate_content(
            [Part.from_text(user_message)],
            generation_config=GenerationConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )
        return resp.text

    try:
        raw_text = await asyncio.to_thread(_sync_call)
    except GoogleAPIError as exc:
        log.error("Vertex AI error", extra={"error": str(exc)})
        raise HTTPException(status_code=503, detail=f"Gemini unreachable: {exc}") from exc
    except Exception as exc:
        log.error("Unexpected Vertex AI error", extra={"error": str(exc)})
        raise HTTPException(status_code=503, detail=f"Gemini call failed: {exc}") from exc

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        log.error("Gemini returned non-JSON", extra={"raw": raw_text[:500]})
        raise HTTPException(status_code=502, detail="Gemini returned non-JSON response") from exc


async def _call_gemini(prompt: str, task: str, dimensions: list[str]) -> dict:
    log.info("Calling Gemini", extra={"model": GEMINI_MODEL, "mode": "vertex" if USE_VERTEX else "api_key"})
    if USE_VERTEX:
        return await _call_gemini_vertex(prompt, task, dimensions)
    return await _call_gemini_api_key(prompt, task, dimensions)

# ---------------------------------------------------------------------------
# Score builder
# ---------------------------------------------------------------------------


def _compute_response(gemini_data: dict, prompt_hash: str, cache_hit: bool) -> ScoreResponse:
    raw_dims = gemini_data.get("dimensions", [])
    dimensions: list[DimensionScore] = []
    for d in raw_dims:
        try:
            dimensions.append(DimensionScore(
                dimension=d["dimension"],
                score=int(d["score"]),
                reason=d.get("reason", ""),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed dimension", extra={"entry": d, "error": str(exc)})

    scores  = [d.score for d in dimensions]
    overall = round(mean(scores) * 10, 1) if scores else 0.0

    return ScoreResponse(
        overall_score=overall,
        dimensions=dimensions,
        strengths=gemini_data.get("strengths", []),
        improvements=gemini_data.get("improvements", []),
        cache_hit=cache_hit,
        prompt_hash=prompt_hash,
    )

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse, tags=["ops"])
async def health() -> HealthResponse:
    """Liveness / readiness probe."""
    return HealthResponse(
        status="ok",
        model=GEMINI_MODEL,
        mode="vertex-ai" if USE_VERTEX else "api-key",
        redis="up" if app_state.redis_ok else "down",
        firestore="up" if app_state.firestore_ok else "down",
    )


@app.post("/score", response_model=ScoreResponse, tags=["scoring"])
async def score(request: ScoreRequest) -> ScoreResponse:
    """
    Evaluate a prompt against a task and optional rubric.

    - Checks Redis cache first (SHA-256 key).
    - On a miss, calls Gemini (Vertex AI or API key mode).
    - Persists result to Firestore and publishes a Pub/Sub event.
    """
    dimensions = request.rubric if request.rubric else DEFAULT_DIMENSIONS
    cache_key, prompt_hash = _build_cache_key(request.prompt, request.task, dimensions)

    log.info("POST /score", extra={"prompt_hash": prompt_hash, "task": request.task})

    # ── Cache HIT ──────────────────────────────────────────────
    cached = await _get_cached(cache_key)
    if cached is not None:
        log.info("Cache HIT", extra={"prompt_hash": prompt_hash})
        cached["cache_hit"] = True
        try:
            return ScoreResponse(**cached)
        except Exception as exc:
            log.warning("Corrupt cache entry — re-scoring", extra={"error": str(exc)})

    # ── Cache MISS → Gemini ────────────────────────────────────
    log.info("Cache MISS — calling Gemini", extra={"prompt_hash": prompt_hash})
    gemini_data = await _call_gemini(request.prompt, request.task, dimensions)
    response    = _compute_response(gemini_data, prompt_hash, cache_hit=False)

    payload = response.model_dump()

    # ── Persist ───────────────────────────────────────────────
    fs_doc = {
        **payload,
        "prompt":    request.prompt[:500],   # truncated for storage
        "task":      request.task,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model":     GEMINI_MODEL,
    }

    await asyncio.gather(
        _set_cached(cache_key, payload),
        _save_to_firestore(prompt_hash, fs_doc),
        _publish_event(fs_doc),
        return_exceptions=True,
    )

    log.info("Score complete", extra={"overall_score": response.overall_score, "hash": prompt_hash})
    return response


@app.get("/scores/{prompt_hash}", response_model=ScoreResponse, tags=["scoring"])
async def get_score(prompt_hash: str) -> ScoreResponse:
    """
    Retrieve a previously computed score by its prompt hash.
    Checks Redis first, then Firestore.
    """
    if len(prompt_hash) != 24:
        raise HTTPException(status_code=400, detail="prompt_hash must be exactly 24 characters.")

    cache_key = f"score:{prompt_hash}"

    # Try Redis
    cached = await _get_cached(cache_key)
    if cached:
        cached["cache_hit"] = True
        return ScoreResponse(**cached)

    # Try Firestore
    doc = await _get_from_firestore(prompt_hash)
    if doc:
        doc["cache_hit"] = False
        return ScoreResponse(**doc)

    raise HTTPException(status_code=404, detail=f"No score found for hash '{prompt_hash}'.")


@app.get("/leaderboard", response_model=list[LeaderboardEntry], tags=["leaderboard"])
async def leaderboard(
    limit: int = Query(default=20, ge=1, le=100, description="Number of entries to return"),
) -> list[LeaderboardEntry]:
    """
    Return top-scoring prompts, ordered by overall_score descending.
    Requires Firestore to be configured — returns empty list otherwise.
    """
    docs = await _leaderboard_from_firestore(limit)
    entries: list[LeaderboardEntry] = []
    for doc in docs:
        try:
            entries.append(LeaderboardEntry(
                prompt_hash=doc.get("prompt_hash", ""),
                task=doc.get("task", ""),
                overall_score=doc.get("overall_score", 0.0),
                timestamp=doc.get("timestamp", ""),
                dimensions=[DimensionScore(**d) for d in doc.get("dimensions", [])],
            ))
        except Exception as exc:
            log.warning("Skipping malformed leaderboard entry", extra={"error": str(exc)})
    return entries


@app.post("/feedback", tags=["scoring"], status_code=204)
async def feedback(request: FeedbackRequest) -> None:
    """
    Submit thumbs-up / thumbs-down feedback on a scored prompt.
    Updates the Firestore document — no response body on success.
    """
    if not app_state.firestore_ok:
        raise HTTPException(status_code=503, detail="Feedback storage unavailable (Firestore not configured).")

    update = {
        "feedback_helpful": request.helpful,
        "feedback_at":      datetime.now(timezone.utc).isoformat(),
    }
    try:
        await asyncio.to_thread(
            lambda: app_state.fs_client
                .collection(FS_COLLECTION)
                .document(request.prompt_hash)
                .update(update)
        )
        log.info("Feedback saved", extra={"hash": request.prompt_hash, "helpful": request.helpful})
    except Exception as exc:
        log.warning("Feedback update failed", extra={"error": str(exc)})
        raise HTTPException(status_code=500, detail="Failed to save feedback.") from exc
