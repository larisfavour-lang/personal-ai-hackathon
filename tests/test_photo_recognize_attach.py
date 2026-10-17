import asyncio, base64, json, pathlib, socket, subprocess, time, urllib.request
import websockets

helper = pathlib.Path(__file__).with_name("prompt-chrome-20261015-0507.py")
ns = {"__file__": str(helper)}
exec(helper.read_text().rsplit("raise SystemExit", 1)[0], ns)

# 一张 1x1 的 PNG，注入文件框用。真实浏览器里 File 注入比构造 base64 稳。
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "/x8AAwMCAO+/6qkAAAAASUVORK5CYII=")


class PhotoCDP(ns["CDP"]):
    """拦 /api/photo/read 与 /api/photo/save，不让验收动真实模型或业务写。"""

    def __init__(self, ws):
        super().__init__(ws)
        self.loaded = asyncio.Event()
        self.reads = 0
        self.saves = []

    async def pump(self):
        # 必须把 Fetch.requestPaused 转给 fulfill：少了这一句，被拦截的请求
        # 永远挂在那里没人应答，页面里那个 await fetch 就一直不返回——
        # 看起来像产品卡在「正在送出…」，其实是验收器把证据吞了。
        # 第一版就栽在这里：crop-ok 之后 reads 恒为 0，按钮永远不恢复。
        async for raw in self.ws:
            m = json.loads(raw)
            if m.get("id") in self.pending:
                self.pending.pop(m["id"]).set_result(m)
            elif m.get("method") == "Page.loadEventFired":
                self.loaded.set()
            elif m.get("method") == "Fetch.requestPaused":
                asyncio.create_task(self.fulfill(m["params"]))

    # 自动识别是加图之后异步跑的（压缩 + 一次模型请求），比点一下按钮慢，
    # 轮询次数比 helper 默认的 60 放宽。try/except 包住：表达式引用还没
    # 初始化的字段时会抛异常，那要当成「还没到」继续等，不是用例失败。
    async def wait(self, expr, tries=150):
        for _ in range(tries):
            if await self.js("(()=>{try{return !!(%s)}catch(e){return false}})()"
                             % expr):
                return True
            await asyncio.sleep(0.15)
        return False

    async def fulfill(self, p):
        r = p["request"]
        if r["method"] in ("GET", "HEAD"):
            await self.send("Fetch.continueRequest", requestId=p["requestId"])
            return
        if r["url"].endswith("/api/photo/read"):
            self.reads += 1
            body = {
                "transcribed": "哥哥自己练了四十分钟",
                "fields": {"完成时间": "40分钟", "内容": "练琴"},
                "happened_on": "2026-10-14",
                "confidence": "high",
                "unclear_parts": [],
                "subject_hint": "",
            }
        elif r["url"].endswith("/api/photo/save"):
            self.saves.append(json.loads(r.get("postData") or "{}"))
            body = {"event_id": "evt_photo_stub", "created": True,
                    "provider": "拍照记录（我拍摄，本人核对后采用）",
                    "happened_on": "2026-10-14", "date_unknown": False,
                    "fields": {}, "attachments": [], "text_edited": False}
        else:
            await self.send("Fetch.continueRequest", requestId=p["requestId"])
            return
        await self.send("Fetch.fulfillRequest", requestId=p["requestId"],
                        responseCode=200,
                        responseHeaders=[{"name": "Content-Type",
                                          "value": "application/json"}],
                        body=base64.b64encode(
                            json.dumps(body, ensure_ascii=False).encode()).decode())


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def pick_file(c, input_id):
    """往指定 file input 注入一张图并触发 change。"""
    b64 = base64.b64encode(TINY_PNG).decode()
    await c.js(
        "(async()=>{const b=atob(%s);const u=new Uint8Array(b.length);"
        "for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);"
        "const f=new File([u],'a.png',{type:'image/png'});"
        "const dt=new DataTransfer();dt.items.add(f);"
        "const inp=document.querySelector(%s);inp.files=dt.files;"
        "inp.dispatchEvent(new Event('change',{bubbles:true}));return true})()"
        % (json.dumps(b64), json.dumps("#" + input_id)))


