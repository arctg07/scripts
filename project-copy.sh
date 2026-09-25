#!/usr/bin/env bash
# Обёртка: python3 scripts.py project-copy ...  (подробности — ./scripts.py project-copy --help)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec "${PYTHON:-python3}" "$DIR/scripts.py" project-copy "$@"
