"""
PromptWars — Prompt Quality Scoring API
FastAPI + Vertex AI Gemini 1.5 Flash + Memorystore Redis
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from statistics import mean
from typing import Optional

import redis.asyncio as aioredis
import vertexai
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google.api_core.exceptions import GoogleAPIError
from pydantic import BaseModel, Field
from vertexai.generative_models import GenerationConfig, GenerativeModel, Part

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "time": self.formatTime(record, self.datefmt),
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
# Environment / config
# ---------------------------------------------------------------------------

GCP_PROJECT: str = os.environ.get("GCP_PROJECT", "my-gcp-project")
GCP_REGION: str = os.environ.get("GCP_REGION", "us-central1")
REDIS_HOST: str = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT: int = int(os.environ.get("REDIS_PORT", "6379"))
CACHE_TTL_SEC: int = int(os.environ.get("CACHE_TTL_SEC", "3600"))
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash-001")

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
    '  "dimensions": [{"dimension": "string", "score": "int 0-10", "reason": "one sentence"}],\n'
    '  "strengths": ["short phrases"],\n'
    '  "improvements": ["short actionable phrases"]\n'
    "}"
)

# ---------------------------------------------------------------------------
# Pydantic v2 models
# ---------------------------------------------------------------------------


class ScoreRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="The prompt to evaluate.")
    task: str = Field(..., min_length=1, description="The target task for the prompt.")
    rubric: Optional[list[str]] = Field(
        default=None,
        description="Custom rubric dimensions. Defaults to the 5 standard ones.",
    )


class DimensionScore(BaseModel):
    dimension: str
    score: int = Field(..., ge=0, le=10)
    reason: str


class ScoreResponse(BaseModel):
    overall_score: float
    dimensions: list[DimensionScore]
    strengths: list[str]
    improvements: list[str]
    cache_hit: bool
    prompt_hash: str


class HealthResponse(BaseModel):
    status: str
    model: str


# ---------------------------------------------------------------------------
# Application state (Redis client held for the lifetime of the process)
# ---------------------------------------------------------------------------

class _AppState:
    redis: Optional[aioredis.Redis] = None


app_state = _AppState()

# ---------------------------------------------------------------------------
# Lifespan — initialise Vertex AI and Redis once at startup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: D401
    log.info("Initialising Vertex AI", extra={"project": GCP_PROJECT, "region": GCP_REGION})
    vertexai.init(project=GCP_PROJECT, location=GCP_REGION)

    log.info("Connecting to Redis", extra={"host": REDIS_HOST, "port": REDIS_PORT})
    try:
        app_state.redis = aioredis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        await app_state.redis.ping()
        log.info("Redis connection established")
    except Exception as exc:  # noqa: BLE001
        log.warning("Redis unavailable at startup — continuing without cache", extra={"error": str(exc)})
        app_state.redis = None

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
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_cache_key(prompt: str, task: str, rubric: Optional[list[str]]) -> str:
    """Deterministic SHA-256 hash (first 24 hex chars) of the request inputs."""
    rubric_str = json.dumps(sorted(rubric or DEFAULT_DIMENSIONS))
    raw = f"{prompt}|{task}|{rubric_str}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"score:{digest}", digest


async def _get_cached(key: str) -> Optional[dict]:
    """Return the cached dict or None (also None if Redis is down)."""
    if app_state.redis is None:
        return None
    try:
        raw = await app_state.redis.get(key)
        if raw:
            return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("Redis GET failed", extra={"key": key, "error": str(exc)})
    return None


async def _set_cached(key: str, value: dict) -> None:
    """Write to Redis; swallow any errors."""
    if app_state.redis is None:
        return
    try:
        await app_state.redis.set(key, json.dumps(value), ex=CACHE_TTL_SEC)
    except Exception as exc:  # noqa: BLE001
        log.warning("Redis SET failed", extra={"key": key, "error": str(exc)})


def _build_user_message(prompt: str, task: str, dimensions: list[str]) -> str:
    dims_formatted = "\n".join(f"- {d}" for d in dimensions)
    return (
        f"Task: {task}\n\n"
        f"Prompt to evaluate:\n{prompt}\n\n"
        f"Rubric dimensions to score:\n{dims_formatted}"
    )


async def _call_gemini(prompt: str, task: str, dimensions: list[str]) -> dict:
    """
    Invoke Gemini and return the parsed JSON dict.
    Raises HTTPException(502) for bad JSON, HTTPException(503) for API errors.
    """
    user_message = _build_user_message(prompt, task, dimensions)
    log.info("Calling Gemini", extra={"model": GEMINI_MODEL, "dimensions": dimensions})

    try:
        model = GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=SYSTEM_PROMPT,
        )
        response = model.generate_content(
            [Part.from_text(user_message)],
            generation_config=GenerationConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )
        raw_text = response.text
    except GoogleAPIError as exc:
        log.error("Gemini API error", extra={"error": str(exc)})
        raise HTTPException(status_code=503, detail=f"Gemini unreachable: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        log.error("Unexpected Gemini error", extra={"error": str(exc)})
        raise HTTPException(status_code=503, detail=f"Gemini call failed: {exc}") from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        log.error("Gemini returned non-JSON", extra={"raw": raw_text[:500]})
        raise HTTPException(status_code=502, detail="Gemini returned non-JSON response") from exc

    return data


def _compute_response(gemini_data: dict, prompt_hash: str, cache_hit: bool) -> ScoreResponse:
    """Validate Gemini output and compute overall_score."""
    raw_dims = gemini_data.get("dimensions", [])
    dimensions: list[DimensionScore] = []
    for d in raw_dims:
        try:
            dimensions.append(
                DimensionScore(
                    dimension=d["dimension"],
                    score=int(d["score"]),
                    reason=d.get("reason", ""),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed dimension entry", extra={"entry": d, "error": str(exc)})

    scores = [d.score for d in dimensions]
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
    return HealthResponse(status="ok", model="gemini-1.5-flash-001")


@app.post("/score", response_model=ScoreResponse, tags=["scoring"])
async def score(request: ScoreRequest) -> ScoreResponse:
    """
    Evaluate a prompt against a task and optional rubric.

    - Checks Redis for a cached result first.
    - On a cache miss, calls Vertex AI Gemini 1.5 Flash.
    - Stores the result in Redis with configurable TTL.
    """
    dimensions = request.rubric if request.rubric else DEFAULT_DIMENSIONS
    cache_key, prompt_hash = _build_cache_key(request.prompt, request.task, dimensions)

    log.info(
        "Received /score request",
        extra={"prompt_hash": prompt_hash, "task": request.task, "rubric": dimensions},
    )

    # ---- Cache HIT ----
    cached = await _get_cached(cache_key)
    if cached is not None:
        log.info("Cache HIT", extra={"prompt_hash": prompt_hash})
        cached["cache_hit"] = True
        try:
            return ScoreResponse(**cached)
        except Exception as exc:  # noqa: BLE001
            log.warning("Corrupt cache entry — re-scoring", extra={"error": str(exc)})

    # ---- Cache MISS → call Gemini ----
    log.info("Cache MISS — calling Gemini", extra={"prompt_hash": prompt_hash})
    gemini_data = await _call_gemini(request.prompt, request.task, dimensions)
    response = _compute_response(gemini_data, prompt_hash, cache_hit=False)

    # Persist to cache (fire-and-forget errors)
    payload = response.model_dump()
    payload["prompt_hash"] = prompt_hash  # ensure hash stored in cache too
    await _set_cached(cache_key, payload)

    log.info(
        "Scoring complete",
        extra={"prompt_hash": prompt_hash, "overall_score": response.overall_score},
    )
    return response
