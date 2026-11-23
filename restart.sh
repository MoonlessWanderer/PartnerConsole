#!/usr/bin/env bash
set -euo pipefail
PP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$PP_ROOT/stop.sh"
bash "$PP_ROOT/start.sh"
