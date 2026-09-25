#!/usr/bin/env bash
# Обёртка: python3 scripts.py project-apply ...  (подробности — ./scripts.py project-apply --help)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec "${PYTHON:-python3}" "$DIR/scripts.py" project-apply "$@"
