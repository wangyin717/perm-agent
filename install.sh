#!/bin/bash
# Permanent installer. Usage:
#   curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash
#   curl … | bash -s -- --ref v0.4.0
#   curl … | bash -s -- --non-interactive
set -euo pipefail

REPO_URL="${PERMANENT_REPO:-https://github.com/wangyin717/perm-agent.git}"
PERMANENT_HOME="${PERMANENT_HOME:-$HOME/.permanent}"
SRC="$PERMANENT_HOME/src"
UV_DIR="$PERMANENT_HOME/bin"
LINK_DIR="${PERMANENT_LINK_DIR:-$HOME/.local/bin}"
TOOL_DIR="$PERMANENT_HOME/uv-tools"
PYTHON_DIR="$PERMANENT_HOME/python"
PYTHON_VERSION="3.11"
REF=""
NON_INTERACTIVE=false
SKIP_BROWSER_USE=false

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
    --no-browser-use)
      SKIP_BROWSER_USE=true
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Permanent installer

  --ref TAG     git tag (default: newest vX.Y.Z)
  --non-interactive
                do not prompt for DEEPSEEK_API_KEY
  --no-browser-use
                skip installing the browser-use CLI
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

with_uv_home() {
  UV_TOOL_DIR="$TOOL_DIR" \
    UV_TOOL_BIN_DIR="$UV_DIR" \
    UV_PYTHON_INSTALL_DIR="$PYTHON_DIR" \
    "$@"
}

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
  if [ -d "$PERMANENT_HOME/tools" ] && [ ! -e "$TOOL_DIR" ]; then
    mv "$PERMANENT_HOME/tools" "$TOOL_DIR"
  fi
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
export PATH="$PERMANENT_HOME/bin:\$PATH"
export UV_TOOL_DIR="$PERMANENT_HOME/uv-tools"
export UV_TOOL_BIN_DIR="$PERMANENT_HOME/bin"
export UV_PYTHON_INSTALL_DIR="$PERMANENT_HOME/python"
exec "$SRC/.venv/bin/perm" "\$@"
EOF
  chmod 755 "$LINK_DIR/perm"
  cp "$LINK_DIR/perm" "$LINK_DIR/permanent"
  chmod 755 "$LINK_DIR/permanent"
  ok "command $LINK_DIR/perm"
}

_append_path_rc() {
  local rc="$1"
  local marker="# permanent-cli"
  if [ -f "$rc" ] && grep -q "$marker" "$rc" 2>/dev/null; then
    return 0
  fi
  printf '\n%s\nexport PATH="%s:$PATH"\n' "$marker" "$LINK_DIR" >>"$rc"
}

ensure_path() {
  _append_path_rc "$HOME/.zshrc"
  _append_path_rc "$HOME/.bashrc"
  _append_path_rc "$HOME/.profile"
  case ":$PATH:" in
    *":$LINK_DIR:"*) return 0 ;;
  esac
  ok "added $LINK_DIR to PATH in ~/.zshrc, ~/.bashrc, and ~/.profile"
}

copy_skills() {
  local src="$SRC/agent_loop/bundled_skills"
  local dest="$PERMANENT_HOME/skills"
  mkdir -p "$dest"
  if [ ! -d "$src" ]; then
    return 0
  fi
  local d name
  for d in "$src"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    if [ -f "$dest/$name/SKILL.md" ] \
        && ! grep -qi placeholder "$dest/$name/SKILL.md"; then
      continue
    fi
    rm -rf "$dest/$name"
    cp -R "$d" "$dest/$name"
  done
  ok "skills $dest"
}

install_browser_use() {
  if [ "$SKIP_BROWSER_USE" = true ]; then
    log "skip browser-use CLI"
    return 0
  fi
  log "browser-use CLI (Python 3.12)"
  mkdir -p "$TOOL_DIR" "$PYTHON_DIR" "$UV_DIR"
  with_uv_home "$UV_DIR/uv" python install 3.12 >/dev/null 2>&1 || true
  if ! with_uv_home "$UV_DIR/uv" tool install --python 3.12 --upgrade browser-use; then
    log "browser-use CLI failed; perm still works. retry: $UV_DIR/uv tool install --python 3.12 browser-use"
    return 0
  fi
  if [ -x "$UV_DIR/browser-use" ]; then
    mkdir -p "$LINK_DIR"
    ln -sf "$UV_DIR/browser-use" "$LINK_DIR/browser-use"
  fi
  ok "browser-use CLI ($TOOL_DIR)"
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
(cd "$SRC" && with_uv_home "$UV_DIR/uv" python install "$PYTHON_VERSION" >/dev/null 2>&1 || true)
(cd "$SRC" && with_uv_home "$UV_DIR/uv" sync)
copy_skills
install_browser_use
write_wrapper
ensure_path
prompt_key
printf '\n✓ done. this shell does not pick up PATH yet; run:\n'
printf '  export PATH="%s:$PATH"\n' "$LINK_DIR"
printf '  perm\n'
printf '  (or: source ~/.bashrc)\n'
printf '  later: perm update\n\n'
