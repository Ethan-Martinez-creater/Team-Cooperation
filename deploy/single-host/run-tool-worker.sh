#!/bin/sh
set -eu

runtime_dir="/run/user/$(id -u)"
if [ ! -S "$runtime_dir/bus" ]; then
    echo "teamcoop user manager is not ready" >&2
    exit 1
fi
export XDG_RUNTIME_DIR="$runtime_dir"
exec /opt/team-cooperation/venv/bin/python -m coifesp_harness.tool_worker_main
