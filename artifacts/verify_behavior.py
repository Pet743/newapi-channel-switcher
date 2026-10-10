"""验证真实核心代码的优先级切换及回切；只使用内存渠道仓储。"""
import importlib.util
import pathlib
import sys
import tempfile

ROOT=pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from src.store import ConfigStore
mode=sys.argv[1]
source=ROOT/'artifacts/ORIGINAL_FILE.core.py' if mode=='BASELINE' else pathlib.Path(sys.argv[2]) if mode=='ROLLBACK' else ROOT/'src/core.py'
spec=importlib.util.spec_from_file_location('verified_core',source)
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
with tempfile.TemporaryDirectory() as tmp:
    store=ConfigStore(str(pathlib.Path(tmp)/'config.db'));g=store.create_group('GPT','GPT')
    store.replace_candidates(g['id'],[{'channel_id':48,'priority':100},{'channel_id':54,'priority':90}])
    store.update_group(g['id'],enabled=True);store.set_current(g['id'],48)
    repo=module.MemoryRepository([module.Channel(48,'A',1,'GPT',1,1),module.Channel(54,'B',2,'GPT',1,1)])
    engine=module.SwitchEngine(repo,store,lambda c:module.CandidateResult(c.id,200,10,True,'pong'))
    if hasattr(engine,'cooldown'):engine.cooldown=0
    engine.handle_503(48,'Service temporarily unavailable')
    switch=next(c.id for c in repo.get_channels() if c.status==1)
    if hasattr(engine,'reconcile'):engine.reconcile()
    recovery=next(c.id for c in repo.get_channels() if c.status==1)
    single=sum(c.status==1 for c in repo.get_channels())
    assert switch==54 and single==1
    assert recovery==(48 if mode=='MODIFIED' else 54)
    print(f'{mode} switch={switch} recovery={recovery} single_active={single}')
