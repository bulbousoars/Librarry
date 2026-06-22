from __future__ import annotations

import re
from dataclasses import dataclass

from librarry.config import QualityConfig  # noqa: TC001 — runtime import for dataclass field


@dataclass(frozen=True)
class ReleaseCandidate:
    title: str
    download_url: str
    size_bytes: int
    indexer: str
    protocol: str  # usenet | torrent
    category: str | None = None
    guid: str | None = None
    score: float = 0.0
    reject_reason: str | None = None
    pub_date: str | None = None
    seeders: int | None = None
    leechers: int | None = None
    grabs: int | None = None


_EXT_RE = re.compile(r"\.([a-z0-9]{2,5})\b", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower())


def token_overlap(query: str, candidate: str) -> float:
    want = {t for t in _norm(query).split() if len(t) > 1}
    if not want:
        return 0.0
    got = set(_norm(candidate).split())
    return len(want & got) / len(want)


def detect_extension(title: str) -> str | None:
    lower = title.lower()
    for ext in ("azw3", "epub", "mobi", "pdf", "fb2", "m4b", "m4a", "mp3", "flac"):
        if re.search(rf"\b{ext}\b", lower) or f".{ext}" in lower:
            return ext
    m = _EXT_RE.search(lower)
    return m.group(1).lower() if m else None


def evaluate_release(title: str, query: str, quality: QualityConfig) -> tuple[float, str | None]:
    lower = title.lower()
    for pattern in quality.reject_patterns:
        if pattern in lower:
            return 0.0, f"reject pattern: {pattern}"

    ext = detect_extension(title)
    if ext and ext in quality.reject_extensions:
        return 0.0, f"reject extension: {ext}"

    allowed = set(quality.required_extensions + quality.acceptable_extensions)
    if ext and ext not in allowed:
        return 0.0, f"unsupported extension: {ext}"

    score = token_overlap(query, title)
    if ext in quality.required_extensions:
        score += 0.25
    elif ext in quality.acceptable_extensions:
        score += 0.1
    for pattern in quality.prefer_patterns:
        if pattern in lower:
            score += 0.05
    return score, None


def rank_candidates(
    title: str,
    author: str,
    candidates: list[ReleaseCandidate],
    quality: QualityConfig,
    fuzz_threshold: float,
) -> list[ReleaseCandidate]:
    query = f"{author} {title}".strip()
    ranked: list[ReleaseCandidate] = []
    for cand in candidates:
        score, reason = evaluate_release(cand.title, query, quality)
        if reason:
            ranked.append(
                ReleaseCandidate(
                    **{**cand.__dict__, "score": 0.0, "reject_reason": reason}
                )
            )
            continue
        if score < fuzz_threshold:
            ranked.append(
                ReleaseCandidate(
                    **{**cand.__dict__, "score": score, "reject_reason": "below fuzz threshold"}
                )
            )
            continue
        ranked.append(ReleaseCandidate(**{**cand.__dict__, "score": score}))
    return sorted(
        [c for c in ranked if not c.reject_reason],
        key=lambda c: c.score,
        reverse=True,
    )
