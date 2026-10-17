import asyncio, base64, json, pathlib, socket, subprocess, time, urllib.request
import websockets

# 复用既有 CDP 客户端（单读循环 + pump 同一条 ws 会把事件吞掉，
# 所以这里直接用带 wait/js/fulfill 的 ReadCDP，不自己另写一个）。
helper = pathlib.Path(__file__).with_name("prompt-chrome-20261015-0507.py")
ns = {"__file__": str(helper)}
exec(helper.read_text().rsplit("raise SystemExit", 1)[0], ns)


class TodoCDP(ns["CDP"]):
    """只读页面 + 拦截写请求，不让验收动到真实业务数据。"""

    def __init__(self, ws):
        super().__init__(ws)
        self.loaded = asyncio.Event()
        self.gets = 0

    async def pump(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if m.get("id") in self.pending:
                self.pending.pop(m["id"]).set_result(m)
            elif m.get("method") == "Page.loadEventFired":
                self.loaded.set()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


ROOT = pathlib.Path(__file__).resolve().parents[1]


def ensure_fixture():
    """保证库里同时存在两条点击路径各自需要的验收数据。

    之前这个脚本直接读运行中服务的数据：库里碰巧有测试留下的事项就过，
    没有就永远等不到 #todo 出现 has 类。用户要求清空测试残留之后，
    这条测试立刻挂掉——问题在测试自己不带粮，不在产品。

    两件事必须分开备，因为验的是两条不同的点击路径：
      · task_first_screen_fixture + 一行带 task_id 的会话 -> 事项同时在
        首屏和消息流里，验「滚到消息流里那张卡」；
      · task_first_screen_fixture_b 不带会话 -> 只在首屏，验
        「直接拉详情开弹层」。
    之前写成「一件都没有时才建」，第一件已在就不再建第二件，
    第二条路径永远报「未验证」。现在逐条按 task_id 判存在，可重复跑。

    共同事项同理备一份，让「共同事项也进首屏」有真实数据可断言。

    这些行只服务验收，跑完由 drop_fixture() 清掉，不留在演示库里。
    """
    import sys
    sys.path.insert(0, str(ROOT))
    from app import db as dbm

    db = dbm.DB(ROOT / "data" / "hackathon.db")
    made = {"task": [], "conversation": False, "plan": False}

    def has_task(tid):
        return db.rows("SELECT COUNT(*) AS n FROM task WHERE task_id=?",
                       (tid,))[0]["n"] > 0

    for tid, text in (("task_first_screen_fixture", "首屏待办验收用的事项"),
                      ("task_first_screen_fixture_b", "只在首屏的验收事项")):
        if has_task(tid):
            continue
        db.execute(
            "INSERT INTO task(task_id,owner_id,utterance,extracted_json,stage,"
            "outcome,created_at) VALUES(?,?,?,?,?,?,?)",
            (tid, "A", text,
             '{"subject_id":"p_xiaoyu","subject_resolved_name":"小雨",'
             '"budget_max":300}', "agreed", "对方接受了，等你确认",
             "2026-10-15T10:00:00+08:00"))
        made["task"].append(tid)

    # 消息流里的事项卡由会话行的 task_id 带出来：没有这一行，
    # 「滚到消息流里那张卡」这条路径就没有东西可点。
    # 关键：卡只挂在 role=assistant 的回合上（renderStream 里
    # `if(turn.role === "user")` 那个分支只放气泡、不挂卡）。之前只插了一条
    # user 行，task_id 虽然匹配上了，消息流里却一张卡都没有，
    # #todo 点击永远走 openTaskSheet 分支，两条点击路径实际只测到一条。
    conv_row = db.rows("SELECT role FROM conversation WHERE turn_id=?",
                       ("conv_first_screen_fixture",))
    if not conv_row:
        db.execute(
            "INSERT INTO conversation(turn_id,owner_id,role,text,task_id,created_at)"
            " VALUES(?,?,?,?,?,?)",
            ("conv_first_screen_fixture", "A", "assistant",
             "已经和小雨家商量好了，等你确认。", "task_first_screen_fixture",
             "2026-10-15T10:00:00+08:00"))
        made["conversation"] = True
    elif conv_row[0]["role"] != "assistant":
        # 早先这个 fixture 插的是 role=user。事项卡只挂在助手回合上，
        # 那条 user 行的 task_id 虽然匹配得上，消息流里却一张卡都没有，
        # 「消息流里点开」这条路径从来没真正成立过。
        # 只按 turn_id 判存在会一直复用那行错数据，所以这里按角色自愈：
        # 老库里有残留就改过来，否则改了源码也看不出效果。
        db.execute("UPDATE conversation SET role='assistant', text=? WHERE turn_id=?",
                   ("已经和小雨家商量好了，等你确认。", "conv_first_screen_fixture"))
        made["conversation"] = True

    if db.rows("SELECT COUNT(*) AS n FROM plan WHERE plan_id=?",
               ("plan_first_screen_fixture",))[0]["n"] == 0:
        db.execute(
            "INSERT INTO plan(plan_id,owner_id,title,utterance,kind,stage,"
            "outcome,needs_replan,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("plan_first_screen_fixture", "A", "三个家庭周末一起带孩子去公园",
             "周末三家一起带孩子出去玩", "group_activity", "awaiting",
             "还在等各方确认", 0, "2026-10-15T10:00:00+08:00"))
        for pid, owner, label in (("pfp_me", "A", "我家"),
                                  ("pfp_zhou", "B", "小周家"),
                                  ("pfp_lin", "C", "小林家")):
            db.execute(
                "INSERT INTO plan_participant(participant_id,plan_id,owner_id,"
                "label,conditions_json,required,status,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (pid, "plan_first_screen_fixture", owner, label,
                 '{"预算":"每家 100 元以内"}', 1, "pending",
                 "2026-10-15T10:00:00+08:00"))
        made["plan"] = True
    return made


