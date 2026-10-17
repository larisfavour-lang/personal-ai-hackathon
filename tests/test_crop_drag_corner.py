"""拖角时固定对边不能漂。

监督实测过的反例：初始选区 (.08,.08,.84,.84)，右下角在 (.92,.92)。
把左上角一路拖过原右下角之后，选区变成 (.94,.94,.06,.06)，
右下漂到了 (1,1)——用户明明没碰右下，他圈中的范围却变了。

正确行为：拖哪个角，就只动那个角，对角的坐标全程不变；
移动边最多推到「固定对边 − 最小尺寸」处就停住。

跑真实 HTTP 栈与真实鼠标事件，但 /api/photo/read 全部拦截：
0 次真实模型调用、0 次写库。

运行：/tmp/hack_venv/bin/python tests/test_crop_drag_corner.py
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
OUT = ROOT / "docs" / "evidence" / "crop-drag-corner-verify.txt"


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
         "--user-data-dir=/tmp/codex-crop-corner-%d" % PORT, "about:blank"],
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

            # 固定到监督用过的初值，反例必须能复现。
            # 滚动到视口中间：裁剪界面在弹层里，不滚动时把手可能落在
            # 视口外，elementFromPoint 取不到元素，事件根本打不到它，
            # 于是「选区没动」会被误判成功能坏了。
            await c.js("cropState.rect={x:.08,y:.08,w:.84,h:.84};drawCrop();"
                       "document.querySelector('#crop-r .tl')"
                       ".scrollIntoView({block:'center'})")
            await asyncio.sleep(0.2)
            before = await c.js("JSON.parse(JSON.stringify(cropState.rect))")
            geo = await c.js("(()=>{let h=document.querySelector('#crop-r .tl')"
                             ".getBoundingClientRect(),b=document.querySelector('#crop-box')"
                             ".getBoundingClientRect();"
                             "return {x:h.x+h.width/2,y:h.y+h.height/2,"
                             "tx:b.x+b.width*.98,ty:b.y+b.height*.98}})()")
            # 目标点必须在裁剪框内才算有效拖动。落到框外的话
            # pointermove 到不了 box，位移证据不成立。
            hit = await c.js("(()=>{let e=document.elementFromPoint(%f,%f);"
                             "return e? e.className : 'null'})()"
                             % (geo["x"], geo["y"]))
            for typ, x, y in [("mouseMoved", geo["x"], geo["y"]),
                              ("mousePressed", geo["x"], geo["y"]),
                              ("mouseMoved", geo["tx"], geo["ty"]),
                              ("mouseReleased", geo["tx"], geo["ty"])]:
                await c.send("Input.dispatchMouseEvent", type=typ, x=x, y=y,
                             button="left" if typ != "mouseMoved" else "none",
                             buttons=1 if typ in ("mousePressed", "mouseMoved") else 0,
                             clickCount=1)
                await asyncio.sleep(0.08)
            after = await c.js("JSON.parse(JSON.stringify(cropState.rect))")

            moved = (abs(after["x"] - before["x"]) > 1e-6
                     or abs(after["y"] - before["y"]) > 1e-6)
            check("拖左上角选区真的移动了", moved,
                  {"before": before, "after": after, "target": [geo["tx"], geo["ty"]],
                   "handle_hit": hit})

            # 右下角坐标必须原样不动。它是这次拖动里的固定对边。
            brx = after["x"] + after["w"]
            bry = after["y"] + after["h"]
            want_x = before["x"] + before["w"]
            want_y = before["y"] + before["h"]
            check("固定对边（右下）没有漂",
                  abs(brx - want_x) < 1e-6 and abs(bry - want_y) < 1e-6,
                  {"got": [brx, bry], "want": [want_x, want_y]})

            # 越过对边也不能翻面：宽高始终不小于最小尺寸。
            check("越过对边后选框没有翻面",
                  after["w"] >= 0.06 - 1e-9 and after["h"] >= 0.06 - 1e-9,
                  {"after": after})

            # 四条边都必须在画面内。
            check("选框始终在画面内",
                  after["x"] >= -1e-9 and after["y"] >= -1e-9
                  and after["x"] + after["w"] <= 1 + 1e-9
                  and after["y"] + after["h"] <= 1 + 1e-9, {"after": after})

            # 裁出来的仍是右下蓝色区：范围没被搬到别处。
            px = await c.js("(async()=>{cropState.rect={x:.5,y:.5,w:.5,h:.5};drawCrop();"
                            "let o=await cropToCompressedB64();"
                            "let i=new Image();i.src='data:'+o.media+';base64,'+o.b64;"
                            "await i.decode();let v=document.createElement('canvas');"
                            "v.width=o.w;v.height=o.h;let x=v.getContext('2d');"
                            "x.drawImage(i,0,0);"
                            "return {w:o.w,h:o.h,pixel:[...x.getImageData(250,250,1,1).data]}})()")
            check("按选中范围裁出的是那块内容",
                  px["w"] == 500 and px["h"] == 500
                  and px["pixel"][2] > 240 and px["pixel"][0] < 20, px)
            pump.cancel()

        ok = sum(1 for _, p, _ in checks if p)
        lines = ["裁剪拖角与固定对边：%d/%d 通过" % (ok, len(checks)),
                 "边界：隔离桌面 Chrome 430×900，合成 1000×1000 图，真实鼠标事件，",
                 "      /api/photo/read 全部拦截（0 次真实模型、0 次写库）；",
                 "      不是安卓真机触屏，真实手指拖拽仍需实机复核。",
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
