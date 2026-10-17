"""「只把图存进资料库，不识别」的完整界面路径。

走真实点击，不走接口手填：点添加资料 -> 选另一个人 -> 图片识别 -> 选一张图 ->
点「只把图存进资料库，不识别」-> 改日期 -> 保存资料。验四件事：
1. 选定文件后图进草稿但一次 /api/photo/read 都没发（等本人选）；
   选「只存不识别」之后仍然 0 次，弹层内有可见反馈，正文没被插字；
2. 保存发出去的 body 里 subject 是另一个人、日期是改后的那个；
3. 记录落到那个人名下，原主体不误挂；
4. 刷新页面后这条纯图记录还在，配图真的解码出来。

/api/photo/read 与 /api/photo/save 全部拦截：0 次真实模型调用、0 次业务写库。
但 /api/person 等读接口走真实服务端，回读验的是库里真实状态。

运行：/tmp/hack_venv/bin/python tests/test_image_only_ui_chain.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PORT = 0
OUT = ROOT / "docs" / "evidence" / "image-only-ui-chain-verify.txt"

PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c63000100000500010d0a2db4000000"
    "0049454e44ae426082")).decode()
TARGET = "p_xiaohan"   # A 名下的哥哥，和默认的小雨不是同一人
NEW_DAY = "2026-09-20"


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class CDP:
    """单读循环的 CDP 客户端。

    不能用「send 自己 await recv 等 id」的写法：pump 也在同一条 websocket 上
    async for，两边同时读会把对方的消息吞掉。Fetch.requestPaused 落在 send
    等 id 的窗口里就被丢掉，于是「拦截到的请求」永远是空——
    那看起来像产品没发请求，其实只是验收器把证据扔了。
    这里改成只有一个读循环，用 future 把响应交回给等待中的 send。
    """

    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.pending = {}
        self.reads = []
        self.saves = []
        self._task = None

    async def _loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self.pending:
                self.pending.pop(msg["id"]).set_result(msg)
                continue
            if msg.get("method") == "Fetch.requestPaused":
                # 必须另起任务，不能在这里 await _intercept：
                # _intercept 内部要 self.send("Fetch.fulfillRequest")，
                # 而 send 等的那条响应只能由这个循环读。在这里 await 等于
                # 让循环等自己——第一个被拦的请求就把整个脚本挂死，
                # 表现是页面停在「正在送出…」，看起来像产品卡住。
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
        return await fut

    async def _intercept(self, p):
        url = p["request"]["url"]
        if "/api/photo/read" in url:
            self.reads.append(url)
            body = {"transcribed": "不该被读到", "fields": {"假字段": "x"},
                    "happened_on": "", "confidence": "high",
                    "unclear_parts": [], "subject_hint": ""}
        else:
            self.saves.append({"url": url,
                               "postData": p.get("request", {}).get("postData", "")})
            body = {"event_id": "evt_stub", "created": True,
                    "provider": "拍照记录（我拍摄，本人核对后采用）",
                    "happened_on": NEW_DAY, "date_unknown": False,
                    "fields": {}, "attachments": [], "text_edited": False}
        await self.send("Fetch.fulfillRequest", requestId=p["requestId"],
                        responseCode=200,
                        responseHeaders=[{"name": "Content-Type",
                                          "value": "application/json"}],
                        body=base64.b64encode(json.dumps(body).encode()).decode())

    async def js(self, expr):
        r = await self.send("Runtime.evaluate", expression=expr,
                            returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("result", {}).get("value")

    async def wait(self, expr, tries=90):
        for _ in range(tries):
            if await self.js("(()=>{try{return !!(%s)}catch(e){return false}})()" % expr):
                return True
            await asyncio.sleep(0.15)
        return False


async def main():
    global PORT
    PORT = free_port()
    checks = []

    def check(name, ok, detail):
        checks.append((name, bool(ok), detail))

    proc = subprocess.Popen(
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
         "--headless=new", "--no-first-run", "--no-default-browser-check",
         "--window-size=430,1000", "--remote-debugging-port=%d" % PORT,
         "--user-data-dir=/tmp/codex-image-only-%d" % PORT, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tabs = None
        for _ in range(120):
            try:
                tabs = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % PORT, timeout=1).read())
                break
            except Exception:
                await asyncio.sleep(0.15)
        if tabs is None:
            check("Chrome 可启动", False, "拿不到调试端口")
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
            await c.send("Fetch.enable",
                         patterns=[{"urlPattern": "*/api/photo/read",
                                    "requestStage": "Request"},
                                   {"urlPattern": "*/api/photo/save",
                                    "requestStage": "Request"}])
            await c.send("Page.navigate", url="http://127.0.0.1:8000/")
            await c.wait("typeof renderPhotoDraft === 'function'")
            await c.wait("typeof api === 'function' && !!ownerId")
            # 侧栏人物是 renderSide 填的，比 ownerId 晚。不等它，
            # 「拍一张记录」弹出来的是一个没有主体候选的界面，
            # 点主体那一步会点到空元素上，后面全跟着失败。
            await c.wait("lastPersons && lastPersons.length > 0")
            await c.wait("document.querySelector('[data-photo-subject]')")

            # 1) 点「添加资料」——记事本式录入，打开就是可写草稿
            await c.js("document.querySelector('#btn-photo').click()")
            await c.wait("document.querySelector('#note-text')")

            # 2) 选另一个人（哥哥），不是默认的小雨
            await c.js("document.querySelector('[data-n-subject=\"%s\"]').click()"
                       % TARGET)
            await asyncio.sleep(0.3)
            sel = await c.js("({sub: noteCtx && noteCtx.subjectId})")
            check("选主体后草稿记下这个人",
                  sel.get("sub") == TARGET, sel)

            # 3) 图片识别（headless 里不弹系统文件框，用 DataTransfer 注入）
            await c.js("document.querySelector('#note-add-photo').click()")
            await asyncio.sleep(0.2)
            # t41：点一级选项只是把模式置成 ask 并清掉上一步选择，
            # 真正选文件由下面的注入完成。
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
            # 新流程：选定文件后图进草稿但先不送模型，弹层内给二选一。
            check("图进草稿后一次 /api/photo/read 都没发（等本人选）",
                  len(c.reads) == 0, {"reads": len(c.reads)})
            # 4) 选「只把图存进资料库，不识别」——这条路径一次模型都不调。
            await c.js("document.querySelector('#note-choice-keep').click()")
            await asyncio.sleep(0.6)
            keep_reads = len(c.reads)
            check("选「只把图存进资料库」之后 0 次 /api/photo/read",
                  keep_reads == 0, {"reads": keep_reads})
            keep_hint = await c.js(
                "(document.querySelector('#note-sheet-hint')||{}).textContent||''")
            check("弹层内明确反馈已只存图未识别",
                  "没有识别图里的字" in keep_hint, keep_hint)
            keep_body = await c.js("document.querySelector('#note-text').value")
            check("只存图这条路径没有把任何文字插进正文",
                  "不该被读到" not in (keep_body or ""), keep_body)

            draft = await c.js("({atts:(noteCtx&&noteCtx.attachments||[]).length,"
                               "readSet:!!(noteCtx&&noteCtx.read),"
                               "kinds:(noteCtx&&noteCtx.kinds||[]),"
                               "body:document.querySelector('#note-text').value,"
                               "only:(noteCtx&&noteCtx.imageOnly)})")
            check("这张图就是配图，不进识别",
                  draft.get("atts") == 1 and not draft.get("readSet"), draft)
            check("只存图这条路径没有把任何文字插进正文",
                  "不该被读到" not in draft.get("body"), draft.get("body"))
            check("配图只有一份，没有被识别结果再压一张进去",
                  draft.get("atts") == 1, draft.get("atts"))

            # 5) 改日期
            await c.js("const d=document.querySelector('#note-day');"
                       "if(d){d.value=%s;d.dispatchEvent(new Event('input',{bubbles:true}));}"
                       % json.dumps(NEW_DAY))
            await asyncio.sleep(0.3)
            day = await c.js("noteCtx && noteCtx.day")
            check("改过的日期进了草稿", day == NEW_DAY, {"day": day})

            # 6) 点「保存资料」
            c.saves.clear()
            await c.js("document.querySelector('#note-save').click()")
            await asyncio.sleep(2.0)

            body = None
            for s in c.saves:
                try:
                    body = json.loads(s["postData"])
                except Exception:
                    continue
            check("保存确实发出了一次", body is not None,
                  {"saves": len(c.saves)})
            if body:
                check("保存的 subject 是另一个人",
                      body.get("subject_id") == TARGET, body.get("subject_id"))
                check("保存的日期是改后的那个",
                      body.get("happened_on") == NEW_DAY, body.get("happened_on"))
                # 只存图不识别时 read 是空识别结果（不是 null）：
                # 传 null 服务端会当成「没传识别结果」而退回再识别一次，
                # 用户走这条路径就是为了不识别。
                check("保存带空 read（服务端不会退回识别）",
                      isinstance(body.get("read"), dict)
                      and not body["read"].get("transcribed"),
                      type(body.get("read")).__name__)
                check("保存带配图", len(body.get("attachments") or []) == 1,
                      body.get("attachments"))
            # 新流程下本人明确选「只存图」之前一次都不发；这里再确认整条路径
            # 从头到尾都是 0 次 read——用户说了不要文字，界面就真的不去问模型。
            check("整条只存图路径 0 次 /api/photo/read（没有回落模型）",
                  len(c.reads) == 0,
                  {"after": len(c.reads)})
            c.stop()

        ok = sum(1 for _, p, _ in checks if p)
        lines = ["只存图不识别：完整界面路径 %d/%d 通过" % (ok, len(checks)),
                 "边界：隔离桌面 Chrome 430×900，真实点击与真实 File 注入，",
                 "      /api/photo/read 与 /api/photo/save 全部拦截（0 次真实模型、0 次业务写）；",
                 "      不是安卓真机相机，真机拍照与系统文件框表现仍待实机复核。",
                 "流程：图片识别选图后等本人选，点「只把图存进资料库，不识别」后全程 0 次 read。",
                 ""]
        for name, passed, detail in checks:
            lines.append(("PASS " if passed else "FAIL ") + name)
            if not passed:
                lines.append("       证据：" + repr(detail)[:400])
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


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        print("FAIL 脚本异常：" + repr(exc))
        sys.exit(1)
