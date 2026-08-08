"""Query embedding helpers shared by semantic weather search flows."""

from functools import lru_cache
import math
from typing import Any

EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


class EmbeddingError(RuntimeError):
    """Raised when the embedding model produces an unusable vector."""


@lru_cache(maxsize=1)
def get_embedding_model() -> Any:
    """Load MiniLM once per application process."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise EmbeddingError("sentence-transformers is not installed.") from exc

    model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    dimension = model.get_sentence_embedding_dimension()
    if dimension != EMBEDDING_DIM:
        raise EmbeddingError(
            f"Expected a {EMBEDDING_DIM}-dimensional model, got {dimension}."
        )
    return model


def embed_query(query: str) -> list[float]:
    """Generate and validate one query embedding."""
    encoded = get_embedding_model().encode(
        [query],
        batch_size=1,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    if len(encoded) != 1:
        raise EmbeddingError("The embedding model did not return one query vector.")

    raw_vector: Any = encoded[0]
    if hasattr(raw_vector, "tolist"):
        raw_vector = raw_vector.tolist()
    if len(raw_vector) != EMBEDDING_DIM:
        raise EmbeddingError(
            f"Expected {EMBEDDING_DIM} embedding values, got {len(raw_vector)}."
        )

    try:
        vector = [float(value) for value in raw_vector]
    except (TypeError, ValueError) as exc:
        raise EmbeddingError("The embedding model returned non-numeric values.") from exc
    if not all(math.isfinite(value) for value in vector):
        raise EmbeddingError("The embedding model returned non-finite values.")
    return vector


def serialize_pgvector(vector: list[float]) -> str:
    """Serialize a validated vector for a parameterized %s::vector cast."""
    if len(vector) != EMBEDDING_DIM:
        raise EmbeddingError(
            f"Expected {EMBEDDING_DIM} embedding values, got {len(vector)}."
        )
    if not all(math.isfinite(value) for value in vector):
        raise EmbeddingError("Cannot serialize non-finite embedding values.")
    return "[" + ",".join(format(value, ".9g") for value in vector) + "]"
