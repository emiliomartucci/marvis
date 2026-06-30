-- Migration 150 — super-session delegations (Constitution v2.0 Rule 6).
--
-- ADDITIVE, costo costante: 1 CREATE TABLE + 1 indice. Nessuna riga esistente
-- toccata. VERIFY prod schema_versions max prima del merge: prod = 149, repo
-- max = 149 → questo file e' 150 (il runner applica solo version > MAX).
--
-- Modello: una riga = una grant umano→agente. Il token umano usato come prova
-- NON viene salvato: si salva solo il suo jti (gia' bruciato in token_blacklist
-- dalla POST /delegations — il vincolo UNIQUE su proof_jti e' la seconda linea
-- anti-replay). La grant delega il RUOLO del concedente (granted_by_role) ma
-- l'identita' che agisce resta quella dell'agente: l'audit non mente mai.
-- Revoca: revoked_at IS NOT NULL = morta; scadenza: expires_at nel passato.
-- In modalita' OSS single-user la tabella resta vuota e inerte (il locale e'
-- gia' is_human_session=True by design).

CREATE TABLE IF NOT EXISTS delegations (
    id TEXT PRIMARY KEY,
    agent_username TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    granted_by_user_id TEXT NOT NULL,
    granted_by_role TEXT NOT NULL,
    proof_jti TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL DEFAULT 'full',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_delegations_agent_active
    ON delegations(agent_username, expires_at);
