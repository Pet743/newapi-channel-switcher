#!/bin/sh
set -eu
TARGET=${1:?用法: ROLLBACK.sh <MODIFIED_FILE>}
BASELINE=${TARGET}.baseline
if [ ! -f "$BASELINE" ]; then
  echo "缺少基线文件: $BASELINE" >&2
  exit 2
fi
cp "$BASELINE" "$TARGET"
echo "restored=$TARGET"
