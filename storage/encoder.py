# SentenceTransformer model loading and text encoding only.
# No ChromaDB, no PR logic here.
#
# Model is lazy-loaded on first call instead of at import time.
# This prevents Render health checks from failing during cold start
# (the model download takes ~20-30s, which exceeds Render's health check window).

from __future__ import annotations
from threading import Lock

_model = None
_model_lock = Lock()
_MODEL_NAME = "all-MiniLM-L6-v2"


def _get_model():
    """Lazy-load the SentenceTransformer model — only once, thread-safe."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:  # double-checked locking
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(_MODEL_NAME)
    return _model


def encode(text: str) -> list[float]:
    """Encode a single string into a vector."""
    return _get_model().encode(text).tolist()


def encode_batch(texts: list[str]) -> list[list[float]]:
    """Encode a batch in one model call instead of per-document inference."""
    if not texts:
        return []
    model = _get_model()
    return model.encode(texts).tolist()
