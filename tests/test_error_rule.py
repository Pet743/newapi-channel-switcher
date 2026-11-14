import base64
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.app import Handler
from src.core import CandidateResult, Channel, LogWatcher, MemoryRepository, SwitchEngine
from src.store import ConfigStore


class TestErrorRule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ConfigStore(str(self.root / 'config.db'))
        self.addCleanup(self.store.conn.close)
        self.repo = MemoryRepository([Channel(i, str(i), 1 if i % 2 else 2, str((i-1)//2), 1, 1) for i in range(1, 5)])
        self.calls, self.events = [], []
        def tester(channel):
            self.calls.append(channel.id)
            return CandidateResult(channel.id, 200, 1, True, 'ok')
        self.engine = SwitchEngine(self.repo, self.store, tester, self.events.append)
        self.gid = self.make_group('first', 1, 2)

    def make_group(self, name, active, backup):
        g = self.store.create_group(name, name)
        self.store.replace_candidates(g['id'], [{'channel_id': active, 'priority': 10}, {'channel_id': backup, 'priority': 1}])
        self.store.set_current(g['id'], active)
        self.store.update_group(g['id'], enabled=True)
        return g['id']

    def test_third_error_triggers_and_does_not_cooldown_early(self):
        for t in (100, 102):
            self.assertEqual(self.engine.handle_503(1, '503', occurred_at=t), [])
            self.assertEqual(self.calls, [])
            self.assertNotIn(1, self.engine._failed_until)
        self.engine.handle_503(1, '503', occurred_at=105)
        self.assertEqual(self.calls, [2])
        self.assertEqual(self.store.get_group(self.gid)['current_channel_id'], 2)
        self.assertEqual([e['error_count'] for e in self.events if e['type'] == '503'], [1, 2, 3])
        self.assertNotIn(self.gid, self.engine._error_windows)

    def test_expired_errors_are_removed(self):
        for t in (100, 103, 106): self.engine.handle_503(1, '503', occurred_at=t)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.events[-1]['error_count'], 2)
        self.engine.handle_503(1, '503', occurred_at=107)
        self.assertEqual(self.calls, [2])

    def test_groups_use_independent_rules_and_counters(self):
        second = self.make_group('second', 3, 4)
        self.store.update_group(second, error_window_seconds=10, error_threshold=2)
        self.engine.handle_503(1, '503', occurred_at=100)
        self.engine.handle_503(3, '503', occurred_at=100)
        self.engine.handle_503(1, '503', occurred_at=101)
        self.assertEqual(self.calls, [])
        self.engine.handle_503(3, '503', occurred_at=109)
        self.assertEqual(self.calls, [4])
        self.assertEqual(self.store.get_group(self.gid)['current_channel_id'], 1)

    def test_new_current_channel_starts_fresh(self):
        for t in (100, 101, 102): self.engine.handle_503(1, '503', occurred_at=t)
        self.engine.handle_503(1, 'late', occurred_at=103)
        self.engine.handle_503(2, '503', occurred_at=103)
        self.assertEqual(self.events[-1]['error_count'], 1)
        self.assertEqual(self.calls, [2])

    def test_rule_change_restarts_counting(self):
        self.engine.handle_503(1, '503', occurred_at=100)
        self.store.update_group(self.gid, error_threshold=2)
        self.engine.handle_503(1, '503', occurred_at=101)
        self.assertEqual(self.calls, [])
        self.engine.handle_503(1, '503', occurred_at=102)
        self.assertEqual(self.calls, [2])

    def test_threshold_one_restores_immediate_behavior(self):
        self.store.update_group(self.gid, error_threshold=1)
        self.engine.handle_503(1, '503', occurred_at=100)
        self.assertEqual(self.calls, [2])

    def test_log_timestamps_and_duplicate_records(self):
        watcher = LogWatcher(self.root, self.engine.handle_503)
        watcher.poll_once()
        path = self.root / 'oneapi-test.log'
        def append(second, request_id):
            prefix = f'[ERR] 2026/11/14 - 09:00:{second:02d} | {request_id} | '
            with path.open('a') as f:
                f.write(prefix + 'channel error (channel #1, status code: 503): unavailable\n')
                f.write(prefix.replace('[ERR]', '[INFO]') + 'record error log: channelId=1, content=status_code=503, unavailable\n')
        # 同一轮读取不等于同一时间窗口：以日志实际时间计算。
        append(0, 'request-a'); append(3, 'request-b'); append(6, 'request-c')
        watcher.poll_once()
        self.assertEqual(self.calls, [])
        self.assertEqual([e['error_count'] for e in self.events], [1, 2, 2])
        append(7, 'request-d'); watcher.poll_once()
        self.assertEqual(self.calls, [2])

    def test_rule_validation_and_persistence(self):
        for value in (0, -1, 1.5, True, None, '3', 10001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.update_group(self.gid, error_threshold=value)
        for value in (0, True, None, 86401):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.update_group(self.gid, error_window_seconds=value)
        self.store.update_group(self.gid, error_window_seconds=12, error_threshold=7)
        reopened = ConfigStore(str(self.root / 'config.db'))
        self.addCleanup(reopened.conn.close)
        self.assertEqual(reopened.get_group(self.gid)['error_threshold'], 7)
        self.assertEqual(reopened.get_group(self.gid)['error_window_seconds'], 12)

    def test_existing_database_migration_keeps_group_state(self):
        path = self.root / 'old.db'
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE groups_config(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL, current_channel_id INTEGER, created_at INTEGER NOT NULL, newapi_group TEXT NOT NULL)')
            conn.execute("INSERT INTO groups_config VALUES(9,'existing',1,123,100,'GPT')")
        migrated = ConfigStore(str(path))
        self.addCleanup(migrated.conn.close)
        self.assertEqual(migrated.get_group(9), dict(id=9,name='existing',enabled=1,current_channel_id=123,created_at=100,newapi_group='GPT',error_window_seconds=5,error_threshold=3))

    def test_http_rule_update_validation_and_reset(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.service = SimpleNamespace(store=self.store, engine=self.engine, record_event=self.events.append)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}/api/groups/{self.gid}/error-rule'
        def post(body):
            return urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type':'application/json', 'Authorization':'Basic '+base64.b64encode(b'root:test').decode()}))
        with patch.dict(os.environ, ADMIN_USER='root', ADMIN_PASSWORD='test'):
            self.engine.handle_503(1, '503', occurred_at=100)
            with post({'error_window_seconds':5,'error_threshold':3}) as response:
                self.assertEqual(json.load(response)['error_threshold'], 3)
            self.assertNotIn(self.gid, self.engine._error_windows)
            self.assertEqual(self.store.get_group(self.gid)['enabled'], 1)
            for body in ({'error_window_seconds':5}, {'error_window_seconds':5,'error_threshold':None}):
                with self.assertRaises(urllib.error.HTTPError) as ctx: post(body)
                self.assertEqual(ctx.exception.code, 400)


if __name__ == '__main__': unittest.main()
