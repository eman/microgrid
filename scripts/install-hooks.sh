#!/usr/bin/env bash
# Install a pre-push hook that runs the local CI gate.
set -euo pipefail
cd "$(dirname "$0")/.."
hook="$(git rev-parse --git-path hooks)/pre-push"
cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
# Installed by scripts/install-hooks.sh. Bypass once with `git push --no-verify`.
# A push that changes only docs (docs/, Markdown, mkdocs.yml) runs the docs part of the
# gate; anything else runs all of it, then the gate of each example with its own
# (examples/*/scripts/ci_local.sh).
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
full() {
  "$root/scripts/ci_local.sh"
  for gate in "$root"/examples/*/scripts/ci_local.sh; do
    if [ -x "$gate" ]; then "$gate"; fi
  done
  exit 0
}
zero=0000000000000000000000000000000000000000
changed=""
while read -r _local_ref local_sha _remote_ref remote_sha; do
  [ "$local_sha" = "$zero" ] && continue  # a deleted branch: nothing to check
  if [ "$remote_sha" = "$zero" ]; then  # a new branch: against main
    base="$(git merge-base "$local_sha" origin/main 2>/dev/null || true)"
  else
    base="$remote_sha"
  fi
  if [ -z "$base" ]; then
    full
  fi
  changed+="$(git diff --name-only "$base" "$local_sha")"$'\n'
done
if [ -n "${changed//$'\n'/}" ] && ! grep -qvE '^(docs/.*|.*\.md|mkdocs\.yml)$' <<<"$(grep -v '^$' <<<"$changed")"; then
  exec "$root/scripts/ci_local.sh" --docs
fi
full
HOOK
chmod +x "$hook"
echo "installed $hook"
