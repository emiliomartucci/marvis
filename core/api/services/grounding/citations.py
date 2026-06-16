# v1.0.0 - 2026-06-04 - Track 2 #2: closed-set citations + verbatim guard + decision policy
"""Span citations + grounding decision policy (Track 2 #2).

Pure, model-free building blocks of the select-then-generate-then-verify
pipeline. NOTHING here loads a model or does I/O; every function is a deterministic
transform so the whole layer is unit-testable on the shared host with zero model
load (learning a09b8754: an ONNX/torch load saturates the host).

Three load-bearing ideas, straight from "Research Insights — #2":

* **Citations are a CLOSED, engine-controlled set.** The engine SELECTs the top-K
  retrieval chunks and assigns each an opaque id ``S1, S2, …`` backed by a
  ``(doc_id, chunk_id, span_start, span_end)`` tuple (the spans come from #4's
  ``chunks`` table; ``span_start``/``span_end`` are UTF-8 byte offsets, half-open).
  The model GENERATEs only ``cite_ids`` drawn from this enumerated set — it can
  never invent an id or a byte offset, because it never writes them. ``EvidenceSet``
  builds the numbered evidence prompt block and ``validate_citations`` rejects any
  id outside the closed set (defense-in-depth: a small local model must never
  smuggle in an out-of-set id).

* **Verbatim guard (the real incident fix).** Before any NLI runs, any version
  string or proper-noun-like token in a sentence MUST appear verbatim in one of the
  sentence's cited spans, else the sentence is flagged for DROP. Near-zero cost,
  and it is the defense against the actual incident classes: invented versions
  (``v0.3.6`` that was never in the evidence) and invented client/person names.
  "v0.3.5" != "v0.3.6" must FAIL even though an NLI model might paraphrase-match
  them — so this guard runs IN ADDITION to NLI, not instead of it.

* **DROP / HEDGE / KEEP / FLAG_SYNTHESIS policy.** ``decide`` is the pure decision
  function. Crucially HEDGE != DROP: AFtG found ~42% of sentences flagged
  "unsupported" were actually PARTIALLY supported, so a cited-but-weak sentence is
  kept and marked "(non verificato)", never silently deleted (silently deleting
  real content is its own credibility hit).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "EvidenceSpan",
    "EvidenceSet",
    "validate_citations",
    "verbatim_guard",
    "Decision",
    "decide",
    "VERSION_RE",
]


# --- version regex ----------------------------------------------------------
# Matches version strings the brief must never invent: an optional leading ``v``,
# MAJOR.MINOR, an OPTIONAL .PATCH, and an OPTIONAL pre-release tag like ``a1``/``b2``
# (alpha/beta build). Examples that match: v0.3.6, 0.3.5, v1.2, 2.10.3, v0.3.6b1.
# Anchored at word boundaries so "v1.2" inside "dev1.2x" does not falsely match.
# Compiled once at import (this module is hot-path-adjacent under the flag).
VERSION_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?(?:[ab]\d+)?\b")

# A "proper-noun-like" token without a heavy NER model: a Capitalized word
# (incl. internal capitals like ``MarvisX`` / ``McCarthy`` and a few common
# punctuated forms like ``D1`` / ``GPT-4``). We DELIBERATELY only consider tokens
# that are NOT sentence-initial-only common words: see ``_candidate_proper_nouns``
# for the (cheap, conservative) heuristic. Acronyms (all-caps, len>=2) included.
_PROPER_NOUN_RE = re.compile(
    r"\b("
    r"[A-Z][a-z]+(?:[A-Z][a-z]+)+"  # InternalCaps: MarvisX, McCarthy, OltrepoCase
    r"|[A-Z]{2,}(?:-?\d+)?"  # ACRONYMS: API, KG, GPT-4, D1 (caps then opt -digits)
    r"|[A-Z][a-z]{2,}"  # Capitalized words: Emilio, Mirko, Anthropic
    r")\b"
)

# Common sentence-initial words that are Capitalized only because they start a
# sentence — NOT proper nouns. Lower-cased for comparison. Italian + English
# stop-ish openers the brief uses. Conservative: missing one only means we run an
# extra (cheap, exact) verbatim check, never a false hallucination flag, because
# the guard requires the token to be ABSENT from the spans to flag — a genuinely
# present word passes regardless.
_SENTENCE_OPENERS = frozenset(
    {
        "il", "lo", "la", "i", "gli", "le", "un", "uno", "una",
        "questo", "questa", "questi", "queste", "quello", "quella",
        "the", "a", "an", "this", "that", "these", "those",
        "in", "su", "per", "con", "di", "da", "tra", "fra", "e", "ed",
        "ora", "poi", "quindi", "inoltre", "dopo", "prima", "oggi",
        "now", "then", "so", "also", "after", "before", "today",
    }
)


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    """One selected span, backed by a chunk from #4's ``chunks`` table.

    ``span_id`` is the opaque, engine-assigned id (``S1`` …). ``doc_id`` +
    ``chunk_id`` + ``span_start`` + ``span_end`` (UTF-8 byte offsets, half-open)
    locate the exact span for deep-linking; ``text`` is the resolved span content
    used both in the numbered prompt block and as the NLI premise / verbatim
    haystack.
    """

    span_id: str
    doc_id: str
    chunk_id: str
    span_start: int
    span_end: int
    text: str


@dataclass(frozen=True, slots=True)
class EvidenceSet:
    """A CLOSED set of selected spans the model is allowed to cite.

    Built engine-side from the top-K retrieval result. The model receives the
    numbered prompt block from :meth:`prompt_block` and may only emit ``cite_ids``
    drawn from :attr:`ids`; anything else is rejected by :func:`validate_citations`.
    """

    spans: tuple[EvidenceSpan, ...]
    _by_id: dict[str, EvidenceSpan] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_chunks(
        cls,
        chunks: list[tuple[str, str, int, int, str]],
    ) -> EvidenceSet:
        """Build a closed evidence set from selected chunk tuples.

        Each tuple is ``(doc_id, chunk_id, span_start, span_end, text)`` — the
        engine-side projection of a row from #4's ``chunks`` table after the
        SELECT step. Ids are assigned deterministically ``S1, S2, …`` in input
        order, so the SELECTion (not the model) controls the numbering.
        """
        spans = tuple(
            EvidenceSpan(
                span_id=f"S{i}",
                doc_id=doc_id,
                chunk_id=chunk_id,
                span_start=span_start,
                span_end=span_end,
                text=text,
            )
            for i, (doc_id, chunk_id, span_start, span_end, text) in enumerate(chunks, start=1)
        )
        return cls(spans=spans)

    def __post_init__(self) -> None:
        # Build the id index once. ``_by_id`` is excluded from eq/repr so the
        # dataclass stays value-comparable on ``spans`` alone.
        object.__setattr__(self, "_by_id", {s.span_id: s for s in self.spans})

    @property
    def ids(self) -> frozenset[str]:
        """The closed set of valid citation ids (``{"S1", "S2", …}``)."""
        return frozenset(self._by_id.keys())

    def get(self, span_id: str) -> EvidenceSpan | None:
        """Resolve a span id to its span, or ``None`` if outside the set."""
        return self._by_id.get(span_id)

    def texts_for(self, cite_ids: list[str]) -> list[str]:
        """Resolved span texts for the given (already-validated) cite ids.

        Unknown ids are skipped (validate first via :func:`validate_citations`).
        Order follows ``cite_ids``; the concatenation of these is the multi-premise
        NLI premise (VeriCite) and the verbatim-guard haystack.
        """
        out: list[str] = []
        for cid in cite_ids:
            span = self._by_id.get(cid)
            if span is not None:
                out.append(span.text)
        return out

    def prompt_block(self) -> str:
        """The numbered evidence list for the GENERATE prompt.

        ``"[S1] <text>\\n[S2] <text>\\n…"`` — the model literally cannot cite what
        is not listed here. Span text is single-lined so each ``[Sn]`` stays on its
        own line in the prompt.
        """
        lines = []
        for s in self.spans:
            flat = " ".join(s.text.split())
            lines.append(f"[{s.span_id}] {flat}")
        return "\n".join(lines)


def validate_citations(
    cite_ids: list[str],
    evidence_set: EvidenceSet,
) -> tuple[list[str], list[str]]:
    """Filter cite ids against the CLOSED set; reject anything not in it.

    A small local model must never invent an id (or, worse, an offset). This
    rejects any ``cite_id`` not present in ``evidence_set.ids``. Returns
    ``(valid, rejected)`` preserving input order; duplicates are de-duped in
    ``valid`` while preserving first-seen order (an over-citing model slapping the
    same id twice should not double-count).
    """
    valid: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()
    ids = evidence_set.ids
    for cid in cite_ids:
        if cid in ids:
            if cid not in seen:
                seen.add(cid)
                valid.append(cid)
        else:
            rejected.append(cid)
    return valid, rejected


def _candidate_proper_nouns(sentence: str) -> list[str]:
    """Cheap, NER-free proper-noun-like tokens in a sentence.

    No spaCy/NER model (host-pressure wheel bloat + model load). Heuristic:
    regex-match Capitalized/InternalCaps/ACRONYM tokens, then drop a token if it is
    BOTH sentence-initial AND a known common opener (so "Il task è bloccato" does
    not flag "Il", but "Emilio ha deciso" still flags "Emilio"). This is
    intentionally conservative — a missed proper noun only costs an extra cheap
    exact-match check; it never fabricates a hallucination flag, because the guard
    only flags a token that is genuinely ABSENT from every cited span.
    """
    out: list[str] = []
    first = True
    for m in _PROPER_NOUN_RE.finditer(sentence):
        tok = m.group(0)
        is_sentence_initial = first and m.start() == _leading_ws_len(sentence)
        first = False
        if is_sentence_initial and tok.lower() in _SENTENCE_OPENERS:
            continue
        out.append(tok)
    return out


def _leading_ws_len(s: str) -> int:
    return len(s) - len(s.lstrip())


def verbatim_guard(sentence: str, cited_spans_text: list[str]) -> list[str]:
    """Flag a sentence for DROP if a version/proper-noun is NOT verbatim in spans.

    The REAL incident fix. Any version string (``VERSION_RE``) or proper-noun-like
    token in ``sentence`` MUST appear verbatim (exact substring, case-sensitive for
    versions) in at least one of ``cited_spans_text``; otherwise it is an offending
    token and the sentence should be DROPped pre-NLI. Near-zero cost; this runs
    BEFORE any model and kills the worst hallucinations (invented ``v0.3.6``,
    invented client/person names) that an NLI head might paraphrase past.

    Returns the list of offending tokens (empty = sentence passes the guard).
    Versions matched case-sensitively (``v0.3.5`` must not satisfy ``v0.3.6``).
    Proper nouns matched as a case-sensitive whole-word presence in the haystack.
    """
    haystack = "\n".join(cited_spans_text)
    offenders: list[str] = []
    seen: set[str] = set()

    for m in VERSION_RE.finditer(sentence):
        tok = m.group(0)
        if tok in seen:
            continue
        seen.add(tok)
        # Exact, case-sensitive substring: v0.3.5 must NOT be satisfied by v0.3.6.
        if tok not in haystack:
            offenders.append(tok)

    for tok in _candidate_proper_nouns(sentence):
        if tok in seen:
            continue
        seen.add(tok)
        if not _present_verbatim(tok, haystack):
            offenders.append(tok)

    return offenders


def _present_verbatim(token: str, haystack: str) -> bool:
    """Case-sensitive whole-word presence of ``token`` in ``haystack``.

    Whole-word so ``Mark`` does not match inside ``Marketing``; case-sensitive so
    an invented ``Aldo`` is not satisfied by a lower-case ``aldo`` fragment.
    """
    if not token:
        return True
    pattern = r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])"
    return re.search(pattern, haystack) is not None


class Decision(str, Enum):
    """The grounding decision for a single brief sentence.

    * ``DROP`` — uncited AND unentailed (or verbatim-guard offender): a fabricated
      fact, remove it.
    * ``HEDGE`` — cited but weak (entailment partial/failed): KEEP the sentence and
      mark it "(non verificato)". Never silently delete (AFtG 42% partial-support).
    * ``KEEP`` — entailed by its cited span(s): keep + render the citation.
    * ``FLAG_SYNTHESIS`` — a cross-doc/synthesis claim ("3 tasks blocked on X") that
      no single span entails; needs a union-premise / atomic-claim check upstream.
    """

    DROP = "drop"
    HEDGE = "hedge"
    KEEP = "keep"
    FLAG_SYNTHESIS = "flag_synthesis"


def decide(
    sentence: str,
    has_citation: bool,
    entailment_verdict: bool,
    is_synthesis: bool,
) -> Decision:
    """Pure decision policy for one sentence. No I/O, no model.

    Inputs:
    * ``has_citation`` — at least one VALID cite id survived ``validate_citations``.
    * ``entailment_verdict`` — the NLI head's binary verdict (sentence entailed by
      the concat of its OWN cited spans). With ``NoopVerifier`` this is always
      ``False`` (unverified passthrough) → uncited/cited-unentailed branches apply.
    * ``is_synthesis`` — a cross-doc claim no single span can entail.

    Policy table (every branch covered):

        has_citation | entailed | synthesis | -> Decision
        -------------|----------|-----------|----------------
        False        |   any    |   False   | DROP        (uncited fabricated fact)
        False        |   any    |   True    | FLAG_SYNTHESIS (needs union premise)
        True         |   True   |   any     | KEEP        (entailed)
        True         |   False  |   True    | FLAG_SYNTHESIS (cited cross-doc, weak)
        True         |   False  |   False   | HEDGE       (cited but weak: keep + mark)

    Rationale: synthesis is recognised even when uncited/unentailed because a
    multi-doc claim is the MOST valuable brief sentence — flagging it for a
    union-premise check beats dropping it. A cited-but-unentailed single-doc claim
    HEDGEs (keep + "(non verificato)"), it is never silently dropped.
    """
    if not has_citation:
        if is_synthesis:
            return Decision.FLAG_SYNTHESIS
        return Decision.DROP
    # has_citation is True from here.
    if entailment_verdict:
        return Decision.KEEP
    if is_synthesis:
        return Decision.FLAG_SYNTHESIS
    return Decision.HEDGE
