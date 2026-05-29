# Marvis deploy template

Template Docker minimale per avviare Marvis in locale con API, Console, SQLite e reverse proxy (nginx baseline o Caddy con TLS automatica). Tutti i valori sono generici: sostituisci i segnaposto in `.env` prima di esporre l'istanza su Internet.

## Requirements

- Docker Engine >= 24 e Docker Compose plugin >= 2.20 (`docker compose version`).
- 2 GB RAM minimo, 4 GB raccomandati (Next.js build + FastAPI + SQLite).
- 8 GB liberi su disco per immagini e volumi.
- Porte libere: `3000` (Console), `8100` (API), `8080` (nginx baseline). Se attivi il profilo Caddy servono anche `80` e `443`.
- Linux, macOS o Windows con WSL2 (Docker Desktop con backend Linux).

## Quick start

1. `git clone <<REPO_URL>> marvis && cd marvis/deploy/_template`
2. `cp .env.example .env`
3. Edita `.env`: sostituisci almeno `PIR_JWT_SECRET` (es. `openssl rand -hex 32`) e `PIR_PASSWORD`.
4. `docker compose up --build`
5. Apri `http://localhost:3000` e accedi con `admin` + il valore di `PIR_PASSWORD`.

Boot atteso: 3-8 min al primo avvio (build immagini), 30-60 s ai successivi.

## Profili Docker

Lo stack si avvia in tre configurazioni mutuamente combinabili.

| Profilo | Comando | Quando usarlo | Prezzo |
|---|---|---|---|
| baseline (default) | `docker compose up --build` | sviluppo locale, demo offline | nginx su `:8080`, nessuna TLS |
| `tunnel` | `docker compose --profile tunnel up -d` | esposizione via Cloudflare Tunnel senza aprire porte | richiede `CLOUDFLARE_TUNNEL_TOKEN` valido |
| `caddy` | `docker compose --profile caddy up -d` | dominio pubblico con TLS Let's Encrypt automatica | richiede DNS A/AAAA che punti all'host + porte `80/443` libere |

`tunnel` e `caddy` non sono mutuamente esclusivi (puoi avere Caddy davanti per LAN e Tunnel per esterno). nginx baseline e Caddy ascoltano su porte diverse, quindi possono coesistere; in produzione di solito ne tieni uno solo.

### Caddy + TLS automatica

Per uso locale lascia `PUBLIC_DOMAIN=localhost`: Caddy emette un certificato dalla sua CA interna e non contatta Let's Encrypt.

Per produzione imposta in `.env`:

```bash
PUBLIC_DOMAIN=marvis.example.com
ACME_EMAIL=tu@example.com
```

Prima di avviare verifica che il DNS A/AAAA di `PUBLIC_DOMAIN` punti gia' a questo host: Let's Encrypt valida via http-01 e la prima richiesta fallisce se il record non e' propagato.

## Comandi utili

Applica schema e seed minimo senza avviare lo stack completo:

```bash
./scripts/init.sh
```

Verifica API, Console e DB:

```bash
./scripts/healthcheck.sh
```

Esegue il test acceptance end-to-end (clone -> boot -> login -> teardown). Usato anche dalla CI:

```bash
./scripts/test-clone-to-boot.sh --source .
```

Valida la sintassi Compose:

```bash
docker compose config
```

## Bootstrap su Linux pulito (Ubuntu / Debian / Fedora / RHEL)

Lo script `core/scripts/setup-server.sh` prepara un server da zero: utente deploy, pacchetti base, Docker + plugin Compose, reverse proxy a scelta, firewall, tmux opzionale. Supporta `apt` (Debian/Ubuntu 22+) e `dnf` (Fedora 39+, Rocky/Alma/RHEL 9+).

Pattern generale:

```bash
cp ../../core/scripts/setup-server.example.env .env.setup
$EDITOR .env.setup
set -a; source .env.setup; set +a
MARVIS_DRY_RUN=1 bash ../../core/scripts/setup-server.sh   # anteprima senza modifiche
sudo -E bash ../../core/scripts/setup-server.sh             # esegui
```

### Esempio 1 — Ubuntu 22+ con Caddy + Let's Encrypt (default OSS)

Bootstrap pubblico con dominio reale e certificato automatico. Richiede record DNS A puntato al server prima dell'esecuzione.

