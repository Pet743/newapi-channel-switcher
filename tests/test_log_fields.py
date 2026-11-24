import tempfile
import unittest
from pathlib import Path

from src.core import CandidateResult, Channel, LogParser, LogWatcher, MemoryRepository, SwitchEngine
from src.store import ConfigStore


class TestLogFields(unittest.TestCase):
    def test_record_uses_fields_in_any_language(self):
        for label in ('record error log', '记录错误日志', '任意说明文字', ''):
            with self.subTest(label=label):
                line = label + ': userId=21, channelId=75, modelName=gpt-6.1-sol, content=status_code=503, unavailable'
                self.assertEqual(LogParser.parse(line), (75, 'unavailable'))
                for status in ('502', '504', '5030'):
                    self.assertIsNone(LogParser.parse(line.replace('status_code=503', 'status_code=' + status)))

    def test_channel_error_uses_structure_in_any_language(self):
        for line in (
            'channel error (channel #75, status code: 503): unavailable',
            '渠道请求出错 (渠道 #75，状态码 503): unavailable',
            '任意说明 (任意 #75，任意 503)：unavailable',
        ):
            with self.subTest(line=line):
                self.assertEqual(LogParser.parse(line), (75, 'unavailable'))
                self.assertIsNone(LogParser.parse(line.replace('503', '5030')))
                self.assertIsNone(LogParser.parse(line.replace('503', '502')))

    def test_unrelated_503_does_not_identify_a_channel(self):
        for line in ('status_code=503', 'channelId=75, content=error mentioning 503',
                     '[GIN] 503 | POST /v1/responses', 'channelId=75, content=status_code=200, upstream 503'):
            with self.subTest(line=line):
                self.assertIsNone(LogParser.parse(line))

    def test_chinese_logs_trigger_once_on_third_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ConfigStore(str(root / 'config.db'))
            self.addCleanup(store.conn.close)
            group = store.create_group('gpt', 'GPT', error_window_seconds=10, error_threshold=3)
            store.replace_candidates(group['id'], [{'channel_id':75, 'priority':10}, {'channel_id':48, 'priority':9}])
            store.set_current(group['id'], 75)
            store.update_group(group['id'], enabled=True)
            repo = MemoryRepository([Channel(75,'A',1,'GPT',10,1), Channel(48,'B',2,'GPT',9,1)])
            calls, events = [], []
            def tester(channel):
                calls.append(channel.id)
                return CandidateResult(channel.id, 200, 1, True, 'pong')
            engine = SwitchEngine(repo, store, tester, events.append)
            watcher = LogWatcher(root, engine.handle_503)
            watcher.poll_once()
            for second in (5, 8, 10):
                prefix = f'[ERR] 2026/11/25 - 00:21:{second:02d} | request-{second} | '
                with (root / 'oneapi-test.log').open('a') as fh:
                    fh.write(prefix + '渠道请求出错 (渠道 #75，状态码 503): unavailable\n')
                    fh.write(prefix.replace('[ERR]', '[INFO]') + '记录错误日志: channelId=75, content=status_code=503, unavailable\n')
                watcher.poll_once()
                self.assertEqual(calls, [48] if second == 10 else [])
            self.assertEqual([e['error_count'] for e in events if e['type'] == '503'], [1, 2, 3])
            self.assertEqual(store.get_group(group['id'])['current_channel_id'], 48)
            self.assertEqual([c.id for c in repo.get_channels() if c.status == 1], [48])
            watcher.poll_once()
            self.assertEqual(calls, [48])


if __name__ == '__main__':
    unittest.main()
