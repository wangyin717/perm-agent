#!/bin/bash
# Permanent installer. Usage:
#   curl -fsSL https://raw.githubusercontent.com/wangyin717/perm-agent/main/install.sh | bash
#   curl … | bash -s -- --ref v0.5.0
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
SKIP_BROWSER_USE=false

while [ $# -gt 0 ]; do
  case "$1" in
    --ref|--version)
      REF="${2:-}"
      shift 2
      ;;
    --no-browser-use)
      SKIP_BROWSER_USE=true
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Permanent installer

  --ref TAG     git tag (default: newest vX.Y.Z)
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

SPIN_ON=0
SPIN_MSG=""

spin_begin() {
  SPIN_MSG="$1"
  if printf '◆ %s' "$SPIN_MSG" 2>/dev/null >/dev/tty; then
    SPIN_ON=1
  else
    SPIN_ON=0
    printf '→ %s\n' "$SPIN_MSG"
  fi
}

spin_mark() {
  [ "$SPIN_ON" = 1 ] || return 0
  printf '\r%s %s' "$1" "$SPIN_MSG" 2>/dev/null >/dev/tty || SPIN_ON=0
}

spin_end() {
  [ "$SPIN_ON" = 1 ] || return 0
  printf '\r→ %s   \n' "$SPIN_MSG" 2>/dev/null >/dev/tty || printf '\n'
  SPIN_ON=0
}

# 长命令的输出先收进日志。同一行上闪一个菱形，表示还在进行。
run_logged() {
  local msg="$1"
  shift
  local logf pid rc=0 on=1
  logf="$(mktemp)"
  spin_begin "$msg"
  "$@" >"$logf" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$on" = 1 ]; then
      spin_mark "◆"
      on=0
    else
      spin_mark "◇"
      on=1
    fi
    sleep 0.5
  done
  spin_end
  wait "$pid" || rc=$?
  if [ "$rc" -ne 0 ]; then
    tail -n 20 "$logf" | sed 's/^/    /' >&2
  fi
  rm -f "$logf"
  return "$rc"
}

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
  local installer
  installer="$(mktemp)"
  log "downloading uv"
  if ! curl -fL --progress-bar -S https://astral.sh/uv/install.sh -o "$installer"; then
    rm -f "$installer"
    die "could not download uv installer"
  fi
  if ! run_logged "installing uv" env UV_UNMANAGED_INSTALL="$UV_DIR" sh "$installer"; then
    rm -f "$installer"
    die "uv install failed"
  fi
  rm -f "$installer"
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
    log "updating Permanent"
    git -C "$SRC" remote set-url origin "$REPO_URL" 2>/dev/null || true
    git -C "$SRC" fetch --tags --progress origin
  else
    if [ -e "$SRC" ] && [ ! -d "$SRC/.git" ]; then
      die "$SRC exists and is not a git checkout"
    fi
    log "downloading Permanent"
    git clone --progress "$REPO_URL" "$SRC"
  fi
  if git -C "$SRC" rev-parse "refs/tags/$target" >/dev/null 2>&1; then
    git -C "$SRC" checkout -f --detach "refs/tags/$target"
  else
    git -C "$SRC" checkout -f --detach "$target"
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
  # 只盖官方同名技能和这两个插件的说明。用户自己加的目录留着。
  # computer-use/tools.md 是运行时写的，这里不碰。
  local bundled="$SRC/agent_loop/bundled_skills"
  local plugs="$SRC/agent_loop/plugins"
  mkdir -p "$PERMANENT_HOME/skills" "$PERMANENT_HOME/plugins"
  local d name skills="" plugins=""
  log "copying skills and plugins"
  if [ -d "$bundled" ]; then
    for d in "$bundled"/*/; do
      [ -f "$d/SKILL.md" ] || continue
      name="$(basename "$d")"
      mkdir -p "$PERMANENT_HOME/skills/$name"
      cp -R "$d"/. "$PERMANENT_HOME/skills/$name"/
      skills="${skills:+$skills, }$name"
    done
  fi
  if [ -d "$plugs" ]; then
    for d in "$plugs"/*/; do
      [ -f "$d/plugin.json" ] || continue
      name="$(basename "$d")"
      mkdir -p "$PERMANENT_HOME/plugins/$name"
      [ -f "$d/SKILL.md" ] && cp "$d/SKILL.md" "$PERMANENT_HOME/plugins/$name/SKILL.md"
      cp "$d/plugin.json" "$PERMANENT_HOME/plugins/$name/plugin.json"
      plugins="${plugins:+$plugins, }$name"
    done
  fi
  ok "skills: ${skills:-none}"
  ok "plugins: ${plugins:-none}"
}

install_browser_use() {
  if [ "$SKIP_BROWSER_USE" = true ]; then
    log "skip browser-use CLI"
    return 0
  fi
  log "browser tools are next (this can take a few minutes)"
  mkdir -p "$TOOL_DIR" "$PYTHON_DIR" "$UV_DIR"
  if run_logged "downloading Python 3.12" \
    with_uv_home "$UV_DIR/uv" python install 3.12; then
    ok "Python 3.12"
  else
    log "Python 3.12 download did not finish; browser tools will try again"
  fi
  if ! run_logged "installing browser tools" \
    with_uv_home "$UV_DIR/uv" tool install --python 3.12 --upgrade browser-use; then
    log "browser-use CLI failed; perm still works. retry: $UV_DIR/uv tool install --python 3.12 browser-use"
    return 0
  fi
  if [ -x "$UV_DIR/browser-use" ]; then
    mkdir -p "$LINK_DIR"
    ln -sf "$UV_DIR/browser-use" "$LINK_DIR/browser-use"
  fi
  ok "browser-use CLI ($TOOL_DIR)"
}

sync_packages() {
  (cd "$SRC" && with_uv_home "$UV_DIR/uv" sync --frozen)
}

printf '\nPermanent installer\n'
printf 'This usually takes a few minutes. Python packages and browser tools are the slow parts.\n\n'
need_cmd curl
ensure_git
if [ -z "$REF" ]; then
  log "looking up the latest release"
  REF="$(latest_tag || true)"
fi
if [ -z "$REF" ]; then
  die "no vX.Y.Z tags on $REPO_URL — pass --ref main to install a branch"
fi
ok "release $REF"
ensure_uv
checkout_src "$REF"
if run_logged "downloading Python $PYTHON_VERSION" \
  with_uv_home "$UV_DIR/uv" python install "$PYTHON_VERSION"; then
  ok "Python $PYTHON_VERSION"
else
  log "Python download did not finish; package install will try again"
fi
run_logged "installing Python packages (this can take a few minutes)" sync_packages
ok "Python packages"
copy_skills
install_browser_use
write_wrapper
ensure_path
printf '\n✓ done. this shell does not pick up PATH yet; run:\n'
printf '  export PATH="%s:$PATH"\n' "$LINK_DIR"
printf '  perm\n'
printf '  (or: source ~/.bashrc)\n'
printf '  later: perm update\n\n'
