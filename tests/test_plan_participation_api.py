"""Isolated HTTP participant capability positive/negative tests. No real model."""
import concurrent.futures
import os
from pathlib import Path
import sys
import tempfile
import time
os.environ['PA_DATA_DIR']=tempfile.mkdtemp(prefix='t25_participation_')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import main as m, service as s, agents
checks=[]
def check(name,ok):
    checks.append((name,bool(ok))); print(('PASS ' if ok else 'FAIL ')+name)
def throws(fn):
    try: fn(); return False
    except ValueError: return True
owner=TestClient(m.app);owner.headers['Origin']='http://testserver'
guest=TestClient(m.app)
owner.get('/api/demo/session')
p=s.create_plan(m.db,'A','限定活动','不应向guest暴露的私人原话')
a=s.add_plan_participant(m.db,p,'A','我家',{'时间':'周六'},True)
other=s.create_plan(m.db,'B','B的秘密标题','B的秘密原话')
s.add_plan_participant(m.db,other,'B','B',{},True)
check('列表仅本人或参加事项，非participant无标题原话',all(x['plan_id']!=other for x in owner.get('/api/plans').json()['plans']))
inv=owner.post(f'/api/plan/{p}/invitations',json={'label':'另一家'}).json()
check('token256bit且库只有hash',len(inv['token'])>=43 and inv['token'] not in str(m.db.rows('SELECT * FROM plan_invitation')))
check('fragment而非query/path', '#token=' in inv['participate_url'])
check('跨源claim拒绝',guest.post('/api/participation/accept',json={'token':inv['token'],'display_name':'自称李'},headers={'Origin':'http://evil'}).status_code==403)
body={'token':inv['token'],'display_name':'自称李'}
with concurrent.futures.ThreadPoolExecutor(2) as pool:
    result=list(pool.map(lambda _:TestClient(m.app).post('/api/participation/accept',json=body,headers={'Origin':'http://testserver'}),range(2)))
check('并发一次性claim仅一次成功',sorted(r.status_code for r in result)==[200,409])
# A second independent invitation exercises the browser cookie route.
inv2=owner.post(f'/api/plan/{p}/invitations',json={'label':'自称家庭'}).json()
r=guest.post('/api/participation/accept',json={'token':inv2['token'],'display_name':'王'},headers={'Origin':'http://testserver'})
data=r.json(); headers={'Origin':'http://testserver','X-CSRF-Token':data['csrf_token']}
check('cookie HttpOnly Strict且非pa_session', 'HttpOnly' in r.headers['set-cookie'] and 'SameSite=strict' in r.headers['set-cookie'] and 'pa_session' not in guest.cookies)
view=guest.get('/api/participation/plan').json()
check('接受邀请不等于认可或accept',not view['approved'] and view['me']['state']=='pending' and not view['agreed'])
check('无原话/history/persons/原meta公开', '不应向guest' not in str(view) and 'families' not in view and 'child_attendance' not in view)
check('身份未冒B且标未核验',data['guest_id'].startswith('guest_') and '未核验' in view['identity_note'])
for path in ['/api/me','/api/persons','/api/conversation','/api/plans','/api/model-config','/api/demo/reset','/api/extension/prepare','/api/note/linkable-plans']:
    check('guest拒普通API '+path,guest.get(path).status_code==403)
guest.cookies.set('pa_session',owner.cookies.get('pa_session'))
check('旧A cookie不能回落A',guest.get('/api/me').status_code==403)
check('condition缺CSRF拒绝',guest.post('/api/participation/condition',json={'conditions':{},'children_count':0,'children':[]},headers={'Origin':'http://testserver'}).status_code==403)
check('伪owner与错plan schema拒绝',guest.post('/api/participation/condition',json={'owner_id':'B','plan_id':other,'conditions':{},'children_count':0},headers=headers).status_code==422)
check('本人自称children不从B补',guest.post('/api/participation/condition',json={'conditions':{'时间':'周六'},'children_count':1,'children':[{'name':'自称孩子','attendance':'coming'}]},headers=headers).status_code==200 and '小雨' not in str(guest.get('/api/participation/plan').json()))
check('未认可不得决定',guest.post('/api/participation/decide',json={'version':1,'decision':'accept'},headers=headers).status_code==409)
check('issuer认可独立动作',owner.post(f'/api/plan/{p}/invitations/{inv2["invitation_id"]}/approve',json={}).status_code==200)
ctx=agents._plan_context(m.db,p)
check('模型guest孩子来自自称字段', '自称孩子' in str(ctx) and '小雨' not in str(ctx))
class Stub:
    def chat_json(self,*a,**kw):return {'content':{'时间':'周六'},'reason':'按已给时间','conflicts':[]}
# Pending invitation is a gap, not an accepted household.
prop=agents.plan_propose(m.db,Stub(),p,'A')
m.db.execute('UPDATE plan SET needs_replan=0 WHERE plan_id=?',(p,))
check('guest本人当前版accept',guest.post('/api/participation/decide',json={'version':prop['version'],'decision':'accept'},headers=headers).status_code==200)
check('refresh持久session',guest.get('/api/participation/plan').json()['me']['state']=='accept')
check('旧版不得accept',guest.post('/api/participation/decide',json={'version':999,'decision':'accept'},headers=headers).status_code==409)
# Revoke while model waits must not leave a version/decision.
count=m.db.row('SELECT count(*) n FROM plan_proposal WHERE plan_id=?',(p,))['n']
class Revoke:
    def chat_json(self,*a,**kw):
        s.invitation_revoke(m.db,'A',p,inv2['invitation_id']); return {'content':{'时间':'周六'},'reason':'迟到'}
