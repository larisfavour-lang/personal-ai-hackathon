"""真实浏览器验：错误原因不是字符串时，界面显示人话而不是 [object Object]。

用 CDP 拦 /api/say，分别返回 detail 为对象、数组、字符串三种形态，
读真实 DOM 里失败回复的文本。
"""
import asyncio, base64, json, subprocess, time, urllib.request
import websockets

PORT = 9611
PROFILE = "/tmp/codex-supervision-errtext-20261015"
RESULT = "/tmp/codex-supervision-errtext-readability.json"

CASES = [
    ("detail 是对象", {"detail": {"code": "pair_expired", "message": "配对码已过期，请重新生成"}}),
    ("detail 是 422 数组", {"detail": [{"type": "missing", "loc": ["body", "utterance"], "msg": "Field required"}]}),
    ("detail 是字符串", {"detail": "这个会话不是该身份，不能替别家操作"}),
    ("error.message 是对象", {"error": {"code": "x", "message": {"msg": "设备已被撤销，请重新连接"}}}),
    ("完全空 body", {}),
]

class CDP:
    def __init__(self, ws):
        self.ws = ws; self.seq = 0; self.pending = {}
    async def pump(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if m.get("id") in self.pending:
                self.pending.pop(m["id"]).set_result(m)
    async def send(self, method, **params):
        self.seq += 1; n = self.seq
        f = asyncio.get_running_loop().create_future(); self.pending[n] = f
        await self.ws.send(json.dumps({"id": n, "method": method, "params": params}))
        r = await asyncio.wait_for(f, 20)
        if "error" in r: raise RuntimeError(str(r["error"]))
        return r.get("result", {})
    async def js(self, expr):
        r = await self.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"): raise RuntimeError(str(r["exceptionDetails"]))
        return r.get("result", {}).get("value")

async def main():
    checks = []
    def check(n, ok, d=None): checks.append({"name": n, "ok": bool(ok), "detail": d})
    chrome = subprocess.Popen(
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--headless=new",
         "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         "--window-size=430,900", "--remote-debugging-port=" + str(PORT),
         "--user-data-dir=" + PROFILE, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await chrome.wait() if False else None
        tabs = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json/list" % PORT, timeout=1)); break
            except Exception:
                await asyncio.sleep(.15)
        if not tabs: raise RuntimeError("Chrome 未启动")
        page = next(x for x in tabs if x["type"] == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=10*1024*1024) as ws:
            c = CDP(ws)
            pump = asyncio.create_task(c.pump())
            await c.send("Page.enable")
            await c.send("Fetch.enable", patterns=[{"urlPattern": "*"}])
            # 每个用例只拦一次 /api/say，其余请求放行
            state = {"payload": None}
            async def on_paused(p):
                url = p["request"]["url"]
                if "/api/say" in url and state["payload"] is not None:
                    body = base64.b64encode(json.dumps(state["payload"], ensure_ascii=False).encode()).decode()
                    await c.send("Fetch.fulfillRequest", requestId=p["requestId"], responseCode=409,
                                 responseHeaders=[{"name": "Content-Type", "value": "application/json"}], body=body)
                else:
                    await c.send("Fetch.continueRequest", requestId=p["requestId"])
            # pump 里转发：重写一个受管循环
            async def pump2():
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("id") in c.pending:
                        c.pending.pop(m["id"]).set_result(m)
                    elif m.get("method") == "Fetch.requestPaused":
                        asyncio.create_task(on_paused(m["params"]))
            pump.cancel()
            p2 = asyncio.create_task(pump2())

            await c.send("Page.navigate", url="http://127.0.0.1:8000/")
            await c.js("new Promise(r=>{const t=setInterval(()=>{if(document.querySelector('#quick button')){clearInterval(t);r(1)}},100)})")
            for name, payload in CASES:
                state["payload"] = payload
                await c.js("(function(){const q=document.querySelector('#quick button');if(q)q.click();})()")
                # 等失败回复出现
                for _ in range(80):
                    txt = await c.js(
                        "(function(){const b=[...document.querySelectorAll('#stream .bubble')]"
                        ".map(e=>e.textContent||'').filter(t=>t.indexOf('没发出去')>=0);"
                        "return b.length?b[b.length-1]:''})()")
                    if txt: break
                    await asyncio.sleep(.15)
                if not txt:
                    diag = await c.js(
                        "(function(){return {quick: document.querySelectorAll('#quick button').length,"
                        "streamKids: document.querySelectorAll('#stream > *').length,"
                        "busy: (typeof busy!=='undefined'?String(busy):'na'),"
                        "tail: (document.querySelector('#stream').innerText||'').slice(-260),"
                        "sayCalls: (typeof fetchLog!=='undefined'?'na':'na')}})()")
                    diag["case"] = name
                    checks.append({"name": "诊断：%s 没有出现失败回复" % name, "ok": False, "detail": diag})
                bad = "[object Object]" in txt or "undefined" in txt
                check("真实浏览器：%s 显示人话" % name,
                      bool(txt) and not bad, {"text": txt[:180]})
                # 清掉这条失败回复，避免影响下一个用例
                await c.js("(function(){document.querySelectorAll('#stream > *').forEach(e=>e.remove());})()")
            p2.cancel()
        print(json.dumps({"checks": checks,
                          "passed": sum(1 for c in checks if c["ok"]),
                          "total": len(checks),
                          "boundary": "真实 Chrome 加载运行中页面，/api/say 由 CDP 拦成不同错误形态；"
                                      "只证错误文本显示，不证安卓实机与真实模型链。"},
                         ensure_ascii=False, indent=2))
        open(RESULT, "w").write(json.dumps(checks, ensure_ascii=False, indent=2))
    finally:
        chrome.terminate()

asyncio.run(main())
