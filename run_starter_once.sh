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
timeout_bin=${TIMEOUT_BIN:-"$(command -v timeout)"}
memory_kb=${TECHNOCORE_MEMORY_KB:-393216}
runtime_seconds=${TECHNOCORE_RUNTIME_SECONDS:-240}
max_log_bytes=${TECHNOCORE_MAX_LOG_BYTES:-1048576}

for value in "$memory_kb" "$runtime_seconds" "$max_log_bytes"; do
  case "$value" in
    ''|*[!0-9]*)
      echo "resource limits must be positive integers" >&2
      exit 2
      ;;
  esac
  if [ "$value" -lt 1 ]; then
    echo "resource limits must be positive integers" >&2
    exit 2
  fi
done

mkdir -p "$state_dir"
chmod 700 "$state_dir"
log_file="$state_dir/starter-cron.log"
: >>"$log_file"
chmod 600 "$log_file"

exec 9>"$state_dir/starter-cron.lock"
"$flock_bin" -n 9 || exit 0

log_size=$(wc -c <"$log_file")
if [ "$log_size" -ge "$max_log_bytes" ]; then
  mv -f "$log_file" "$log_file.1"
  : >"$log_file"
  chmod 600 "$log_file"
fi

cd "$project_dir"
ulimit -v "$memory_kb"
exec "$timeout_bin" --signal=TERM --kill-after=5s "${runtime_seconds}s" \
  "$python_bin" "$project_dir/starter_agent.py" serve --once --quiet \
  >>"$log_file" 2>&1
