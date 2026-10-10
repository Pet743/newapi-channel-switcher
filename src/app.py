import base64
import json
import logging
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .core import LogWatcher, SwitchEngine
from .newapi import PostgresChannelRepository, UpstreamTester
from .store import ConfigStore

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / 'static'

class Service:
    def __init__(self):
        self.store = ConfigStore(os.getenv('CONFIG_DB', str(ROOT / 'data/config.db')))
        self.repository = PostgresChannelRepository(os.environ['NEWAPI_DB_DSN'])
        self.tester = UpstreamTester(os.getenv('TEST_MODEL','gpt-6.1-sol'), float(os.getenv('TEST_TIMEOUT','20')))
        self.engine = SwitchEngine(self.repository,self.store,self.tester,self.record_event,cooldown=int(os.getenv('RECOVERY_INTERVAL','60')))
        self.watcher = LogWatcher(os.getenv('NEWAPI_LOG_DIR','/var/lib/newapi/logs'),self.on_503,float(os.getenv('LOG_POLL_INTERVAL','1')))
        self.stopped = threading.Event()
        self.worker = None

    def record_event(self, event): self.store.add_event(json.dumps(event, ensure_ascii=False))
    def on_503(self, cid, message): self.engine.handle_503(cid,message)
    def start(self):
        self.watcher.start()
        self.worker = threading.Thread(target=self._periodic,daemon=True,name='recovery-worker')
        self.worker.start()
    def _periodic(self):
        interval = max(10,int(os.getenv('RECOVERY_INTERVAL','60')))
        while not self.stopped.wait(interval): self.engine.reconcile()
    def stop(self):
        self.stopped.set(); self.watcher.stop()

    def validate_candidates(self, gid, rows):
        group = self.store.get_group(gid)
        if not group: raise ValueError('分组不存在')
        if group['enabled']: raise ValueError('修改渠道前请先暂停自动切换')
        if not isinstance(rows,list) or len(rows)>100: raise ValueError('渠道配置需为列表，最多100个')
        for row in rows:
            if not isinstance(row,dict) or type(row.get('channel_id')) is not int or type(row.get('priority')) is not int:
                raise ValueError('渠道ID和优先级必须是整数')
        ids=[r['channel_id'] for r in rows]
        all_channels = self.repository.get_channels()
        channels = [c for c in all_channels if c.id in ids]
        if {c.id for c in channels} != set(ids): raise ValueError('渠道ID不存在')
        target_group = group['newapi_group']
        if target_group:
            for c in channels:
                # newapi 的 status 是全局状态，多分组渠道会影响别组，故不能单独接管。
                if c.group != target_group: raise ValueError(f'渠道 #{c.id} 不属于所选单一newapi分组')
            extras = [c.id for c in all_channels if c.group==target_group and c.status==1 and c.id not in ids]
            if extras: raise ValueError(f'该newapi分组还有启用渠道 {extras}，请一并纳入以保证单活')
        self.store.replace_candidates(gid,rows)
        self.store.save_snapshot(gid,channels)
        return self.store.candidates(gid)

