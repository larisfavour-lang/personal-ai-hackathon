"""t26 stateful actual DOM tests: synthetic HTTP fixture OR --live BASE.
Fixture mode writes no app DB, calls no model. --live uses provided isolated
server and separate Chrome profiles; persistence/revocation is backend real,
model must be identified separately (this suite never claims real Step/Android).
Only sanitized screenshot is committed; invitation secrets are not printed.
"""
import argparse, asyncio, base64, functools, hashlib, json, pathlib, subprocess
import tempfile, threading, time, urllib.parse, urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import websockets
ROOT=pathlib.Path(__file__).resolve().parents[1]
ns={'__file__':str(ROOT/'tests/prompt-chrome-20261015-0507.py')}
exec((ROOT/'tests/prompt-chrome-20261015-0507.py').read_text().rsplit('raise SystemExit',1)[0],ns)

class Fixture(SimpleHTTPRequestHandler):
    calls=[]; invites=[]; notes=[]; fields={'正文':'周六上午去公园','备注':'PRIVATE_SOURCE_DO_NOT_DISCLOSE'}
    version=1; replan=False; approved=False; revoked=False; accepted=False; fail=False; condition={}; children=[]; count=None
    def log_message(self,*_):pass
    def send_json(self,x,status=200):
        b=json.dumps(x,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
    @classmethod
    def plan(cls,guest=False):
        d={'plan_id':'plan_ui','title':'周末家庭活动','utterance':'周末三家一起带孩子出去玩，我出100以内','requested_families':3,'unknown_families':1,'current_version':cls.version,'needs_replan':cls.replan,'agreed':False,'confirmation_gaps':['第三家与孩子出席未确认'],'participants':[{'owner_id':'A','participant_id':'part_A','label':'本人','state':'pending','conditions':{'本人出资上限':'100元以内'}}],'waiting_on':['自称家庭'],'proposal':{'时间':'周六上午','地点':'公园'}}
        if guest:d['decisions']=[{'participant_id':'guest_part','version':cls.version,'decision':'accept','decided_by':'guest_ui'}]if cls.accepted else []
        if guest:d.update(me={'owner_id':'guest_ui','participant_id':'guest_part','label':'来宾一家','conditions':cls.condition,'children':cls.children,'children_count':cls.count,'state':'accept' if cls.accepted else 'pending'},approved=cls.approved,csrf_token='fixture-csrf',identity_note='邀请参与者自称；未核验账号身份')
        return d
    def do_GET(self):
        p=urllib.parse.urlsplit(self.path).path;type(self).calls.append((p,'GET',{}))
        if p=='/':self.path='/app/static/index.html';return super().do_GET()
        if p.startswith('/static/'):self.path='/app'+p;return super().do_GET()
        if p=='/api/participation/plan':
            if self.revoked:return self.send_json({'detail':'邀请已撤回'},403)
            return self.send_json(self.plan(True))
        if p=='/api/plan/plan_ui':return self.send_json(self.plan())
        if p=='/api/plan/plan_ui/invitations':return self.send_json({'invitations':self.invites})
        if p=='/api/plan/plan_ui/linked-notes':return self.send_json({'notes':self.notes})
        if p.startswith('/api/note/') and p.endswith('/fields'):return self.send_json({'event_id':p.split('/')[-2],'fields':self.fields,'provider':'本人授权虚构便签','happened_on':'2026-10-14'})
        d={'/api/me':{'user_id':'A','display_name':'本人'},'/api/conversation':{'turns':[]},'/api/matters':{'matters':[],'waiting':[]},'/api/overview':{'persons':[]},'/api/plans':{'plans':[self.plan()]},'/api/note/linkable-plans':{'plans':[self.plan()]},'/api/ui-revision':{'revision':'fixture'},'/api/health':{'model':{'ok':True}},'/api/model-config':{}}
        if p.startswith('/api/'):return self.send_json(d.get(p,{}))
        return super().do_GET()
    def do_POST(self):
        p=urllib.parse.urlsplit(self.path).path;b=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0')))or'{}');type(self).calls.append((p,'POST',b));C=type(self)
        if p=='/api/plan/plan_ui/invitations':
            C.invites=[{'invitation_id':'inv_ui','label':b['label'],'state':'invited','approved':False,'expires_at':'2026-10-17 03:00','guest_id':'','claimed_name':''}]
            return self.send_json({'invitation_id':'inv_ui','expires_at':'2026-10-17 03:00','participate_url':'/static/participate.html#token='+('x'*43),'token':'x'*43})
        if p=='/api/participation/accept':
            if C.invites and C.invites[0]['guest_id']:return self.send_json({'detail':'邀请已使用'},409)
            C.invites[0].update(state='claimed',guest_id='guest_ui',claimed_name=b['display_name']);return self.send_json({'csrf_token':'fixture-csrf','plan_id':'plan_ui'})
        if p.endswith('/approve'):C.approved=True;C.invites[0]['approved']=True;return self.send_json({'ok':True})
        if p.endswith('/invitations/inv_ui/revoke'):C.revoked=True;C.invites[0]['state']='revoked';return self.send_json({'ok':True})
        if p.startswith('/api/participation/'):
            if C.revoked:return self.send_json({'detail':'已撤回'},403)
            if self.headers.get('X-CSRF-Token')!='fixture-csrf':return self.send_json({'detail':'CSRF'},403)
            if p.endswith('/condition'):
                time.sleep(.2)
                if C.fail:return self.send_json({'detail':'合成保存失败'},503)
                C.condition=b['conditions'];C.children=b['children'];C.count=b['children_count'];return self.send_json({'ok':True})
            if p.endswith('/decide'):
                if not C.approved or b['version']!=C.version or C.replan:return self.send_json({'detail':'当前版本不可确认'},409)
                C.accepted=b['decision']=='accept';return self.send_json({'ok':True})
            return self.send_json({'ok':True})
        if p in ['/api/note/link-plan','/api/plan/create-with-note']:
            if C.fail:return self.send_json({'detail':'合成关联失败'},503)
            if b.get('public_conditions') and (not b.get('publish_confirmed') or any(v not in [C.fields[k] for k in b['selected_fields']] for v in b['public_conditions'].values())):return self.send_json({'detail':'未确认公开'},403)
            old=next((n for n in C.notes if n['event_id']==b['event_id']),None)
            n={'event_id':b['event_id'],'link_id':'link_ui','status':'active','fields':{k:C.fields[k]for k in b['selected_fields']},'used':False,'provider':'本人授权虚构便签','happened_on':'2026-10-14'}
            if old:C.notes.remove(old)
            C.notes.append(n)
            if b.get('public_conditions'):C.replan=True
            return self.send_json({'plan_id':'plan_ui','linked':True})
        if p.endswith('/correct'):
            C.fields={**C.fields,**b['payload']};C.notes[0].update(status='stale',replacement_event_id='event_new');C.replan=True;return self.send_json({'new_event_id':'event_new'})
        if p.endswith('/revoke') or p=='/api/note/unlink-plan':
            C.replan=True
            if C.notes:C.notes[0]['status']='revoked' if p.endswith('/revoke') else 'unlinked'
            return self.send_json({'ok':True})
        if p.endswith('/replan') or p.endswith('/propose'):C.replan=False;C.version+=1;C.accepted=False;return self.send_json({'ok':True})
        return self.send_json({'ok':True})

class SafeCDP(ns['CDP']):
    def __init__(self,ws):super().__init__(ws);self.requests=[]
    async def pump(self):
        async for raw in self.ws:
            m=json.loads(raw)
            if m.get('id') in self.pending:self.pending.pop(m['id']).set_result(m)
            elif m.get('method')=='Network.requestWillBeSent':self.requests.append(m['params']['request']['url'])
    async def wait(self,expr):
        for _ in range(1400):
            v=await self.js('Boolean('+expr+')')
            if v:return v
            await asyncio.sleep(.15)
        print('WAIT_STATE',await self.js('document.body.innerText'),flush=True);raise RuntimeError('UI wait timeout: '+expr[:100])
class Browser:
    def __init__(self,port):self.port=port;self.process=None
    async def start(self,base):
        self.profile=tempfile.mkdtemp(prefix='t26-chrome-');self.process=subprocess.Popen(['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome','--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--remote-debugging-port='+str(self.port),'--user-data-dir='+self.profile,'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:tabs=json.load(urllib.request.urlopen(f'http://127.0.0.1:{self.port}/json/list',timeout=.3));break
            except Exception:await asyncio.sleep(.1)
        self.ws=await websockets.connect(next(t['webSocketDebuggerUrl']for t in tabs if t['type']=='page'),max_size=20*1024*1024);self.c=SafeCDP(self.ws);self.pump=asyncio.create_task(self.c.pump());await self.c.send('Network.enable');await self.c.send('Runtime.enable');await self.c.send('Page.enable');await self.c.send('Emulation.setDeviceMetricsOverride',width=375,height=900,deviceScaleFactor=1,mobile=True)
        await self.go(base);return self.c
    async def go(self,url):await self.c.send('Page.navigate',url=url);await asyncio.sleep(.5)
    async def close(self):
        if self.process:
            self.pump.cancel();await self.ws.close();self.process.terminate()
            try:self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
async def set_value(c,sel,value):await c.js('(()=>{const e=document.querySelector('+json.dumps(sel)+');e.value='+json.dumps(value)+';e.dispatchEvent(new Event("input",{bubbles:true}));e.dispatchEvent(new Event("change",{bubbles:true}));})()')
async def layout(c):
    return await c.js('({overflow:document.documentElement.scrollWidth>innerWidth,small:[...document.querySelectorAll("button:not(:disabled)")].filter(x=>x.getBoundingClientRect().width&&x.getBoundingClientRect().height<44).length})')
async def screenshot_panel(c,selector='h1'):
    await c.js('document.querySelector('+json.dumps(selector)+').scrollIntoView({block:"start"})')
    await asyncio.sleep(.1)
    shot=await c.send('Page.captureScreenshot',format='png',captureBeyondViewport=False)
    return base64.b64decode(shot['data'])
def save_panels(path,panels):
    from PIL import Image,ImageDraw
    import io
    images=[Image.open(io.BytesIO(p)) for label,p in panels]
    out=Image.new('RGB',(sum(i.width for i in images),max(i.height for i in images)+34),'#f7f6f3');draw=ImageDraw.Draw(out);x=0
    for (label,_),im in zip(panels,images):draw.text((x+10,8),label,fill='#34463f');out.paste(im,(x,34));x+=im.width
    out.save(path)
async def run(base,live=False):
    checks=[];panels=[]
    def check(n,ok):checks.append((n,bool(ok)));print(('PASS 'if ok else'FAIL ')+n,flush=True)
    a,g=Browser(9846),Browser(9847)
    try:
        c=await a.start(base);await c.wait('typeof openPlan==="function"&&ownerId==="A"')
        if live:
            res=await c.js('post("/api/plan/create",{title:"虚构家庭UI持久验收"+Date.now(),participants:[{owner_id:"A",label:"本人",conditions:{"时间":"周六上午","本人出资上限":"100元以内"}}]})');pid=res.get('plan_id')or res.get('plan',{}).get('plan_id');assert pid,'create response '+str(res)
        else:pid='plan_ui'
        await c.js('openPlan('+json.dumps(pid)+')');await asyncio.sleep(.4)
        if not await c.js('!!document.querySelector("[data-invite-create]")'):
            print('DEBUG',await c.js('document.querySelector("#sheet-body").innerText'),[p for p,m,b in Fixture.calls],flush=True)
        await c.wait('!!document.querySelector("[data-invite-create]")')
        await set_value(c,'#invite-label','来宾一家');await c.click('[data-invite-create]');await c.wait('!!document.querySelector("#invite-url")');url=await c.js('document.querySelector("#invite-url").value')
        check('邀请只在fragment，无query或B身份',urllib.parse.urlsplit(url).fragment.startswith('token=')and not urllib.parse.urlsplit(url).query)
        q=await g.start(url);await q.wait('!!document.querySelector("#claim")');panels.append(('1. Explicit invitation acceptance',await screenshot_panel(q)))
        check('接收前URL已清token，尚未自动claim',await q.js('location.hash===""&&!!document.querySelector("#claim")'));check('token未落local/session/DOM',await q.js('localStorage.length===0&&sessionStorage.length===0&&!document.documentElement.innerHTML.includes('+json.dumps(urllib.parse.urlsplit(url).fragment.split('=',1)[1])+')'));await q.js('document.querySelector("#guest-name").focus()');check('实际键盘焦点可见',await q.js('document.activeElement.id==="guest-name"&&parseFloat(getComputedStyle(document.activeElement).outlineWidth)>=2'))
        check('未初始化普通页面资料与配置',await q.js('!document.querySelector("#side")&&!document.querySelector("#btn-config")&&localStorage.length===0'))
        await set_value(q,'#guest-name','来宾一家');await q.click('#claim');await q.wait('!!document.querySelector("#save-condition")')
        check('接邀请不同于家庭认可/方案接受',await q.js('document.querySelector("#approval").textContent.includes("等待")&&document.querySelector("#accept-version").disabled'))
        await set_value(q,'[data-condition="时间"]','周六上午');await set_value(q,'#child-count','2');await set_value(q,'#children','孩子一|来\n孩子二|待定')
        if not live:
            Fixture.fail=True;await q.click('#save-condition');await q.wait('!document.querySelector("#save-condition").disabled');check('失败留本人输入，可重试',await q.js('document.querySelector("[data-condition=时间]").value==="周六上午"'));Fixture.fail=False
        await q.click('#save-condition');await q.wait('!!document.querySelector("#status").textContent.includes("已保存")')
        check('仅保存本方孩子与条件',await q.js('document.querySelector("#child-count").value==="2"'))
        check('名单数2不冒充2人已出席，待定仍待确认',await q.js('document.querySelector("label[for=child-count]").textContent==="本人提供的孩子名单数"&&document.body.innerText.includes("每个孩子的出席分别确认")&&document.body.innerText.includes("留空表示名单人数未知，0 表示没有孩子")&&document.querySelector("#children").value==="孩子一|来\\n孩子二|待定"&&document.querySelector(".state").textContent.includes("待确认")&&!document.querySelector(".state").textContent.includes("已约定")'))
        await c.js('openPlan('+json.dumps(pid)+')');await c.wait('!!document.querySelector("[data-invite-approve]")');await c.click('[data-invite-approve]');await asyncio.sleep(.3)
        if live:
            await c.js('post("/api/plan/"+'+json.dumps(pid)+'+"/propose",{})')
        await q.click('#refresh');await q.wait('!!document.querySelector("#approval").textContent.includes("已认可")')
        check('认可自称家庭不是实名或代接受',await q.js('document.querySelector("#approval").textContent.includes("不是实名")'))
        if await q.js('document.querySelector("#accept-version").disabled'):
            await c.js('post("/api/plan/"+'+json.dumps(pid)+'+"/replan",{})');await q.click('#refresh')
        await q.wait('!document.querySelector("#accept-version").disabled');await q.click('#review');await q.wait('!!document.querySelector("#status").textContent.includes("代表审阅")')
        check('代表审阅不等于本人接受',await q.js('document.querySelector("#my-decision").textContent.includes("尚未本人确认")'));panels.append(('2. Representative is not personal consent',await screenshot_panel(q,'#my-decision')))
        await q.click('#accept-version');await q.wait('!!document.querySelector("#status").textContent.includes("已记录本人")');await g.go(base+'/static/participate.html');await q.wait('!!document.querySelector("#save-condition")')
        check('独立profile刷新恢复同事项/本方条件',await q.js('document.querySelector("#child-count").value==="2"&&document.querySelector("[data-condition=时间]").value==="周六上午"'))
        check('layout375无overflow且44px按钮', (await layout(q))=={'overflow':False,'small':0})
        for w in [320,414,768,1280]:
            await q.send('Emulation.setDeviceMetricsOverride',width=w,height=900,deviceScaleFactor=1,mobile=w<768);check('参与页宽度'+str(w)+'无横溢',not(await layout(q))['overflow'])
        await q.send('Emulation.setDeviceMetricsOverride',width=375,height=900,deviceScaleFactor=1,mobile=True)
        # Sanitized screen contains no token, cookies or private source.
        save_panels(ROOT/'tests/participation-ui.png',panels)
        if live:
            check('guest普通私人API被禁止，不回落A',await q.js('(async()=>{const r=await fetch("/api/me");return r.status===403})()'))
        else:
            check('guest页面全部API请求只限participation',all('/api/participation/' in u for u in q.requests if '/api/' in u))
            # second claim uses the same capability, server rejects it.
            check('单用邀请第二次兑换拒绝',await q.js('(async()=>{const r=await fetch("/api/participation/accept",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:"'+'x'*43+'",display_name:"其他自称"})});return r.status===409})()'))
        await c.js('window.confirm=()=>true;openPlan('+json.dumps(pid)+')');await c.wait('!!document.querySelector("[data-invite-revoke]")');await c.click('[data-invite-revoke]');await q.click('#refresh');await q.wait('!document.querySelector("#save-condition")')
        check('撤回清受限页面不回首页不露私人',await q.js('!document.querySelector("#save-condition")&&!document.querySelector("#side")&&location.pathname==="/static/participate.html"'))
        print(json.dumps({'mode':'live isolated server with two Chrome profiles'if live else'stateful synthetic HTTP fixture','model':'isolated real app with explicit fixture upstream; NOT Step'if live else'0 model calls','source_sha256':hashlib.sha256((ROOT/'app/static/participate.html').read_bytes()).hexdigest(),'passed':sum(ok for _,ok in checks),'total':len(checks)},ensure_ascii=False));return 0 if all(ok for _,ok in checks)else 1
    finally:await a.close();await g.close()

def serve_isolated_fixture_model():
    """Run the real app/DB/routes with only upstream model replaced; no real key."""
    import os,uvicorn
    assert os.environ.get('PA_DATA_DIR','').startswith('/tmp/t26-')
    from app import main as application
    class Model:
        calls=0
        def chat_json(self,messages,**kw):
            self.calls+=1
            system=messages[0].get('content','')
            if 'decision' in system:
                return {'decision':'accept','reason':'明确的界面测试模型代表审阅','changes':{}}
            return {'content':{'时间':'周六上午'},'reason':'界面测试模型按本人条件','conflicts':[]}
        def ping(self):return {'ok':True,'model':'UI-fixture-only-no-Step'}
        def chat(self,*a,**kw):return 'ok'
        api_key='' 
        base_url='http://127.0.0.1/fixture-no-network'
        model='UI-fixture-only-no-Step'
    application.client=Model()
    uvicorn.run(application.app,host='127.0.0.1',port=18828,log_level='warning',access_log=False)

def main():
    p=argparse.ArgumentParser();p.add_argument('--live');p.add_argument('--serve-isolated-fixture-model',action='store_true');a=p.parse_args();server=None
    if a.serve_isolated_fixture_model:serve_isolated_fixture_model();return 0
    if a.live:
        assert a.live.rstrip('/')=='http://127.0.0.1:18828','Live mode restricted to separately initialized UI test backend18828'
        base=a.live.rstrip('/')
    else:
        server=ThreadingHTTPServer(('127.0.0.1',18826),functools.partial(Fixture,directory=str(ROOT)));threading.Thread(target=server.serve_forever,daemon=True).start();base='http://127.0.0.1:18826'
    try:return asyncio.run(run(base,bool(a.live)))
    finally:
        if server:server.shutdown();server.server_close()
if __name__=='__main__':raise SystemExit(main())
