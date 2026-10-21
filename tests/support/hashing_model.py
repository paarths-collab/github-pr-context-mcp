"""A tiny offline stand-in for the SentenceTransformer embedding model.

Hashes words into a fixed-size, L2-normalised vector, so documents that share
words land close together under cosine distance. That is enough to exercise
indexing, persistence and retrieval end to end without downloading a model;
it makes no claim about the quality of real embeddings.
"""

from __future__ import annotations

import hashlib
import re

import numpy as np

DIM = 128


class HashingModel:
    def _vector(self, text: str) -> np.ndarray:
        vec = np.zeros(DIM, dtype=np.float32)
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            digest = hashlib.sha256(word.encode()).digest()
            vec[int.from_bytes(digest[:4], "big") % DIM] += 1.0
        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec

    def encode(self, texts):
        if isinstance(texts, str):
            return self._vector(texts)
        return np.stack([self._vector(text) for text in texts])
