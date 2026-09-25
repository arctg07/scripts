#!/usr/bin/env bash
#
# Reverse of git-copy.sh: reads sh/export/changed_classes.txt and applies it to the
# current project — creates/overwrites every FILE: block (java, liquibase xml, yml, ...)
# and removes every path listed in the trailing DELETED FILES section.
#
# Run from anywhere:  ./sh/git-apply.sh  [path/to/changed_classes.txt]

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CALLER_DIR="$PWD"

if [[ $# -gt 0 ]]; then
    case "$1" in
        /*) INPUT="$1" ;;
        *) INPUT="$CALLER_DIR/$1" ;;
    esac
else
    INPUT="$SCRIPT_DIR/export/changed_classes.txt"
fi

cd "$PROJECT_DIR"
SEP_RE='^=+$'

if [[ ! -f "$INPUT" ]]; then
    echo "ERROR: input file not found: $INPUT" >&2
    exit 1
fi

# Read the whole file into an array (bash 3.2 compatible, preserves every line).
lines=()
while IFS= read -r line || [[ -n "$line" ]]; do
    lines+=("$line")
done < "$INPUT"
n=${#lines[@]}

mode="none"          # none | file | deleted
curfile=""
content=()
written=0
deleted=0
skipped=0

flush_file() {
    [[ -z "$curfile" ]] && return
    # Drop the trailing blank lines that git-copy.sh appends after each block.
    local end=${#content[@]}
    while (( end > 0 )) && [[ -z "${content[end-1]}" ]]; do
        end=$((end - 1))
    done
    mkdir -p "$(dirname "$curfile")"
    if (( end == 0 )); then
        : > "$curfile"
    else
        printf '%s\n' "${content[@]:0:end}" > "$curfile"
    fi
    echo "WROTE:   $curfile"
    written=$((written + 1))
    content=()
    curfile=""
}

i=0
while (( i < n )); do
    line="${lines[i]}"
    nxt="${lines[i+1]:-}"
    nxt2="${lines[i+2]:-}"

    if [[ "$line" =~ $SEP_RE ]]; then
        if [[ "$nxt" == FILE:\ * && "$nxt2" =~ $SEP_RE ]]; then
            flush_file
            curfile="${nxt#FILE: }"
            mode="file"
            i=$((i + 3))
            continue
        elif [[ "$nxt" == "DELETED FILES" && "$nxt2" =~ $SEP_RE ]]; then
            flush_file
            mode="deleted"
            i=$((i + 3))
            continue
        fi
    fi

    if [[ "$mode" == "file" ]]; then
        content+=("$line")
    elif [[ "$mode" == "deleted" ]]; then
        if [[ -n "$line" ]]; then
            if [[ -e "$line" ]]; then
                rm -f "$line"
                echo "DELETED: $line"
                deleted=$((deleted + 1))
            else
                echo "ABSENT:  $line (already missing, skipped)"
                skipped=$((skipped + 1))
            fi
        fi
    fi
    i=$((i + 1))
done

flush_file

echo "----------------------------------------------------------------"
echo "Done. written=$written deleted=$deleted skipped=$skipped"
