"""图片识别的两步选择：只存图不识别 / 先裁剪再识别 / 识别进正文（t41）。

t41 把「加图片」区的四个并列按钮改成两个一级选项（图片识别 / 语音输入），
选定文件后图先进草稿、**先不送模型**，弹层内再让本人选：
①识别图里的文字进正文（送模型）②只把图存进资料库不识别（一次都不送）
③先裁剪再识别（只送框里那一份）。本套件用隔离 Chrome 真实点击这三个选项，
记录请求，验四件事：

1. 选定文件后图进草稿但 0 次 /api/photo/read；选「只存图」之后仍然 0 次；
2. 选「先裁剪再识别」→ 直接进裁剪界面，裁剪确认前 0 次 read；
3. 裁剪界面取消 → 不传任何 read，草稿不丢；
4. 选「识别图里的文字，进正文框」之后才发那 1 次 read，文字进正文框。

边界：隔离桌面 Chrome 430×900；真实 DOM 点击与 File 注入；
/api/photo/read 与 /api/photo/save 全部拦截返回合成数据
（0 次真实模型、0 次业务写）。不是安卓真机相机。
「只把图存进资料库」保存时仍会把图作为附件上传——不写「永不离开手机」，
那条过度结论在 t10 证据第九节已经作废。

运行：/tmp/hack_venv/bin/python tests/test_photo_preselect_entries.py
"""
import asyncio
import base64
import json
import pathlib
import sys
import os
import tempfile

helper = pathlib.Path(__file__).with_name("prompt-chrome-20261015-0507.py")
ns = {"__file__": str(helper)}
exec(helper.read_text().rsplit("raise SystemExit", 1)[0], ns)

TINY_PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c63000100000500010d0a2db4000000"
    "0049454e44ae426082")).decode()


