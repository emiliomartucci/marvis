# shellcheck shell=bash
# core/scripts/lib/setup-server/hf-cache.sh
#
# Prepare the shared Hugging Face cache for the local Granite embedding model so
# hosted tenants (one systemd unit each, all running as the service user) reuse a
# single warmed copy instead of every tenant downloading it on first search.
#
# Why this is needed (discovered dogfooding a fresh hosted tenant): semantic
# search reported 'embedding_unavailable' because (a) the shared cache was empty,
# (b) tenants had no HF_HOME so each would fetch into ~/.cache, and (c) the tenant
# unit has ProtectSystem=strict so the cache was read-only and onnxruntime could
# not persist the optimized ONNX graph it writes on first load. provision_tenant.py
# fixes (b)+(c) per tenant (HF_HOME env + ReadWritePaths); this module owns (a):
# create the cache writable by the service user and optionally pre-download.
#
# Opt-in via MARVIS_WARM_EMBEDDING_MODEL=1 (default off) — general / dev-local
# runs should not pay a ~390MB download. The download is torch-free (huggingface
# snapshot only), single-shot, and non-fatal: if no python with huggingface_hub
# is available the runtime still fetches lazily once the cache is writable.

ensure_hf_cache() {
  mark_step "hf-cache"

  if [[ "${MARVIS_WARM_EMBEDDING_MODEL:-0}" != "1" ]]; then
    log "Embedding model warm-up disabled (set MARVIS_WARM_EMBEDDING_MODEL=1 on hosts that serve local embeddings)."
    return 0
  fi

  local cache="${MARVIS_HF_CACHE:-${MARVIS_BASE_DIR}/hf-cache}"
  local owner="${MARVIS_HF_CACHE_OWNER:-marvis}"
  local model_id="${MARVIS_GRANITE_MODEL_ID:-ibm-granite/granite-embedding-97m-multilingual-r2}"
  # Pin matches core/api/services/embedding_internal.py MODEL_REVISION so the
  # warmed snapshot is the exact one the runtime loads (no re-download).
  local revision="${MARVIS_GRANITE_REVISION:-835ad14087e140460703cf0fae09f97d469d65c2}"

  log "Preparing shared embedding cache at $cache (model=$model_id)"
  run_root mkdir -p "$cache"

  # The tenant units run as the service user and must be able to write the
  # optimized-graph sidecar into the cache. chown the tree to that user if it
  # exists; otherwise warn and leave the dir for the operator to re-own once the
  # service user is created (before provisioning tenants).
  local have_owner=0
  if is_dry_run; then
    have_owner=1
  elif id -u "$owner" >/dev/null 2>&1; then
    have_owner=1
  else
    warn "service user '$owner' does not exist yet; tenant units run as that user and need write access to $cache. Re-run with MARVIS_HF_CACHE_OWNER set, or chown $cache to the service user before provisioning tenants."
  fi

  # Pick a python with huggingface_hub: prefer the deploy venv, fall back to
  # system python3. No torch / onnxruntime needed — snapshot download only.
  local py="${MARVIS_BASE_DIR}/venv/bin/python"
  if [[ ! -x "$py" ]]; then
    py="$(command -v python3 || true)"
  fi
  if [[ -z "$py" ]]; then
    warn "no python found to warm the embedding model; the runtime will fetch it lazily on first search."
    [[ "$have_owner" == "1" ]] && run_root chown -R "$owner":"$owner" "$cache"
    return 0
  fi

  if is_dry_run; then
    print_command env HF_HOME="$cache" "$py" -c "snapshot_download($model_id @ $revision)"
    return 0
  fi

  if ! "$py" -c "import huggingface_hub" >/dev/null 2>&1; then
    warn "huggingface_hub not importable by $py; skipping warm-up (runtime fetches lazily). Install deps first, then re-run."
    [[ "$have_owner" == "1" ]] && run_root chown -R "$owner":"$owner" "$cache"
    return 0
  fi

  log "Downloading Granite ONNX graph + tokenizer (one-time, torch-free)…"
  if HF_HOME="$cache" HF_HUB_DISABLE_TELEMETRY=1 "$py" - "$model_id" "$revision" <<'PYEOF'
import sys
from huggingface_hub import snapshot_download

model_id, revision = sys.argv[1], sys.argv[2]
path = snapshot_download(
    model_id,
    revision=revision,
    allow_patterns=[
        "onnx/model.onnx",
        "onnx/model.onnx_data",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "config.json",
        "1_Pooling/config.json",
        "config_sentence_transformers.json",
    ],
)
print(f"warmed: {path}")
PYEOF
  then
    log "Embedding model warmed into $cache"
  else
    warn "embedding model warm-up failed (network/deps); the runtime will fetch it lazily on first search."
  fi

  # Re-own whatever was written so the service user can read it AND persist the
  # optimized-graph sidecar (root-written files would otherwise be unwritable).
  [[ "$have_owner" == "1" ]] && run_root chown -R "$owner":"$owner" "$cache"
}
