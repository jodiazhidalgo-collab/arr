#!/bin/sh
set -eu

event_dir="${ARR_EVENT_DIR:-/data/torrents/events/inbox/qbt}"
mkdir -p "$event_dir"

stamp="$(date +%s)"
tmp="$event_dir/.qbt-${stamp}-$$.tmp"
dst="$event_dir/qbt-${stamp}-$$.event"

printf 'hash=%s\n' "${1:-}" > "$tmp"
mv "$tmp" "$dst"
