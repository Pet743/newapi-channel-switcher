"""Deterministic offline behavior probe; no upstream calls or production writes."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from src.core import Channel, CandidateResult, MemoryRepository, SwitchEngine
from src.store import ConfigStore

with tempfile.TemporaryDirectory() as tmp:
    store = ConfigStore(str(Path(tmp) / 'config.db'))
    group = store.create_group('probe', 'probe')
    store.replace_candidates(group['id'], [{'channel_id':1,'priority':10}, {'channel_id':2,'priority':1}])
    store.set_current(group['id'], 1)
    store.update_group(group['id'], enabled=True)
    repo = MemoryRepository([Channel(1,'A',1,'probe',1,1), Channel(2,'B',2,'probe',1,1)])
    engine = SwitchEngine(repo, store, lambda c: CandidateResult(c.id,200,1,True,'ok'))
    active = []
    for timestamp in (100, 102, 104):
        with patch('src.core.time.time', return_value=timestamp): engine.handle_503(1,'503')
        active.append(store.get_group(group['id'])['current_channel_id'])
    print(json.dumps({'input':{'channel_id':1,'status':503,'times':[100,102,104]},'active_after_each':active},separators=(',',':')))
    store.conn.close()
