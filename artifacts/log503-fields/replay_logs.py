"""Replay incident logs in isolation; never write production logs or channel data.

Run inside the deployed container:
  docker exec -i newapi-companions-switcher-1 python - < replay_logs.py
"""
import json
import os
import tempfile
from collections import Counter
from pathlib import Path

from src.core import LogWatcher


def main():
    lines = []
    for path in sorted(Path(os.getenv('NEWAPI_LOG_DIR', '/var/lib/newapi/logs')).glob('oneapi-*.log')):
        with path.open(errors='replace') as fh:
            for line in fh:
                if '2026/11/25 - 00:' not in line:
                    continue
                timestamp = line.split(' - ', 1)[1][:8]
                if '00:21:00' <= timestamp < '00:31:00':
                    lines.append(line)
    with tempfile.TemporaryDirectory() as directory:
        events = []
        watcher = LogWatcher(directory, lambda cid, detail, **context: events.append(cid))
        watcher.poll_once()
        Path(directory, 'oneapi-replay.log').write_text(''.join(lines))
        for _ in range(len(lines) // 5000 + 1):
            watcher.poll_once()
        counts = dict(Counter(events))
        assert counts == {75:41, 76:130}, counts
        print(json.dumps({'period_beijing':'2026-11-25 00:21:00–00:30:59',
                          'deduplicated_503_by_channel':counts,
                          'production_data_modified':False}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
