import asyncio, base64, json, pathlib, socket, subprocess, time, urllib.request
import websockets
ns={};exec(pathlib.Path(__file__).with_name('prompt-chrome-20261015-0507.py').read_text().rsplit('raise SystemExit',1)[0],ns)
class ReadCDP(ns['CDP']):
    def __init__(self,ws):
        super().__init__(ws);self.loaded=asyncio.Event();self.gets=0
    async def pump(self):
        async for raw in self.ws:
            m=json.loads(raw)
            if m.get('id') in self.pending:self.pending.pop(m['id']).set_result(m)
            elif m.get('method')=='Page.loadEventFired':self.loaded.set()
            elif m.get('method')=='Fetch.requestPaused':asyncio.create_task(self.fulfill(m['params']))
    async def fulfill(self,p):
        r=p['request']
        if r['method'] in ('GET','HEAD'):
            self.gets+=1;await self.send('Fetch.continueRequest',requestId=p['requestId']);return
        self.calls.append({'method':r['method'],'url':r['url']})
        await self.send('Fetch.fulfillRequest',requestId=p['requestId'],responseCode=503,responseHeaders=[{'name':'Content-Type','value':'application/json'}],body=base64.b64encode(b'{"detail":"read-only supervision"}').decode())

async def main():
    checks=[]
    def check(n,ok,d=None):
        checks.append({'name':n,'ok':bool(ok),'detail':d});print(json.dumps(checks[-1],ensure_ascii=False),flush=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    proc=subprocess.Popen(['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome','--headless=new','--no-first-run','--no-default-browser-check','--window-size=430,1000','--remote-debugging-port='+str(port),'--user-data-dir=/tmp/codex-supervision-history-'+str(port),'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        tabs=None
        for _ in range(90):
            try:
                tabs=json.load(urllib.request.urlopen('http://127.0.0.1:%d/json/list'%port,timeout=1))
                if any(x['type']=='page' for x in tabs):break
            except Exception:pass
            await asyncio.sleep(.15)
        if not tabs:raise RuntimeError('Chrome未启动')
        tab=next(x for x in tabs if x['type']=='page')
        async with websockets.connect(tab['webSocketDebuggerUrl'],max_size=20*1024*1024) as ws:
            c=ReadCDP(ws);pump=asyncio.create_task(c.pump())
            await c.send('Page.enable');await c.send('Runtime.enable');await c.send('Page.bringToFront')
            await c.send('Emulation.setDeviceMetricsOverride',width=430,height=900,deviceScaleFactor=1,mobile=False)
            await c.send('Fetch.enable',patterns=[{'urlPattern':'*/api/*','requestStage':'Request'}])
            await c.send('Page.navigate',url='http://127.0.0.1:8000/')
            await asyncio.wait_for(c.loaded.wait(),20)
            await c.wait("typeof openPerson === 'function' && document.querySelector('#status-line').textContent.includes('准备就绪')")
            record=await c.js("(async()=>{let d=await api('/api/person/p_xiaoyu');let e=(d.life_events||[]).find(e=>(e.attachments||[]).some(a=>a.attachment_id==='att_198e77e4be57'));return e?{id:e.event_id,text:e.fields['正文'],attachment:e.attachments.find(a=>a.attachment_id==='att_198e77e4be57').attachment_id}:null})()")
            check('真实人物详情含既有保存事件的正文和附件',bool(record) and bool(record.get('text')),record)
            if not record:raise RuntimeError('既有测试事件未找到，未形成浏览器回读证据')
            sel='img[src$="/'+record['attachment']+'"]'
            async def verify(tag):
                await c.js("(async()=>{await openPerson('p_xiaoyu');return true})()")
                await c.wait('!!document.querySelector('+json.dumps(sel)+')')
                await c.js('document.querySelector('+json.dumps(sel)+').scrollIntoView({block:"center"})')
                await c.wait('document.querySelector('+json.dumps(sel)+').complete')
                state=await c.js('(()=>{let i=document.querySelector('+json.dumps(sel)+');return {body:document.querySelector("#sheet-body").textContent.includes('+json.dumps(record['text'])+'),src:i.getAttribute("src"),complete:i.complete,w:i.naturalWidth,h:i.naturalHeight}})()')
                check(tag+'：正文来自已保存记录',state['body'],{'event_id':record['id']})
                check(tag+'：配图实际加载且可解码',state['complete'] and state['w']>0 and state['h']>0,state)
            await verify('首次打开')
            await c.js("document.querySelector('#sheet [data-close-sheet]').click()")
            await verify('关闭后重开')
            c.loaded.clear();await c.send('Page.reload',ignoreCache=True);await asyncio.wait_for(c.loaded.wait(),20)
            await c.wait("typeof openPerson === 'function' && document.querySelector('#status-line').textContent.includes('准备就绪')")
            await verify('完整刷新后重开')
            check('全程仅真实读取，无变更请求',len(c.calls)==0,{'blocked_mutations':c.calls,'api_gets':c.gets})
            shot=await c.send('Page.captureScreenshot',format='png');pathlib.Path(__file__).with_suffix('.png').write_bytes(base64.b64decode(shot['data']))
            pump.cancel()
        result={'utc':time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime()),'passed':sum(x['ok'] for x in checks),'total':len(checks),'checks':checks,'boundary':'隔离桌面Chrome430×900；复用主应用已实际保存事件，真实HTTP读取/图片加载；直接调用现有openPerson渲染器并完整Page.reload；全部非GET/HEAD请求拦截，未创建业务记录或调用识别；配图是1×1测试PNG，不是相机照片或安卓。'}
        pathlib.Path(__file__).with_suffix('.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False));return 0 if all(x['ok'] for x in checks) else 1
    finally:
        proc.terminate()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
raise SystemExit(asyncio.run(main()))
