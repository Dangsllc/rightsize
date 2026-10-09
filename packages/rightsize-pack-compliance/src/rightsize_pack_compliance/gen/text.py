"""Small, dependency-free text helpers shared by the generators."""

from __future__ import annotations

import hashlib
import re

STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "been", "being", "by", "can", "could", "do", "does", "for", "from", "had", "has", "have", "how", "i", "if", "in", "into", "is", "it", "its", "may", "must", "no", "not", "of", "on", "or", "our", "shall", "should", "so", "such", "than", "that", "the", "their", "them", "then", "there", "these", "they", "this", "those", "to", "under", "up", "us", "was", "we", "were", "what", "when", "where", "which", "while", "who", "whom", "why", "will", "with", "would", "you", "your", "any", "all", "each", "other", "also", "only", "same"]
)

_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def content_words(text: str, allow: set[str] | frozenset[str] = frozenset()) -> list[str]:
    return [w for w in words(text) if w not in STOPWORDS and w not in allow and len(w) > 2]


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def stable_hash(*parts: object) -> int:
    h = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:16], 16)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_title(title: str) -> str:
    t = re.sub(r"^\s*(Standard|Implementation specifications?)\s*:\s*", "", title, flags=re.IGNORECASE)
    t = re.sub(r"\((Required|Addressable)\)", "", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip(" .:")
