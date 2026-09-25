#!/usr/bin/env bash
# Обёртка: python3 scripts.py git-apply ...  (подробности — ./scripts.py git-apply --help)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec "${PYTHON:-python3}" "$DIR/scripts.py" git-apply "$@"
