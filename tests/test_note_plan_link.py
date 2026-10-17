"""Isolated actual DB note-plan lifecycle + model input/privacy regression."""
import os
import json
import sys
import tempfile
from pathlib import Path
os.environ['PA_DATA_DIR']=tempfile.mkdtemp(prefix='t25_notes_')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import main as m, service as s, agents, db as dbm
checks=[]
def check(name,ok):
    checks.append((name,bool(ok)));print(('PASS ' if ok else 'FAIL ')+name)
def throws(fn):
    try: fn(); return False
    except ValueError:return True
client=TestClient(m.app);client.headers['Origin']='http://testserver';client.get('/api/me')
db=m.db
person=db.row("SELECT person_id FROM person WHERE owner_id='A' LIMIT 1")['person_id']
eid,_=s.import_life_event(db,'note-test',person,'photo_record',{'时间':'周六上午','地点':'公园','正文':'不应公开的健康原文','meta_hidden':{'private':'hidden'}},'2026-10-16','本人核对虚构记录',owner_id='A')
bperson=db.row("SELECT person_id FROM person WHERE owner_id='B' LIMIT 1")['person_id']
bid,_=s.import_life_event(db,'B-source',bperson,'photo_record',{'时间':'B私有'},'','B资料',owner_id='B')
p=s.create_plan(db,'A','资料活动');s.add_plan_participant(db,p,'A','我家',{})
other=s.create_plan(db,'A','未引用活动');s.add_plan_participant(db,other,'A','我家',{})
check('fields只投影短标量不附件metadata',client.get(f'/api/note/{eid}/fields').json()['fields']=={'时间':'周六上午','地点':'公园','正文':'不应公开的健康原文'})
check('不能读他人资料',client.get(f'/api/note/{bid}/fields').status_code==404)
base={'event_id':eid,'plan_id':p,'selected_fields':[]}
check('默认零selected仍可持久关联',client.post('/api/note/link-plan',json=base).status_code==200)
check('私有字段零选择不算used',client.get(f'/api/plan/{p}/linked-notes').json()['notes'][0]['fields']=={} and not client.get(f'/api/plan/{p}/linked-notes').json()['notes'][0]['used'])
check('伪payload整体拒绝',client.post('/api/note/link-plan',json={**base,'payload':{'时间':'假'}}).status_code==422)
check('owner参数冒B拒403',client.post('/api/note/link-plan',json={**base,'owner_id':'B'}).status_code==403)
check('未知field拒绝',client.post('/api/note/link-plan',json={**base,'selected_fields':['image_b64']}).status_code==409)
check('外人source拒绝且无link',client.post('/api/note/link-plan',json={**base,'event_id':bid}).status_code==409 and not db.row('SELECT 1 FROM event_plan_link WHERE event_id=?',(bid,)))
private={**base,'selected_fields':['正文','时间']}
check('selected私有投影持久',client.post('/api/note/link-plan',json=private).status_code==200 and '健康原文' in str(client.get(f'/api/plan/{p}/linked-notes').json()))
check('publish未二次confirm拒绝',client.post('/api/note/link-plan',json={**private,'public_conditions':{'时间':'周六上午'}}).status_code==409)
check('伪public值拒绝',client.post('/api/note/link-plan',json={**private,'public_conditions':{'时间':'假时间'},'publish_confirmed':True}).status_code==409)
check('二次明确publish合法来源值',client.post('/api/note/link-plan',json={**private,'public_conditions':{'时间':'周六上午'},'publish_confirmed':True}).status_code==200)
last=db.rows('SELECT * FROM event_plan_link');before=s.plan_dependency_snapshot(db,p)
check('重复selection幂等无revision变化',client.post('/api/note/link-plan',json={**private,'public_conditions':{'时间':'周六上午'},'publish_confirmed':True}).status_code==200 and len(db.rows('SELECT * FROM event_plan_link'))==len(last) and before==s.plan_dependency_snapshot(db,p))
class Stub:
    def __init__(self):self.inputs=[]
    def chat_json(self,messages,*a,**kw):
        self.inputs.append(str(messages));return {'content':{'时间':'周六上午'},'reason':'按公开时间','conflicts':[],'decision':'accept','changes':{}}
