import asyncio, base64, json, pathlib, socket, subprocess, time, urllib.request
import websockets
helper=pathlib.Path(__file__).with_name('history-browser-20261015-0654.py');ns={'__file__':str(helper)};exec(helper.read_text().rsplit('raise SystemExit',1)[0],ns)
class SubjectCDP(ns['ReadCDP']):
    async def fulfill(self,p):
        r=p['request']
        if r['method'] in ('GET','HEAD'):
            await self.send('Fetch.continueRequest',requestId=p['requestId']);return
        self.calls.append({'url':r['url'],'body':json.loads(r.get('postData') or '{}')})
        if r['url'].endswith('/correct'):
            await self.send('Fetch.fulfillRequest',requestId=p['requestId'],responseCode=200,responseHeaders=[{'name':'Content-Type','value':'application/json'}],body=base64.b64encode(json.dumps({'new_event_id':'evt_fixed'},ensure_ascii=False).encode()).decode());return
        if len([x for x in self.calls if x['url'].endswith('/api/suggestion')])>1:
            await self.send('Fetch.fulfillRequest',requestId=p['requestId'],responseCode=200,responseHeaders=[{'name':'Content-Type','value':'application/json'}],body=base64.b64encode(json.dumps({'status':'ok','suggestion':'按四十分钟算，下次加一次复习。','sources':[],'used_sources':[],'confidence':'high'},ensure_ascii=False).encode()).decode());return
        await self.send('Fetch.fulfillRequest',requestId=p['requestId'],responseCode=200,responseHeaders=[{'name':'Content-Type','value':'application/json'}],body=base64.b64encode(json.dumps({'status':'needs_confirm','suggestion':'','sources':[{'event_id':'evt_fixme','source':'synthetic','happened_on':'2026-10-02','fields':{'完成时间':'10分钟','正文':'哥哥自己练了四十分钟。'}}],'conflicts':[{'field':'完成时间','source':'synthetic','event_id':'evt_fixme','happened_on':'2026-10-02'}],'note':'记录里的完成时间和你后来写的不一致，先定用哪个，再给下一步建议。'},ensure_ascii=False).encode()).decode())
async def main():
    checks=[]
    def check(n,v,d):checks.append({'name':n,'ok':bool(v),'detail':d});print(json.dumps(checks[-1],ensure_ascii=False),flush=True)
    sock=socket.socket();port=int(__import__('os').environ.get('CHROME_PORT','9591'))
    proc=subprocess.Popen(['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome','--headless=new','--no-first-run','--no-default-browser-check','--window-size=430,1000','--remote-debugging-port='+str(port),'--user-data-dir=/tmp/codex-supervision-subject-'+str(port),'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
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
            c=SubjectCDP(ws);pump=asyncio.create_task(c.pump())
            await c.send('Page.enable');await c.send('Runtime.enable')
            await c.send('Emulation.setDeviceMetricsOverride',width=430,height=900,deviceScaleFactor=1,mobile=False)
            await c.send('Fetch.enable',patterns=[{'urlPattern':'*/api/*','requestStage':'Request'}])
            await c.send('Page.navigate',url='http://127.0.0.1:8000/apps');await asyncio.wait_for(c.loaded.wait(),20)
            await c.wait("typeof renderPlan==='function' && myPersons.length>0")
            await c.js("document.querySelector('[data-view=plan]').click()")
            await c.wait("!!document.querySelector('#btn-ask')")
            await c.js("document.querySelector('#btn-ask').click()")
            await c.wait("document.querySelector('#plan-result').textContent && !document.querySelector('#plan-result').textContent.includes('正在想')")
            st=await c.js("({text:document.querySelector('#plan-result').textContent,buttons:Array.from(document.querySelectorAll('#plan-result button')).map(b=>b.textContent),hasFix:!!document.querySelector('#ta-fix'),error:window.__paErr})")
            check('needs_confirm实际展示服务端核对说明', '记录里的完成时间' in st['text'] and '先定用哪个' in st['text'],st)
            check('冲突不被渲染为没有可用依据的成功空建议','没有可用依据' not in st['text'],st)
            check('冲突卡片给出可点的定值入口',st['hasFix'] and '定这个，再给建议' in st['buttons'],st)
            check('只有一次拦截建议请求且无页面脚本错误',len(c.calls)==1 and c.calls[0]['url'].endswith('/api/suggestion') and not st['error'],{'mutations':[x['url'] for x in c.calls],'error':st['error']})
            await c.js("document.querySelector('#ta-fix').value='四十分钟';document.querySelector('#btn-fix').click()")
            await c.wait("!document.querySelector('#btn-fix') && document.querySelector('#plan-result').textContent.includes('按四十分钟算')")
            after=await c.js("({text:document.querySelector('#plan-result').textContent,calls:window.__paCalls||[],error:window.__paErr})")
            urls=[x['url'] for x in c.calls]
            check('定值后真的发出纠正请求且载荷保留其余字段',any(u.endswith('/correct') for u in urls) and any((x['url'].endswith('/correct') and (x['body'] or {}).get('payload',{}).get('完成时间')=='四十分钟' and (x['body'] or {}).get('payload',{}).get('正文')=='哥哥自己练了四十分钟。') for x in c.calls),{'urls':urls,'bodies':[x['body'] for x in c.calls]})
            check('定值后自动重取建议并显示新建议','按四十分钟算' in after['text'] and not after['error'],after)
            shot=await c.send('Page.captureScreenshot',format='png');pathlib.Path(__file__).with_suffix('.png').write_bytes(base64.b64decode(shot['data']))
            pump.cancel()
        result={'utc':time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime()),'checks':checks,'passed':sum(x['ok'] for x in checks),'total':len(checks),'boundary':'隔离Chrome430×900；真实GET小应用页面/人物，DOM点周末安排和给建议；唯一POST /api/suggestion被拦截返回合成needs_confirm，0真实模型/共享业务写。只证实际前端对该后端状态的处理，不是实际HTTP后端或冲突解决后持久整链。'}
        pathlib.Path(__file__).with_suffix('.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False));return 0 if all(x['ok'] for x in checks) else 1
    finally:
        proc.terminate()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
raise SystemExit(asyncio.run(main()))
