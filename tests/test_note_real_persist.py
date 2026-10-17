"""资料库三种录入方式的真实保存与回读。

和 test_image_only_ui_chain.py 的分工：那个脚本把 /api/photo/read 和
/api/photo/save 全拦掉，只证明前端行为（0 次真实模型、0 次业务写）。
本脚本反过来——一个接口都不拦，页面点「保存资料」之后真的写进隔离服务
的临时库，再刷新页面、调真实读接口回读，验的是当前后端而不是请求载荷。

跑三遍同一个界面路径，只是输入不同：

1. 纯文字：只写正文，不加图。保存后回读，正文、对象、日期都在；
2. 只存图不识别：图片识别选定文件后点「只把图存进资料库，不识别」（全程 0 次
   /api/photo/read）。回读时这条记录里那张图真的解码得出来，且选了不识别之后
   一次识别都没再发；
3. 图文混合：图片识别选定文件、点「识别图里的文字，进正文框」后文字进正文框，
   本人再改成自己的话后保存。
   回读的是本人最终采用的那版正文，不是模型读的原样。

每次保存后都刷新页面再回读：不刷新的话只能证明内存里有值，
而任务书要求的是「保存并回读、重新打开后还在」。

运行：/tmp/hack_venv/bin/python tests/test_note_real_persist.py
（沙盒里跑需要提权：脚本要 bind 本地端口、起隔离服务。）
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "docs" / "evidence" / "note-real-persist.txt"

PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c63000100000500010d0a2db4000000"
    "0049454e44ae426082")).decode()


def make_text_png() -> str:
    """造一张有真实文字的便签图，返回 base64。

    第 3 段要验「加图后自动识别出的字真的进了正文」，用 1×1 空图验不了——
    那种图模型只会回一句「图上没有文字」，拿空结果当通过等于没验。
    字句沿用 test_photo_chain 里已经被真实模型读出来过的那几句，
    不新造内容：新句子的识别率没有证据，写进断言就是拿运气当验收。
    """
    from PIL import Image, ImageDraw, ImageFont
    path = pathlib.Path("/tmp/pa-note-real-mixed.png")
    lines = [("10月12日 星期日", 40), ("小雨 今天读了《昆虫记》", 46),
             ("读了 25 分钟", 44), ("自己说：法布尔的观察真细", 42)]
    W, H = 900, 1150
    img = Image.new("RGB", (W, H), (246, 242, 231))
    d = ImageDraw.Draw(img)

    def font(sz):
        for p in ("/System/Library/Fonts/Supplemental/Kaiti.ttc",
                  "/System/Library/Fonts/Supplemental/Songti.ttc"):
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
        return ImageFont.load_default()

    y = 140
    for text, sz in lines:
        d.text((100, y), text, font=font(sz), fill=(38, 44, 58))
        y += sz + 48
    img.save(path, "PNG")
    return base64.b64encode(path.read_bytes()).decode("ascii")


TEXT_PNG = make_text_png()


TARGET = "p_xiaohan"      # A 名下的哥哥，和默认的小雨不是同一人
DAY = "2026-09-20"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class CDP:
    """单读循环的 CDP 客户端（与另外两个界面脚本同一套写法）。

    读循环只有一个：pump 和 send 同时 await recv 会把对方的消息吞掉，
    被拦的请求永远不 fulfill，看起来像产品卡住，其实是验收器丢了证据。
    """

    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.pending = {}
        self.calls = []          # 所有被拦到的请求
        self._task = None

    async def _loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self.pending:
                self.pending.pop(msg["id"]).set_result(msg)
                continue
            # requestPaused 的处理必须另起 task，不能在这里 await。
            # _intercept 内部要发 Fetch.continueRequest，而它的响应只能由
            # 这个 _loop 读出来——在这里 await 就等于让自己等自己，
            # 第一个被拦的请求就把整条读循环锁死，页面再也收不到任何响应，
            # 表现是「点了保存什么都没发生」，而脚本连一行输出都没有。
            if msg.get("method") == "Fetch.requestPaused":
                asyncio.create_task(self._intercept(msg["params"]))

    def start(self):
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        if self._task:
            self._task.cancel()

    async def send(self, method, **params):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.n] = fut
        await self.ws.send(json.dumps({"id": self.n, "method": method,
                                       "params": params}))
        # 没有超时的话，读循环一旦出事就是干等到天荒：脚本既不报错也不输出，
        # 分不清是产品卡住还是验收器自己锁死了。超时至少把现场留下来。
        try:
            return await asyncio.wait_for(fut, timeout=30)
        except asyncio.TimeoutError:
            self.pending.pop(self.n, None)
            raise RuntimeError("CDP 等 %s 的响应超时（读循环可能已停）" % method)

    async def _intercept(self, p):
        """两个阶段都只记录、不改内容：要的就是真实服务端的真实响应。

        Request 阶段记 URL 和请求体，然后 continueRequest 让原请求照常打过去；
        Response 阶段把服务端返回的真实响应体记到同一条上。
        拦下来却不 continue / fulfill 的话，被拦请求永远悬着，
        页面表现是「点了什么都没发生」——那是验收器制造出来的假故障。
        """
        # 响应阶段：CDP 在这一阶段给 responseStatusCode / responseHeaders。
        if p.get("responseStatusCode") is not None or "responseHeaders" in p:
            try:
                raw = p.get("responseBody") or ""
                text = base64.b64decode(raw).decode("utf-8", "replace") if raw else ""
            except Exception:
                text = ""
            url = p.get("request", {}).get("url", "")
            for e in reversed(self.calls):
                if e["url"] == url:
                    e["response"] = text
                    break
            await self.send("Fetch.continueResponse", requestId=p["requestId"])
            return
        url = p["request"]["url"]
        self.calls.append({"url": url,
                           "body": json.loads(p.get("request", {}).get("postData") or "{}"),
                           "response": None})
        try:
            await self.send("Fetch.continueRequest", requestId=p["requestId"])
        except Exception:
            # 这一条拦不住不能带翻整轮：原请求还悬着，页面会停在这一步，
            # 后面的断言自然失败并给出证据，比整个脚本无声消失好。
            pass

    async def js(self, expr):
        r = await self.send("Runtime.evaluate", expression=expr,
                            returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("result", {}).get("value")

    async def wait(self, expr, tries=120):
        for _ in range(tries):
            if await self.js("(()=>{try{return !!(%s)}catch(e){return false}})()" % expr):
                return True
            await asyncio.sleep(0.15)
        return False


async def attachments_of(base, event_id):
    """走真实取图端点回读配图，不是猜 payload 里有没有图。

    /api/life-events 按设计不直接塞图片数据：刷一次就把这个人名下所有
    配图全拉一遍，手机上又慢又费流量。配图由
    /api/event/{event_id}/attachments 单独给，服务端在那里校验身份。
    拿列表端点断言「有没有图」是断言错了地方，不是产品丢了图。
    """
    d = await http_json(base + "/api/event/%s/attachments" % event_id)
    return (d or {}).get("attachments") or []


async def http_bytes(url, timeout=60):
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except Exception:
            await asyncio.sleep(0.4)
    return None


async def http_json(url, timeout=60):
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception:
            await asyncio.sleep(0.4)
    return None


async def type_into(c, sel, value):
    """往输入框里写一段字并触发 input，和本人打字等效。

    必须包在函数体里，不能让 const 落在顶层：Runtime.evaluate 的顶层
    let/const 进的是全局词法环境，同一轮页面生命周期里第二次写
    const t 直接 SyntaxError，而 CDP 的 js() 把异常吞成 None——
    表现是「赋值没生效、正文还是旧值」，不是报错，极难定位。
    """
    await c.js("(function(){const t=document.querySelector(%s);"
               "t.value=%s;t.dispatchEvent(new Event('input',{bubbles:true}));"
               "})()" % (json.dumps(sel), json.dumps(value)))


async def main():
    checks = []

    def check(name, ok, detail):
        checks.append((name, bool(ok), detail))
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  " + repr(detail)[:300]),
              flush=True)

    srv_port = free_port()
    db_dir = "/tmp/pa-note-real-%d" % srv_port
    subprocess.run(["rm", "-rf", db_dir], check=False)
    env = dict(os.environ)
    env["PA_DATA_DIR"] = db_dir
    env["PA_BIND"] = "127.0.0.1"
    env["PA_DEMO_OWNER"] = "A"
    # 日志必须落到文件，不能用 PIPE：这个脚本要等 uvicorn 起来、又要等
    # Chrome，中间没有第二个协程去读管道。管道写满之后 uvicorn 自己会被
    # 阻塞在 write 上，表现就是「服务起不来、什么输出都没有」。
    # 真实 Chromium 的日志也同理——headless 会把 CVDisplayLink 之类的
    # 错误刷进 stderr，用 PIPE 同样会把它自己堵死。
    srv_log = open("/tmp/pa-note-real-%d-server.log" % srv_port, "wb")
    srv = subprocess.Popen(
        ["/tmp/hack_venv/bin/python", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(srv_port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=srv_log, stderr=srv_log)
    try:
        base = "http://127.0.0.1:%d" % srv_port
        ready = None
        for _ in range(80):
            ready = await http_json(base + "/api/health", timeout=5)
            if ready:
                break
            await asyncio.sleep(0.3)
        check("隔离服务可启动且模型在线", bool(ready) and ready.get("model", {}).get("ok"),
              ready)

        chrome_port = free_port()
        proc = subprocess.Popen(
            ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             "--headless=new", "--no-first-run", "--no-default-browser-check",
             "--window-size=430,1000",
             "--remote-debugging-port=%d" % chrome_port,
             "--user-data-dir=/tmp/codex-note-real-%d" % chrome_port,
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            tabs = None
            for _ in range(120):
                try:
                    tabs = json.loads(urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/list" % chrome_port, timeout=1).read())
                    break
                except Exception:
                    await asyncio.sleep(0.15)
            if tabs is None:
                check("隔离 Chrome 可启动", False, "拿不到调试端口")
                return 1
            page = next(x for x in tabs if x["type"] == "page")
            import websockets
            async with websockets.connect(page["webSocketDebuggerUrl"],
                                          max_size=20 * 1024 * 1024) as ws:
                c = CDP(ws)
                c.start()
                await c.send("Page.enable")
                await c.send("Runtime.enable")
                await c.send("Emulation.setDeviceMetricsOverride",
                             width=430, height=900, deviceScaleFactor=1, mobile=False)
                # 请求和响应两个阶段都拦：请求阶段计数、让请求照常打过去，
                # 响应阶段把服务端的真实响应体留下来。少响应这一半，
                # 「请求体对但落库结果不对」时就分不清是没发、发错、
                # 还是服务端没收下。
                await c.send("Fetch.enable",
                             patterns=[{"urlPattern": "*/api/photo/*",
                                        "requestStage": "Request"},
                                       {"urlPattern": "*/api/photo/*",
                                        "requestStage": "Response"}])
                await c.send("Page.navigate", url=base + "/")
                await c.wait("typeof api === 'function' && !!ownerId")
                await c.wait("lastPersons && lastPersons.length > 0")
                await c.wait("document.querySelector('[data-photo-subject]')")

                # ---------- 1) 纯文字 ----------
                await c.js("document.querySelector('#btn-photo').click()")
                await c.wait("document.querySelector('#note-text')")
                await c.js("document.querySelector('[data-n-subject=\"%s\"]').click()"
                           % TARGET)
                text1 = "数学期中复习安排在周四晚上，家长要陪着过一遍错题。"
                await c.js("const t=document.querySelector('#note-text');"
                           "t.value=%s;t.dispatchEvent(new Event('input',{bubbles:true}));"
                           % json.dumps(text1))
                await c.js("const d=document.querySelector('#note-day');"
                           "d.value=%s;d.dispatchEvent(new Event('input',{bubbles:true}));"
                           % json.dumps(DAY))
                await asyncio.sleep(0.3)
                c.calls.clear()
                await c.js("document.querySelector('#note-save').click()")
                await c.wait("!document.querySelector('#sheet').classList.contains('open')"
                             " || document.querySelector('#note-save') === null", tries=60)
                await asyncio.sleep(1.0)
                saves = [x for x in c.calls if "/api/photo/save" in x["url"]]
                check("纯文字：保存发往真实服务端", len(saves) == 1, len(saves))
                check("纯文字：保存体带正文和日期",
                      saves and saves[0]["body"].get("draft_text") == text1
                      and saves[0]["body"].get("happened_on") == DAY,
                      saves[0]["body"] if saves else None)
                check("纯文字：保存体不带图", saves and not saves[0]["body"].get("image_b64"),
                      bool(saves and saves[0]["body"].get("image_b64")))

                # 刷新页面后回读：证明落库而不是只在内存里
                await c.send("Page.reload")
                await c.wait("typeof api === 'function' && !!ownerId")
                await c.wait("lastPersons && lastPersons.length > 0")
                evs = await http_json(base + "/api/life-events?subject_id=" + TARGET)
                mine = [e for e in (evs or {}).get("events", [])
                        if text1 in json.dumps(e, ensure_ascii=False)]
                check("纯文字：刷新后回读到这条记录", len(mine) == 1,
                      {"events": len((evs or {}).get("events", []))})
                if mine:
                    e = mine[0]
                    check("纯文字：回读的日期是本人填的那个",
                          e.get("happened_on") == DAY, e.get("happened_on"))
                    check("纯文字：回读的内容是本人最终正文",
                          text1 in json.dumps(e, ensure_ascii=False), "")

                # ---------- 2) 只把图存进资料库，不识别 ----------
                await c.js("document.querySelector('#btn-photo').click()")
                await c.wait("document.querySelector('#note-text')")
                await c.js("document.querySelector('[data-n-subject=\"%s\"]').click()"
                           % TARGET)
                await c.js("document.querySelector('#note-add-photo').click()")
                await asyncio.sleep(0.2)
                await c.js("(async()=>{"
                           "const b=atob(%s);const u=new Uint8Array(b.length);"
                           "for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);"
                           "const f=new File([u],'a.png',{type:'image/png'});"
                           "const dt=new DataTransfer();dt.items.add(f);"
                           "const inp=document.querySelector('#note-photo-input');"
                           "inp.files=dt.files;"
                           "inp.dispatchEvent(new Event('change',{bubbles:true}));"
                           "})()" % json.dumps(PNG))
                await c.wait("noteCtx && noteCtx.attachments.length > 0")
                # 新流程：选定文件后图进草稿但先不送模型，等本人选。
                check("只存图：选定文件后还没发 /api/photo/read",
                      not any("/api/photo/read" in x["url"] for x in c.calls),
                      len(c.calls))
                await c.js("document.querySelector('#note-choice-keep').click()")
                await asyncio.sleep(0.5)
                c.calls.clear()
                await c.js("document.querySelector('#note-save').click()")
                await c.wait("!document.querySelector('#sheet').classList.contains('open')"
                             " || document.querySelector('#note-save') === null", tries=60)
                await asyncio.sleep(1.0)
                saves = [x for x in c.calls if "/api/photo/save" in x["url"]]
                reads = [x for x in c.calls if "/api/photo/read" in x["url"]]
                check("只存图：保存发往真实服务端", len(saves) == 1, len(saves))
                # 选了「只把图存进资料库」之后不能再发识别：用户说了不要文字，
                # 界面就真的不再去问模型。
                check("只存图：选了不识别之后没再调过识别", len(reads) == 0,
                      len(reads))
                check("只存图：保存体带一张配图",
                      saves and len(saves[0]["body"].get("attachments") or []) == 1,
                      len((saves[0]["body"].get("attachments") or [])) if saves else None)

                await c.send("Page.reload")
                await c.wait("typeof api === 'function' && !!ownerId")
                await c.wait("lastPersons && lastPersons.length > 0")
                evs = await http_json(base + "/api/life-events?subject_id=" + TARGET)
                check("只存图：刷新后回读到这条记录",
                      len((evs or {}).get("events", [])) >= 1,
                      len((evs or {}).get("events", [])))
                # 按 event_id 单独取图：这条路才是界面 <img src> 真正用的那条
                atts = []
                for e in (evs or {}).get("events", []):
                    got = await attachments_of(base, e["event_id"])
                    if got:
                        atts.append((e["event_id"], got))
                check("只作配图：刷新后回读到带配图的记录", len(atts) >= 1,
                      {"events": len((evs or {}).get("events", []))})
                if atts:
                    ev_id, first = atts[-1]
                    check("只作配图：配图索引里就是一张",
                          len(first) == 1, [a["attachment_id"] for a in first])
                    raw = await http_bytes(
                        base + "/api/event/%s/attachment/%s" % (ev_id, first[0]["attachment_id"]))
                    check("只作配图：配图本体取回来是 PNG",
                          bool(raw) and raw[:8] == bytes.fromhex("89504e470d0a1a0a"),
                          {"len": len(raw or b""), "head": (raw or b"")[:8].hex()})

                # ---------- 3) 图文混合 ----------
                await c.js("document.querySelector('#btn-photo').click()")
                await c.wait("document.querySelector('#note-text')")
                await c.js("document.querySelector('[data-n-subject=\"%s\"]').click()"
                           % TARGET)
                # 本人先打一句：自动识别的字必须接在它后面，不能把它顶掉。
                # 「录入流程简化」省掉的是加图后那一步选择，不是用户已经
                # 写好的内容——省错了地方就等于替他改稿。
                lead3 = "班主任发来的，我照着整理一下："
                await type_into(c, "#note-text", lead3)
                await c.js("document.querySelector('#note-add-photo').click()")
                await asyncio.sleep(0.2)
                await c.js("(async()=>{"
                           "const b=atob(%s);const u=new Uint8Array(b.length);"
                           "for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);"
                           "const f=new File([u],'b.png',{type:'image/png'});"
                           "const dt=new DataTransfer();dt.items.add(f);"
                           "const inp=document.querySelector('#note-photo-input');"
                           "inp.files=dt.files;"
                           "inp.dispatchEvent(new Event('change',{bubbles:true}));"
                           "})()" % json.dumps(TEXT_PNG))
                await c.wait("noteCtx && noteCtx.attachments.length > 0")
                # 新流程（t41）：图进草稿后先不送模型，本人点「识别图里的文字，
                # 进正文框」之后文字才自己进正文框。
                await c.js("document.querySelector('#note-choice-ocr').click()")
                # 这里必须等它落定再往下走：不等的话，本人改写正文时那个
                # 识别刚好回来，会把他刚写好的话后面又接一段模型读的字，
                # 保存下去的那版就不是他采用的那版。
                # 预算给到 180 秒：实测这张图送进模型要 40-70 秒才回来，
                # 按「点一下按钮」的节奏去等必然超时。等不到就明说，
                # 不静默往下走——静默会让后面几条断言全变成假失败。
                # 条件里带上 error：识别真失败时立刻收手，不用干等满预算。
                settled = await c.wait(
                    "noteCtx && noteCtx.ocrBusy === false"
                    " && noteCtx.reads.length > 0"
                    " && (noteCtx.reads[0].ok || noteCtx.reads[0].error)",
                    tries=1200)
                check("图文混合：加图后自动识别在预算内落定", settled,
                      "180 秒内没落定；reads[0].error="
                      + str(await c.js("(noteCtx.reads[0]||{}).error||''")))
                auto = await c.js(
                    "({body:document.querySelector('#note-text').value,"
                    "read:(noteCtx.reads[0]||{}).text||''})")
                reads_auto = [x for x in c.calls
                              if "/api/photo/read" in x["url"]]
                check("图文混合：本人点识别之后恰好发一次（没有多认）",
                      len(reads_auto) == 1, len(reads_auto))
                # 空结果不算识别通过：图上没字的图模型只会回一句「没有文字」，
                # 拿它当通过，验的就不是识别而是空转。
                check("图文混合：识别结果不是空的",
                      bool(auto["read"].strip()), auto["read"])
                check("图文混合：图上写的字真的被读出来了",
                      "\u6606\u866b\u8bb0" in auto["read"], auto["read"])
                # 一致性和追加位置一起验：正文就是「本人那句 + 换行 + 识别那段」，
                # 一段不多一段不少。这也说明识别没有把他的原话抹掉。
                check("图文混合：识别文字接在本人已打内容后面进正文",
                      auto["body"] == lead3 + "\n" + auto["read"],
                      {"lead": lead3, "body": auto["body"],
                       "read": auto["read"]})
                c.calls.clear()
                text3 = "这是学校发的活动通知，已按上面读到的内容改成我自己的话。"
                await type_into(c, "#note-text", text3)
                await asyncio.sleep(0.3)
                c.calls.clear()
                await c.js("document.querySelector('#note-save').click()")
                await c.wait("!document.querySelector('#sheet').classList.contains('open')"
                             " || document.querySelector('#note-save') === null", tries=60)
                await asyncio.sleep(1.0)
                saves = [x for x in c.calls if "/api/photo/save" in x["url"]]
                check("图文混合：保存发往真实服务端", len(saves) == 1, len(saves))
                # 证据只摘关键字段：整个 body 里那张图的 base64 有几十 KB，
                # 整段打出来把真正要看的那几个值挤到看不见。
                check("图文混合：保存体同时带图、带正文",
                      saves and saves[0]["body"].get("draft_text") == text3
                      and len(saves[0]["body"].get("attachments") or []) == 1,
                      {"draft_text": (saves[0]["body"].get("draft_text")
                                      if saves else None),
                       "attachments": (len(saves[0]["body"].get("attachments") or [])
                                       if saves else None),
                       "image_len": (len(saves[0]["body"].get("image_b64") or "")
                                     if saves else None),
                       "happened_on": (saves[0]["body"].get("happened_on")
                                       if saves else None),
                       "input_kinds": (saves[0]["body"].get("input_kinds")
                                       if saves else None)} if saves else None)

                await c.send("Page.reload")
                await c.wait("typeof api === 'function' && !!ownerId")
                await c.wait("lastPersons && lastPersons.length > 0")
                evs = await http_json(base + "/api/life-events?subject_id=" + TARGET)
                hit = [e for e in (evs or {}).get("events", [])
                       if text3 in json.dumps(e, ensure_ascii=False)]
                check("图文混合：刷新后回读到这条记录", len(hit) == 1,
                      {"hit": len(hit), "events": len((evs or {}).get("events", [])),
                       "texts": [e.get("text") for e in
                                 (evs or {}).get("events", [])][-3:]})
                if hit:
                    e = hit[-1]
                    atts = await attachments_of(base, e["event_id"])
                    check("图文混合：回读保留配图", len(atts) == 1,
                          [a["attachment_id"] for a in atts])
                    raw = await http_bytes(
                        base + "/api/event/%s/attachment/%s" % (e["event_id"], atts[0]["attachment_id"])
                    ) if atts else None
                    check("图文混合：配图本体取回来是 PNG",
                          bool(raw) and raw[:8] == bytes.fromhex("89504e470d0a1a0a"),
                          {"len": len(raw or b""), "head": (raw or b"")[:8].hex()})
                    check("图文混合：回读的是本人改后的正文，不是模型原样",
                          text3 in json.dumps(e, ensure_ascii=False), "")
                c.stop()

            ok = sum(1 for _, p, _ in checks if p)
            lines = ["资料库三种录入的真实保存与回读：%d/%d 通过" % (ok, len(checks)),
                     "隔离服务：独立端口 + 独立临时数据目录，/api/photo/* 一个都没拦，",
                     "     保存真实写库，刷新后调 /api/life-events 真实回读；",
                     "     隔离桌面 Chrome 430×900，真实点击与真实 File 注入。",
                     "不是安卓真机相机；真机拍照与系统文件框表现仍待实机复核。", ""]
            for name, passed, detail in checks:
                lines.append(("PASS " if passed else "FAIL ") + name)
                if not passed:
                    lines.append("       证据：" + repr(detail)[:400])
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text("\n".join(lines) + "\n", encoding="utf8")
            print("\n".join(lines))
            return 0 if ok == len(checks) else 1
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    finally:
        srv_log.close()
        srv.send_signal(signal.SIGTERM)
        try:
            srv.wait(timeout=8)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        print("FAIL 脚本异常：" + repr(exc))
        sys.exit(1)
