#!/bin/sh
# Casharks SQL Backup launcher. Set SQLBACKUP_PYTHON to force a specific interpreter.
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" exec "${SQLBACKUP_PYTHON:-python3}" -m fivem_sqlbackup "$@"