```bash
export MARVIS_DOMAIN="marvis.example.com"
export MARVIS_EMAIL="ops@example.com"
export MARVIS_SSH_PUBLIC_KEY="ssh-ed25519 AAAA... user@host"
export MARVIS_PROXY_MODE="caddy"
export MARVIS_ENABLE_SYSTEMD_STACK=1
sudo -E bash ../../core/scripts/setup-server.sh
```

Prezzo: ottieni HTTPS valido automatico, ma porte 80/443 devono essere raggiungibili (no NAT cieco).

### Esempio 2 — Fedora 39+ con Cloudflare Tunnel

Esposizione via Cloudflare Tunnel, senza aprire 80/443 in ingresso.

```bash
export MARVIS_DOMAIN="marvis.example.com"
export MARVIS_CLOUDFLARE_TUNNEL_TOKEN="eyJh..."
export MARVIS_PROXY_MODE="cloudflare"
sudo -E bash ../../core/scripts/setup-server.sh
```

Prezzo: zero porte pubbliche, ma dipendi da Cloudflare; la creazione del token avviene nella loro dashboard.

### Esempio 3 — dev-local con cert self-signed

Setup single-machine per sviluppo o demo offline. Nessun DNS richiesto.

```bash
export MARVIS_PRESET=dev-local
sudo -E bash ../../core/scripts/setup-server.sh
```

Prezzo: browser warning sui cert, ma funziona anche dietro firewall aziendale o senza Internet outbound.

### Preset disponibili

| Preset | Use case | Env coerenti |
|---|---|---|
| `oss-default` | Deploy pubblico OSS | Caddy + LE, user `marvis`, systemd stack on |
| `dev-local` | Sviluppo single-machine | self-signed, `$HOME/marvis-dev`, no UFW |

I preset impostano i default ma rispettano qualunque env esplicita che hai gia' esportato.

### Configurazione Caddy

Il template canonico vive in `deploy/_template/caddy/Caddyfile.example` e usa la sintassi env-var nativa di Caddy (`{$PUBLIC_DOMAIN}`, `{$ACME_EMAIL}`, `{$MARVIS_API_UPSTREAM}`, `{$MARVIS_CONSOLE_UPSTREAM}`). Lo script `setup-server.sh` lo copia in `/etc/caddy/Caddyfile` e materializza `/etc/default/caddy` (consumato dal systemd unit) con i valori reali. Per personalizzare la config (header CORS, rate limit, virtual host extra) modifica direttamente il template prima dell'esecuzione.

### Backward compatibility

Senza preset e senza `MARVIS_PROXY_MODE` esplicito, lo script preserva il comportamento storico: usa Cloudflare Tunnel se `MARVIS_CLOUDFLARE_TUNNEL_TOKEN` e' valorizzato, altrimenti nessun proxy.

Prezzo: imposta firewall UFW di default; su host con firewall custom imposta `MARVIS_ENABLE_UFW=0` prima di eseguirlo.

## Security checklist

Prima di esporre l'istanza su Internet o di caricare dati reali:

- [ ] Cambia `PIR_PASSWORD` (init.sh fa hash bcrypt al primo avvio).
- [ ] Genera un `PIR_JWT_SECRET` reale: `openssl rand -hex 32`.
- [ ] Imposta `FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN=true` se condividi l'host.
- [ ] Aggiorna `CORS_ORIGINS_PROD` con i tuoi domini reali (formato JSON array).
- [ ] Sostituisci tutti i `<<API_KEY>>` / `<<DOMAIN>>` / `<<EMAIL>>` rimasti.
- [ ] Lockdown firewall a monte: con `tunnel` chiudi `80/443/3000/8100/8080` esterni; con `caddy` apri solo `80/443`.
- [ ] Backup periodico del volume `sqlite-data` (file `console.db`).
- [ ] Se attivi BYOK LLM, custodisci `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` fuori dal controllo versione.

## BYOK LLM

Per default lo stack non chiama LLM esterni (`PIR_VOYAGE_DISABLED=true`, parser ingest disattivati). Per attivare:

1. Anthropic (raccomandato): `ANTHROPIC_API_KEY=sk-ant-...` + `LLM_BASE_URL=https://api.anthropic.com/v1`.
2. OpenAI o gateway compatibile (LiteLLM, vLLM): `OPENAI_API_KEY=...` oppure `LLM_GATEWAY_BASE_URL=...` + `LLM_GATEWAY_API_KEY=...`.
3. Voyage embeddings (per semantic search): `VOYAGE_API_KEY=...` + `PIR_VOYAGE_DISABLED=false`.

