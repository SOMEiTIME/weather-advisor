#!/usr/bin/env bash
# Run the unit tests with the first Python 3.11+ found (tomllib needs 3.11).
set -euo pipefail
cd "$(dirname "$0")/.."

candidates=(python3 python3.15 python3.14 python3.13 python3.12 python3.11)
if command -v brew >/dev/null; then
  for v in 3.15 3.14 3.13 3.12 3.11; do
    candidates+=("$(brew --prefix)/opt/python@$v/bin/python$v")
  done
fi

for py in "${candidates[@]}"; do
  if command -v "$py" >/dev/null && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    exec "$py" -m unittest -v
  fi
done

echo "verify.sh: no Python 3.11+ found" >&2
exit 1
