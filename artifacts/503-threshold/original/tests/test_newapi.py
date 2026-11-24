import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from src.core import Channel
from src.newapi import UpstreamTester

class Upstream(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.request_payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        status,payload=self.server.answer
        body=json.dumps(payload).encode()
        self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass

class TestNewAPI(unittest.TestCase):
    def setUp(self):
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        self.server.answer=(200,{'choices':[{'message':{'content':'pong'}}]})
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.channel=Channel(54,'test',2,'GPT',0,1,'http://127.0.0.1:'+str(self.server.server_port),'secret')
        self.tester=UpstreamTester()
    def test_simple_payload(self):
        result=self.tester(self.channel)
        self.assertTrue(result.success)
        self.assertEqual(self.server.request_payload['model'],'gpt-6.1-sol')
        self.assertFalse(self.server.request_payload['stream'])
        self.assertEqual(self.server.request_payload['messages'][0]['content'],'ping')
    def test_errors_rejected(self):
        for status,payload in [(503,{'error':'temporarily unavailable'}),(401,{'error':'bad token'}),(200,{'error':{'code':503}}),(200,{})]:
            with self.subTest(status=status,payload=payload):
                self.server.answer=(status,payload)
                self.assertFalse(self.tester(self.channel).success)

if __name__=='__main__':unittest.main()