check('等待中撤回无迟到version',throws(lambda:agents.plan_propose(m.db,Revoke(),p,'A')) and m.db.row('SELECT count(*) n FROM plan_proposal WHERE plan_id=?',(p,))['n']==count)
check('撤回session立即失效',guest.get('/api/participation/plan').status_code==403)
check('撤回仍不回落A',guest.get('/api/me').status_code==403)
m.db.execute('UPDATE plan_guest_session SET expires_at=?',(time.time()-1,))
check('过期cookie仍拒普通API',guest.get('/api/plans').status_code==403)
inv3=s.invitation_create(m.db,'A',p,'到期')
m.db.execute('UPDATE plan_invitation SET expires_at=? WHERE invitation_id=?',(time.time()-1,inv3['invitation_id']))
check('过期invite不能claim',throws(lambda:s.invitation_accept(m.db,inv3['token'],'过期')))
# Browser drops the 24h capability naturally; the deny-only marker must remain.
guest.cookies.delete('pa_matter')
check('capcookie自然消失后marker仍阻旧A',guest.get('/api/me').status_code==403)
check('guest跨源condition拒绝',guest.post('/api/participation/condition',json={'conditions':{},'children_count':0,'children':[]},headers={'Origin':'http://evil','X-CSRF-Token':data['csrf_token']}).status_code==403)
p2=s.create_plan(m.db,'A','明示替候选');s.add_plan_participant(m.db,p2,'A','我家',{})
bpart=s.add_plan_participant(m.db,p2,'B','候选B',{})
i=s.invitation_create(m.db,'A',p2,'另一家');cl=s.invitation_accept(m.db,i['token'],'不是已验证B')
s.invitation_approve(m.db,'A',p2,i['invitation_id'],bpart)
check('明确替空槽不继承B且无双家庭',m.db.row('SELECT role FROM plan_participant WHERE participant_id=?',(bpart,))['role']=='retired' and s.plan_detail(m.db,p2,'B') is None and len(s._selected_family_keys(m.db,p2))==2)
check('guest本人条件为空不继承B',m.db.row('SELECT conditions_json,attendance_json FROM plan_participant WHERE participant_id=?',(cl['participant_id'],))=={'conditions_json':'{}','attendance_json':'{}'})
sess=s.matter_session(m.db,cl['session_token']);sess['raw_token']=cl['session_token']
s.guest_condition(m.db,sess,{},[],None)
check('未知childcount持久不当成0且保gap',any('孩子' in g for g in s.plan_status(m.db,p2)['confirmation_gaps']))
# Session persistence is a DB fact, not the in-memory A/B demo map.
from app import db as dbmod
reopened=dbmod.DB(m.db.path)
check('新DB实例仍识别scope session',s.matter_session(reopened,cl['session_token'])['plan_id']==p2)
# A fresh plan has no pending other invitation; representative-only agreement must fail.
s.guest_condition(m.db,sess,{'时间':'周六'},[],0)
pr=agents.plan_replan(m.db,Stub(),p2)
a2=m.db.row("SELECT participant_id FROM plan_participant WHERE plan_id=? AND owner_id='A'",(p2,))['participant_id']
s.decide_plan(m.db,p2,a2,pr['version'],'accept',actor_id='A')
class Review:
    def chat_json(self,*a,**kw):return {'decision':'accept','reason':'代表建议','changes':{}}
agents.plan_review(m.db,Review(),p2,cl['participant_id'],pr['version'])
check('guest代表accept不是本人accept且不约定',not s.plan_status(m.db,p2)['agreed'] and s.plan_status(m.db,p2)['required_agreed']==1 and next(x for x in s.plan_status(m.db,p2)['participants'] if x['owner_id']==cl['guest_id'])['state']=='reviewed')
s.decide_plan(m.db,p2,cl['participant_id'],pr['version'],'accept',actor_id=cl['guest_id'])
check('guest本人独立当前版accept才约定',s.plan_status(m.db,p2)['agreed'])
check('guest代表不得覆盖已明确本人accept',throws(lambda:agents.plan_review(m.db,Review(),p2,cl['participant_id'],pr['version'])) and s.plan_status(m.db,p2)['agreed'])
s.update_plan_condition(m.db,p2,'A',{'时间':'周日'})
nextp=agents.plan_replan(m.db,Stub(),p2)
class RaceDecision:
    def chat_json(self,*a,**kw):
        s.decide_plan(m.db,p2,cl['participant_id'],nextp['version'],'reject','本人不同意',actor_id=cl['guest_id'])
        return {'decision':'accept','reason':'迟到代表','changes':{}}
check('review等待中本人reject不被代表覆盖',throws(lambda:agents.plan_review(m.db,RaceDecision(),p2,cl['participant_id'],nextp['version'])) and m.db.row('SELECT decision,decided_by FROM plan_decision WHERE plan_id=? AND participant_id=? AND version=?',(p2,cl['participant_id'],nextp['version']))=={'decision':'reject','decided_by':'person'})
print(f'{sum(ok for _,ok in checks)}/{len(checks)} passed')
sys.exit(any(not ok for _,ok in checks))
