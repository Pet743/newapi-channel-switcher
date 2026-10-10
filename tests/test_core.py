import tempfile
import unittest
from pathlib import Path
from src.core import Channel, CandidateResult, LogParser, MemoryRepository, SwitchEngine
from src.store import ConfigStore

class TestCore(unittest.TestCase):
    def test_parser_only_503(self):
        line='[INFO] record error log: userId=1, channelId=48, modelName=gpt-6.1-sol, content=status_code=503, Service temporarily unavailable '
        self.assertEqual(LogParser.parse(line), (48, 'Service temporarily unavailable'))
        self.assertIsNone(LogParser.parse(line.replace('503', '400')))

    def test_switch_highest_success(self):
        with tempfile.TemporaryDirectory() as d:
            store=ConfigStore(str(Path(d)/'config.db')); g=store.create_group('测试组'); store.replace_candidates(g['id'], [{'channel_id':1,'priority':10},{'channel_id':2,'priority':20},{'channel_id':3,'priority':5}])
            repo=MemoryRepository([Channel(1,'a',1,'',0,1),Channel(2,'b',2,'',0,1),Channel(3,'c',2,'',0,1)])
            results={2:CandidateResult(2,503,10,False,'503'),3:CandidateResult(3,200,10,True,'ok')}
            events=[]; engine=SwitchEngine(repo,store,lambda c:results[c.id],events.append); engine.handle_503(1,'Service temporarily unavailable')
            self.assertEqual(repo.channels[3].status,1); self.assertEqual(repo.channels[2].status,2); self.assertEqual([e['candidate_channel_id'] for e in events if e['type']=='test'],[2,3])

if __name__=='__main__': unittest.main()
