import json,os,tempfile,time
import psycopg
from psycopg.conninfo import make_conninfo
from src.core import CandidateResult,SwitchEngine
from src.newapi import PostgresChannelRepository
from src.store import ConfigStore
schema='switcher_verify_'+str(int(time.time()))
dsn=os.environ['NEWAPI_DB_DSN']
with psycopg.connect(dsn,autocommit=True) as conn:
    conn.execute('CREATE SCHEMA '+schema)
    try:
        conn.execute('SET search_path TO '+schema)
        conn.execute('CREATE TABLE channels (id bigint primary key,name text,status bigint,"group" text,priority bigint,weight bigint,base_url text,key text,models text,setting text)')
        conn.execute('CREATE TABLE abilities (channel_id bigint primary key,enabled boolean)')
        conn.execute("INSERT INTO channels VALUES(48,'test A',1,'GPT',0,1,'','','gpt-6.1-sol','{}'),(54,'test B',2,'GPT',0,1,'','','gpt-6.1-sol','{}')")
        conn.execute('INSERT INTO abilities VALUES(48,true),(54,false)')
        repo=PostgresChannelRepository(make_conninfo(dsn,options='-c search_path='+schema))
        def state():
            return ' '.join(f'{r[0]}:{r[1]}:{str(r[2]).lower()}' for r in conn.execute('SELECT c.id,c.status,a.enabled FROM channels c JOIN abilities a ON c.id=a.channel_id ORDER BY c.id'))
        print('BASELINE '+state())
        with tempfile.TemporaryDirectory() as tmp:
            store=ConfigStore(tmp+'/config.db');g=store.create_group('GPT','GPT')
            store.replace_candidates(g['id'],[{'channel_id':48,'priority':100},{'channel_id':54,'priority':90}])
            store.set_current(g['id'],48);store.update_group(g['id'],enabled=True);store.save_snapshot(g['id'],repo.get_channels())
            engine=SwitchEngine(repo,store,lambda c:CandidateResult(c.id,200,1,True,'pong'),cooldown=0)
            engine.handle_503(48,'Service temporarily unavailable')
            assert state()=='48:2:false 54:1:true'
            print('MODIFIED '+state())
            engine.reconcile();assert state()=='48:1:true 54:2:false'
            print('RECOVERY '+state())
            engine.restore(g['id']);assert state()=='48:1:true 54:2:false'
            print('ROLLBACK '+state()+' paused=true')
            # 不存在目标渠道时事务完整回滚。
            try:repo.set_active([48,99999],99999)
            except ValueError:pass
            else:raise AssertionError('missing channel should fail')
            assert state()=='48:1:true 54:2:false'
            print('TRANSACTION failure=rolled_back')
    finally:
        conn.execute('DROP SCHEMA '+schema+' CASCADE')
print('ISOLATED_SCHEMA removed=true production_channels=unchanged')