stub=Stub();prop=agents.plan_replan(db,stub,p)
check('模型仅公开条件不私有原文/source metadata', '周六上午' in str(stub.inputs) and '健康原文' not in str(stub.inputs) and 'hidden' not in str(stub.inputs) and 'note-test' not in str(stub.inputs))
check('真正public模型input后used来源回读',client.get(f'/api/plan/{p}/linked-notes').json()['notes'][0]['used'])
part=db.row('SELECT participant_id FROM plan_participant WHERE plan_id=?',(p,))['participant_id']
s.decide_plan(db,p,part,prop['version'],'accept',actor_id='A')
check('明确当前版accept可约定',s.plan_status(db,p)['agreed'])
unrelated=s.plan_dependency_snapshot(db,other)
new=s.correct_life_event(db,eid,{'时间':'周日上午'},'A')
check('更正保留未投影metadata', 'meta_hidden' in db.row('SELECT payload_json FROM life_event WHERE event_id=?',(new,))['payload_json'])
check('更正仅依赖plan失效旧accept不复活',s.plan_status(db,p)['needs_replan'] and not s.plan_status(db,p)['agreed'] and unrelated==s.plan_dependency_snapshot(db,other))
check('更正旧来源不给下次prompt且去掉旧public条件','时间' not in db.row('SELECT conditions_json FROM plan_participant WHERE plan_id=?',(p,))['conditions_json'] and client.get(f'/api/plan/{p}/linked-notes').json()['notes'][0]['fields']=={})
lid=db.row('SELECT link_id FROM event_plan_link WHERE event_id=?',(eid,))['link_id']
check('新来源须本人再选再确认',client.post('/api/note/link-plan',json={**base,'event_id':new,'selected_fields':['时间'],'public_conditions':{'时间':'周日上午'},'publish_confirmed':True,'replace_link_id':lid}).status_code==200)
check('替换保旧ref历史且只新字段',db.row('SELECT status FROM event_plan_link WHERE link_id=?',(lid,))['status']=='superseded' and '健康原文' not in str(s.plan_provenance(db,p)))
prop=agents.plan_replan(db,Stub(),p)
count=db.row('SELECT count(*) n FROM plan_proposal WHERE plan_id=?',(p,))['n']
class During:
    def chat_json(self,*a,**kw):
        s.revoke_life_event(db,new,'A');return {'content':{'时间':'周日上午'},'reason':'迟到'}
check('模型等待中撤回不落迟到version',throws(lambda:agents.plan_propose(db,During(),p,'A')) and db.row('SELECT count(*) n FROM plan_proposal WHERE plan_id=?',(p,))['n']==count)
check('撤回后来源不进入next prompt','周日上午' not in str(agents._plan_context(db,p)))
# Transaction must roll back a newly created plan if the source fails ownership.
n=db.row('SELECT count(*) n FROM plan')['n']
check('原子create-with-note失败无半份草稿',client.post('/api/plan/create-with-note',json={'title':'失败创建','event_id':bid,'selected_fields':['时间']}).status_code==409 and db.row('SELECT count(*) n FROM plan')['n']==n)
e2,_=s.import_life_event(db,'new-note',person,'photo_record',{'地点':'公园'},'','本人',owner_id='A')
r=client.post('/api/plan/create-with-note',json={'title':'成功创建','event_id':e2,'selected_fields':['地点']})
check('no-plan原子成功并可重开source',r.status_code==200 and bool(client.get(f'/api/plan/{r.json()["plan_id"]}/linked-notes').json()['notes']))
created=r.json()['plan_id'];s.unlink_event_from_plan(db,'A',e2,created)
check('unlink stale且不再投影',s.plan_status(db,created)['needs_replan'] and s.linked_plan_events(db,created,'A')[0]['fields']=={})
# Completed history is not rewritten; source state still reports revoked.
e3,_=s.import_life_event(db,'completed-note',person,'photo_record',{'地点':'家'},'','本人',owner_id='A')
s.link_event_to_plan(db,'A',e3,other,['地点']);db.execute("UPDATE plan SET stage='completed',needs_replan=0 WHERE plan_id=?",(other,))
s.revoke_life_event(db,e3,'A')
check('completed历史不改stage/replan',db.row('SELECT stage,needs_replan FROM plan WHERE plan_id=?',(other,))=={'stage':'completed','needs_replan':0})
check('无新增分享grant/执行authorization',not db.row('SELECT 1 FROM event_grant WHERE event_id IN (?,?)',(eid,new)) and not db.row('SELECT 1 FROM authorization WHERE owner_id=? AND subject_id=?',('A',person)))
# Replan / review can race source changes; no stale result may be stored.
raceplan=s.create_plan(db,'A','race-source');rp=s.add_plan_participant(db,raceplan,'A','我家',{})
erev,_=s.import_life_event(db,'race-event',person,'photo_record',{'时间':'周六上午','地点':'公园'},'','本人',owner_id='A')
s.link_event_to_plan(db,'A',erev,raceplan,['时间'],{'时间':'周六上午'},True)
first=agents.plan_replan(db,Stub(),raceplan)
class LateReview:
    def chat_json(self,*a,**kw):
        s.link_event_to_plan(db,'A',erev,raceplan,['地点'])
        return {'decision':'counter','reason':'迟到','changes':{'地点':'公园'}}