class Handler(BaseHTTPRequestHandler):
    server_version = 'newapi-channel-switcher/0.2'
    def _service(self): return self.server.service
    def _challenge(self):
        self.send_response(401); self.send_header('WWW-Authenticate','Basic realm="newapi-channel-switcher", charset="UTF-8"'); self.send_header('Content-Length','0'); self.end_headers(); return False
    def _auth(self):
        value=self.headers.get('Authorization','')
        if not value.startswith('Basic '): return self._challenge()
        try: user,password=base64.b64decode(value[6:],validate=True).decode().split(':',1)
        except Exception: return self._challenge()
        expected=os.getenv('ADMIN_PASSWORD','')
        if not expected or not(secrets.compare_digest(user.encode(),os.getenv('ADMIN_USER','root').encode()) and secrets.compare_digest(password.encode(),expected.encode())):
            return self._challenge()
        return True
    def _json(self,status,payload):
        data=json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def _body(self):
        if not self.headers.get('Content-Type','').startswith('application/json'): raise ValueError('请求必须使用application/json')
        # Basic Auth 同样禁止其他站点发起带凭证的修改请求。
        origin=self.headers.get('Origin')
        if origin and urlparse(origin).netloc != self.headers.get('Host'): raise ValueError('请求来源不匹配')
        length=int(self.headers.get('Content-Length','0'))
        if not 0<length<=65536: raise ValueError('请求体大小不正确')
        body=json.loads(self.rfile.read(length))
        if not isinstance(body,dict): raise ValueError('请求体必须是对象')
        return body
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/health': return self._json(200,{'ok':True,'service':'newapi渠道切换'})
        if not self._auth(): return
        service=self._service();store=service.store
        try:
            if path in ('/','/index.html'):
                data=(STATIC/'index.html').read_bytes();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data);return
            if path=='/api/groups':
                groups=store.groups()
                for g in groups: g['channels']=store.candidates(g['id'])
                return self._json(200,{'groups':groups})
            if path=='/api/channels': return self._json(200,{'channels':[c.public() for c in service.repository.get_channels()],'tests':store.test_results()})
            if path=='/api/events': return self._json(200,{'events':store.recent_events()})
            return self._json(404,{'error':'接口不存在'})
        except Exception:
            logging.exception('查询失败');self._json(500,{'error':'查询失败，请检查服务日志和数据库连接'})
    def do_POST(self):
        if not self._auth(): return
        try:
            body=self._body();path=urlparse(self.path).path;service=self._service();store=service.store
            with service.engine.lock:
                if path=='/api/groups':
                    newapi_group=str(body.get('newapi_group',''))
                    if not newapi_group: raise ValueError('请选择newapi分组')
                    return self._json(201,store.create_group(str(body.get('name','')),newapi_group))
                if path.startswith('/api/groups/'):
                    parts=path.strip('/').split('/');gid=int(parts[2]);g=store.get_group(gid)
                    if not g: return self._json(404,{'error':'分组不存在'})
                    action=parts[3] if len(parts)==4 else None
                    if action=='channels': return self._json(200,{'channels':service.validate_candidates(gid,body.get('channels'))})
                    if action=='activate':
                        # 所有候选按优先级逐一测试，不直接按指定ID绕过优先级。
                        return self._json(200,service.engine.activate_best(gid))
                    if action=='restore': service.engine.restore(gid);return self._json(200,{'ok':True})
                    if action is None:
                        if body.get('enabled'): raise ValueError('请点击启用并选优以完成测试后启用')
                        return self._json(200,store.update_group(gid,body.get('name'),body.get('enabled')))
                if path=='/api/test-channel':
                    cid=int(body['channel_id']);channel=next(iter(service.repository.get_channels([cid])),None)
                    if not channel: return self._json(404,{'error':'渠道不存在'})
                    result=service.tester(channel);store.record_test(result);return self._json(200,result.__dict__)
                self._json(404,{'error':'接口不存在'})
        except (ValueError,KeyError,TypeError) as exc: self._json(400,{'error':str(exc)})
        except Exception:
            logging.exception('操作失败');self._json(400,{'error':'操作失败，请检查配置和服务日志'})
    def do_DELETE(self):
        if not self._auth(): return
        try:
            path=urlparse(self.path).path;service=self._service()
            if not path.startswith('/api/groups/'): return self._json(404,{'error':'接口不存在'})
            with service.engine.lock:
                gid=int(path.split('/')[3]);g=service.store.get_group(gid)
                if not g: return self._json(404,{'error':'分组不存在'})
                if g['enabled']: raise ValueError('请先暂停或恢复再删除分组')
                service.store.delete_group(gid);self._json(200,{'ok':True})
        except ValueError as exc: self._json(400,{'error':str(exc)})
    def log_message(self,fmt,*args): logging.info(fmt,*args)

def main():
    if not os.getenv('ADMIN_PASSWORD'): raise RuntimeError('请设置ADMIN_PASSWORD')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    service=Service();service.start()
    server=ThreadingHTTPServer((os.getenv('BIND_HOST','0.0.0.0'),int(os.getenv('BIND_PORT','19041'))),Handler);server.service=service
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:service.stop();server.server_close()

if __name__=='__main__': main()
