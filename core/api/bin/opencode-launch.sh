#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:?target directory required}"
shift || true

RUNTIME_HOME="${HOME:-$(getent passwd "$(id -un)" | cut -d: -f6)}"
RUNTIME_PATH_PREFIX="${RUNTIME_HOME}/.local/bin:${RUNTIME_HOME}/bin:${RUNTIME_HOME}/.npm-global/bin:${RUNTIME_HOME}/.opencode/bin"
OPENCODE_RUNTIME_CONFIG_DIR="$(cd "${SCRIPT_DIR}/../opencode-runtime" && pwd)"

export HOME="$RUNTIME_HOME"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$RUNTIME_HOME/.config}"
export PATH="$RUNTIME_PATH_PREFIX:${PATH:-}"
export COLORTERM="${COLORTERM:-truecolor}"

if [[ -d "$OPENCODE_RUNTIME_CONFIG_DIR" ]]; then
  export OPENCODE_CONFIG_DIR="${OPENCODE_CONFIG_DIR:-$OPENCODE_RUNTIME_CONFIG_DIR}"
fi

if [[ -f "$OPENCODE_RUNTIME_CONFIG_DIR/tui.json" ]]; then
  export OPENCODE_TUI_CONFIG="${OPENCODE_TUI_CONFIG:-$OPENCODE_RUNTIME_CONFIG_DIR/tui.json}"
fi

load_key_from_file() {
  local key="$1"
  local file="$2"

  [[ -n "${!key:-}" ]] && return 0
  [[ -f "$file" ]] || return 0

  local line
  line="$(grep -E "^${key}=" "$file" | head -n 1 || true)"
  [[ -n "$line" ]] || return 0

  export "${key}=${line#*=}"
}

for file in /data/pir/.env "${RUNTIME_HOME}/workspace/.env" "${RUNTIME_HOME}/openclaw/.env"; do
  load_key_from_file GROQ_API_KEY "$file"
  load_key_from_file OPENAI_API_KEY "$file"
  load_key_from_file GOOGLE_API_KEY "$file"
  load_key_from_file GOOGLE_GENERATIVE_AI_API_KEY "$file"
  load_key_from_file GEMINI_API_KEY "$file"
  load_key_from_file TASKS_API_TOKEN "$file"
  load_key_from_file TASKS_API_URL "$file"
  load_key_from_file PIR_API_URL "$file"
  load_key_from_file N8N_API_URL "$file"
  load_key_from_file N8N_API_KEY "$file"
  load_key_from_file EXA_API_KEY "$file"
  load_key_from_file GOOGLE_OAUTH_CLIENT_ID "$file"
  load_key_from_file GOOGLE_OAUTH_CLIENT_SECRET "$file"
done

if [[ -z "${GOOGLE_GENERATIVE_AI_API_KEY:-}" ]]; then
  if [[ -n "${GEMINI_API_KEY:-}" ]]; then
    export GOOGLE_GENERATIVE_AI_API_KEY="${GEMINI_API_KEY}"
  elif [[ -n "${GOOGLE_API_KEY:-}" ]]; then
    export GOOGLE_GENERATIVE_AI_API_KEY="${GOOGLE_API_KEY}"
  fi
fi

if [[ -z "${GEMINI_API_KEY:-}" && -n "${GOOGLE_GENERATIVE_AI_API_KEY:-}" ]]; then
  export GEMINI_API_KEY="${GOOGLE_GENERATIVE_AI_API_KEY}"
fi

if [[ -z "${PIR_API_URL:-}" && -n "${TASKS_API_URL:-}" ]]; then
  export PIR_API_URL="${TASKS_API_URL}"
fi

if [[ -z "${PIR_API_URL:-}" ]]; then
  export PIR_API_URL="http://127.0.0.1:8100"
fi

cd "$TARGET_DIR"
exec opencode "$@"
