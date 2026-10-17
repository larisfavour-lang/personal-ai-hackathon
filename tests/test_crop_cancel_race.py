"""编码中取消：不能送出识别，也不能弹出一份不想要的草稿。

用户实际会遇到这个顺序：点了「就用这个范围」→ 图片解码和 canvas 编码
还没回来 → 等不及，点了取消或重拍 → 旧图的编码结果这时才回来。
没有代次校验的话，那张他明确说不要的图照样被送去做识别，
屏幕上还会突然多出一份草稿——他不知道自己刚才按了什么才会这样。

监督在真实 Chrome 里测出过失败（1 次 /api/photo/read、弹层重开、草稿出现），
所以这里跑真实页面源码 + 延迟编码复现同一场景。

跑真实 HTTP 栈，但 /api/photo/read 全部拦截：0 次真实模型调用、0 次写库。

运行：/tmp/hack_venv/bin/python tests/test_crop_cancel_race.py
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
OUT = ROOT / "docs" / "evidence" / "crop-cancel-race-verify.txt"


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.calls = []

    async def send(self, method, **params):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method,
                                       "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.n:
                return msg

    async def pump(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg.get("method") == "Fetch.requestPaused":
                await self._intercept(msg["params"])

    async def _intercept(self, p):
        self.calls.append(p["request"]["url"])
        body = {"transcribed": "测试识别", "fields": {"测试": "值"},
                "happened_on": "", "confidence": "high", "unclear_parts": []}
        await self.send("Fetch.fulfillRequest", requestId=p["requestId"],
                        responseCode=200,
                        responseHeaders=[{"name": "Content-Type",
                                          "value": "application/json"}],
                        body=base64.b64encode(json.dumps(body).encode()).decode())

    async def js(self, expr):
        r = await self.send("Runtime.evaluate", expression=expr,
                            returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("result", {}).get("value")

    async def wait(self, expr, tries=80):
        for _ in range(tries):
            if await self.js("(()=>{try{return !!(%s)}catch(e){return false}})()" % expr):
                return True
            await asyncio.sleep(0.15)
        return False


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def main():
    checks = []
    global PORT
    PORT = free_port()

    def check(name, ok, detail):
        checks.append((name, bool(ok), detail))

    proc = subprocess.Popen(
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
         "--headless=new", "--no-first-run", "--no-default-browser-check",
         "--window-size=430,1000", "--remote-debugging-port=%d" % PORT,
         "--user-data-dir=/tmp/codex-crop-race-%d" % PORT, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tabs = None
        for _ in range(60):
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
            pump = asyncio.create_task(c.pump())
            await c.send("Page.enable")
            await c.send("Runtime.enable")
            await c.send("Emulation.setDeviceMetricsOverride",
                         width=430, height=900, deviceScaleFactor=1, mobile=False)
            await c.send("Fetch.enable",
                         patterns=[{"urlPattern": "*/api/photo/read",
                                    "requestStage": "Request"}])
            await c.send("Page.navigate", url="http://127.0.0.1:8000/")
            await c.wait("typeof renderPhotoCrop === 'function'")

            await c.js("window.__cropB64=(()=>{let v=document.createElement('canvas');"
                       "v.width=1000;v.height=1000;let x=v.getContext('2d');"
                       "x.fillStyle='red';x.fillRect(0,0,1000,1000);"
                       "x.fillStyle='blue';x.fillRect(500,500,500,500);"
                       "return v.toDataURL('image/png').split(',')[1]})();"
                       "photoCtx={subjectId:'p_xiaoyu'};"
                       "renderPhotoCrop(__cropB64,'image/png')")
            await c.wait("document.querySelector('#crop-r .tl') && cropState && cropState.w===1000")

            # 场景：点了「就用这个范围」，编码还没回来，用户点了取消。
            await c.js("cropToCompressedB64=()=>new Promise(r=>window.__cropResolve=r);"
                       "document.querySelector('#crop-ok').click()")
            await c.wait("typeof window.__cropResolve === 'function'")
            await c.js("document.querySelector('#sheet [data-close-sheet]').click();"
                       "__cropResolve({b64:__cropB64,media:'image/png',w:1000,h:1000})")
            await asyncio.sleep(1.0)
            st = await c.js("({open:document.querySelector('#sheet')"
                            ".classList.contains('open'),draft:!!photoCtx.draft})")
            check("编码中取消后不送出识别", len(c.calls) == 0,
                  {"requests": len(c.calls)})
            check("编码中取消后不弹出草稿", not st["draft"], st)
            check("编码中取消后弹层已关", not st["open"], st)

            # 遮罩关闭：点弹层外的灰色区域也是「这个界面我不要了」。
            # 这条路径和取消按钮是两条不同的代码路径，用户意图相同，
            # 漏掉它就会出现：人已经关掉界面，迟到的旧编码又送来一次识别，
            # 弹层还重新打开。监督在真实 Chrome 里测出过这个失败。
            c.calls.clear()
            await c.js("photoCtx={subjectId:'p_xiaoyu'};"
                       "renderPhotoCrop(__cropB64,'image/png')")
            await c.wait("document.querySelector('#crop-r .tl') && cropState && cropState.w===1000")
            await c.js("cropToCompressedB64=()=>new Promise(r=>window.__cropResolve2=r);"
                       "document.querySelector('#crop-ok').click()")
            await c.wait("typeof window.__cropResolve2 === 'function'")
            await c.js("document.querySelector('#sheet').click();"
                       "__cropResolve2({b64:__cropB64,media:'image/png',w:1000,h:1000})")
            await asyncio.sleep(1.0)
            st2 = await c.js("({open:document.querySelector('#sheet')"
                             ".classList.contains('open'),draft:!!photoCtx.draft})")
            check("点遮罩关闭后不送出识别", len(c.calls) == 0,
                  {"requests": len(c.calls)})
            check("点遮罩关闭后不弹出草稿", not st2["draft"], st2)
            check("点遮罩关闭后弹层已关", not st2["open"], st2)
            pump.cancel()

        ok = sum(1 for _, p, _ in checks if p)
        lines = ["裁剪取消竞态：%d/%d 通过" % (ok, len(checks)),
                 "边界：隔离桌面 Chrome 430×900，合成 1000×1000 图，"
                 "/api/photo/read 全部拦截（0 次真实模型、0 次写库）；",
                 "      不是安卓真机，也不替代真实 OCR 识别。",
                 ""]
        for name, passed, detail in checks:
            lines.append(("PASS " if passed else "FAIL ") + name)
            if not passed:
                lines.append("       证据：" + repr(detail)[:300])
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
