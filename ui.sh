#!/usr/bin/env bash
# Обёртка: python3 scripts.py ui ...  (подробности — ./scripts.py ui --help)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec "${PYTHON:-python3}" "$DIR/scripts.py" ui "$@"
