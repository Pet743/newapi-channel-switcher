#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "${1:-}" = "--deployment" ]; then
    PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
    cd "$PROJECT_DIR"
    docker compose stop newapi-channel-switcher
    for relative in src/core.py src/store.py src/app.py static/index.html README.md tests/test_core.py; do
        cp "$SCRIPT_DIR/original/$relative" "$PROJECT_DIR/$relative"
    done
    rm -f tests/test_error_rule.py
    # 保留线上数据库和新增列，旧版本会忽略新列，避免覆盖后续配置与事件。
    docker image tag newapi-channel-switcher:before-503-threshold-20261114 newapi-channel-switcher-newapi-channel-switcher
    docker compose up -d --no-build --force-recreate newapi-channel-switcher
    printf '%s\n' 'ROLLBACK deployment restored: previous image and source; current database retained'
else
    TARGET_DIR=${1:?Usage: ROLLBACK.sh /absolute/path/to/test-copy | --deployment}
    mkdir -p "$TARGET_DIR"
    cp -R "$SCRIPT_DIR/original/src" "$TARGET_DIR/"
    cp -R "$SCRIPT_DIR/original/static" "$TARGET_DIR/"
    cp -R "$SCRIPT_DIR/original/tests" "$TARGET_DIR/"
    cp "$SCRIPT_DIR/original/README.md" "$TARGET_DIR/"
    rm -f "$TARGET_DIR/tests/test_error_rule.py"
    printf '%s\n' 'ROLLBACK copy restored: original source and immediate-on-503 behavior'
fi
