#!/usr/bin/env sh
# Writes .env with freshly generated inter-container tokens. Safe to re-run: it
# refuses to overwrite an existing .env.
set -eu

cd "$(dirname "$0")/.."

if [ -e .env ]; then
  echo ".env already exists, leaving it alone." >&2
  echo "Delete it first if you want a new one." >&2
  exit 1
fi

gen() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

cat > .env <<ENVEOF
HERMES_RUNNER_TOKEN=$(gen)
HERMES_LLM_ROUTER_TOKEN=$(gen)
USE_TUNA_MIRROR=${USE_TUNA_MIRROR:-1}
ENVEOF

chmod 600 .env
echo "Wrote .env with generated tokens."
echo "Next: docker compose up -d, then read the generated access codes with:"
echo "  docker compose logs hermes | grep 'generated credentials' -A 4"
