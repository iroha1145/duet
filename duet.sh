#!/usr/bin/env bash
# Run from a trusted checkout as root or the original installation user.
# Root installs use a dedicated low privilege service account. Never enable tracing.
set -euo pipefail
umask 077
command -v python3 >/dev/null || { printf '需要 Python 3.11+\n' >&2; exit 2; }
exec python3 "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/scripts/manage.py" "$@"
