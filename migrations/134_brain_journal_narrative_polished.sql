-- Wave 3.1 Gap 2: persist LLM-polished narrative on journal entries.
-- Polished after cycle publish when settings.brain_llm_polish_enabled=1
-- and the entry is non-empty (is_empty=0). NULL when polish disabled or LLM
-- unavailable; deterministic body_json always present as fallback.

ALTER TABLE brain_journal_entries ADD COLUMN narrative_polished TEXT;
ALTER TABLE brain_journal_entries ADD COLUMN narrative_polished_at TEXT;
ALTER TABLE brain_journal_entries ADD COLUMN narrative_polished_model TEXT;
