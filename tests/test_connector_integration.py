"""Connector integration uses a unique temporary DB and real loopback HTTP, no models."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import db as dbm, service


class Stub(BaseHTTPRequestHandler):
    hits = 0
    def log_message(self, *args):
        pass
    def do_GET(self):
        Stub.hits += 1
        content, status, media = json.dumps({'items': [{'id': 'external-1', 'progress': '读完两本绘本', 'completed': '数学练习 20 题', 'date': '2026-10-17', 'subject': 'p_me', 'private': 'NEVER_MODEL'}]}, ensure_ascii=False).encode(), 200, 'application/json'
        if self.path == '/missing':
            content = b'[{"id":"x"}]'
        elif self.path == '/large':
            content = b' ' * (service.CONNECTOR_MAX_BYTES + 1)
        elif self.path == '/bad':
            content, media = b'not JSON', 'text/html'
        elif self.path == '/invalid-json':
            content = b'not JSON'
        elif self.path == '/failed':
            content, status = b'{"error":"secret should not echo"}', 503
        self.send_response(status)
        self.send_header('Content-Type', media)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class ConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_port
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='t42-test-')
        self.db = dbm.DB(Path(self.tmp.name)/'isolated.db')
        dbm.seed(self.db)
        self.mapping = {'progress':'进度','completed':'已完成项目','date':'发生日期','subject':'对象'}
    def tearDown(self):
        self.tmp.cleanup()
    def create(self, path='/items'):
        return service.connector_create(self.db, 'A', 'self_app', '我的接口', self.base, path, self.mapping)
    def rows(self):
        return self.db.rows("SELECT * FROM life_event WHERE source_kind='connector'")
    def test_real_http_preview_no_import_and_mapping(self):
        cid = self.create()
        before = Stub.hits
        out = service.connector_test(self.db, 'A', cid)
        self.assertEqual(Stub.hits, before+1)
        self.assertFalse(out['simulated'])
        self.assertTrue(out['network_request'])
        self.assertEqual(out['preview'][0]['mapped']['进度'], '读完两本绘本')
        self.assertIn('private', out['preview'][0]['raw_keys'])
        self.assertNotIn('NEVER_MODEL', json.dumps(out))
        self.assertEqual(self.rows(), [])
    def test_missing_fields_preview_and_atomic_sync_failure(self):
        cid = self.create('/missing')
        out = service.connector_test(self.db, 'A', cid)
        self.assertFalse(out['preview'][0]['importable'])
        self.assertTrue(any('无法映射' in s for s in out['preview'][0]['warnings']))
        with self.assertRaisesRegex(ValueError, '映射未完成'):
            service.connector_sync(self.db, 'A', cid)
        self.assertEqual(self.rows(), [])
        self.assertIn('映射未完成', service.connector_list(self.db, 'A')[0]['last_error'])
    def test_idempotent_and_correction_preserves_history(self):
        cid = self.create()
        self.assertEqual(service.connector_sync(self.db, 'A', cid)['created'], 1)
        eid = self.rows()[0]['event_id']
        service.connector_authorize(self.db, 'A', cid, eid, ['进度'])
        new = service.correct_life_event(self.db, eid, {'进度':'本人更正的进度'}, 'A')
        self.assertEqual(self.db.row('SELECT status FROM life_event WHERE event_id=?', (eid,))['status'], 'superseded')
        record = service.connector_list(self.db,'A')[0]['events'][0]
        self.assertEqual(record['event_id'], new)
        self.assertEqual(record['authorized_fields'], [])
        self.assertEqual(service.connector_sync(self.db,'A',cid)['created'], 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(record['fields']['进度'], '本人更正的进度')
    def test_private_import_excluded_then_field_consent_then_revoke(self):
        cid = self.create()
        service.connector_sync(self.db, 'A', cid)
        eid = self.rows()[0]['event_id']
        self.assertTrue(any(e['event_id']==eid for e in service.life_events_for(self.db,'p_me','A')))
        self.assertFalse(any(e['event_id']==eid for e in service._life_events_both_sides(self.db,'A','p_me')))
        service.connector_authorize(self.db, 'A', cid, eid, ['进度'])
        seen = next(e for e in service._life_events_both_sides(self.db,'A','p_me') if e['event_id']==eid)
        self.assertEqual(seen['fields'], {'进度':'读完两本绘本'})
        service.connector_authorize(self.db, 'A', cid, eid, [])
        self.assertFalse(any(e['event_id']==eid for e in service._life_events_both_sides(self.db,'A','p_me')))
        service.connector_revoke(self.db,'A',cid)
        self.assertEqual(self.rows()[0]['status'], 'revoked')
        self.assertEqual(len(self.rows()), 1)
        with self.assertRaises(ValueError):
            service.connector_sync(self.db,'A',cid)
    def test_suggestion_context_never_calls_model_before_consent(self):
        cid = self.create()
        service.connector_sync(self.db,'A',cid)
        class NoModel:
            def chat_json(self,*args,**kwargs):
                raise AssertionError('unconsented connector data must never reach model')
        self.assertEqual(service.app_suggestion(self.db,NoModel(),'A','p_me')['status'],'no_data')
    def test_simulated_catalog_no_network(self):
        with patch.object(httpx:=service.httpx, 'Client', side_effect=AssertionError('no network')):
            for kind, entry in service.CONNECTOR_CATALOG.items():
                cid = service.connector_create(self.db,'A','third_party',entry['name'],path=kind)
                result = service.connector_sync(self.db,'A',cid)
                self.assertTrue(result['simulated'])
                self.assertFalse(result['network_request'])
        self.assertEqual(len(self.rows()),4)
    def test_bounded_failures_and_readable_reason(self):
        for path, text in [('/large','512 KiB'),('/bad','Content-Type'),('/invalid-json','可解析'),('/failed','HTTP 503')]:
            cid = self.create(path)
            with self.assertRaisesRegex(ValueError,text):
                service.connector_test(self.db,'A',cid)
            row = next(r for r in service.connector_list(self.db,'A') if r['connector_id']==cid)
            self.assertEqual(row['status'],'failed')
            self.assertIn(text,row['last_error'])
            self.assertNotIn('secret',row['last_error'])
    def test_timeout_credentials_and_url_guards(self):
        for base,path in [('file:///tmp/x','/items'),('http://u:password@localhost','/items'),(self.base,'//evil.com'),('http://169.254.169.254','/items'),(self.base,'/items?token=secret')]:
            with self.assertRaises(ValueError):
                service.connector_create(self.db,'A','self_app','bad',base,path,self.mapping)
        with self.assertRaisesRegex(ValueError,'不接收'):
            service.connector_create(self.db,'A','self_app','token',self.base,'/items',self.mapping,'secret')
        cid=self.create()
        with patch.object(service.httpx.Client,'stream',side_effect=service.httpx.ReadTimeout('token=DO_NOT_LOG')):
            with self.assertRaisesRegex(ValueError,'超时'):
                service.connector_test(self.db,'A',cid)
        self.assertNotIn('DO_NOT_LOG',json.dumps(service.connector_list(self.db,'A')))
    def test_wrong_owner_cannot_manage_or_map_other_person(self):
        cid=self.create()
        with self.assertRaises(ValueError):
            service.connector_sync(self.db,'B',cid)
        self.assertEqual(service.connector_list(self.db,'B'),[])
        with patch.object(service,'_connector_fetch',return_value=[{'id':'x','progress':'secret','completed':'a','date':'2026-10-17','subject':'p_xiaoyu_b'}]):
            out=service.connector_test(self.db,'A',cid)
            self.assertFalse(out['preview'][0]['importable'])
    def test_task_and_plan_sources_require_consent_and_withdraw_stales(self):
        cid=self.create();service.connector_sync(self.db,'A',cid);eid=self.rows()[0]['event_id']
        task=dbm.new_id('task')
        self.db.execute("INSERT INTO task(task_id,owner_id,utterance,extracted_json,stage,outcome,created_at) VALUES(?,?,?,?,?,?,?)",(task,'A','测试','{}','planning','','2026-10-17'))
        service.link_event_to_task(self.db,'A',eid,task)
        self.assertEqual(service.linked_events(self.db,task,'A'),[])
        pid=service.create_plan(self.db,'A','连接器授权测试')
        service.add_plan_participant(self.db,pid,'A','我家',{})
        with self.assertRaisesRegex(ValueError,'单独授权'):
            service.link_event_to_plan(self.db,'A',eid,pid,['进度'])
        service.connector_authorize(self.db,'A',cid,eid,['进度'])
        self.assertEqual(service.linked_events(self.db,task,'A')[0]['fields'],{'进度':'读完两本绘本'})
        service.link_event_to_plan(self.db,'A',eid,pid,['进度'])
        service.connector_authorize(self.db,'A',cid,eid,[])
        self.assertEqual(self.db.row('SELECT status FROM event_plan_link WHERE event_id=?',(eid,))['status'],'stale')
        self.assertEqual(service.linked_events(self.db,task,'A'),[])
    def test_builtin_demo_maps_to_other_current_owner(self):
        cid=service.connector_create(self.db,'B','self_app','B应用',self.base,'/items',self.mapping)
        with patch.object(service,'_connector_fetch',return_value=service.connector_demo_items(self.db,'A')):
            out=service.connector_sync(self.db,'B',cid)
            self.assertEqual(out['created'],1)
        self.assertEqual(self.rows()[0]['owner_id'],'B')
        self.assertEqual(self.rows()[0]['subject_id'],'p_azhou_b')
    def test_http_owner_csrf_credentials_and_guest_denial(self):
        # Import main only after selecting an isolated PA_DATA_DIR.
        os.environ['PA_DATA_DIR']=self.tmp.name
        from app import main
        from fastapi.testclient import TestClient
        old=main.db;main.db=self.db
        try:
            with TestClient(main.app) as client:
                info=client.get('/api/connectors').json()
                self.assertNotIn('auth_token',json.dumps(info))
                body={'kind':'third_party','name':'学校','path':'school'}
                self.assertEqual(client.post('/api/connectors',json=body).status_code,403)
                hdr={'Origin':'http://testserver','X-CSRF-Token':info['csrf_token']}
                created=client.post('/api/connectors',json=body,headers=hdr)
                self.assertEqual(created.status_code,200,created.text)
                cid=created.json()['connector_id']
                bad=client.post('/api/connectors/'+cid+'/sync',headers={**hdr,'Origin':'http://evil.test'})
                self.assertEqual(bad.status_code,403)
                out=client.post('/api/connectors/'+cid+'/sync',headers=hdr)
                self.assertEqual(out.status_code,200,out.text)
                rejected=client.post('/api/connectors',json={**body,'auth_token':'TOP_SECRET'},headers=hdr)
                self.assertEqual(rejected.status_code,409)
                self.assertNotIn('TOP_SECRET',rejected.text)
                invalid=client.post('/api/connectors',json={**body,'auth_token':'TOP_SECRET'*300},headers=hdr)
                self.assertEqual(invalid.status_code,422)
                self.assertNotIn('TOP_SECRET',invalid.text)
                self.assertNotIn('TOP_SECRET',client.get('/api/connectors').text)
                client.cookies.set('pa_matter_mode','1')
                self.assertEqual(client.get('/api/connectors').status_code,403)
        finally:
            main.db=old


if __name__=='__main__':
    unittest.main(verbosity=2)
