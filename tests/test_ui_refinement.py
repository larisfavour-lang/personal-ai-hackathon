"""UI refinement regression: isolated Chrome + synthetic local HTTP fixtures.

No app import, database, real model, port 8000, reset or real credential access.
Serves the actual index file; all /api routes are synthetic fixture responses.
Run: /tmp/hack_venv/bin/python tests/test_ui_refinement.py [--phase before|after]
Screenshots are visual review inputs, not Android acceptance evidence.
"""
import argparse
import asyncio
import base64
import functools
import hashlib
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import websockets

ROOT = Path(__file__).resolve().parents[1]
helper = ROOT / "tests" / "prompt-chrome-20261015-0507.py"
ns = {"__file__": str(helper)}
exec(helper.read_text().rsplit("raise SystemExit", 1)[0], ns)


class Fixture(SimpleHTTPRequestHandler):
    """Only the browser talks to this server; synthetic data is in memory."""
    mode = "ok"
    calls = []

    def log_message(self, *_):
        pass

    def response(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            self.path = "/app/static/index.html"
            return super().do_GET()
        if path.startswith("/static/"):
            self.path = "/app" + path
            return super().do_GET()
        data = {
            "/api/me": {"user_id": "A", "display_name": "演示本人"},
            "/api/conversation": {"turns": [
                {"role": "user", "created_at": "2026-10-16T09:00:00", "text": "周末三家一起带孩子出去玩，我出100以内。\n如果下雨，希望有室内备选。"},
                {"role": "assistant", "created_at": "2026-10-16T09:00:01", "text": "已按家庭活动记下。100 元是你家出资上限，不是整场总预算。\n还有一家待确认，孩子出席也等各家自己说。"},
                {"role": "user", "created_at": "2026-10-16T09:01:00", "text": "参考地址：" + "https://example.invalid/活动安排/" + "long-path-" * 14},
                {"role": "assistant", "created_at": "2026-10-16T09:01:01", "text": "可以继续补条件，不会替别家确认。你已保存的记录仍可以回来修改。"},
            ]},
            "/api/matters": {"matters": [], "waiting": []},
            "/api/overview": {"persons": []},
            "/api/plans": {"plans": []},
            "/api/health": {"model": {"ok": True, "model": "fixture-model", "base_url": "https://example.invalid/" + "long-provider-path/" * 9 + "v1"}},
            "/api/model-config": {"base_url": "https://example.invalid/v1", "model_name": "fixture-model", "has_api_key": True},
            "/api/ui-revision": {"revision": "fixture"},
            "/api/phone-access": {"phone_urls": ["http://fixture.invalid:8100"], "pwa_install_note": "合成地址，不是真机证明", "steps": []},
        }
        if path.startswith("/api/"):
            return self.response(data.get(path, {}))
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or "{}")
        type(self).calls.append({"path": path, "body": body})
        if path == "/api/model-config":
            time.sleep(.65)
            if self.mode == "fail":
                return self.response({"detail": "合成测试：接口拒绝保存，请核对地址后重试"}, 503)
            return self.response({"model": {"ok": self.mode != "offline", "sample": "合成测试连接成功", "error": "合成测试：模型暂不可用", "model": body.get("model_name"), "base_url": body.get("base_url")}, "has_api_key": True})
        if path == "/api/model-config/clear-key":
            time.sleep(.65)
            return self.response({"has_api_key": False})
        if path == "/api/say":
            return self.response({"kind": "family_update", "reply": "合成回复：记录已接续"})
        return self.response({"detail": "Fixture forbids other writes"}, 403)