nb=db.row('SELECT count(*) n FROM plan_proposal WHERE plan_id=?',(raceplan,))['n']
check('review等待中selection变更无counter/decision落库',throws(lambda:agents.plan_review(db,LateReview(),raceplan,rp,first['version'])) and db.row('SELECT count(*) n FROM plan_proposal WHERE plan_id=?',(raceplan,))['n']==nb and not db.row('SELECT 1 FROM plan_decision WHERE plan_id=?',(raceplan,)))
class LateReplan:
    def chat_json(self,*a,**kw):
        s.correct_life_event(db,erev,{'地点':'新地点'},'A')
        return {'content':{'地点':'公园'},'reason':'迟到'}
check('replan等待中更正无迟到version且needsreplan保留',throws(lambda:agents.plan_replan(db,LateReplan(),raceplan)) and db.row('SELECT count(*) n FROM plan_proposal WHERE plan_id=?',(raceplan,))['n']==nb and s.plan_status(db,raceplan)['needs_replan'])
check('completed来源可private回读撤回状态',s.linked_plan_events(db,other,'A')[0]['fields']=={})
# Only actually published selected keys are recorded as used, not private body keys.
prov=json.loads(db.row('SELECT provenance_json FROM plan_proposal WHERE plan_id=? ORDER BY version LIMIT 1',(p,))['provenance_json'])
check('usedprovenance不声称采用private正文','正文' not in str(prov))
up=s.create_plan(db,'A','withdraw-unique');ua=s.add_plan_participant(db,up,'A','我家',{})
ue,_=s.import_life_event(db,'withdraw-src',person,'photo_record',{'地点':'撤回独特标记'},'','本人',owner_id='A')
s.link_event_to_plan(db,'A',ue,up,['地点'],{'地点':'撤回独特标记'},True)
class Unique:
    def chat_json(self,*a,**kw):return {'content':{'地点':'撤回独特标记','备注':'也在其他字段转述撤回独特标记'},'reason':'理由含撤回独特标记'}
uv=agents.plan_replan(db,Unique(),up)
s.unlink_event_from_plan(db,'A',ue,up)
check('unlink保tombstone但下一ctx无旧方案任何转述','撤回独特标记' not in str(agents._plan_context(db,up)))
captured=Stub();check('unlink后旧review不调用model',throws(lambda:agents.plan_review(db,captured,up,ua,uv['version'])) and not captured.inputs)
agents.plan_replan(db,captured,up)
check('unlink后replaninput无旧来源值及reason','撤回独特标记' not in str(captured.inputs))
print(f'{sum(ok for _,ok in checks)}/{len(checks)} passed');sys.exit(any(not ok for _,ok in checks))
