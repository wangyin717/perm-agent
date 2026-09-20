#!/bin/bash
# Permanent installer. Usage:
#   curl -fsSL https://raw.githubusercontent.com/wangyin717/spark-agent/main/install.sh | bash
#   curl … | bash -s -- --ref v0.1.0
#   curl … | bash -s -- --non-interactive
set -euo pipefail

REPO_URL="${PERMANENT_REPO:-https://github.com/wangyin717/spark-agent.git}"
PERMANENT_HOME="${PERMANENT_HOME:-$HOME/.permanent}"
SRC="$PERMANENT_HOME/src"
UV_DIR="$PERMANENT_HOME/bin"
LINK_DIR="${PERMANENT_LINK_DIR:-$HOME/.local/bin}"
PYTHON_VERSION="3.11"
REF=""
NON_INTERACTIVE=false

if [ -t 0 ]; then
  IS_INTERACTIVE=true
else
  IS_INTERACTIVE=false
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --ref|--version)
      REF="${2:-}"
      shift 2
      ;;
    --non-interactive)
      NON_INTERACTIVE=true
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Permanent installer

  --ref TAG     git tag (default: newest vX.Y.Z)
  --non-interactive
                do not prompt for DEEPSEEK_API_KEY
  -h, --help

Data stays in $PERMANENT_HOME (default ~/.permanent). Re-running updates
src + venv and leaves chats, memory, and .env alone.
EOF
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 1
      ;;
  esac
done

log() { printf '→ %s\n' "$1"; }
ok() { printf '✓ %s\n' "$1"; }
die() { printf '✗ %s\n' "$1" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "need $1 on PATH"
}

latest_tag() {
  git ls-remote --tags --refs "$REPO_URL" 2>/dev/null \
    | sed 's#.*refs/tags/##' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
    | sort -t. -k1.2,1n -k2,2n -k3,3n \
    | tail -1
}

ensure_git() {
  if command -v git >/dev/null 2>&1 && git --version >/dev/null 2>&1; then
    ok "git $(git --version | awk '{print $3}')"
    return 0
  fi
  die "git not found. On macOS: xcode-select --install"
}

ensure_uv() {
  mkdir -p "$UV_DIR"
  if [ -x "$UV_DIR/uv" ]; then
    ok "uv $($UV_DIR/uv --version 2>/dev/null | awk '{print $2}')"
    return 0
  fi
  need_cmd curl
  log "installing uv into $UV_DIR"
  local installer logf
  installer="$(mktemp)"
  logf="$(mktemp)"
  if ! curl -LsSf https://astral.sh/uv/install.sh -o "$installer"; then
    rm -f "$installer" "$logf"
    die "could not download uv installer"
  fi
  if ! UV_UNMANAGED_INSTALL="$UV_DIR" sh "$installer" >"$logf" 2>&1; then
    sed 's/^/    /' "$logf" >&2
    rm -f "$installer" "$logf"
    die "uv install failed"
  fi
  rm -f "$installer" "$logf"
  [ -x "$UV_DIR/uv" ] || die "uv installer ran but $UV_DIR/uv is missing"
  ok "uv $($UV_DIR/uv --version | awk '{print $2}')"
}

checkout_src() {
  local target="$1"
  mkdir -p "$PERMANENT_HOME"
  chmod 700 "$PERMANENT_HOME" 2>/dev/null || true
  if [ -d "$SRC/.git" ]; then
    log "updating $SRC"
    git -C "$SRC" remote set-url origin "$REPO_URL" 2>/dev/null || true
    git -C "$SRC" fetch --tags origin
  else
    if [ -e "$SRC" ] && [ ! -d "$SRC/.git" ]; then
      die "$SRC exists and is not a git checkout"
    fi
    log "cloning $REPO_URL"
    git clone "$REPO_URL" "$SRC"
  fi
  if git -C "$SRC" rev-parse "refs/tags/$target" >/dev/null 2>&1; then
    git -C "$SRC" checkout --detach "refs/tags/$target"
  else
    git -C "$SRC" checkout --detach "$target"
  fi
  ok "code at $target"
}

write_wrapper() {
  mkdir -p "$LINK_DIR"
  cat >"$LINK_DIR/perm" <<EOF
#!/bin/sh
export PERMANENT_HOME="$PERMANENT_HOME"
exec "$SRC/.venv/bin/perm" "\$@"
EOF
  chmod 755 "$LINK_DIR/perm"
  cp "$LINK_DIR/perm" "$LINK_DIR/permanent"
  chmod 755 "$LINK_DIR/permanent"
  ok "command $LINK_DIR/perm"
}

ensure_path() {
  case ":$PATH:" in
    *":$LINK_DIR:"*) return 0 ;;
  esac
  local rc=""
  if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = zsh ]; then
    rc="$HOME/.zshrc"
  else
    rc="$HOME/.bashrc"
  fi
  local marker="# permanent-cli"
  if [ -f "$rc" ] && grep -q "$marker" "$rc" 2>/dev/null; then
    return 0
  fi
  printf '\n%s\nexport PATH="%s:$PATH"\n' "$marker" "$LINK_DIR" >>"$rc"
  ok "added $LINK_DIR to PATH in $rc (open a new terminal)"
}

prompt_key() {
  if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
    return 0
  fi
  local envfile="$PERMANENT_HOME/.env"
  if [ -f "$envfile" ] && grep -q '^DEEPSEEK_API_KEY=.' "$envfile"; then
    return 0
  fi
  if [ "$NON_INTERACTIVE" = true ]; then
    log "no DEEPSEEK_API_KEY; set it in $envfile"
    return 0
  fi
  if [ ! -r /dev/tty ] || [ ! -w /dev/tty ]; then
    log "no TTY; set DEEPSEEK_API_KEY in $envfile"
    return 0
  fi
  printf "DEEPSEEK_API_KEY: " >/dev/tty
  local key=""
  IFS= read -r key </dev/tty || true
  if [ -z "$key" ]; then
    log "skipped; put DEEPSEEK_API_KEY in $envfile later"
    return 0
  fi
  mkdir -p "$PERMANENT_HOME"
  if [ -f "$envfile" ]; then
    grep -v '^DEEPSEEK_API_KEY=' "$envfile" >"$envfile.tmp" || true
    mv "$envfile.tmp" "$envfile"
  fi
  printf 'DEEPSEEK_API_KEY=%s\n' "$key" >>"$envfile"
  chmod 600 "$envfile"
  ok "wrote $envfile"
}

printf '\nPermanent installer\n\n'
need_cmd curl
ensure_git
if [ -z "$REF" ]; then
  REF="$(latest_tag || true)"
fi
if [ -z "$REF" ]; then
  die "no vX.Y.Z tags on $REPO_URL — pass --ref main to install a branch"
fi
log "ref $REF"
ensure_uv
checkout_src "$REF"
log "uv sync"
(cd "$SRC" && "$UV_DIR/uv" python install "$PYTHON_VERSION" >/dev/null 2>&1 || true)
(cd "$SRC" && "$UV_DIR/uv" sync)
write_wrapper
ensure_path
prompt_key
printf '\n✓ done. run: perm\n'
printf '  update later: perm update\n\n'
