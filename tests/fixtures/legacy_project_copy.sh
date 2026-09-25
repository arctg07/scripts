#!/bin/bash
#
# project-copy.sh — снимок проекта в набор текстовых файлов для переноса.
#
# Состав снимка:
#   - проект в git-репозитории: все файлы, которые видит git — отслеживаемые и
#     новые неигнорируемые, т.е. ровно то, что попало бы в коммит. build/,
#     .gradle/, .idea/ и локальные секреты отсекает сам .gitignore;
#   - проект без git: все файлы, кроме служебных каталогов сборки/IDE
#     (is_build_junk). Здесь .gitignore не работает — проверьте, не уедут ли
#     локальные секреты (.env, application-local.yml).
# Скрипты переноса, их папка export и локальные настройки Claude Code в снимок
# не входят (is_protected).
#
# Файлы кладутся в tar.gz как есть — с правами (gradlew остаётся исполняемым),
# кодировкой и переводами строк. Архив кодируется в base64 и режется на
# export/project-copy-<время>-part-NNN.txt не длиннее MAX_ENCODED_LINES строк.
#
# Бинарные файлы (есть NUL-байт: gradle-wrapper.jar, keystore, картинки) по
# умолчанию НЕ переносятся: в манифесте они помечаются KEEP, и project-apply.sh
# их не удаляет. Флаг --with-binaries включает их в снимок.
#
# Запуск из любой папки:  ./sh/project-copy.sh [--with-binaries]

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT_REL="${SCRIPT_DIR#"$PROJECT_DIR"/}"
OUTPUT_DIR="$SCRIPT_DIR/export"
MAX_ENCODED_LINES="${MAX_ENCODED_LINES:-7000}"
WITH_BINARIES=0

for arg in "$@"; do
    case "$arg" in
        --with-binaries) WITH_BINARIES=1 ;;
        *) echo "Ошибка: неизвестный аргумент: $arg" >&2; exit 1 ;;
    esac
done

# --- Общая часть с project-apply.sh: держать одинаковой в обоих скриптах ----

# Не переносятся и никогда не удаляются при восстановлении.
is_protected() {
    case "$1" in
        "$SCRIPT_REL/export/"*|"$SCRIPT_REL/project-copy.sh"|"$SCRIPT_REL/project-apply.sh") return 0 ;;
        .claude/settings.local.json|CLAUDE.local.md) return 0 ;;
    esac
    return 1
}

