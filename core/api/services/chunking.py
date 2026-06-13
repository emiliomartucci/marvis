# v1.0.0 - 2026-06-04 - Track 2 #4: fixed-size prose chunker (flag MARVIS_CHUNKING)
"""Pure, model-free fixed-size prose chunker (Track 2 #4).

Splits a prose document into fixed-size, overlapping, sentence-aware windows
BEFORE it is embedded, so a long handoff/plan no longer collapses to a single
over-compressed CLS vector (the embedder is a 32k-context ModernBert with CLS
pooling — there is no truncation pressure, the motivation is over-compression,
not truncation; see the roadmap "Research Insights — #4").

Design constraints (all load-bearing):

* **Model-free + pure.** No ONNX session, no model load, no I/O. The only
  dependency is a *tokenizer* injected by the caller (``tokenizer`` param), so
  this is unit-testable with a FAKE tokenizer and the real path reuses Granite's
  already-loaded Rust ``tokenizers.Tokenizer`` (no second tokenizer, no second
  model). The tokenizer is only asked for ``encode(text)`` exposing ``.ids`` and
  ``.offsets`` (per-token ``(char_start, char_end)`` into the ORIGINAL string —
  exactly what the Rust ``tokenizers`` package returns).
* **UTF-8 BYTE offsets.** ``span_start`` / ``span_end`` are byte offsets into the
  original text encoded as UTF-8, NOT character indices. Byte offsets round-trip
  losslessly through multibyte Italian accents (à/è/ù) for the #2 span-citation
  layer: ``text.encode("utf-8")[span_start:span_end].decode("utf-8")`` reproduces
  the chunk content exactly.
* **Sentence-aware.** Window edges snap to the nearest sentence boundary within a
  ``±boundary_slack_tokens`` slack, so a chunk rarely cuts mid-sentence. The
  splitter is a punkt-lite regex (no spaCy / nltk — those are host-pressure wheel
  bloat; at ~15% overlap an imperfect boundary heals across the overlap).
* **Per-chunk content hash.** ``content_hash`` = sha256 of the chunk's byte span,
  so a re-index re-embeds only the chunks that actually changed (editing one
  paragraph churns ~1-2 chunks, not the whole doc).

Scope: PROSE ONLY. Code is already chunked per-symbol in
``core/cli/_index_source.py`` (``_embed_symbols``); a token windower over code
would split mid-symbol and break the KG anchors. This module is never invoked on
code.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

__all__ = [
    "Chunk",
    "TokenizerLike",
    "chunk_prose",
    "DEFAULT_TARGET_TOKENS",
    "DEFAULT_OVERLAP_TOKENS",
]

# Mirror of the F2 EMBEDDING_* knobs: defaults chosen for retrieval precision
# (recall peaks ~400 tokens per the Chroma/firecrawl ablation), ~15% overlap.
DEFAULT_TARGET_TOKENS = 400
DEFAULT_OVERLAP_TOKENS = 64
DEFAULT_BOUNDARY_SLACK_TOKENS = 32


@dataclass(frozen=True, slots=True)
class Chunk:
    """One prose chunk.

    ``span_start`` / ``span_end`` are UTF-8 BYTE offsets into the ORIGINAL text
    (half-open ``[start, end)``). ``content_hash`` is the sha256 hex of the chunk
    byte span — the idempotency key (re-embed only changed chunks).
    """

    chunk_idx: int
    span_start: int
    span_end: int
    content_hash: str


@runtime_checkable
class TokenizerLike(Protocol):
    """Minimal surface of the Rust ``tokenizers.Tokenizer`` this chunker needs.

    The real Granite tokenizer (``tokenizers.Tokenizer.from_file(...)``) satisfies
    this: ``encode(text)`` returns an ``Encoding`` whose ``.offsets`` is a list of
    ``(char_start, char_end)`` tuples — one per token — into the ORIGINAL string.
    A FAKE tokenizer in tests implements the same two attributes, so the chunker
    is exercised with zero model load.
    """

    def encode(self, text: str) -> "EncodingLike": ...


@runtime_checkable
class EncodingLike(Protocol):
    ids: Sequence[int]
    # (char_start, char_end) per token, into the ORIGINAL (unencoded) string.
    offsets: Sequence[tuple[int, int]]


# Punkt-lite sentence splitter: a boundary is sentence-final punctuation
# (. ! ? plus their typographic variants … ; and the IT/EN newline) followed by
# whitespace. Intentionally simple — overlap heals the misses (an abbreviation
# like "es." that splits early is recovered in the next window's prefix).
_SENTENCE_END = re.compile(r"[.!?…;\n]+[\s\"')\]]*")


def _utf8_byte_offsets(text: str) -> list[int]:
    """Map char index -> UTF-8 byte offset (length == len(text) + 1).

    ``out[i]`` = byte offset where character ``i`` starts; ``out[len(text)]`` =
    total byte length. Computed once per doc so char→byte conversion of every
    token offset is O(1). Multibyte-safe: an accented char advances the byte
    cursor by its UTF-8 width (2 for à/è/ù), so byte spans round-trip losslessly.
    """
    out = [0] * (len(text) + 1)
    byte = 0
    for i, ch in enumerate(text):
        out[i] = byte
        byte += len(ch.encode("utf-8"))
    out[len(text)] = byte
    return out


def _sentence_boundary_chars(text: str) -> list[int]:
    """Character indices that are sentence boundaries (end of a sentence).

    Returns the char index just AFTER each sentence-final punctuation run (i.e. a
    valid place to start the next chunk). Always includes 0 and len(text) so the
    snap logic has terminal anchors.
    """
    bounds = {0, len(text)}
    for m in _SENTENCE_END.finditer(text):
        bounds.add(m.end())
    return sorted(bounds)


def _snap_to_boundary(
    token_char_pos: int,
    boundaries: Sequence[int],
    *,
    text_char_pos_window: tuple[int, int],
    slack_chars: int,
) -> int:
    """Snap a proposed cut (char index) to the nearest sentence boundary in slack.

    ``token_char_pos`` is the char index the token-window would cut at. If a
    sentence boundary lies within ``±slack_chars`` AND inside the allowed window
    ``text_char_pos_window`` (so we never snap past the doc edge or before the
    chunk start), use the closest one; otherwise keep the token cut (hard split).
    """
    lo, hi = text_char_pos_window
    best = token_char_pos
    best_dist = slack_chars + 1
    for b in boundaries:
        if b < lo or b > hi:
            continue
        dist = abs(b - token_char_pos)
        if dist <= slack_chars and dist < best_dist:
            best = b
            best_dist = dist
    return best


def chunk_prose(
    text: str,
    tokenizer: TokenizerLike,
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    boundary_slack_tokens: int = DEFAULT_BOUNDARY_SLACK_TOKENS,
) -> list[Chunk]:
    """Split ``text`` into fixed-size, overlapping, sentence-aware prose chunks.

    Windowing is in TOKEN space (reusing the injected tokenizer's per-token char
    offsets), so the window size is stable across mixed IT/EN text where char
    windows over/under-fill. Each window edge is snapped to the nearest sentence
    boundary within ``±boundary_slack_tokens`` (converted to a char slack via the
    local token density). Span offsets are emitted as UTF-8 BYTE offsets.

    Returns ``[]`` for empty/whitespace-only input. A short doc (<= target_tokens)
    yields a single chunk covering the whole byte range.
    """
    if not text or not text.strip():
        return []
    if target_tokens <= 0:
        raise ValueError("target_tokens must be > 0")
    if overlap_tokens < 0 or overlap_tokens >= target_tokens:
        # overlap must leave forward progress; clamp defensively rather than loop.
        overlap_tokens = max(0, min(overlap_tokens, target_tokens - 1))

    enc = tokenizer.encode(text)
    offsets = list(enc.offsets)
    n_tokens = len(offsets)
    byte_at = _utf8_byte_offsets(text)
    total_bytes = byte_at[len(text)]

    # Degenerate: tokenizer produced no usable offsets → one chunk, whole doc.
    if n_tokens == 0:
        return [
            Chunk(
                chunk_idx=0,
                span_start=0,
                span_end=total_bytes,
                content_hash=_hash_byte_span(text, 0, total_bytes),
            )
        ]

    boundaries = _sentence_boundary_chars(text)
    stride = target_tokens - overlap_tokens  # > 0 (overlap clamped above)

    # Mean chars/token for this doc → convert the token-space slack to char slack
    # for the boundary snap (the boundary list is in char space).
    mean_chars_per_token = max(1.0, len(text) / n_tokens)
    slack_chars = int(round(boundary_slack_tokens * mean_chars_per_token))

    chunks: list[Chunk] = []
    chunk_idx = 0
    tok_start = 0
    while tok_start < n_tokens:
        tok_end = min(tok_start + target_tokens, n_tokens)

        # Char window for THIS chunk: start at the first token's char-start, end
        # at the last covered token's char-end.
        char_start = offsets[tok_start][0]
        char_end = offsets[tok_end - 1][1]

        is_last = tok_end >= n_tokens

        # Snap the chunk START to a sentence boundary at/just-before it (only when
        # this is NOT the first chunk — chunk 0 must begin at 0 to cover the head).
        if chunk_idx > 0:
            char_start = _snap_to_boundary(
                char_start,
                boundaries,
                text_char_pos_window=(0, char_end),
                slack_chars=slack_chars,
            )

        # Snap the chunk END to a sentence boundary near it (never on the last
        # chunk — the tail must reach len(text) so no bytes are dropped).
        if not is_last:
            char_end = _snap_to_boundary(
                char_end,
                boundaries,
                text_char_pos_window=(char_start + 1, len(text)),
                slack_chars=slack_chars,
            )

        span_start = byte_at[char_start]
        span_end = byte_at[char_end] if not is_last else total_bytes

        if span_end > span_start:
            chunks.append(
                Chunk(
                    chunk_idx=chunk_idx,
                    span_start=span_start,
                    span_end=span_end,
                    content_hash=_hash_byte_span(text, span_start, span_end),
                )
            )
            chunk_idx += 1

        if is_last:
            break
        tok_start += stride

    return chunks


def _hash_byte_span(text: str, span_start: int, span_end: int) -> str:
    """sha256 of the chunk's UTF-8 byte span (the per-chunk idempotency key)."""
    raw = text.encode("utf-8")[span_start:span_end]
    return hashlib.sha256(raw).hexdigest()


def chunk_text_bytes(text: str, span_start: int, span_end: int) -> bytes:
    """Round-trip helper: the exact bytes a chunk's byte span refers to.

    ``chunk_text_bytes(text, c.span_start, c.span_end).decode("utf-8")`` is the
    chunk content (used by the #2 citation resolver and by the round-trip test).
    """
    return text.encode("utf-8")[span_start:span_end]
