#!/bin/sh
set -eu
umask 077

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
state_dir=${TECHNOCORE_STATE_DIR:-"$project_dir/.technocore"}
if [ -n "${PYTHON_BIN:-}" ]; then
  python_bin=$PYTHON_BIN
elif [ -x "$project_dir/.venv/bin/python" ]; then
  python_bin="$project_dir/.venv/bin/python"
else
  python_bin=$(command -v python3)
fi
flock_bin=${FLOCK_BIN:-"$(command -v flock)"}

mkdir -p "$state_dir"
chmod 700 "$state_dir"
: >>"$state_dir/starter-cron.log"
chmod 600 "$state_dir/starter-cron.log"

cd "$project_dir"
exec "$flock_bin" -n "$state_dir/starter-cron.lock" \
  "$python_bin" "$project_dir/starter_agent.py" serve --once --quiet \
  >>"$state_dir/starter-cron.log" 2>&1
