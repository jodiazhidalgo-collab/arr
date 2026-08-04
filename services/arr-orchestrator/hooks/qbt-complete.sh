#!/bin/sh
set -eu

category="${2:-}"
case "$category" in
    [Hh][Oo][Ss][Pp][Ii][Tt][Aa][Ll]) exit 0 ;;
esac

event_dir="${ARR_EVENT_DIR:-/data/torrents/events/inbox/qbt}"
mkdir -p "$event_dir"

stamp="$(date +%s)"
tmp="$event_dir/.qbt-${stamp}-$$.tmp"
dst="$event_dir/qbt-${stamp}-$$.event"

printf 'hash=%s\ncategory=%s\n' "${1:-}" "$category" > "$tmp"
mv "$tmp" "$dst"
