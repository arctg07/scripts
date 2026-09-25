#!/bin/bash
#
# project-apply.sh — восстановление проекта из снимка project-copy.sh.
#
# 1) Распаковывает файлы снимка в проект: отсутствующие создаются, существующие
#    перезаписываются (права вроде исполняемого gradlew сохраняются).
# 2) Удаляет неактуальные файлы — те, что есть в проекте, но отсутствуют в
#    снимке (классы/ресурсы, удалённые на рабочем проекте). Кандидаты отбираются
#    тем же способом, что и в project-copy.sh: через git, если проект —
#    git-репозиторий, иначе обходом без служебных каталогов сборки/IDE. Не
#    удаляются: бинарные файлы, помеченные в снимке KEEP, сами скрипты переноса
#    с папкой export, настройки IDE (.idea, *.iml, .vscode) и локальные
#    настройки Claude Code. Опустевшие после
#    удаления каталоги убираются.
#    Если удалять предстоит подозрительно много файлов (снимок другого проекта,
#    не тот target), скрипт останавливается — продолжить можно с --force.
#
# Снимки старого формата (part_*.txt, манифест с ROOT) тоже принимаются; для них
# удаление, как и раньше, ограничено корнями из манифеста.
#
# Запуск из любой папки:  ./sh/project-apply.sh [--dry-run] [--force] [export] [target-dir]
#   --dry-run  показать, что будет создано, изменено и удалено, ничего не меняя
#   --force    удалять, даже если неактуальных файлов подозрительно много

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT_REL="${SCRIPT_DIR#"$PROJECT_DIR"/}"
CALLER_DIR="$PWD"
DRY_RUN=0
FORCE=0
positional=()

for arg in "$@"; do
    case "$arg" in
        -n|--dry-run) DRY_RUN=1 ;;
        --force) FORCE=1 ;;
        -*) echo "Ошибка: неизвестный аргумент: $arg" >&2; exit 1 ;;
        *) positional+=("$arg") ;;
    esac
done