# Служебные файлы сборки/IDE; применяется только к проекту без git. Каталоги
# build/out/target отсекаются лишь вне src/ — внутри src/ это обычные пакеты.
is_build_junk() {
    local path="/$1"
    case "$path" in
        */.git/*|*/.gradle/*|*/.idea/*|*/.vscode/*|*/.settings/*|*/node_modules/*|*/.kotlin/*) return 0 ;;
        */.DS_Store|*.iml|*.class|*/.classpath|*/.project) return 0 ;;
    esac
    case "${path%%/src/*}/" in
        */build/*|*/out/*|*/target/*) return 0 ;;
    esac
    return 1
}

# git — если каталог внутри рабочего дерева git, иначе find.
detect_list_mode() {
    if [ "$(git -C "$1" rev-parse --is-inside-work-tree 2>/dev/null)" = "true" ]; then
        echo git
    else
        echo find
    fi
}

# Файлы проекта (пути от корня, по одному в строке, отсортированы для comm).
list_project_files() {
    local root="$1" mode="$2" path
    if [ "$mode" = git ]; then
        git -C "$root" -c core.quotepath=off ls-files --cached --others --exclude-standard
    else
        (cd "$root" && find . \( -name .git -o -name .gradle -o -name node_modules \) -prune \
            -o \( -type f -o -type l \) -print) | sed 's|^\./||'
    fi | while IFS= read -r path; do
        # git ls-files отдаёт и удалённые с диска файлы индекса, и вложенные репозитории.
        { [ -f "$root/$path" ] || [ -L "$root/$path" ]; } || continue
        is_protected "$path" && continue
        [ "$mode" = find ] && is_build_junk "$path" && continue
        printf '%s\n' "$path"
    done | LC_ALL=C sort -u
}

# bsdtar (macOS) пишет xattr и AppleDouble-файлы ._*, на которые ругается GNU tar.
TAR_FLAGS=()
if tar --version 2>/dev/null | grep -qi bsdtar; then
    TAR_FLAGS=(--no-xattrs --no-mac-metadata)
fi
export COPYFILE_DISABLE=1

# --- Конец общей части -------------------------------------------------------

# Бинарный — только при наличии NUL-байта. Пустые и текстовые файлы — не бинарные.
is_binary() {
    LC_ALL=C tr -d '\000' < "$1" | cmp -s - "$1" && return 1 || return 0
}

cd "$PROJECT_DIR" || exit 1
mkdir -p "$OUTPUT_DIR"
# Старые комплекты удаляются, иначе project-apply.sh увидит части разных снимков.
rm -f "$OUTPUT_DIR"/project-copy-*.txt

work_tmp=$(mktemp -d)
trap 'rm -rf "$work_tmp"' EXIT HUP INT TERM
stage="$work_tmp/stage"
mkdir -p "$stage/files"
MANIFEST="$stage/manifest.txt"
FILE_LIST="$work_tmp/files.txt"
SKIPPED="$work_tmp/skipped.txt"
: > "$FILE_LIST"
: > "$SKIPPED"

LIST_MODE=$(detect_list_mode "$PROJECT_DIR")
{
    echo "# project snapshot manifest"
    printf 'FORMAT\t2\n'
    printf 'SOURCE\t%s\n' "$LIST_MODE"
} > "$MANIFEST"

written=0
skipped=0
while IFS= read -r path; do
    if [ ! -L "$path" ] && [ "$WITH_BINARIES" -eq 0 ] && is_binary "$path"; then
        printf 'KEEP\t%s\n' "$path" >> "$MANIFEST"
        printf '%s\n' "$path" >> "$SKIPPED"
        skipped=$((skipped + 1))
        continue
    fi
    printf 'FILE\t%s\n' "$path" >> "$MANIFEST"
    printf '%s\n' "$path" >> "$FILE_LIST"
    written=$((written + 1))
done < <(list_project_files "$PROJECT_DIR" "$LIST_MODE")

if [ "$written" -eq 0 ]; then
    echo "Ошибка: не найдено ни одного файла для снимка (режим: $LIST_MODE)." >&2
    exit 1
fi

tar -cf - ${TAR_FLAGS[@]+"${TAR_FLAGS[@]}"} -T "$FILE_LIST" | tar -xf - -C "$stage/files" || {
    echo "Ошибка: не удалось собрать файлы проекта." >&2
    exit 1
}

timestamp=$(date '+%Y-%m-%d_%H-%M-%S')
snapshot_prefix="$OUTPUT_DIR/project-copy-$timestamp-part-"
archive_tmp="$work_tmp/snapshot.tar.gz"

(cd "$stage" && tar -czf "$archive_tmp" ${TAR_FLAGS[@]+"${TAR_FLAGS[@]}"} manifest.txt files) || {
    echo "Ошибка: не удалось создать архив снимка." >&2
    exit 1
}

base64 < "$archive_tmp" | fold -w 76 | awk -v max="$MAX_ENCODED_LINES" -v prefix="$snapshot_prefix" '
    (NR - 1) % max == 0 {
        if (out != "") close(out)
        out = sprintf("%s%03d.txt", prefix, int((NR - 1) / max) + 1)
    }
    { print > out }
' || {
    rm -f "${snapshot_prefix}"*.txt
    echo "Ошибка: не удалось закодировать и разделить снимок." >&2
    exit 1
}

encoded_parts=$(find "$OUTPUT_DIR" -maxdepth 1 -type f -name "project-copy-$timestamp-part-*.txt" | wc -l | tr -d ' ')

echo "----------------------------------------------------------------"
echo "Режим отбора файлов: $LIST_MODE"
[ "$LIST_MODE" = find ] && echo "WARN: проект не в git — проверьте, что в снимок не попали локальные секреты." >&2
if [ "$skipped" -gt 0 ]; then
    echo "Бинарные файлы НЕ перенесены ($skipped, для переноса — флаг --with-binaries):"
    sed 's/^/  /' "$SKIPPED"
fi
echo "Готово. Файлов в снимке: $written, пропущено бинарных: $skipped"
echo "Результат: ${snapshot_prefix}*.txt ($encoded_parts файлов, максимум $MAX_ENCODED_LINES строк в каждом)"
