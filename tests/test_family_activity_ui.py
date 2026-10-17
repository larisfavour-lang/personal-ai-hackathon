"""家庭活动在手机界面上的完整可操作路径（t17 R2/R3 浏览器级）。

覆盖从原话到能继续办完的每一步，以及失败后的恢复：

1. 原话「周末三家一起带孩子出去玩，我出100以内」发出去 → 进家庭活动草稿，
   不建礼物、不出现「点一个孩子」；
2. 「打开这件事」能进事项卡，卡上看得见目标几家、缺几家、孩子出席；
3. 只有本人一方有条件也能排第一版；模型失败时事项还在、能重试；
4. 刷新页面后这件事还在，且不新开重复事项；
5. 新建共同事项的表单只渲染本人一方的条件——别家是待回应占位，
   不会出现「界面让填、服务端 403」的割裂。

边界：隔离桌面 Chrome 430×900；真实 DOM 点击与真实输入；
/api/say、/api/plan/create、/api/plan/*/propose 全部拦截返回合成数据
（0 次真实模型、0 次业务写）。不是安卓真机。
「独立参与」的真实多方会话由接口级用例覆盖（test_family_activity_flow.py），
本脚本只证界面路径本身走得通。

运行：/tmp/hack_venv/bin/python tests/test_family_activity_ui.py
"""
import asyncio
import base64
import json
import pathlib
import subprocess
import sys
import time
import os
import tempfile
import urllib.request

import websockets

helper = pathlib.Path(__file__).with_name("prompt-chrome-20261015-0507.py")
ns = {"__file__": str(helper)}
exec(helper.read_text().rsplit("raise SystemExit", 1)[0], ns)

UTTERANCE = "周末三家一起带孩子出去玩，我出100以内"
PORT = 9637
PROFILE = tempfile.mkdtemp(prefix="t22-famui-chrome-")
BASE = os.environ.get("PA_TEST_BASE", "http://127.0.0.1:8000")


class C(ns["CDP"]):
    """拦 say / plan 写入类接口，返回合成数据；记录发出的请求。"""

    def __init__(self, ws):
        super().__init__(ws)
        self.loaded = asyncio.Event()
        self.says = []
        self.proposes = 0
        self.creates = []
        self.fail_propose = False
        self.version = 0
        self.exists = False

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

    def _json(self, p, code, obj):
        return self.send("Fetch.fulfillRequest", requestId=p["requestId"],
                         responseCode=code,
                         responseHeaders=[{"name": "Content-Type",
                                           "value": "application/json"}],
                         body=base64.b64encode(json.dumps(
                             obj, ensure_ascii=False).encode()).decode())

    async def fulfill(self, p):
        r = p["request"]
        url = r["url"]
        if r["method"] == "GET" and url.endswith("/api/plan/plan_ui_stub"):
            await self._json(p, 200, self.detail())
            return
        if r["method"] == "GET" and url.endswith("/api/conversation"):
            turns = [{"role":"user", "text":UTTERANCE, "created_at":"2026-10-16T23:00:00", "plan_id":"plan_ui_stub"},
                     {"role":"system", "text":"好，这件事按家庭活动记下来了，不开礼物。100元是本人出资上限，其余家庭待确认。", "created_at":"2026-10-16T23:00:01", "plan_id":"plan_ui_stub"}] if self.exists else []
            await self._json(p, 200, {"turns":turns})
            return
        if r["method"] == "GET" and url.endswith("/api/plans"):
            await self._json(p, 200, {"plans": [self.detail()] if self.exists else []})
            return
        if r["method"] in ("GET", "HEAD"):
            await self.send("Fetch.continueRequest", requestId=p["requestId"])
            return
        url = r["url"]
        if url.endswith("/api/say"):
            body = json.loads(r.get("postData") or "{}")
            self.says.append(body)
            self.exists = True
            await self._json(p, 200, {
                "intent": "group_activity", "kind": "group_activity",
                "reply": "好，这件事按家庭活动记下来了，不开礼物、也不用点一个孩子。\\n"
                         "你说的「100 元以内」记在本人出资上限里。\\n"
                         "你说了 3 家，目前只认得出 2 家。剩下的1家先留着待确认。",
                "plan_id": "plan_ui_stub", "reused": False,
                "title": UTTERANCE, "own_cap": "100 元以内",
                "families": [{"key": "f_p_me", "label": "我家", "is_me": True,
                              "adults": [{"person_id": "p_me", "name": "我",
                                          "relation": "self",
                                          "same_family_confirmed": False}],
                              "children": [], "children_known": False,
                              "children_note": "还没有能证明这一家有几个孩子的关系记录",
                              "evidence": []}],
                "missing_families": 1, "requested_families": 3,
            })
            return
        if url.endswith("/api/plan/create"):
            self.exists = True
            self.creates.append(json.loads(r.get("postData") or "{}"))
            await self._json(p, 200, {"plan_id": "plan_ui_stub", "reused": False,
                                      "status": {"plan_id": "plan_ui_stub",
                                                 "title": "x", "stage": "draft"}})
            return
        if "/propose" in url:
            self.proposes += 1
            if self.fail_propose:
                await self._json(p, 503, {"detail": "模型接口不可用"})
            else:
                self.version += 1
                await self._json(p, 200, {
                    "plan_proposal_id": "pprop_stub", "version": self.version,
                    "content": {"时间": "周六上午", "地点": "世纪公园"},
                    "reason": "按各方当前条件排的", "conflicts": []})
            return
        await self.send("Fetch.continueRequest", requestId=p["requestId"])


    def detail(self):
        return {"plan_id":"plan_ui_stub", "title":UTTERANCE, "utterance":UTTERANCE,
                "owner_id":"A", "stage":"awaiting" if self.version else "draft",
                "outcome":"待确认", "agreed":False, "needs_replan":False,
                "current_version":self.version, "requested_families":3,
                "unknown_families":2, "invited":[], "child_attendance":{},
                "family_note":"A/B/C 是两个家庭", "candidate_only":True,
                "confirmation_gaps":["还有2家未明确进入事项"],
                "waiting_on":["还有2家未明确进入事项"],
                "proposal":{"时间":"周六上午", "地点":"世纪公园"} if self.version else None,
                "proposal_reason":"按各方条件排", "versions":[], "decisions":[],
                "participants":[{"participant_id":"pp_a", "owner_id":"A", "label":"我家", "required":True, "state":"pending", "conditions":{"本人出资上限":"100元以内"}}],
                "families":[{"key":"f_a", "is_me":True, "label":"我家", "children":[], "children_known":False, "adults":[], "children_note":"还没有记录"}]}


