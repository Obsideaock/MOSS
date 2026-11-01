#!/usr/bin/env bash
# MOSS one-line installer for Raspberry Pi OS, other Linux, and macOS.
#
#   curl -sSL https://raw.githubusercontent.com/Obsideaock/MOSS/main/install.sh | bash
#
# Testing the experimental branch instead:
#   curl -sSL https://raw.githubusercontent.com/Obsideaock/MOSS/experimental/install.sh | MOSS_BRANCH=experimental bash
#
# What it does, and it's safe to run again (that's also how to repair an install):
#   1. installs git and Python's venv module if they're missing (Debian/Pi: apt)
#   2. gets MOSS into ~/MOSS, or updates the copy that's already there
#      (an existing ~/Documents/MOSS checkout is used in place, not duplicated)
#   3. makes a Python environment in MOSS/.venv and installs MOSS into it
#   4. adds a `moss` command (~/.local/bin/moss)
#   5. runs `moss setup`, which asks whether this is a reader or a Seed Vault
#
# It never deletes your files: edits in an existing checkout are set aside
# with `git stash`, and vault keys, identities and settings in ~/.moss and
# /etc/moss are left alone.
#
# Options (environment variables): MOSS_DIR, MOSS_BRANCH (main|experimental),
# MOSS_ROLE (reader|vault), MOSS_REPO (a different git URL), MOSS_NO_SETUP=1.

# Everything is inside a function that runs on the last line, so a download
# cut short can't run half an installer.
main() {
set -euo pipefail

REPO_URL="${MOSS_REPO:-https://github.com/Obsideaock/MOSS.git}"
BRANCH="${MOSS_BRANCH:-main}"
green=$'\033[32m'; yellow=$'\033[33m'; red=$'\033[31m'; bold=$'\033[1m'; off=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$green" "$off" "$*"; }
warn() { printf '%s!%s %s\n' "$yellow" "$off" "$*"; }
die()  { printf '%s✗%s %s\n' "$red" "$off" "$*" >&2; exit 1; }

printf '\n%sMOSS installer%s  (branch %s)\n\n' "$bold" "$off" "$BRANCH"

case "$BRANCH" in main|experimental) ;; *) die "MOSS_BRANCH must be main or experimental";; esac
[[ $(id -u) -eq 0 ]] && die "run this as your normal user, not with sudo (it asks for sudo itself when needed)"

# ---- 1. git and Python
need_apt=()
command -v git >/dev/null 2>&1 || need_apt+=(git)
if ! command -v python3 >/dev/null 2>&1; then
  need_apt+=(python3 python3-venv python3-pip)
elif ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
  need_apt+=(python3-venv python3-pip)
fi
if ((${#need_apt[@]})); then
  if command -v apt-get >/dev/null 2>&1; then
    echo "installing ${need_apt[*]} (sudo may ask for your password)..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq "${need_apt[@]}"
  elif [[ "$(uname)" == "Darwin" ]]; then
    die "please install ${need_apt[*]} first (for example: brew install git python), then run this again"
  else
    die "please install ${need_apt[*]} with your package manager, then run this again"
  fi
fi
python3 - <<'PY' || die "MOSS needs Python 3.10 or newer"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
ok "git and $(python3 --version) ready"

# ---- 2. the code
is_moss_checkout() { [[ -d "$1/.git" && -f "$1/moss/__init__.py" ]]; }
if [[ -n "${MOSS_DIR:-}" ]]; then
  DIR="$MOSS_DIR"
elif is_moss_checkout "$HOME/MOSS"; then
  DIR="$HOME/MOSS"
elif is_moss_checkout "$HOME/Documents/MOSS"; then
  DIR="$HOME/Documents/MOSS"
else
  DIR="$HOME/MOSS"
fi

if [[ -e "$DIR" ]] && ! is_moss_checkout "$DIR"; then
  if [[ -z "$(ls -A "$DIR" 2>/dev/null)" ]]; then
    rmdir "$DIR"
  else
    die "$DIR exists but isn't a MOSS git checkout. Move it aside (or set MOSS_DIR) and run this again."
  fi
fi

if [[ -d "$DIR" ]]; then
  echo "updating the MOSS already in $DIR..."
  cd "$DIR"
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "$REPO_URL"
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    git stash push --quiet -m "set aside by the MOSS installer $(date '+%Y-%m-%d %H:%M')"
    warn "your own edits in $DIR are saved with git stash (get them back: cd $DIR && git stash pop)"
  fi
  git fetch --quiet origin "$BRANCH"
  if git rev-parse --verify --quiet "$BRANCH" >/dev/null; then
    git checkout --quiet "$BRANCH"
  else
    git checkout --quiet -b "$BRANCH" --track "origin/$BRANCH"
  fi
  if ! git merge --ff-only --quiet "origin/$BRANCH"; then
    die "this copy has commits GitHub doesn't, so it can't simply move forward. Push them or sort it out with git, then run this again. Nothing was changed."
  fi
else
  echo "downloading MOSS into $DIR..."
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$DIR"
  cd "$DIR"
fi
ok "MOSS code in $DIR ($(git log -1 --format='%h, %cd' --date=short))"

# ---- 3. Python environment
if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c 'import sys' >/dev/null 2>&1; then
  rm -rf .venv
  python3 -m venv .venv
fi
echo "installing Python packages (the first time takes a few minutes on a Pi)..."
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e .
ok "Python packages installed"

# ---- 4. the moss command
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
ln -sf "$DIR/.venv/bin/moss" "$BIN/moss"
case ":$PATH:" in
  *":$BIN:"*) ok "the moss command is ready" ;;
  *)
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
      [[ -f "$rc" ]] || [[ "$rc" == "$HOME/.profile" ]] || continue
      grep -qs 'MOSS: add ~/.local/bin' "$rc" || \
        printf '\n# MOSS: add ~/.local/bin to PATH\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
    done
    ok "the moss command is ready (open a new terminal for it to work everywhere)"
    ;;
esac

# Serial ports on Linux need the dialout group (takes effect after logging in again).
if [[ "$(uname)" == "Linux" ]] && getent group dialout >/dev/null && ! id -nG | grep -qw dialout; then
  if sudo usermod -aG dialout "$USER" 2>/dev/null; then
    warn "added you to the dialout group for the radio; log out and back in (or reboot) if the radio says permission denied"
  else
    warn "couldn't add you to the dialout group; if the radio says permission denied, run: sudo usermod -aG dialout $USER"
  fi
fi

# ---- 5. setup
if [[ "${MOSS_NO_SETUP:-}" == "1" ]]; then
  ok "done. Run  moss setup  when you're ready."
  return 0
fi
setup_args=(setup --branch "$BRANCH")
[[ -n "${MOSS_ROLE:-}" ]] && setup_args+=(--role "$MOSS_ROLE")
echo
# Piped from curl, stdin is the script itself; answer questions from the keyboard.
if [[ -t 0 ]]; then
  "$DIR/.venv/bin/moss" "${setup_args[@]}"
elif (exec </dev/tty) 2>/dev/null; then
  "$DIR/.venv/bin/moss" "${setup_args[@]}" </dev/tty
else
  "$DIR/.venv/bin/moss" "${setup_args[@]}" --yes
fi
}

main "$@"