Le chiavi vivono nel file `.env` locale. La template non ha vault integration: se vuoi gestione segreti centralizzata, monta `.env` da Vault/SOPS prima di `docker compose up`.

## Update procedure

```bash
cd marvis            # repository root
git fetch origin
git checkout main
git pull --ff-only
cd deploy/_template
docker compose pull   # aggiorna immagini base (caddy, nginx, cloudflared)
docker compose build --no-cache
docker compose up -d
```

Caveat: le migration SQLite sono applicate da `init.sh` all'avvio della API. Se vedi errori 500 dopo l'update, controlla `docker compose logs api` e in caso esegui manualmente `docker compose exec api python -m core.scripts.run_migrations`.

## Troubleshooting

### Porta gia' occupata

Errore: `Bind for 0.0.0.0:3000 failed: port is already allocated`.

Cambia in `.env`:

```bash
CONSOLE_PORT=3001   # o un'altra porta libera
API_PORT=8101
NGINX_PORT=8081
CADDY_HTTP_PORT=8080
CADDY_HTTPS_PORT=8443
```

Verifica chi occupa la porta con `ss -tlnp | grep 3000` (Linux) o `lsof -i :3000` (macOS).

### Migration fail al primo avvio

Errore: `OperationalError: table ... has no column named ...` nei log API.

Il volume `sqlite-data` contiene uno schema vecchio. Reset locale:

```bash
docker compose down -v   # cancella tutti i volumi locali
docker compose up --build
```

Prezzo: cancella dati. In produzione esegui invece `docker compose exec api python -m core.scripts.run_migrations` e leggi i log per capire quale migration manca.

### Container OOM (killed exit 137)

Il container Console (Next.js) richiede >= 1.5 GB durante il build. Se la VM ha 2 GB totali, il build puo' fallire con exit 137.

Mitigazioni:

- Aggiungi swap: `sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`.
- Builda l'immagine altrove (CI o macchina piu' grande) e fai `docker push`/`pull`.
- Pinna i limiti in `docker-compose.yml` aggiungendo `mem_limit: 1500m` al servizio `console` solo dopo aver verificato che il build passa fuori dal container.

### BYOK key mancante

Sintomo: l'API parte, ma le funzioni che richiamano LLM rispondono 503 o `provider unavailable`.

Controlla in `.env` di aver impostato almeno una chiave attiva (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` o `LLM_GATEWAY_*`) e che `PIR_VOYAGE_DISABLED=true` se non hai una chiave Voyage. Riavvia con `docker compose up -d --force-recreate api`.

### Caddy: certificate request failed

Sintomo nei log Caddy: `acme: error: 400 ... DNS problem`.

Causa: il DNS A/AAAA di `PUBLIC_DOMAIN` non punta a questo host (o la propagazione non e' completa).

Verifica con `dig +short marvis.example.com` da una rete esterna. Aspetta la propagazione (fino a 1 h con TTL standard) o usa `PUBLIC_DOMAIN=localhost` per Caddy locale.

### Tunnel Cloudflare: 502 o 530

Sintomo: il tunnel e' connesso ma la console resta 502/530.

Causa comune: `nginx` non e' health, quindi `cloudflared` non puo' raggiungerlo. Esegui `docker compose ps` e controlla che `nginx` sia in stato `healthy` prima di attivare `--profile tunnel`.

## Porte

- Console: `http://localhost:3000`
- API: `http://localhost:8100`
- nginx baseline: `http://localhost:8080`
- Caddy (profilo `caddy`): `http://localhost:80`, `https://localhost:443`

Login locale iniziale:

- utente: `admin`
- password: valore di `PIR_PASSWORD` in `.env`

## Dati persistenti

| Volume | Contenuto |
|---|---|
| `sqlite-data` | DB principale (`console.db`) e indici. Backup-target. |
| `workspace-data` | File mostrati dal Finder (progetti utente). |
| `runtime-data` | Stato sessioni CLI, identita' agenti. |
| `caddy-data` | Certificati Let's Encrypt emessi (solo profilo `caddy`). |
| `caddy-config` | Cache Caddy interna. |

Reset locale completo:

```bash
docker compose down -v
```

Prezzo: cancella database, workspace, runtime, certificati TLS e cache Caddy.