async def main():
    checks = []

    def check(name, ok, detail=None):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(json.dumps(checks[-1], ensure_ascii=False), flush=True)

    port = free_port()
    proc = subprocess.Popen(
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
         "--headless=new", "--no-first-run", "--no-default-browser-check",
         "--window-size=430,1000", "--remote-debugging-port=%d" % port,
         "--user-data-dir=/tmp/codex-photo-rec-%d" % port, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tabs = None
        for _ in range(120):
            try:
                tabs = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % port, timeout=1).read())
                break
            except Exception:
                await asyncio.sleep(0.15)
        if tabs is None:
            check("Chrome 可启动", False, "拿不到调试端口")
            return 1
        page = next(x for x in tabs if x["type"] == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"],
                                      max_size=20 * 1024 * 1024) as ws:
            c = PhotoCDP(ws)
            pump = asyncio.create_task(c.pump())
            await c.send("Page.enable")
            await c.send("Runtime.enable")
            await c.send("Emulation.setDeviceMetricsOverride",
                         width=430, height=900, deviceScaleFactor=1, mobile=False)
            await c.send("Fetch.enable",
                         patterns=[{"urlPattern": "*/api/photo/*",
                                    "requestStage": "Request"}])
            await c.send("Page.navigate", url="http://127.0.0.1:8000/")
            await asyncio.wait_for(c.loaded.wait(), 20)
            await c.wait("typeof api === 'function' && !!ownerId")
            await c.wait("lastPersons && lastPersons.length > 0")

            # 1) 打开拍照弹层：识别必须是看得见的主路，不是藏在点人名背后
            await c.js("document.querySelector('#btn-photo').click()")
            await c.wait("document.querySelector('#sheet').classList.contains('open')")
            await asyncio.sleep(1.0)
            btns = await c.js(
                "Array.from(document.querySelectorAll('#sheet-body button'))"
                ".map(b=>b.textContent.trim())")
            # 1) 打开资料草稿：加图与识别都是看得见的动作。
            #    记事本式录入取代了旧的「拍一张记录」弹层，photoCtx /
            #    photo-input / #photo-shoot 已随之退役，这里跟着改。
            await c.js("document.querySelector('#btn-photo').click()")
            await c.wait("document.querySelector('#sheet').classList.contains('open')")
            await asyncio.sleep(1.0)
            btns = await c.js(
                "Array.from(document.querySelectorAll('#sheet-body button'))"
                ".map(b=>b.textContent.trim())")
            check("资料草稿的一级选项是「图片识别」",
                  "\u56fe\u7247\u8bc6\u522b" in btns, btns)
            check("\u8bed\u97f3\u5165\u53e3\u662f\u53ef\u7528\u7684\u529f\u80fd\uff0c\u4e0d\u6807\u6210\u6682\u4e0d\u53ef\u7528",
                  any("\u8bed\u97f3\u8f93\u5165" in b for b in btns)
                  and not any("\u6682\u4e0d\u53ef\u7528" in b for b in btns), btns)

            # 2) 点人名只选主体，不直接开始拍
            await c.js("document.querySelector('[data-n-subject]').click()")
            await asyncio.sleep(0.2)
            st = await c.js(
                "({ctx:noteCtx&&noteCtx.subjectId,"
                "stillSheet:document.querySelector('#sheet').classList.contains('open'),"
                "reads:%d,status:document.querySelector('#status-line').textContent})"
                % c.reads)
            check("点人名后主体记下、弹层不关、没有偷偷开始拍",
                  st["ctx"] and st["stillSheet"] and st["reads"] == 0, st)

            # 3) 图片识别：选定文件后图进草稿、先不送模型，本人点「识别」才发
            await c.js("document.querySelector('#note-add-photo').click()")
            await pick_file(c, "note-photo-input")
            await c.wait("noteCtx && noteCtx.attachments.length > 0")
            check("选定文件后还没发 /api/photo/read（等本人选）",
                  c.reads == 0, {"reads": c.reads})
            await c.js("document.querySelector('#note-choice-ocr').click()")
            # 等 ocrBusy 落回去再看——新建条目时 ok 就是 false，
            # 只等 reads[0].ok 会在请求还没发出去时就命中。
            await c.wait("noteCtx && noteCtx.ocrBusy === false"
                         " && noteCtx.reads.length > 0 && noteCtx.reads[0].ok",
                         tries=150)
            check("本人点过「识别」之后才发那一次，不用再点多下",
                  c.reads == 1, {"reads": c.reads})
            body_now = await c.js("document.querySelector('#note-text').value")
            check("识别出的文字自动进正文框",
                  body_now.startswith("\u54e5\u54e5\u81ea\u5df1\u7ec3"), body_now)
            acts = await c.js(
                "Array.from(document.querySelectorAll('#note-image-acts button'))"
                ".map(b=>({id:b.id,dis:b.disabled}))")
            ids = [a["id"] for a in acts]
            check("识别 / 撤掉识别文字只留图 / 先选范围 都在，且都没卡在禁用态",
                  "note-ocr" in ids and "note-keep-image" in ids
                  and "note-crop-first" in ids
                  and all(not a["dis"] for a in acts), acts)

            # 4) 配图：可加、可删，缩略图真的渲染出来
            draft = await c.js(
                "({readText:(noteCtx.read&&noteCtx.read.transcribed)||'',"
                "attachments:noteCtx.attachments.length,"
                "sub:noteCtx.subjectId})")
            check("草稿带识别出的正文",
                  draft["readText"].startswith("\u54e5\u54e5\u81ea\u5df1\u7ec3"), draft)
            n0 = draft["attachments"]
            check("加图时这张图已经作为配图在草稿里", n0 >= 1, {"n": n0})
            # 「另选一张图」是纯配图，不自动识别：用户已经明确说是配图，
            # 再插一段识别文字进正文反而要把刚写好的内容挤到后面。
            await pick_file(c, "note-attach-input")
            await asyncio.sleep(0.6)
            n1 = await c.js("noteCtx.attachments.length")
            check("「另选一张图」再加一张", n1 == n0 + 1, {"n0": n0, "n1": n1})
            check("另选配图不再自动识别（那是配图入口，不是识别入口）",
                  c.reads == 1, {"reads": c.reads})
            imgs = await c.js(
                "Array.from(document.querySelectorAll('#note-attachments img'))"
                ".map(i=>({w:i.naturalWidth,h:i.naturalHeight,ok:i.complete}))")
            check("配图缩略图实际加载可解码",
                  len(imgs) == n1 and all(i["ok"] and i["w"] > 0 for i in imgs),
                  imgs)
            await c.js("document.querySelector('[data-n-del=\"0\"]').click()")
            n3 = await c.js("noteCtx.attachments.length")
            check("「移除」删掉指定那张", n3 == n1 - 1, {"n1": n1, "n3": n3})

            # 6) 保存：配图随记录一起送出去
            await c.js("document.querySelector('#note-save').click()")
            for _ in range(80):
                if len(c.saves) > 0:
                    break
                await asyncio.sleep(0.15)
            check("保存确实发出了一次", len(c.saves) == 1,
                  {"n": len(c.saves)})
            if c.saves:
                b = c.saves[0]
                check("保存带配图（识别图不会自动混进来）",
                      len(b.get("attachments") or []) == n3,
                      {"sent": len(b.get("attachments") or []), "expect": n3})
                check("保存带识别结果，服务端不会退回再识别",
                      b.get("read") and b["read"].get("transcribed"),
                      {"read": b.get("read")})
                check("保存的主体是选中的那个人",
                      b.get("subject_id") == draft["sub"],
                      {"sent": b.get("subject_id"), "picked": draft["sub"]})

            shot = await c.send("Page.captureScreenshot", format="png")
            pathlib.Path(__file__).with_suffix(".png").write_bytes(
                base64.b64decode(shot["data"]))
            pump.cancel()
        result = {
            "utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "checks": checks,
            "passed": sum(x["ok"] for x in checks),
            "total": len(checks),
            "boundary": "隔离 Chrome 430×900；真实 DOM 点击与 File 注入，"
                        "/api/photo/read 与 /api/photo/save 全部拦截返回合成数据"
                        "（0 次真实模型、0 次业务写）。只证加图即自动识别、"
                        "文字进正文框、识别入口与配图入口分开、配图可增删，"
                        "不证服务端持久化与真机相机。",
        }
        pathlib.Path(__file__).with_suffix(".json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if all(x["ok"] for x in checks) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


raise SystemExit(asyncio.run(main()))
