# Quality Gates Bootstrap

Bootstrap deterministico per installare i quality gate TS/JS su repo esterni.

## Scope

Lo script prepara un repo Node/TypeScript con:
- `knip:check`
- `knip:baseline`
- `lint`
- `lint:baseline`
- `lint:raw` se il repo aveva gia' uno script ESLint

e copia i template riusabili da MarvisX in:
- `scripts/knip-check.mjs`
- `scripts/knip-count.mjs`
- `scripts/lint-check.mjs`
- `scripts/lint-count.mjs`
- `knip.json` se manca
- `eslint.config.mjs` se manca e non esiste gia' `eslint.config.js|cjs|mjs`

## Uso

```bash
bash scripts/install-quality-gates.sh /absolute/path/to/repo
```

Per evitare di puntare al path sbagliato, usa il wrapper manifest-driven:

```bash
bash scripts/run-quality-gates-rollout.sh --dry-run cer-webapp
bash scripts/run-quality-gates-rollout.sh cer-webapp
```

## Stack supportati

- `next`
- `vite`
- `worker`
- fallback `react` -> template `vite`

Lo stack viene rilevato leggendo `package.json`.

## Note pratiche

- Il repo target deve avere `package.json` in root.
- Se il repo ha gia' una config ESLint (`eslint.config.js|cjs|mjs`), lo script non la sovrascrive.
- Il gate ESLint standard del bootstrap e' baseline-aware via `.eslint-baseline.json`.
- `targets.json` definisce il `repo_path` corretto e, se serve, un `subdir` come root reale del rollout.
- In caso di failure lo script ripristina i file toccati, ma lascia gli eventuali package installati in `node_modules`.

## Smoke test consigliato

```bash
bash scripts/install-quality-gates.sh /path/to/repo
cd /path/to/repo
npm run lint:baseline
npm run knip:check
npm run lint
```

## Output atteso

- `.knip-baseline.json`
- `.eslint-baseline.json`
- scripts npm aggiunti o completati in `package.json`