async def run(phase):
    checks = []
    def check(name, ok, detail=None):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name, flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Fixture, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    profile = tempfile.mkdtemp(prefix="t19-ui-chrome-")
    chrome = subprocess.Popen(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        active = Path(profile) / "DevToolsActivePort"
        for _ in range(100):
            if active.exists():
                break
            await asyncio.sleep(.1)
        port = int(active.read_text().splitlines()[0])
        pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5))
        page = next(p for p in pages if p["type"] == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=20*1024*1024) as ws:
            c = ns["CDP"](ws)
            pump = asyncio.create_task(c.pump())
            await c.send("Page.enable")
            await c.send("Runtime.enable")
            async def viewport(width, height=900):
                await c.send("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=width<900)
            async def navigate():
                await c.send("Page.navigate", url=base + "/")
                await c.wait("document.querySelectorAll('.msg').length >= 4")
                await c.js("document.querySelector('#stream').scrollTop=0;window.scrollTo(0,0)")
            async def shot(name):
                data = await c.send("Page.captureScreenshot", format="png")
                target = ROOT / "tests" / ("ui-refinement-" + name + ".png")
                target.write_bytes(base64.b64decode(data["data"]))
                print("SCREENSHOT " + target.name, flush=True)
            async def overflow():
                return await c.js("({viewport:innerWidth,document:document.documentElement.scrollWidth,offscreen:[...document.querySelectorAll('.bubble,#model-config-box,input:not([type=file]),button')].filter(e=>e.getClientRects().length && !e.closest('.side:not(.open)') && !e.closest('.quick')).filter(e=>{let r=e.getBoundingClientRect();return r.left<-.5||r.right>innerWidth+.5}).map(e=>e.id||e.className)})")
            await viewport(375)
            await navigate()
            await shot(phase + "-mobile")
            o = await overflow()
            check("375px long URL/Chinese has no page overflow", o["document"] <= o["viewport"] and not o["offscreen"], o)
            await viewport(430)
            await c.click("#btn-demo")
            await c.click("#demo-model-config")
            await c.wait("!!document.getElementById('mc-key')")
            await c.js("document.querySelector('.sheet .panel').scrollTop=0")
            await shot(phase + "-settings")
            if phase == "before":
                print(json.dumps({"phase": phase, "checks": checks, "boundary": "Synthetic in-memory fixtures, no DB/model/8000"}, ensure_ascii=False))
                pump.cancel()
                return 0
            for width in (375, 430):
                await viewport(width)
                o = await overflow()
                check(f"settings {width}px no page overflow", o["document"] <= o["viewport"] and not o["offscreen"], o)
                dims = await c.js("['mc-base','mc-model','mc-key'].map(id=>{let e=document.getElementById(id),r=e.getBoundingClientRect();return {id,w:r.width,h:r.height,font:parseFloat(getComputedStyle(e).fontSize),label:!!document.querySelector('label[for='+id+']'),help:!!document.getElementById(e.getAttribute('aria-describedby'))}})")
                # t58：模型连接是运行者维护项，手机窄屏（≤600px）整段不呈现。
                # 所以这两档要验的不再是「配置表单满宽可点」，而是
                # ① 配置表单确实不呈现（offsetParent 为空）；
                # ② 窄屏上仍然呈现的那部分（手机访问说明 / 加入助手说明）排版没坏。
                if width <= 600:
                    hidden = await c.js("['mc-base','mc-model','mc-key'].every(id=>!document.getElementById(id).offsetParent)")
                    check(f"settings {width}px hides runner-only model config", hidden, dims)
                    # 手机侧说明的**文案**必须在 DOM 里（回归护栏：改窄屏分支时不能把
                    # 「回执回到同一件事」这段丢掉）。可见性由媒体查询负责，上面
                    # 「hides runner-only model config」已经证明 ≤600px 时分叉生效。
                    shown = await c.js("(()=>{let els=[...document.querySelectorAll('#sheet-body .pair-phone-only')];return {n:els.length,text:els.map(e=>e.textContent||'').join(' ')}})()")
                    check(f"settings {width}px phone-side copy keeps receipt guidance",
                          shown and shown["n"] >= 2 and "回执" in shown["text"]
                          and "桌面" in shown["text"], shown)
                else:
                    check(f"settings {width}px full width labels/help and >=16px text", all(d["w"] > width*.7 and d["h"] >=44 and d["font"]>=16 and d["label"] and d["help"] for d in dims), dims)
            await viewport(375)
            await c.js("document.getElementById('mc-key').scrollIntoView({block:'center'})")
            await shot("after-settings-bottom-375")
            lower = await c.js("['mc-key','mc-save','mc-clearkey','mc-cancel'].map(id=>{let e=document.getElementById(id),r=e.getBoundingClientRect();return {id,l:r.left,r:r.right,t:r.top,b:r.bottom}})")
            check("lower settings actions do not overlap", all(a['b'] <= b['t']+.5 or a['r'] <= b['l']+.5 or b['r'] <= a['l']+.5 for i,a in enumerate(lower) for b in lower[i+1:]), lower)
            check("password stays empty/non-disclosing", await c.js("document.getElementById('mc-key').type==='password' && document.getElementById('mc-key').value===''"))
            targets = await c.js("[...document.querySelectorAll('#sheet-body button,.topbar button')].filter(e=>e.getClientRects().length).map(e=>({id:e.id,h:e.getBoundingClientRect().height,w:e.getBoundingClientRect().width}))")
            check("settings/topbar targets at least 44px", all(t["h"]>=44 and t["w"]>=44 for t in targets), targets)
            geometry = await c.js("(()=>{let e=document.getElementById('mc-base');let r=e.getBoundingClientRect();return {w:r.width,h:r.height,b:getComputedStyle(e).borderWidth}})()")
            await c.js("document.getElementById('mc-base').focus()")
            await c.send("Input.dispatchKeyEvent", type="keyDown", key="Tab", code="Tab", windowsVirtualKeyCode=9)
            await c.send("Input.dispatchKeyEvent", type="keyUp", key="Tab", code="Tab", windowsVirtualKeyCode=9)
            focus = await c.js("(()=>{let e=document.activeElement,s=getComputedStyle(e);return {id:e.id,visible:e.matches(':focus-visible'),outline:s.outlineWidth}})()")
            check("keyboard Tab has visible focus ring", focus["visible"] and float(focus["outline"].replace('px',''))>=2, focus)
            Fixture.mode = "fail"
            await c.js("document.getElementById('mc-base').value='https://edited.invalid/v1';document.getElementById('mc-key').value='synthetic-not-a-real-secret'")
            await c.click("#mc-save")
            loading = await c.js("({save:document.getElementById('mc-save').disabled,clear:document.getElementById('mc-clearkey').disabled,busy:document.getElementById('model-config-box').getAttribute('aria-busy'),label:document.getElementById('mc-save').textContent})")
            check("save loading blocks duplicate save/clear and keeps label", loading["save"] and loading["clear"] and loading["busy"]=="true" and bool(loading["label"]), loading)
            before_duplicate = len(Fixture.calls)
            await c.js("document.getElementById('mc-save').click();document.getElementById('mc-clearkey').click();document.getElementById('mc-key').scrollIntoView({block:'center'})")
            await shot("after-settings-loading")
            check("disabled save/clear dispatch no duplicate request", len(Fixture.calls)==before_duplicate)
            await c.wait("document.getElementById('mc-msg').textContent.includes('没保存')")
            await shot("after-settings-error")
            failure = await c.js("({value:document.getElementById('mc-base').value,key:document.getElementById('mc-key').value,state:document.getElementById('mc-msg').dataset.state,disabled:document.getElementById('mc-save').disabled})")
            check("save error keeps edited fields/key and permits retry", failure["value"]=='https://edited.invalid/v1' and failure["key"]=='synthetic-not-a-real-secret' and failure["state"]=='error' and not failure["disabled"], {k:v for k,v in failure.items() if k!='key'})
            Fixture.mode = "offline"
            await c.click("#mc-save")
            await c.wait("document.getElementById('mc-msg').textContent.includes('已保存，但没连上')")
            check("saved-but-offline distinguishable from failed save", await c.js("document.getElementById('mc-msg').dataset.state==='warning'"))
            Fixture.mode = "ok"
            await c.click("#mc-save")
            await c.wait("document.getElementById('mc-msg').textContent.includes('已保存并连上')")
            check("successful save has success status", await c.js("document.getElementById('mc-msg').dataset.state==='success'"))
            before_clear = len(Fixture.calls)
            await c.click("#mc-clearkey")
            await c.wait("document.getElementById('mc-msg').textContent.includes('密钥已清空')")
            check("clear-key explicit endpoint/body once; input cleared", len(Fixture.calls)==before_clear+1 and Fixture.calls[-1]=={"path":"/api/model-config/clear-key","body":{}} and await c.js("document.getElementById('mc-key').value===''"))
            after_geometry = await c.js("(()=>{let e=document.getElementById('mc-base');let r=e.getBoundingClientRect();return {w:r.width,h:r.height,b:getComputedStyle(e).borderWidth}})()")
            check("form focus/error/loading do not shift border geometry", geometry==after_geometry, {"before":geometry,"after":after_geometry})
            # Canvas normalizes computed OKLCH/rgb to actual sRGB pixels; WCAG 2.1 ratios.
            contrast = await c.js("""(()=>{let style=getComputedStyle(document.documentElement);let canvas=document.createElement('canvas');canvas.width=canvas.height=1;let ctx=canvas.getContext('2d');function rgb(token){ctx.clearRect(0,0,1,1);ctx.fillStyle=style.getPropertyValue(token).trim();ctx.fillRect(0,0,1,1);return [...ctx.getImageData(0,0,1,1).data].slice(0,3)}function lum(c){return c.map(v=>{v/=255;return v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4)}).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0)}return [['--ink','--paper',4.5],['--ink-2','--surface',4.5],['--ink-3','--paper',4.5],['--ink','--me-soft',4.5],['--accent-ink','--accent',4.5],['--bad','--bad-soft',4.5],['--wait','--wait-soft',4.5],['--accent','--accent-soft',4.5],['--field-line','--surface',3],['--focus','--surface',3]].map(([fg,bg,min])=>{let f=rgb(fg),b=rgb(bg),l1=lum(f),l2=lum(b);return {fg,bg,rgb:[f,b],ratio:(Math.max(l1,l2)+.05)/(Math.min(l1,l2)+.05),min}})})()""")
            check("measured text >=4.5 and field/focus >=3 contrast", all(t["ratio"]>=t["min"] for t in contrast), contrast)
            await c.send("Emulation.setEmulatedMedia", features=[{"name":"prefers-reduced-motion","value":"reduce"},{"name":"hover","value":"none"}])
            check("reduced-motion animation disabled", await c.js("parseFloat(getComputedStyle(document.querySelector('.panel')).animationDuration)<.02"))
            await c.js("document.getElementById('mc-cancel').click()")
            check("collapse keeps configuration entry reusable", await c.js("document.getElementById('model-config-box').innerHTML==='' && !!document.getElementById('demo-model-config')"))
            await c.js("document.querySelector('[data-close-sheet]').click()")
            await viewport(1280)
            await c.js("window.scrollTo(0,0)")
            await shot("after-desktop")
            o = await overflow()
            check("desktop 1280px no overflow", o["document"]<=o["viewport"] and not o["offscreen"], o)
            check("t41/t58 photo/voice entry markers intact", all(marker in (ROOT/'app/static/index.html').read_text() for marker in ('note-add-photo','note-add-voice','note-pick-choice','note-sheet-hint','note-choice-ocr','note-choice-keep','note-choice-crop','notePickMode','note-image-acts','note-more-acts','attachPushToTalk','data-plan-propose','said.kind === "group_activity"','data-act="open-plan"')))
            check("no browser JS runtime errors", await c.js("window.__paErr === ''"))
            check("no reset or non-fixture writes", all(x['path'] in ('/api/model-config','/api/model-config/clear-key') for x in Fixture.calls))
            pump.cancel()
        result = {"phase":phase,"source_sha256":hashlib.sha256((ROOT/'app/static/index.html').read_bytes()).hexdigest(),"viewports":[375,430,1280],"checks":checks,"passed":sum(x['ok'] for x in checks),"total":len(checks),"boundary":"Actual Chrome/CDP DOM and keyboard; synthetic HTTP fixture, no app/DB/model/8000; not Android"}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if all(x['ok'] for x in checks) else 1
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome.kill();chrome.wait()
        server.shutdown();server.server_close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--phase',choices=('before','after'),default='after')
    raise SystemExit(asyncio.run(run(parser.parse_args().phase)))
