#!/bin/bash
# install.sh — idempotent installer for the Xahau Muse skill.
#
# What it does:
#   1. Checks for python3 (>= 3.9).
#   2. Makes sure the Python requirements are importable:
#        - already satisfied  -> skip quietly
#        - pip works          -> python3 -m pip install -r requirements.txt
#        - PEP 668 system     -> private venv at ~/.xahau/venv, wrappers in
#                                ~/.local/bin that use it (never
#                                --break-system-packages)
#   3. Exposes xahau, xahau-payload, xmerch in ~/.local/bin — as symlinks
#      when the system python works, as venv wrappers otherwise. Never
#      overwrites a file or symlink it did not create.
#   4. Runs the test suite as a self-check.
#
# Safe to re-run: every step detects existing state and skips cleanly.
# The installer never touches ~/.xahau/config.json or policy.json — your
# wallet config and spending policy are created only by `xahau setup` and
# `xahau init-policy`, by your hand.
set -u

SRC="$(cd "$(dirname "$0")" && pwd)"
BIN_DST="$HOME/.local/bin"
VENV="$HOME/.xahau/venv"
MARKER="# installed by xahau-muse-skill install.sh"
PASS=0
FAIL=0

say() { printf '%s\n' "$*"; }
die() { say "ERROR: $*" >&2; exit 1; }

say "== Xahau Muse skill installer =="
say "   source: $SRC"
say ""

# ---- 1. python3 -----------------------------------------------------------
say "-- checking python3"
command -v python3 >/dev/null 2>&1 || die "python3 not found. Install Python 3.9+ (https://www.python.org/downloads/) and re-run."
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    die "python3 is $PYVER; this skill needs Python 3.9 or newer."
fi
say "   ok: python3 $PYVER"

# ---- 2. requirements ------------------------------------------------------
req_ok() {
    # $1 = python binary to check (system python3, or the venv's)
    local py="${1:-python3}"
    "$py" - <<'EOF'
import sys
try:
    import requests
    from requests import __version__ as rv
    ok_req = tuple(int(x) for x in rv.split(".")[:2]) >= (2, 31)
except Exception:
    ok_req = False
try:
    import xrpl  # noqa: F401
    ok_xrpl = True
except Exception:
    ok_xrpl = False
sys.exit(0 if (ok_req and ok_xrpl) else 1)
EOF
}

PYBIN="python3"   # python that will run the skill; may become the venv's
CHECKPY="python3"
[ -x "$VENV/bin/python" ] && CHECKPY="$VENV/bin/python"
say "-- checking requirements (requests>=2.31, xrpl-py==5.2.0)"
if req_ok "$CHECKPY"; then
    say "   ok: already importable, nothing to install"
    PYBIN="$CHECKPY"
else
    say "   not satisfied — installing"
    python3 -m pip --version >/dev/null 2>&1 || die "pip is missing for python3. Install it (https://pip.pypa.io/en/stable/installation/) and re-run."
    if python3 -m pip install -r "$SRC/requirements.txt" >/tmp/xahau-pip.log 2>&1; then
        say "   ok: installed with pip"
    elif grep -q "externally-managed-environment" /tmp/xahau-pip.log; then
        say "   system python is externally managed (PEP 668)."
        say "   creating a private venv at $VENV (system python left untouched)"
        python3 -m venv "$VENV" 2>/dev/null || die "could not create a venv (Debian/Ubuntu: sudo apt install python3-venv) and re-run."
        "$VENV/bin/pip" install -r "$SRC/requirements.txt" || die "venv pip install failed."
        PYBIN="$VENV/bin/python"
        say "   ok: requirements live in the private venv"
    else
        say "--- pip output ---" >&2
        cat /tmp/xahau-pip.log >&2
        die "pip install failed."
    fi
fi

# ---- 3. expose the commands -----------------------------------------------
say "-- wiring up commands in $BIN_DST"
mkdir -p "$BIN_DST"
chmod +x "$SRC"/bin/xahau "$SRC"/bin/xahau-payload "$SRC"/bin/xmerch

# Ours to manage? Missing, our symlink, or our wrapper (marker line).
ours() { # $1 = dst, $2 = src
    local dst="$1" src="$2"
    [ -e "$dst" ] || [ -L "$dst" ] || return 0
    if [ -L "$dst" ]; then
        [ "$(readlink "$dst")" = "$src" ] && return 0 || return 1
    fi
    # regular file: ours if it carries our marker (shebang is line 1,
    # the marker is line 2 in wrappers we wrote)
    head -n 2 "$dst" 2>/dev/null | grep -qF "$MARKER" && return 0 || return 1
}

for cmd in xahau xahau-payload xmerch; do
    src="$SRC/bin/$cmd"
    dst="$BIN_DST/$cmd"
    if ! ours "$dst" "$src"; then
        if [ -L "$dst" ]; then
            say "   SKIP $dst is a symlink to $(readlink "$dst") — left untouched"
        else
            say "   SKIP $dst exists as a real file (not ours) — left untouched"
        fi
        continue
    fi
    if [ "$PYBIN" = "python3" ]; then
        ln -sf "$src" "$dst"
        say "   LINK $dst -> $src"
    else
        { printf '#!/bin/bash\n%s\n' "$MARKER"
          printf 'exec "%s" "%s" "$@"\n' "$PYBIN" "$src"; } > "$dst"
        chmod +x "$dst"
        say "   WRAPPER $dst (uses private venv python)"
    fi
done
case ":$PATH:" in
    *":$BIN_DST:"*) say "   ok: $BIN_DST is on your PATH" ;;
    *) say "   NOTE: $BIN_DST is not on your PATH yet."
       say "   Add this line to your shell profile (~/.bashrc, ~/.zshrc):"
       say "       export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# ---- 4. self-check ---------------------------------------------------------
say ""
say "-- self-check: running test suite"
for t in "$SRC"/tests/test_*.py; do
    name="$(basename "$t")"
    if "$PYBIN" "$t" >/tmp/xahau-install-test.log 2>&1; then
        say "   PASS $name"
        PASS=$((PASS + 1))
    else
        say "   FAIL $name (last lines):"
        tail -5 /tmp/xahau-install-test.log | sed 's/^/        /'
        FAIL=$((FAIL + 1))
    fi
done

say ""
[ "$FAIL" -gt 0 ] && die "self-check: $PASS passed, $FAIL failed. Do not use this install until it is green."
say "self-check: all $PASS suites passed."
say ""
say "Installed. Next steps:"
say "  1. xahau setup --address rYOURADDRESS --network xahau-testnet   # start on testnet"
say "  2. xahau init-policy   # then edit ~/.xahau/policy.json (allowlist destinations)"
say "  3. See QUICKSTART.md for your first testnet payment."
say "Or tell your Muse: 'set up my Xahau wallet on testnet'"
