import tempfile
import unittest
from pathlib import Path
from src.core import Channel,CandidateResult,LogParser,LogWatcher,MemoryRepository,SwitchEngine
from src.store import ConfigStore

class TestCore(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=ConfigStore(str(Path(self.tmp.name)/'config.db'))
        self.group=self.store.create_group('GPT','GPT')
        self.repo=MemoryRepository([Channel(48,'A',1,'GPT',1,1),Channel(54,'B',2,'GPT',1,1),Channel(55,'C',2,'GPT',1,1)])
        self.store.replace_candidates(self.group['id'],[{'channel_id':48,'priority':100},{'channel_id':54,'priority':80},{'channel_id':55,'priority':50}])
        self.store.set_current(self.group['id'],48);self.store.update_group(self.group['id'],enabled=True)
        self.store.save_snapshot(self.group['id'],self.repo.get_channels())
        self.results={48:True,54:True,55:True};self.calls=[];self.events=[]
        def test(c):
            self.calls.append(c.id);ok=self.results[c.id]
            return CandidateResult(c.id,200 if ok else 503,12,ok,'pong' if ok else '503')
        self.engine=SwitchEngine(self.repo,self.store,test,self.events.append,cooldown=0)
    def test_parser_503_only(self):
        s='record error log: userId=1, channelId=48, content=status_code=503, Service temporarily unavailable'
        self.assertEqual(LogParser.parse(s),(48,'Service temporarily unavailable'))
        self.assertIsNone(LogParser.parse(s.replace('503','5030')))
        self.assertIsNone(LogParser.parse(s.replace('503','400')))
        self.assertEqual(LogParser.parse('channel error (channel #48, status code: 503): temporarily unavailable'),(48,'temporarily unavailable'))
    def test_switch_priority_success(self):
        self.engine.handle_503(48,'503')
        self.assertEqual(self.calls,[54]);self.assertEqual([c.id for c in self.repo.get_channels() if c.status==1],[54])
    def test_skip_failing_high_candidate(self):
        self.results[54]=False;self.engine.handle_503(48,'503')
        self.assertEqual(self.calls,[54,55]);self.assertEqual(self.repo.channels[55].status,1)
    def test_stale_event_ignored(self):
        self.engine.handle_503(48,'503');self.engine.handle_503(48,'late 503')
        self.assertEqual(self.calls,[54])
    def test_restore_higher_priority(self):
        self.engine.handle_503(48,'503');self.engine.reconcile()
        self.assertEqual(self.repo.channels[48].status,1);self.assertEqual(self.repo.channels[54].status,2)
    def test_no_candidate_retains_state(self):
        self.results[54]=self.results[55]=False;self.engine.handle_503(48,'503')
        self.assertEqual(self.repo.channels[48].status,1)
    def test_periodic_does_not_test_current(self):
        self.engine.reconcile();self.assertEqual(self.calls,[])
    def test_paused_ignored(self):
        self.store.update_group(self.group['id'],enabled=False)
        self.engine.handle_503(48,'503');self.engine.reconcile();self.assertEqual(self.calls,[])
    def test_activation_tries_all_by_priority(self):
        self.results[48]=False;self.engine.activate_best(self.group['id'])
        self.assertEqual(self.calls,[48,54]);self.assertEqual(self.repo.channels[54].status,1)
    def test_rollback_original_statuses(self):
        self.engine.handle_503(48,'503');self.engine.restore(self.group['id'])
        self.assertEqual(self.repo.channels[48].status,1);self.assertEqual(self.repo.channels[54].status,2)
        self.assertFalse(self.store.get_group(self.group['id'])['enabled'])
    def test_unique_channel_management(self):
        g=self.store.create_group('second','OTHER')
        with self.assertRaises(ValueError): self.store.replace_candidates(g['id'],[{'channel_id':48,'priority':1}])
    def test_public_does_not_include_secrets(self):
        c=Channel(1,'x',1,'g',1,1,key='secret',setting='secret_proxy')
        self.assertNotIn('key',c.public());self.assertNotIn('setting',c.public())
    def test_log_rotation_and_partial_lines(self):
        root=Path(self.tmp.name);path=root/'oneapi-1.log';path.write_text('old channel error (channel #48, status code: 503): old\n')
        events=[];w=LogWatcher(str(root),lambda cid,msg:events.append((cid,msg)))
        w.poll_once();self.assertEqual(events,[])
        s='record error log: channelId=48, content=status_code=503, unavailable'
        with path.open('a') as f:f.write(s)
        w.poll_once();self.assertEqual(events,[])
        with path.open('a') as f:f.write('\n')
        w.poll_once();self.assertEqual(events,[(48,'unavailable')])
        (root/'oneapi-2.log').write_text(s+'\n');w.poll_once();self.assertEqual(len(events),2)
    def test_log_watcher_survives_callback_failure(self):
        root=Path(self.tmp.name);w=LogWatcher(str(root),lambda *args:(_ for _ in ()).throw(RuntimeError('test')))
        w.poll_once();(root/'oneapi-2.log').write_text('channel error (channel #48, status code: 503): x\n')
        with self.assertLogs(level='ERROR'):w.poll_once()
        w.poll_once()

if __name__=='__main__':unittest.main()
