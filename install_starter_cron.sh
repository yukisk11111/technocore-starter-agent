#!/bin/sh
set -eu
umask 077

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
begin="# >>> technocore-starter managed >>>"
end="# <<< technocore-starter managed <<<"
current="$(mktemp)"
clean="$(mktemp)"
trap 'rm -f "$current" "$clean"' EXIT

quote_for_cron() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

once_runner=$(quote_for_cron "$project_dir/run_starter_once.sh")
maintenance_runner=$(quote_for_cron "$project_dir/run_starter_maintenance.sh")

crontab -l >"$current" 2>/dev/null || true
awk -v begin="$begin" -v end="$end" '
  $0 == begin { skip = 1; next }
  $0 == end { skip = 0; next }
  !skip { print }
' "$current" >"$clean"

{
  printf '\n%s\n' "$begin"
  printf '* * * * * %s\n' "$once_runner"
  printf '23 3 * * * %s\n' "$maintenance_runner"
  printf '%s\n' "$end"
} >>"$clean"

crontab "$clean"