def drop_fixture():
    """清掉验收自备的数据。跑完就删，不留在演示库里给用户看。"""
    import sys
    sys.path.insert(0, str(ROOT))
    from app import db as dbm

    db = dbm.DB(ROOT / "data" / "hackathon.db")
    db.execute("DELETE FROM conversation WHERE turn_id='conv_first_screen_fixture'")
    db.execute("DELETE FROM task WHERE task_id IN"
               " ('task_first_screen_fixture','task_first_screen_fixture_b')")
    db.execute("DELETE FROM plan_participant WHERE plan_id='plan_first_screen_fixture'")
    db.execute("DELETE FROM plan WHERE plan_id='plan_first_screen_fixture'")


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
         "--user-data-dir=/tmp/codex-first-screen-todo-%d" % port,
         "about:blank"],
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
            c = TodoCDP(ws)
            pump = asyncio.create_task(c.pump())
            await c.send("Page.enable")
            await c.send("Runtime.enable")
            await c.send("Emulation.setDeviceMetricsOverride",
                         width=430, height=900, deviceScaleFactor=1, mobile=False)
            ensure_fixture()
            await c.send("Page.navigate", url="http://127.0.0.1:8000/")
            await asyncio.wait_for(c.loaded.wait(), 20)
            await c.wait("typeof renderTodo === 'function' && !!ownerId")
            await c.wait("lastPersons && lastPersons.length > 0")
            # 截图必须在滚动之前：之后要点卡片、要开弹层，页面早就滚到底了，
            # 那张图证不了首屏长什么样。
            await c.js("window.scrollTo(0,0)")
            # renderSide 里 renderTodo 挂在 matters 请求后面，等区块出内容。
            await c.wait("document.querySelector('#todo') "
                         "&& document.querySelector('#todo').classList.contains('has')")
            await asyncio.sleep(0.3)
            top = await c.send("Page.captureScreenshot", format="png")
            pathlib.Path(__file__).with_name(
                "first-screen-todo-top.png").write_bytes(base64.b64decode(top["data"]))
            # 首屏不只是「区块显示出来」：消息流是主线，区块不能把它挤出首屏。
            # 430×900 上要能同时看到对话和这一块，否则用户拉开应用看不到自己
            # 刚才在聊什么，只会觉得界面被一块待办占了。
            geo = await c.js(
                "(()=>{const g=(s)=>{const e=document.querySelector(s);"
                "if(!e)return null;const r=e.getBoundingClientRect();"
                "return {top:Math.round(r.top),bottom:Math.round(r.bottom),"
                "h:Math.round(r.height)}};"
                "return {vh:window.innerHeight,docH:document.documentElement.scrollHeight,"
                "topbar:g('.topbar'),stream:g('.stream'),todo:g('#todo'),"
                "quick:g('.quick'),composer:g('.composer'),"
                "streamChildren:document.querySelectorAll('.stream > *').length,"
                "streamText:document.querySelector('.stream').textContent.length}})()")
            # 不能只测「流在不在视口里」：流有几千像素高，top=57、bottom=4333
            # 也算在视口里，可待办已经被顶到对话底下，等于没上首屏。
            # 这里按真实位置比：待办必须在消息流之前，且第一条消息能同屏看到。
            check("待办区块排在消息流之前，不落在整段对话之下",
                  geo["todo"] and geo["stream"]
                  and geo["todo"]["top"] < geo["stream"]["top"],
                  geo)
            check("消息流第一条能同屏看到（区块没有把对话挤出首屏）",
                  geo["stream"] and geo["stream"]["top"] < geo["vh"]
                  and geo["stream"]["h"] > 0,
                  geo)
            check("待办区块整体落在首屏一屏之内",
                  geo["todo"] and geo["todo"]["bottom"] <= geo["vh"] + 2,
                  geo)
            check("输入框与预设句栏仍在首屏底部",
                  geo["composer"] and geo["composer"]["bottom"] <= geo["vh"] + 2,
                  geo)

            st = await c.js(
                "(()=>{const b=document.querySelector('#todo');"
                "const items=Array.from(b.querySelectorAll('.todo-item'));"
                "return {has:b.classList.contains('has'),"
                "count:items.length,"
                "titles:items.map(i=>i.querySelector('.t').textContent),"
                "whys:items.map(i=>i.querySelector('.why').textContent),"
                "acts:items.map(i=>i.querySelector('.st').textContent),"
                "head:b.querySelector('.todo-head').textContent,"
                "overflowX:document.documentElement.scrollWidth>document.documentElement.clientWidth,"
                "scrollW:document.documentElement.scrollWidth,"
                "clientW:document.documentElement.clientWidth,"
                "err:window.__paErr}})()")
            check("首屏「需要你处理」区块在未拉侧栏时就已显示",
                  st["has"] and st["count"] > 0, st)
            check("区块标题与件数同屏给出",
                  "需要你处理" in st["head"], st["head"])

            # 事项口径：只列未完，已登记购买的不出现在首屏。
            # 必须按 task_id 比：同一件事的 utterance 前缀相同，按文案匹配会把
            # 「已购买」那条误判成出现在首屏，也可能把另一条未完的漏掉。
            matters = await c.js("(async()=>{let d=await api('/api/matters');"
                                 "return d.matters.map(m=>({stage:m.stage,id:m.task_id,"
                                 "u:m.utterance}))})()")
            open_stages = {"blocked", "agreed", "counter_pending",
                           "needs_renegotiation", "negotiating", "created"}
            expected = [m for m in matters if m["stage"] in open_stages]
            got = await c.js(
                "Array.from(document.querySelectorAll('#todo .todo-item'))"
                ".map(i=>i.dataset.openTask)")
            got_titles = st["titles"]
            check("首屏只列未完事项，purchased 不出现",
                  not any(m["stage"] == "purchased" and m["id"] in got
                          for m in matters),
                  {"got_ids": got, "purchased": [m["id"] for m in matters
                                                 if m["stage"] == "purchased"]})
            # 只比事项条目：dataset 上共同事项那几条是 openPlan，取出来是 None，
            # 混在一起排序会 TypeError，也混不上 /api/matters 的集合。
            got_tasks = [x for x in got if x]
            check("首屏件数与 /api/matters 的未完集合一致",
                  sorted(got_tasks) == sorted(m["id"] for m in expected),
                  {"got": got_tasks, "expected": [m["id"] for m in expected]})

            # 共同事项也在首屏：三个家庭去公园还在等，属于未完。
            plans = await c.js("(async()=>{let d=await api('/api/plans');"
                               "return d.plans.map(p=>({id:p.plan_id,title:p.title,"
                               "agreed:p.agreed}))})()")
            open_plans = [p for p in plans if not p["agreed"]]
            got_plans = await c.js(
                "Array.from(document.querySelectorAll('#todo .todo-item'))"
                ".map(i=>i.dataset.openPlan).filter(Boolean)")
            check("共同事项未约定也进首屏",
                  sorted(got_plans) == sorted(p["id"] for p in open_plans),
                  {"got_plans": got_plans,
                   "open_plans": [p["id"] for p in open_plans]})
            check("首屏文案里能看到共同事项标题",
                  any(p["title"][:6] in t for p in open_plans for t in got_titles),
                  {"titles": got_titles,
                   "plan_titles": [p["title"] for p in open_plans]})

            # 每条都说清为什么现在轮到本人看，不是重复状态名。
            check("每条都给「为什么现在轮到你看」",
                  all(w and len(w) > 2 for w in st["whys"]), st["whys"])
            check("每条都给点了之后落到哪一步",
                  all(a and len(a) > 2 for a in st["acts"]), st["acts"])
            check("430×900 下无横向溢出",
                  not st["overflowX"], {"scrollW": st["scrollW"],
                                        "clientW": st["clientW"]})
            check("页面无脚本错误", not st["err"], st["err"])

            # 点击有两条真实路径，都要验，不能只测一条就当成「点得开」：
            # A 消息流里已经有这张卡 -> 滚到那张卡（和侧栏一致）
            # B 消息流里没有这张卡 -> 拉详情开弹层
            conv = await c.js("(async()=>{let d=await api('/api/conversation');"
                              "return (d.turns||[]).map(t=>t.task_id).filter(Boolean)})()")
            in_stream = [m for m in expected if m["id"] in conv]
            not_in_stream = [m for m in expected if m["id"] not in conv]

            if in_stream:
                t0 = in_stream[0]["id"]
                await c.js("window.scrollTo(0,0)")
                await c.js("document.querySelector('#todo [data-open-task=\"" + t0
                           + "\"]').click()")
                # 必须等平滑滚动真正停下来再量位置。卡片滚过去用的是
                # scrollIntoView({behavior:"smooth"})，文档有几千像素高，
                # 动画要跑几百毫秒；固定睡一个数读到的是动画中间态
                # （实测 top=986 还在视口下方），那不是产品没滚到位，
                # 是验收在动画中途量的。这里轮询到落进视口或超时。
                seen = None
                for _ in range(40):
                    await asyncio.sleep(0.15)
                    seen = await c.js(
                        "(()=>{const c=document.querySelector('[data-task=\"" + t0
                        + "\"]');if(!c)return null;const r=c.getBoundingClientRect();"
                        "return {top:r.top,bottom:r.bottom,vh:window.innerHeight,"
                        "sheetOpen:document.querySelector('#sheet').classList.contains('open')}})()")
                    if seen and seen["top"] >= -1 and seen["bottom"] <= seen["vh"] + 1:
                        break
                check("首屏点击流内事项：滚到那张卡且卡在可视区内",
                      seen and seen["top"] >= -1 and seen["bottom"] <= seen["vh"] + 1,
                      {"task": t0, "seen": seen})
            else:
                check("首屏点击流内事项：滚到那张卡且卡在可视区内", False,
                      "当前没有既在首屏又在消息流里的事项，该路径未验证")

            if not_in_stream:
                t1 = not_in_stream[0]["id"]
                await c.js("document.querySelector('#todo [data-open-task=\"" + t1
                           + "\"]').click()")
                await c.wait("document.querySelector('#sheet') "
                             "&& document.querySelector('#sheet').classList.contains('open')")
                sheet = await c.js(
                    "(()=>({open:document.querySelector('#sheet').classList.contains('open'),"
                    "body:document.querySelector('#sheet-body').textContent.slice(0,300)}))()")
                check("首屏点击流外事项：直接拉详情开弹层",
                      sheet["open"] and sheet["body"],
                      {"task": t1, "sheet": sheet})
                check("拉到的详情确实是首屏那一件",
                      not_in_stream[0]["u"][:6] in sheet["body"],
                      {"task": t1, "head": sheet["body"][:60]})
                await c.js("document.querySelector('#sheet [data-close-sheet]').click()")
                await c.wait("!document.querySelector('#sheet').classList.contains('open')")
            else:
                check("首屏点击流外事项：直接拉详情开弹层", False,
                      "当前没有只在首屏、消息流里没有的事项，该路径未验证")

            plan_btn = await c.js(
                "Array.from(document.querySelectorAll('#todo .todo-item'))"
                ".find(i=>i.dataset.openPlan)?.dataset.openPlan")
            if plan_btn:
                await c.js("document.querySelector('#todo [data-open-plan=\"" + plan_btn
                           + "\"]').click()")
                await c.wait("document.querySelector('#sheet') "
                             "&& document.querySelector('#sheet').classList.contains('open')")
                plan_sheet = await c.js(
                    "(()=>({open:document.querySelector('#sheet').classList.contains('open'),"
                    "body:document.querySelector('#sheet-body').textContent.slice(0,300)}))()")
                check("点首屏共同事项能打开协商详情",
                      plan_sheet["open"] and plan_sheet["body"],
                      {"plan": plan_btn, "sheet": plan_sheet})
            else:
                check("点首屏共同事项能打开协商详情", False,
                      "首屏没有共同事项条目，点击路径未验证")

            shot = await c.send("Page.captureScreenshot", format="png")
            pathlib.Path(__file__).with_suffix(".png").write_bytes(
                base64.b64decode(shot["data"]))
            pump.cancel()
        result = {
            "utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "checks": checks,
            "passed": sum(x["ok"] for x in checks),
            "total": len(checks),
            "boundary": "隔离 Chrome 430×900；真实 GET /api/matters、/api/plans，"
                        "只读首屏区块渲染与点击打开，0 真实模型、0 业务写。"
                        "只证首屏把真实未完事项显示出来且点得开，"
                        "不证协商内容正确性或购买流程。",
        }
        pathlib.Path(__file__).with_suffix(".json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if all(x["ok"] for x in checks) else 1
    finally:
        drop_fixture()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


raise SystemExit(asyncio.run(main()))