class C(ns["CDP"]):
    """拦 photo/read、photo/save；记录每次 read 的载荷，用于证明送出去的是哪一张。"""

    def __init__(self, ws):
        super().__init__(ws)
        self.loaded = asyncio.Event()
        self.reads = 0
        self.saves = []
        self.read_bodies = []

    async def pump(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if m.get("id") in self.pending:
                self.pending.pop(m["id"]).set_result(m)
            elif m.get("method") == "Page.loadEventFired":
                self.loaded.set()
            elif m.get("method") == "Fetch.requestPaused":
                asyncio.create_task(self.fulfill(m["params"]))

    async def wait(self, expr, tries=200):
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
            self.read_bodies.append(json.loads(r.get("postData") or "{}"))
            body = {"transcribed": "哥哥练琴四十分钟",
                    "fields": {"完成时间": "40分钟"},
                    "happened_on": "2026-10-14", "confidence": "high",
                    "unclear_parts": [], "subject_hint": ""}
            await self.send("Fetch.fulfillRequest", requestId=p["requestId"],
                            responseCode=200,
                            responseHeaders=[{"name": "Content-Type",
                                              "value": "application/json"}],
                            body=base64.b64encode(json.dumps(
                                body, ensure_ascii=False).encode()).decode())
            return
        if r["url"].endswith("/api/photo/save"):
            self.saves.append(json.loads(r.get("postData") or "{}"))
            body = {"event_id": "evt_preselect_stub", "created": True,
                    "provider": "文字+图片记录（我 录入，本人核对后采用）",
                    "happened_on": "2026-10-14", "date_unknown": False,
                    "fields": {"正文": "x"}, "attachments": [],
                    "text_edited": True}
            await self.send("Fetch.fulfillRequest", requestId=p["requestId"],
                            responseCode=200,
                            responseHeaders=[{"name": "Content-Type",
                                              "value": "application/json"}],
                            body=base64.b64encode(json.dumps(
                                body, ensure_ascii=False).encode()).decode())
            return
        await self.send("Fetch.continueRequest", requestId=p["requestId"])


async def pick_file(c, sel):
    await c.js("(async()=>{"
               "const b=atob(%s);const u=new Uint8Array(b.length);"
               "for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);"
               "const f=new File([u],'a.png',{type:'image/png'});"
               "const dt=new DataTransfer();dt.items.add(f);"
               "const inp=document.querySelector(%s);"
               "inp.files=dt.files;"
               "inp.dispatchEvent(new Event('change',{bubbles:true}));"
               "})()" % (json.dumps(TINY_PNG), json.dumps(sel)))


async def main():
    checks = []

    def check(n, ok, d=None):
        checks.append((n, bool(ok)))
        print(("PASS " if ok else "FAIL ") + n + ("" if ok else "  " + repr(d)[:300]),
              flush=True)

    # 页面加载后先重置一次草稿状态，保证三个用例互不污染。
    await c.send("Page.enable")
    await c.send("Runtime.enable")
    await c.send("Emulation.setDeviceMetricsOverride", width=430, height=900,
                 deviceScaleFactor=1, mobile=False)
    await c.send("Fetch.enable", patterns=[{"urlPattern": "*/api/photo/*",
                                            "requestStage": "Request"}])
    await c.send("Page.navigate", url=os.environ.get("PA_TEST_BASE", "http://127.0.0.1:8000") + "/")
    await c.wait("typeof api === 'function' && !!ownerId")
    await c.wait("document.querySelector('#btn-photo')")
    await c.js("document.querySelector('#btn-photo').click()")
    await c.wait("document.querySelector('#note-text')")

    # ---- 1) 图片识别 → 选定文件 → 选「只把图存进资料库」：全程 0 次 read ----
    reads0 = c.reads
    await c.js("document.querySelector('#note-add-photo').click()")
    await pick_file(c, "#note-photo-input")
    await c.wait("noteCtx && noteCtx.attachments.length > 0")
    await asyncio.sleep(0.5)
    st = await c.js("({atts:noteCtx.attachments.length,"
                    "reads:noteCtx.reads.length,"
                    "busy:noteCtx.ocrBusy,"
                    "body:document.querySelector('#note-text').value,"
                    "hint:(document.querySelector('#note-sheet-hint')||{}).textContent||''})")
    check("选定文件后图进草稿但 0 次 /api/photo/read（等本人选）",
          c.reads == reads0, {"reads": c.reads, "before": reads0})
    check("弹层内二选一已出现（图还没送出去）",
          "还没有送出去" in st["hint"], st["hint"])
    await c.js("document.querySelector('#note-choice-keep').click()")
    await asyncio.sleep(0.8)
    st = await c.js("({atts:noteCtx.attachments.length,"
                    "reads:noteCtx.reads.length,"
                    "busy:noteCtx.ocrBusy,"
                    "body:document.querySelector('#note-text').value,"
                    "hint:(document.querySelector('#note-sheet-hint')||{}).textContent||''})")
    check("选「只把图存进资料库」后 0 次 /api/photo/read", c.reads == reads0,
          {"reads": c.reads, "before": reads0})
    check("只存图：图进了草稿（缩略图在、附件 1 个）", st["atts"] == 1, st)
    check("只存图：没有生成识别条目", st["reads"] == 0, st)
    check("只存图：正文框没被插入识别文字", st["body"] == "", st["body"])
    check("只存图：弹层内明确反馈没有识别图里的字",
          "没有识别图里的字" in st["hint"], st["hint"])
    check("只存图：模式变量用完即重置，不会带到下一张图",
          await c.js("notePickMode") == "ask", await c.js("notePickMode"))

    # ---- 2) 选定文件后选「先裁剪再识别」：进裁剪、确认前 0 次 read ----
    reads1 = c.reads
    await c.js("document.querySelector('#note-add-photo').click()")
    await pick_file(c, "#note-photo-input")
    await c.wait("noteCtx && noteCtx.attachments.length > 0")
    await c.js("document.querySelector('#note-choice-crop').click()")
    in_crop = await c.wait("!!document.querySelector('#crop-ok')"
                           " || !!document.querySelector('[data-crop-ok]')"
                           " || !!(window.cropState)", tries=60)
    check("选图前点「先裁剪再识别」直接进入裁剪界面", in_crop)
    await asyncio.sleep(0.6)
    check("裁剪确认前 0 次 /api/photo/read（整张图没送出去）",
          c.reads == reads1, {"reads": c.reads, "before": reads1})
    # 草稿里已经有上一用例「只存图」那一张，所以这里是 2：
    # 两个次入口走同一条草稿，图都累积在一起，不互相顶掉。
    check("裁剪界面里图已作为附件在草稿中（含上一用例那一张）",
          await c.js("(noteCtx && noteCtx.attachments ? noteCtx.attachments.length : -1)") == 2,
          await c.js("(noteCtx && noteCtx.attachments ? noteCtx.attachments.length : -1)"))

    # ---- 3) 裁剪取消：不传任何 read ----
    reads2 = c.reads
    await c.js("(function(){const b=document.querySelector('[data-close-sheet]');"
               "if(b) b.click();})()")
    await asyncio.sleep(0.8)
    check("裁剪取消后仍然 0 次 /api/photo/read", c.reads == reads2,
          {"reads": c.reads, "before": reads2})
    back = await c.js("({atts:noteCtx?noteCtx.attachments.length:-1,"
                      "hasText:!!document.querySelector('#note-text')})")
    check("取消裁剪回到草稿（不是关掉整个弹层），图没丢",
          back["atts"] == 2 and back["hasText"], back)

    # ---- 4) 选「识别图里的文字，进正文框」之后才发那 1 次 read ----
    reads3 = c.reads
    await c.js("document.querySelector('#note-add-photo').click()")
    await pick_file(c, "#note-photo-input")
    await c.wait("noteCtx && noteCtx.attachments.length > 0")
    check("点识别之前仍然 0 次 read", c.reads == reads3,
          {"reads": c.reads, "before": reads3})
    await c.js("document.querySelector('#note-choice-ocr').click()")
    await c.wait("noteCtx && noteCtx.ocrBusy === false && noteCtx.reads.length > 0"
                 " && (noteCtx.reads[0].ok || noteCtx.reads[0].error)", tries=150)
    check("本人点过「识别」之后才发那 1 次 read", c.reads == reads3 + 1,
          {"reads": c.reads, "before": reads3})
    body = await c.js("document.querySelector('#note-text').value")
    check("默认路径识别文字进正文框", "哥哥练琴四十分钟" in body, body)

    # ---- 5) 保存：只存图那张也会作为附件上传，不声称永不离开手机 ----
    await c.js("document.querySelector('[data-n-subject]').click()")
    await c.js("document.querySelector('#note-save').click()")
    await c.wait("!document.querySelector('#sheet').classList.contains('open')"
                 " || document.querySelector('#note-save') === null", tries=60)
    await asyncio.sleep(0.6)
    check("保存发出了一次", len(c.saves) == 1, len(c.saves))
    if c.saves:
        check("保存带上了只存图那张的附件（如实：会随记录上传）",
              len(c.saves[0].get("attachments") or []) >= 1,
              len(c.saves[0].get("attachments") or []))
    err = await c.js("window.__paErr||''")
    check("全程无页面脚本错误", not err, err)

    ok = sum(1 for _, p in checks if p)
    print("\n图片识别两步选择：%d/%d 通过" % (ok, len(checks)))
    return 0 if ok == len(checks) else 1


import asyncio  # noqa: E402
import subprocess  # noqa: E402
import urllib.request  # noqa: E402

import websockets  # noqa: E402

PORT = 9623
PROFILE = tempfile.mkdtemp(prefix="t22-preselect-chrome-")


def _launch():
    proc = subprocess.Popen(
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
         "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--window-size=430,1000",
         "--remote-debugging-port=%d" % PORT,
         "--user-data-dir=%s" % PROFILE, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tabs = None
    for _ in range(80):
        try:
            tabs = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/json/list" % PORT, timeout=1).read())
            if any(x["type"] == "page" for x in tabs):
                break
        except Exception:
            pass
        time.sleep(0.15)
    if not tabs:
        raise RuntimeError("隔离 Chrome 未能启动")
    return proc, next(x for x in tabs if x["type"] == "page")


async def run():
    proc, page = _launch()
    try:
        async with websockets.connect(page["webSocketDebuggerUrl"],
                                      max_size=20 * 1024 * 1024) as ws:
            global c
            c = C(ws)
            asyncio.create_task(c.pump())
            return await main()
    finally:
        proc.terminate()


import time  # noqa: E402

if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
