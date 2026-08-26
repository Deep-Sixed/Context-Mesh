#!/usr/bin/env bash
# Fail if anything private has got into the public repository.
#
# "Bleached" should be a property you can check, not a claim someone made once.
# This runs over tracked file content *and* commit metadata, because the leaks
# that matter here have historically been in metadata: a session URL in a commit
# message, a personal address in an author field.
#
#   ops/leak-check.sh              # check the whole history
#   ops/leak-check.sh origin/main  # check only what is new against a base
set -uo pipefail

BASE="${1:-}"
RANGE=""
[ -n "$BASE" ] && RANGE="$BASE..HEAD"
FAILED=0

say()  { printf '%s\n' "$*"; }
fail() { printf '  ✗ %s\n' "$*"; FAILED=1; }
pass() { printf '  ✓ %s\n' "$*"; }

# ── 1. secrets and personal data in tracked file content ────────────────
say "content"
CONTENT_PATTERNS=(
  'BEGIN [A-Z ]*PRIVATE KEY'
  'ghp_[A-Za-z0-9]{20,}'
  'github_pat_[A-Za-z0-9_]{20,}'
  'sk-[A-Za-z0-9]{20,}'
  'xox[baprs]-[A-Za-z0-9-]{10,}'
  'AKIA[0-9A-Z]{16}'
  '-----BEGIN OPENSSH PRIVATE KEY-----'
)
for pat in "${CONTENT_PATTERNS[@]}"; do
  if git grep -nIE "$pat" -- . >/dev/null 2>&1; then
    fail "secret-shaped string matching /$pat/"
    git grep -nIE "$pat" -- . | head -3 | sed 's/^/      /'
  fi
done

# Any email that is not the Anthropic no-reply used for co-authorship.
if git grep -nIE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' -- . 2>/dev/null \
     | grep -v 'noreply@anthropic.com' | grep -q .; then
  fail "email address in tracked content"
  git grep -nIE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' -- . \
    | grep -v 'noreply@anthropic.com' | head -3 | sed 's/^/      /'
else
  pass "no email addresses in tracked content"
fi

# Machine-local and internal paths. An internal directory layout is itself
# private information, so these do not belong in a public tree.
if git grep -nIE '/mnt/[a-z]|/home/[a-z]+/|/Users/[A-Za-z]+/|[A-Z]:\\\\Users' -- . 2>/dev/null | grep -q .; then
  fail "machine-local or internal path in tracked content"
  git grep -nIE '/mnt/[a-z]|/home/[a-z]+/|/Users/[A-Za-z]+/' -- . | head -3 | sed 's/^/      /'
else
  pass "no machine-local or internal paths"
fi

# Private Claude links: session URLs are private, artifact URLs are per-user.
if git grep -nIE 'claude\.ai/code/(session|artifact)' -- . 2>/dev/null | grep -q .; then
  fail "private claude.ai session or artifact URL in tracked content"
  git grep -nIE 'claude\.ai/code/(session|artifact)' -- . | head -3 | sed 's/^/      /'
else
  pass "no private claude.ai URLs in tracked content"
fi

# ── 2. commit metadata ──────────────────────────────────────────────────
say "commit metadata${RANGE:+ ($RANGE)}"

# shellcheck disable=SC2086
MSGS="$(git log $RANGE --format='%H%n%B' 2>/dev/null)"
if printf '%s' "$MSGS" | grep -qiE 'claude\.ai/code/(session|artifact)'; then
  fail "private claude.ai URL in a commit message"
  printf '%s' "$MSGS" | grep -iE 'claude\.ai/code/(session|artifact)' | head -3 | sed 's/^/      /'
else
  pass "no private claude.ai URLs in commit messages"
fi

# shellcheck disable=SC2086
AUTHORS="$(git log $RANGE --format='%ae%n%ce' 2>/dev/null | sort -u | grep -v '^$')"
# The whole address is matched, not the domain: the Anthropic no-reply has
# "noreply" as its local part, while GitHub's has it inside the domain.
ALLOWED='^(noreply@anthropic\.com|noreply@github\.com|.+@users\.noreply\.github\.com)$'
UNEXPECTED="$(printf '%s\n' "$AUTHORS" | grep -vE "$ALLOWED" || true)"
if [ -n "$UNEXPECTED" ]; then
  fail "author or committer address that is not a no-reply:"
  printf '%s\n' "$UNEXPECTED" | sed 's/^/      /'
  printf '      set a no-reply identity, or accept these as intentionally public\n'
else
  pass "every author and committer uses a no-reply address"
fi

say ""
if [ "$FAILED" -eq 0 ]; then
  say "clean"
else
  say "LEAKS FOUND — do not publish until these are resolved"
fi
exit "$FAILED"