async def main():
    checks = []

    def check(n, ok, d=None):
        checks.append((n, bool(ok)))
        print(("PASS " if ok else "FAIL ") + n + ("" if ok else "  " + repr(d)[:300]),
              flush=True)

    await c.send("Page.enable")
    await c.send("Runtime.enable")
    await c.send("Emulation.setDeviceMetricsOverride", width=430, height=900,
                 deviceScaleFactor=1, mobile=False)
    await c.send("Fetch.enable", patterns=[{"urlPattern": "*/api/*",
                                            "requestStage": "Request"}])
    await c.send("Page.navigate", url=BASE + "/")
    await c.wait("typeof api === 'function' && !!ownerId")
    await c.wait("document.querySelector('#input')")

    # ---- 1) 原话发出去 ----
    await c.js("(function(){const i=document.querySelector('#input');"
               "i.value=%s;i.dispatchEvent(new Event('input',{bubbles:true}));"
               "document.querySelector('#send').click();})()" % json.dumps(UTTERANCE))
    # 不能等「准备就绪」：发送前它就是准备就绪，会当场命中、读到的是旧消息流。
    # 等回复里出现本用例的合成文本，才说明这一轮真的回来了。
    if not await c.wait("document.querySelector('#stream').textContent.includes('不开礼物')", tries=120):
        raise RuntimeError("stateful conversation not rendered")
    await asyncio.sleep(0.3)
    check("原话发出了一次 /api/say", len(c.says) == 1, len(c.says))
    stream = await c.js("document.querySelector('#stream').textContent")
    check("回复说明按家庭活动记下，不开礼物", "不开礼物" in stream, stream[:120])
    check("回复说明 100 记在本人出资上限", "本人出资上限" in stream, stream[:160])
    check("回复说明缺的第三家待确认", "待确认" in stream, stream[:200])
    check("界面上没有出现「点一个孩子」这类送礼引导",
          "点一个就行" not in stream and "送礼对象" not in stream, stream[:200])
    has_open = await c.js("!!document.querySelector('[data-act=\"open-plan\"]')")
    check("给了一个能点开的「打开这件事」入口", has_open)

    # ---- 2) 打开事项卡 ----
    await c.js("document.querySelector('[data-act=\"open-plan\"]').click()")
    await c.wait("document.querySelector('#sheet').classList.contains('open')", tries=60)
    if not await c.wait("!!document.querySelector('[data-plan-propose]')", tries=100):
        raise RuntimeError("事项GET详情未渲染，不能用弹层空壳算通过")
    card = await c.js("document.querySelector('#sheet-body').textContent")
    check("事项卡显示目标几家", "目标" in card and "3 家" in card, card[:200])
    check("事项卡显示还缺几家、说明不凑数",
          "还没有记录" in card or "待确认" in card, card[:240])
    check("事项卡说明 A/B/C 是两个家庭", "两个家庭" in card, card[:240])
    check("事项卡显示家庭与孩子（按关系）", "家庭与孩子" in card, card[:200])

    # First attempt fails before any version exists; retry then creates version 1.
    check("只有本人条件也有排第一版入口", await c.js("!!document.querySelector('[data-plan-propose]')"))
    c.fail_propose = True
    await c.js("document.querySelector('[data-plan-propose]').click()")
    if not await c.wait("document.querySelector('#sheet-body').textContent.includes('没能排出这一版')", tries=100):
        raise RuntimeError("模型失败原因未显示")
    err = await c.js("document.querySelector('#sheet-body').textContent")
    check("首次模型失败发出propose但不假落版", c.proposes == 1 and c.version == 0)
    check("失败原因明确且事项保留", "事项还在" in err and "没能排出这一版" in err)
    check("失败后仍有重试入口", await c.js("!!document.querySelector('[data-plan-propose]')"))
    c.fail_propose = False
    await c.js("document.querySelector('[data-plan-propose]').click()")
    if not await c.wait("document.querySelector('#sheet-body').textContent.includes('第 1 版')", tries=100):
        raise RuntimeError("重试后第1版真实详情未显示")
    check("重试成功且GET显示精确第1版", c.proposes == 2 and c.version == 1 and await c.js("document.querySelector('#sheet-body').textContent.includes('第 1 版')"))

    # ---- 5) 刷新后这件事还在，不新开重复事项 ----
    await c.send("Page.reload")
    await c.wait("typeof api === 'function' && !!ownerId")
    await c.wait("document.querySelector('#input')")
    await asyncio.sleep(0.5)
    check("刷新后同一事项仍在状态化GET", await c.js("document.body.textContent.includes('" + UTTERANCE + "')"))
    if not await c.wait("document.querySelectorAll('#stream [data-act=\"open-plan\"][data-plan=\"plan_ui_stub\"]').length === 1", tries=100):
        raise RuntimeError("刷新后同plan持久入口缺失或重复")
    check("刷新后同plan打开入口恰好一份", await c.js("document.querySelectorAll('#stream [data-act=\"open-plan\"][data-plan=\"plan_ui_stub\"]').length") == 1)
    check("刷新后没有自动新开事项（say 次数不变）", len(c.says) == 1, len(c.says))

    # ---- 6) 新建表单只渲染本人一方条件 ----
    await c.js("document.querySelector('#btn-new-plan').click()")
    await c.wait("document.querySelector('#btn-create-plan')", tries=60)
    owners = await c.js(
        "Array.from(document.querySelectorAll('[data-cond-owner]'))"
        ".map((e) => e.dataset.condOwner)")
    check("新建表单只渲染本人一方的条件输入",
          owners and set(owners) == {owner_id()}, owners)
    placeholders = await c.js(
        "Array.from(document.querySelectorAll('#sheet-body .item .v'))"
        ".map((e) => e.textContent)")
    check("别家只显示待回应占位，不给输入框",
          any("待这一方从自己的会话补条件" in t for t in placeholders), placeholders)
    check("预算口径说明在提交前可见", "活动总预算" in await c.js("document.querySelector('#sheet-body').textContent") and "本人出资上限" in await c.js("document.querySelector('#sheet-body').textContent"))
    await c.js("(()=>{const e=document.querySelector('[data-cond-owner=\"A\"]');e.value='周六上午';e.dispatchEvent(new Event('input',{bubbles:true}));})()")
    # 提交时只带本人一方：不会被服务端 403。
    await c.js("document.querySelector('#btn-create-plan').click()")
    await asyncio.sleep(1.5)
    check("提交只带本人一方（不会触发代填 403）",
          c.creates and all(
              [p["owner_id"] for p in x.get("participants", [])] == [owner_id()]
              for x in c.creates),
          [x.get("participants") for x in c.creates])


    err = await c.js("window.__paErr||''")
    check("全程无页面脚本错误", not err, err)

    ok = sum(1 for _, p in checks if p)
    print("\n家庭活动界面路径：%d/%d 通过" % (ok, len(checks)))
    return 0 if ok == len(checks) else 1


def owner_id():
    return "A"


async def run():
    proc = subprocess.Popen(
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
         "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--window-size=430,1000",
         "--remote-debugging-port=%d" % PORT,
         "--user-data-dir=%s" % PROFILE, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tabs = None
        for _ in range(80):
            try:
                tabs = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % PORT, timeout=1).read())
                if any(x["type"] == "page" for x in tabs):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.15)
        if not tabs:
            raise RuntimeError("隔离 Chrome 未能启动")
        page = next(x for x in tabs if x["type"] == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"],
                                      max_size=20 * 1024 * 1024) as ws:
            global c
            c = C(ws)
            asyncio.create_task(c.pump())
            return await main()
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
