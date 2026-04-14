# SentenceTransformer model loading and text encoding only.
# No ChromaDB, no PR logic here.

from sentence_transformers import SentenceTransformer

# Loaded once at module level — expensive to reload on every call
_model = SentenceTransformer("all-MiniLM-L6-v2")


def encode(text: str) -> list[float]:
    """Encode a single string into a vector."""
    return _model.encode(text).tolist()

def encode_batch(texts: list[str]) -> list[list[float]]:
    """Encode a list of strings into vectors in one pass."""
    return [_model.encode(t).tolist() for t in texts]
