from __future__ import annotations

import math
import re
from typing import Protocol, Sequence

_WORD = re.compile(r"[^\W_]{2,64}", re.UNICODE)
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class MemoryEmbeddingProvider(Protocol):
    dimensions: int
    max_data_classification: int
    external: bool

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


def tokenize(value: str, *, maximum: int = 128) -> tuple[str, ...]:
    normalized = value.casefold()
    tokens = set(_WORD.findall(normalized))
    cjk = "".join(_CJK.findall(normalized))
    tokens.update(cjk[index:index + 2] for index in range(max(0, len(cjk) - 1)))
    return tuple(sorted(token for token in tokens if 2 <= len(token) <= 64))[:maximum]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("embedding dimensions do not match")
    if any(not math.isfinite(value) for value in (*left, *right)):
        raise ValueError("embedding contains a non-finite value")
    denominator = math.sqrt(sum(v * v for v in left)) * math.sqrt(sum(v * v for v in right))
    return 0.0 if denominator == 0 else max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right)) / denominator))
