#!/usr/bin/env bash
# Обёртка: python3 scripts.py inbox ...  (подробности — ./scripts.py inbox --help)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec "${PYTHON:-python3}" "$DIR/scripts.py" inbox "$@"