resolve_path() {
    case "$1" in
        /*) printf '%s' "$1" ;;
        *) printf '%s' "$CALLER_DIR/$1" ;;
    esac
}

INPUT_DIR="$SCRIPT_DIR/export"
TARGET_DIR="$PROJECT_DIR"
[ "${#positional[@]}" -ge 1 ] && INPUT_DIR=$(resolve_path "${positional[0]}")
[ "${#positional[@]}" -ge 2 ] && TARGET_DIR=$(resolve_path "${positional[1]}")

if [ ! -d "$INPUT_DIR" ]; then
    echo "Ошибка: папка со снимком не найдена: $INPUT_DIR" >&2
    exit 1
fi
if [ ! -d "$TARGET_DIR" ]; then
    echo "Ошибка: целевой проект не найден: $TARGET_DIR" >&2
    exit 1
fi
TARGET_DIR="$(cd "$TARGET_DIR" && pwd)"

# --- Общая часть с project-copy.sh: держать одинаковой в обоих скриптах -----

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

work_tmp=$(mktemp -d)
trap 'rm -rf "$work_tmp"' EXIT HUP INT TERM

# --- 0. Распаковка снимка ----------------------------------------------------
# Текущий формат — последовательные base64-части с tar.gz внутри. Для обратной
# совместимости принимаются единый project-copy-*.txt и каталог с part_*.txt.
DATA_DIR="$INPUT_DIR"
parts=$(find "$INPUT_DIR" -maxdepth 1 -type f -name 'part_*.txt' | sort)
if [ -z "$parts" ]; then
    snapshots=$(find "$INPUT_DIR" -maxdepth 1 -type f -name 'project-copy-*.txt' | sort)
    if [ -z "$snapshots" ]; then
        echo "Ошибка: в $INPUT_DIR не найден project-copy-*.txt или part_*.txt" >&2
        exit 1
    fi
    snapshot_group_count=$(printf '%s\n' "$snapshots" | sed -E 's/-part-[0-9]+\.txt$/.txt/' | sort -u | grep -c .)
    if [ "$snapshot_group_count" -gt 1 ]; then
        echo "Ошибка: в $INPUT_DIR найдены части разных снимков; оставьте один комплект." >&2
        exit 1
    fi

    encoded_tmp="$work_tmp/snapshot.txt"
    archive_tmp="$work_tmp/snapshot.tar.gz"
    : > "$encoded_tmp"
    while IFS= read -r snapshot_part; do
        cat "$snapshot_part" >> "$encoded_tmp"
    done <<EOF
$snapshots
EOF

    # macOS использует base64 -D, GNU/Linux — base64 -d.
    if ! base64 -d < "$encoded_tmp" > "$archive_tmp" 2>/dev/null \
        && ! base64 -D < "$encoded_tmp" > "$archive_tmp" 2>/dev/null; then
        echo "Ошибка: части снимка не содержат корректный base64." >&2
        exit 1
    fi
    if ! tar -tzf "$archive_tmp" >/dev/null 2>&1; then
        echo "Ошибка: снимок повреждён, скопирован не полностью или отсутствует одна из частей." >&2
        exit 1
    fi
    DATA_DIR="$work_tmp/snapshot"
    mkdir -p "$DATA_DIR"
    tar -xzf "$archive_tmp" -C "$DATA_DIR" || { echo "Ошибка: не удалось распаковать снимок." >&2; exit 1; }
    rm -f "$archive_tmp" "$encoded_tmp"
    parts=$(find "$DATA_DIR" -maxdepth 1 -type f -name 'part_*.txt' | sort)
fi

MANIFEST="$DATA_DIR/manifest.txt"
if [ -f "$MANIFEST" ] && grep -qx "FORMAT	2" "$MANIFEST"; then
    FORMAT=2
elif [ -n "$parts" ]; then
    FORMAT=1
else
    echo "Ошибка: внутри снимка нет ни manifest.txt формата 2, ни part_*.txt" >&2
    exit 1
fi

[ "$DRY_RUN" -eq 1 ] && echo "DRY RUN: изменения не применяются."

EXPECTED="$work_tmp/expected.txt"
SCOPE="$work_tmp/scope.txt"
DOOMED="$work_tmp/doomed.txt"
created=0
updated=0
unchanged=0

is_exec() { [ -x "$1" ] && echo 1 || echo 0; }

# Приводит пути к Unicode NFC. tar на macOS распаковывает не-ASCII имена в NFD, и
# без нормализации "файл й.txt" из снимка не совпал бы побайтно с только что
# записанным файлом — и тот ушёл бы в удаление. Нормализация лишь сужает список
# удаляемого: при отсутствии iconv UTF-8-MAC и perl пути сравниваются как есть.
normalize_paths() {
    if iconv -f UTF-8-MAC -t UTF-8 </dev/null >/dev/null 2>&1; then
        iconv -f UTF-8-MAC -t UTF-8
    elif perl -MUnicode::Normalize -e 1 2>/dev/null; then
        perl -MUnicode::Normalize -CS -pe '$_ = NFC($_)'
    else
        cat
    fi | LC_ALL=C sort -u
}

# Настройки IDE переносятся, если есть в снимке, но локально никогда не удаляются:
# у рабочего и домашнего проекта они свои, даже когда .idea лежит в git.
is_local_ide_file() {
    case "/$1" in
        */.idea/*|*.iml|*/.vscode/*) return 0 ;;
    esac
    return 1
}

# Неактуальные файлы: есть в target (в пределах области отбора), но нет в снимке.
compute_doomed() {
    : > "$DOOMED"
    if [ "$FORMAT" -eq 2 ]; then
        TARGET_MODE=$(detect_list_mode "$TARGET_DIR")
        list_project_files "$TARGET_DIR" "$TARGET_MODE" | normalize_paths > "$SCOPE"
    elif [ -f "$MANIFEST" ]; then
        TARGET_MODE="roots"
        while IFS=$'\t' read -r tag root filter; do
            [ "$tag" = "ROOT" ] || continue
            [ -d "$TARGET_DIR/$root" ] || continue
            (cd "$TARGET_DIR" && find "$root" -type f -name "$filter")
        done < "$MANIFEST" | normalize_paths > "$SCOPE"
    else
        return
    fi
    LC_ALL=C comm -23 "$SCOPE" "$EXPECTED" | while IFS= read -r path; do
        is_local_ide_file "$path" || printf '%s\n' "$path"
    done > "$DOOMED"
}

: > "$EXPECTED"
[ -f "$MANIFEST" ] && grep -E '^(FILE|KEEP)	' "$MANIFEST" | cut -f2- | normalize_paths > "$EXPECTED"
[ -f "$MANIFEST" ] || echo "WARN: manifest.txt не найден — шаг удаления неактуальных файлов пропущен." >&2
TARGET_MODE="-"

# --- 1. Проверка до изменений ------------------------------------------------
# Порог: больше 20 файлов и больше пятой части снимка — похоже на снимок другого
# проекта или не тот target. Проверяется до записи, чтобы отказ ничего не менял.
compute_doomed
doomed_count=$(wc -l < "$DOOMED" | tr -d ' ')
expected_count=$(wc -l < "$EXPECTED" | tr -d ' ')
limit=$((expected_count / 5))
[ "$limit" -lt 20 ] && limit=20
if [ "$doomed_count" -gt "$limit" ] && [ "$FORCE" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    echo "Ошибка: к удалению $doomed_count файлов при $expected_count в снимке — подозрительно много." >&2
    echo "Ничего не изменено. Проверьте список (--dry-run) и повторите с --force. Первые файлы:" >&2
    sed 's/^/  /' "$DOOMED" | head -n 30 >&2
    exit 1
fi

# --- 2. Запись файлов (create/overwrite) ------------------------------------
if [ "$FORMAT" -eq 2 ]; then
    grep '^FILE	' "$MANIFEST" | cut -f2- > "$work_tmp/files.txt"
    if [ ! -s "$work_tmp/files.txt" ]; then
        echo "Ошибка: в манифесте снимка нет ни одного файла." >&2
        exit 1
    fi

    while IFS= read -r path; do
        src="$DATA_DIR/files/$path"
        dst="$TARGET_DIR/$path"
        state="UPDATED"
        if [ ! -e "$dst" ] && [ ! -L "$dst" ]; then
            state="NEW"
        elif [ -L "$src" ] || [ -L "$dst" ]; then
            [ -L "$src" ] && [ -L "$dst" ] && [ "$(readlink "$src")" = "$(readlink "$dst")" ] && state=""
        elif cmp -s "$src" "$dst" && [ "$(is_exec "$src")" = "$(is_exec "$dst")" ]; then
            state=""
        fi
        case "$state" in
            NEW) created=$((created + 1)) ;;
            UPDATED) updated=$((updated + 1)) ;;
            *) unchanged=$((unchanged + 1)) ;;
        esac
        [ "$DRY_RUN" -eq 1 ] && [ -n "$state" ] && echo "$state: $path"
    done < "$work_tmp/files.txt"

    if [ "$DRY_RUN" -eq 0 ]; then
        # Переносятся только файлы по списку: каталог-корень "." в архив не попадает,
        # иначе tar переписал бы права и mtime самого target.
        (cd "$DATA_DIR/files" && tar -cf - ${TAR_FLAGS[@]+"${TAR_FLAGS[@]}"} -T "$work_tmp/files.txt") \
            | tar -xf - -C "$TARGET_DIR" || {
            echo "Ошибка: не удалось записать файлы в $TARGET_DIR" >&2
            exit 1
        }
    fi
else
    # Формат 1: блоки "FILE: путь" ... "/-------------/" внутри part_*.txt.
    # awk печатает строки "NEW: путь" (для dry-run) и последней — "COUNT создано записано".
    awk_out="$work_tmp/legacy.txt"
    awk -v target="$TARGET_DIR" -v dry="$DRY_RUN" '
        function flush() {
            if (path != "") {
                if (n > 0 && buf[n] == "") { n-- }   # снять один пустой ряд перед разделителем
                full = target "/" path
                if ((getline probe < full) < 0) { created++; print "NEW: " path } else { existing++ }
                close(full)
                if (dry != 1) {
                    dir = full
                    sub(/\/[^\/]*$/, "", dir)
                    system("mkdir -p \"" dir "\"")
                    printf "" > full
                    for (i = 1; i <= n; i++) { print buf[i] > full }
                    close(full)
                }
            }
            path = ""; n = 0; collecting = 0; skipblank = 0
        }
        /^FILE: / {
            flush()
            path = substr($0, 7)
            collecting = 1; skipblank = 1
            next
        }
        {
            if (collecting) {
                if ($0 == "/-------------/") { flush(); next }
                if (skipblank == 1 && $0 == "") { skipblank = 0; next }
                skipblank = 0
                buf[++n] = $0
            }
        }
        END { flush(); print "COUNT", created + 0, existing + 0 }
    ' $parts > "$awk_out"
    [ "$DRY_RUN" -eq 1 ] && grep '^NEW: ' "$awk_out"
    created=$(awk '$1 == "COUNT" { print $2 }' "$awk_out")
    updated=$(awk '$1 == "COUNT" { print $3 }' "$awk_out")
fi

# --- 3. Удаление неактуальных файлов ----------------------------------------
# После записи список пересчитывается: снимок мог принести новый .gitignore.
[ "$DRY_RUN" -eq 0 ] && compute_doomed

deleted=0
while IFS= read -r path; do
    [ -n "$path" ] || continue
    deleted=$((deleted + 1))
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DELETE: $path"
        continue
    fi
    rm -f "$TARGET_DIR/$path"
    echo "DELETED: $path"
    # Подчистить каталоги, которые опустели после удаления (пакеты удалённых классов).
    dir=$(dirname "$path")
    while [ "$dir" != "." ] && rmdir "$TARGET_DIR/$dir" 2>/dev/null; do
        dir=$(dirname "$dir")
    done
done < "$DOOMED"

if [ "$FORMAT" -eq 2 ] && grep -qx 'gradle/wrapper/gradle-wrapper.properties' "$work_tmp/files.txt" \
    && ! grep -qx 'gradle/wrapper/gradle-wrapper.jar' "$work_tmp/files.txt" \
    && [ ! -f "$TARGET_DIR/gradle/wrapper/gradle-wrapper.jar" ]; then
    echo "WARN: нет gradle/wrapper/gradle-wrapper.jar (бинарный, не перенесён) — выполните 'gradle wrapper'" \
        "или снимите снимок с --with-binaries." >&2
fi

echo "----------------------------------------------------------------"
[ "$DRY_RUN" -eq 1 ] && echo "DRY RUN — ничего не изменено."
if [ "$FORMAT" -eq 2 ]; then
    echo "Готово. Создано: $created, изменено: $updated, без изменений: $unchanged, удалено неактуальных: $deleted (отбор: $TARGET_MODE, target: $TARGET_DIR)"
else
    echo "Готово (старый формат снимка). Создано: $created, перезаписано: $updated, удалено неактуальных: $deleted (target: $TARGET_DIR)"
fi
