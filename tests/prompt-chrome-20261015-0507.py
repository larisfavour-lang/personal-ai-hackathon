import asyncio, base64, json, pathlib, subprocess, time, urllib.request
import websockets

PORT=9579
PROFILE="/tmp/codex-supervision-prompt-chrome-20261015"
RESULT="/tmp/codex-supervision-prompt-chrome-20261015.json"
class CDP:
    def __init__(self, ws):
        self.ws=ws; self.seq=0; self.pending={}; self.calls=[]; self.mode="ok"
    async def pump(self):
        async for raw in self.ws:
            m=json.loads(raw)
            if m.get("id") in self.pending:
                self.pending.pop(m["id"]).set_result(m)
            elif m.get("method")=="Fetch.requestPaused":
                asyncio.create_task(self.fulfill(m["params"]))
    async def send(self, method, **params):
        self.seq+=1; n=self.seq
        f=asyncio.get_running_loop().create_future(); self.pending[n]=f
        await self.ws.send(json.dumps({"id":n,"method":method,"params":params}))
        r=await asyncio.wait_for(f, 15)
        if "error" in r: raise RuntimeError(str(r["error"]))
        return r.get("result",{})
    async def fulfill(self,p):
        req=p["request"]
        self.calls.append({"url":req["url"],"body":json.loads(req.get("postData") or "{}")})
        body={"kind":"family_update","reply":"浏览器验收测试回复"} if self.mode=="ok" else {"detail":[{"loc":["body","utterance"],"msg":"测试字段错误"}]}
        await self.send("Fetch.fulfillRequest",requestId=p["requestId"],responseCode=200 if self.mode=="ok" else 422,
                        responseHeaders=[{"name":"Content-Type","value":"application/json"}],
                        body=base64.b64encode(json.dumps(body,ensure_ascii=False).encode()).decode())
    async def js(self, expr):
        r=await self.send("Runtime.evaluate",expression=expr,returnByValue=True,awaitPromise=True)
        if r.get("exceptionDetails"): raise RuntimeError(str(r["exceptionDetails"]))
        return r.get("result",{}).get("value")
    async def wait(self, expr):
        for _ in range(60):
            v=await self.js(expr)
            if v: return v
            await asyncio.sleep(.15)
        raise RuntimeError("页面等待超时: "+expr[:100])
    async def click(self, selector):
        await self.js("document.querySelector("+json.dumps(selector)+").scrollIntoView({block:'center',inline:'center'})")
        b=await self.js("(function(){const e=document.querySelector("+json.dumps(selector)+");if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
        if not b: raise RuntimeError("无元素 "+selector)
        print(json.dumps({"click_target":selector,"point":b,"hit":await self.js("document.elementFromPoint("+str(b['x'])+","+str(b['y'])+")?.outerHTML.slice(0,350)")},ensure_ascii=False),flush=True)
        await self.js("document.querySelector("+json.dumps(selector)+").click()")
        await asyncio.sleep(.2)
async def main():
    checks=[]
    def check(n,ok,detail=None): checks.append({"name":n,"ok":bool(ok),"detail":detail})
    chrome=subprocess.Popen(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome","--headless=new","--disable-gpu","--no-first-run","--no-default-browser-check","--window-size=430,1000","--remote-debugging-port="+str(PORT),"--user-data-dir="+PROFILE,"about:blank"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        tabs=None
        for _ in range(60):
            try:
                tabs=json.load(urllib.request.urlopen("http://127.0.0.1:%d/json/list"%PORT,timeout=1));break
            except Exception: await asyncio.sleep(.15)
        if not tabs: raise RuntimeError("隔离Chrome未能启动")
        page=next(x for x in tabs if x["type"]=="page")
        async with websockets.connect(page["webSocketDebuggerUrl"],max_size=10*1024*1024) as ws:
            c=CDP(ws); pump=asyncio.create_task(c.pump())
            await c.send("Page.enable")
            await c.send("Page.bringToFront")
            await c.send("Runtime.enable")
            await c.send("Emulation.setDeviceMetricsOverride",width=430,height=900,deviceScaleFactor=1,mobile=False)
            await c.send("Fetch.enable",patterns=[{"urlPattern":"*/api/say","requestStage":"Request"}])
            await c.send("Page.navigate",url="http://127.0.0.1:8000/")
            await c.wait("document.querySelectorAll('#quick button').length > 0 && document.querySelector('#status-line').textContent.includes('准备就绪')")
            initial=await c.js("document.querySelectorAll('#quick button').length")
            preset=await c.js("document.querySelector('#quick button').textContent")
            await c.js("document.querySelector('#input').focus()")
            await c.send("Input.insertText",text=preset)
            await c.click("#quick button")
            for _ in range(40):
                if c.calls: break
                await asyncio.sleep(.1)
            if not c.calls:
                shot=await c.send("Page.captureScreenshot",format="png")
                pathlib.Path("/tmp/codex-supervision-prompt-failure.png").write_bytes(base64.b64decode(shot["data"]))
                print(json.dumps({"error":"鼠标未触发请求","diagnostic":await c.js("({url:location.href,quick:document.querySelector('#quick').innerHTML,input:document.querySelector('#input').value,errors:window.__paErr,viewport:[innerWidth,innerHeight],status:document.querySelector('#status-line').textContent})")},ensure_ascii=False))
                raise RuntimeError("未取得真实发送请求，测试未形成有效证据")
            await c.wait("document.querySelector('#status-line').textContent.includes('准备就绪')")
            state=await c.js("({input:document.querySelector('#input').value,n:document.querySelectorAll('#quick button').length})")
            check("真实Chrome DOM点击预设后清框",state["input"]=="",state)
            check("真实Chrome横栏仅移除已用条目",state["n"]==initial-1,state)
            check("一次点击只发一次正确文本",len(c.calls)==1 and c.calls[0]["body"].get("utterance")==preset,{"requests":len(c.calls)})
            check("发送体不带身份字段","owner_id" not in c.calls[0]["body"],None)
            c.mode="error"
            # 失败用例点的是横栏上第二条（第一条已在成功用例里发出并移除）。
            # 要比的「原句」必须是这一条自己的文本，不是开头记下的第一条：
            # 拿第一条比，失败提示明明带着正确原句，断言也会判错。
            preset2=await c.js("document.querySelector('#quick button').textContent")
            await c.click("#quick button")
            await c.wait("document.querySelector('#status-line').textContent.includes('准备就绪') && [...document.querySelectorAll('.bubble')].some(e=>e.textContent.includes('测试字段错误'))")
            state3=await c.js("({input:document.querySelector('#input').value,n:document.querySelectorAll('#quick button').length})")
            check("预设失败不回填输入框",state3["input"]=="",state3)
            check("预设失败后横栏不出现重复条目",state3["n"]==initial-2,state3["n"])
            errtext=await c.js("[...document.querySelectorAll('.bubble')].map(e=>e.textContent).find(t=>t.includes('测试字段错误'))||''")
            check("422错误为可读文本",errtext and "[object Object]" not in errtext,errtext)
            check("失败回复内提供再发一次",await c.js("!!document.querySelector('#stream button[data-act=\"resend-preset\"]')"),None)
            resend_text=await c.js("(document.querySelector('#stream button[data-act=\"resend-preset\"]')||{dataset:{}}).dataset.text||''")
            check("再发一次带原句",resend_text==preset2,{"resend_text":resend_text,"preset2":preset2})
            typed="手打草稿保留验收"
            await c.js("document.querySelector('#input').focus()")
            await c.send("Input.insertText",text=typed)
            await c.click("#send")
            await c.wait("document.querySelector('#status-line').textContent.includes('准备就绪') && document.querySelector('#input').value === "+json.dumps(typed))
            check("手打失败保留原草稿",await c.js("document.querySelector('#input').value") == typed,None)
            # 点「再发一次」。此时接口仍在错误模式，重发会再失败一次：
            # 期待的是「又发了一次原句」和「旧按钮被换掉、不是叠一个」，
            # 不是按钮消失——失败后仍要留一条能再点的路。
            before_resend=await c.js("document.querySelectorAll('#stream button[data-act=\"resend-preset\"]').length")
            # 请求条数必须在点之前取：点了之后再去取，等待条件当场就成立
            # （上一条失败的收尾状态正好满足它），计数器停在旧值上，
            # 于是新请求被算进「点击前」，断言看到的是空列表。
            n_before_click=len(c.calls)
            await c.click('#stream button[data-act="resend-preset"]')
            await c.wait("document.querySelectorAll('#stream button[data-act=\"resend-preset\"]').length >= 1 && document.querySelector('#input').value === ''")
            for _ in range(60):
                if len(c.calls)>n_before_click: break
                await asyncio.sleep(.1)
            resent=[x for x in c.calls[n_before_click:] if x["body"].get("utterance")==preset2]
            check("再发一次真的重发原句",len(resent)>=1,{"new_calls":[x["body"].get("utterance") for x in c.calls[n_before_click:]]})
            check("再发一次不叠按钮",await c.js("document.querySelectorAll('#stream button[data-act=\"resend-preset\"]').length")<=before_resend,{"before":before_resend,"after":await c.js("document.querySelectorAll('#stream button[data-act=\"resend-preset\"]').length")})
            check("再发一次也不回填输入框",await c.js("document.querySelector('#input').value")=="",None)
            check("再发一次后横栏仍未变回",await c.js("document.querySelectorAll('#quick button').length")==initial-2,None)
            check("全部发送请求均被截获，恰好四次",len(c.calls)==4,{"requests":len(c.calls)})
            shot=await c.send("Page.captureScreenshot",format="png")
            pathlib.Path("/tmp/codex-supervision-prompt-chrome-20261015.png").write_bytes(base64.b64decode(shot["data"]))
            pump.cancel()
        result={"at_utc":time.strftime("%Y-%m-%d %H:%M:%S UTC",time.gmtime()),"browser":"isolated desktop Chrome 430x900","source":"http://127.0.0.1:8000/","api_say_intercepted":True,"model_calls":0,"task_writes":0,"checks":checks,"passed":sum(x["ok"] for x in checks),"total":len(checks),"boundary":"真实Chrome加载运行中页面，以DOM click触发真实事件处理、CDP键盘输入；api/say被CDP拦截；不是鼠标命中、安卓实机或真实模型链路证明。"}
        pathlib.Path(RESULT).write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False))
        return 0 if all(x["ok"] for x in checks) else 1
    finally:
        chrome.terminate()
        try: chrome.wait(timeout=5)
        except subprocess.TimeoutExpired: chrome.kill();chrome.wait()
raise SystemExit(asyncio.run(main()))

