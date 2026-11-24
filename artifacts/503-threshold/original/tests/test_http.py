import base64
import json
import os
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from src.app import Handler
from src.core import Channel, MemoryRepository

class DummyService:
    repository=MemoryRepository([Channel(1,'sample',1,'GPT',1,1,key='secret',setting='proxy credentials')])
    class Store:
        def test_results(self):return {}
    store=Store()

class TestHTTP(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'ADMIN_USER':'root','ADMIN_PASSWORD':'test'});self.env.start();self.addCleanup(self.env.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.service=DummyService()
        threading.Thread(target=self.server.serve_forever,daemon=True).start();self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
    def test_html_requires_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:urllib.request.urlopen(self.base+'/')
        self.assertEqual(ctx.exception.code,401)
    def test_channels_export_no_secrets(self):
        req=urllib.request.Request(self.base+'/api/channels',headers={'Authorization':'Basic '+base64.b64encode(b'root:test').decode()})
        with urllib.request.urlopen(req) as r:body=r.read().decode()
        self.assertNotIn('secret',body);self.assertNotIn('credentials',body)
    def test_health(self):
        with urllib.request.urlopen(self.base+'/api/health') as r:self.assertTrue(json.load(r)['ok'])

if __name__=='__main__':unittest.main()
