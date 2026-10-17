"""资料进入事项：保存 -> 明确关联 -> 落库回读 -> 进协商上下文。

不打浏览器，直接打真实隔离服务（独立端口 + 独立临时库）。
验四件事：
1. 未关联前，事项的 linked_notes 是空的（不自动挂）；
2. 点「用于这件事」之后真的落库，/api/task/{id} 和
   /api/task/{id}/linked-notes 都回读得到；
3. 关联进的是协商上下文（agents._a_context 的 linked_notes），
   不是只在界面上列一遍；
4. 身份边界：拿别人的资料或别人的事项来关联，服务端拒绝。
"""
import json, os, shutil, signal, socket, subprocess, sys, time, urllib.request, urllib.error

ROOT = str(__import__("pathlib").Path(__file__).resolve().parents[1])
os.chdir(ROOT)
sys.path.insert(0, ROOT)
PY_BIN = "/tmp/hack_venv/bin/python"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

PORT = free_port()
D = "/tmp/pa-link-%d" % PORT
shutil.rmtree(D, ignore_errors=True)
env = dict(os.environ); env["PA_DATA_DIR"] = D; env["PA_BIND"] = "127.0.0.1"
log = open("/tmp/pa-link-srv.log", "wb")
srv = subprocess.Popen([PY_BIN, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                        "--port", str(PORT), "--log-level", "warning"],
                       cwd=ROOT, env=env, stdout=log, stderr=log)
BASE = "http://127.0.0.1:%d" % PORT
checks = []

def check(name, ok, detail=None):
    checks.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  " + repr(detail)[:300]), flush=True)

def get(p, timeout=60):
    with urllib.request.urlopen(BASE + p, timeout=timeout) as r:
        return json.loads(r.read().decode())

def post(p, body, timeout=90):
    req = urllib.request.Request(BASE + p, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"__http_error": e.code, "detail": e.read().decode()[:300]}

try:
    for _ in range(80):
        try:
            get("/api/health", timeout=5); break
        except Exception:
            time.sleep(0.3)

    # 一件未完事项。全新库里本来没有事项——任务是靠对话创建的，
    # 所以先说一句话建一件，再对它关联。这也正是产品里的真实路径。
    tasks = get("/api/note/linkable-tasks")["tasks"]
    check("能列出可关联的本人未完事项", isinstance(tasks, list), len(tasks) if tasks else 0)
    if not tasks:
        said = post("/api/say", {"utterance": "给小周的女儿准备生日礼物，300元以内"})
        tid = said.get("task_id")
        check("先建一件事项", bool(tid), said)
        tasks = get("/api/note/linkable-tasks")["tasks"]
    task_id = tasks[0]["task_id"]

    # 1) 未关联前是空的
    before = get("/api/task/%s/linked-notes" % task_id)["notes"]
    check("未关联前这件事没有资料", before == [], len(before))

    # 2) 保存一条资料
    text = "学校通知：周五下午三点家长会，要求带上学期的素质报告册。"
    saved = post("/api/photo/save", {
        "subject_id": tasks[0]["subject_id"] or "p_xiaoyu",
        "image_b64": "", "media_type": "image/png", "captured_by": "我",
        "confirmed_fields": {},
        "read": {"happened_on": "", "items": [], "note": "纯文字录入"},
        "draft_text": text, "happened_on": "2026-10-14",
        "attachments": [{"b64": PNG, "media": "image/png"}],
        "input_kinds": ["text"], "voice_text": "",
    })
    event_id = saved.get("event_id")
    check("资料保存成功", bool(event_id), saved.get("__http_error") or saved.get("detail"))

    # 3) 还没关联时，事项里看不到它
    mid = get("/api/task/%s/linked-notes" % task_id)["notes"]
    check("保存后不自动挂到事项上", all(n["event_id"] != event_id for n in mid), len(mid))

    # 4) 点「用于这件事」
    linked = post("/api/note/link-task", {"event_id": event_id, "task_id": task_id})
    check("关联成功", linked.get("created") is True, linked)

    # 5) 落库回读：两个端点都要有
    after = get("/api/task/%s/linked-notes" % task_id)["notes"]
    hit = [n for n in after if n["event_id"] == event_id]
    check("关联后可从事项回读到这条资料", len(hit) == 1, [n["event_id"] for n in after])
    if hit:
        check("回读的资料正文是本人写的那版",
              (hit[0]["fields"] or {}).get("正文") == text, hit[0]["fields"])
        check("回读的资料带配图索引", len(hit[0]["attachments"]) == 1, hit[0]["attachments"])
        check("回读的资料带来源和日期",
              bool(hit[0]["provider"]) and hit[0]["happened_on"] == "2026-10-14",
              {"provider": hit[0]["provider"], "day": hit[0]["happened_on"]})

    card = get("/api/task/%s" % task_id)
    check("事项详情也带上这条资料",
          any(n["event_id"] == event_id for n in (card.get("linked_notes") or [])),
          len(card.get("linked_notes") or []))

    # 6) 重复关联不叠记录
    again = post("/api/note/link-task", {"event_id": event_id, "task_id": task_id})
    check("重复关联不新建记录", again.get("created") is False, again)
    after2 = get("/api/task/%s/linked-notes" % task_id)["notes"]
    check("重复关联后回读仍只有一条", len(after2) == len(after), len(after2))

    # 7) 关联真的进协商上下文
    sys.path.insert(0, ROOT)
    from app import db as dbm, agents
    db2 = dbm.DB(os.path.join(D, "hackathon.db"))
    extracted = json.loads(db2.row("SELECT extracted_json FROM task WHERE task_id=?", (task_id,))["extracted_json"] or "{}")
    sid = extracted.get("subject_id") or "p_xiaoyu"
    ctx = agents._a_context(db2, "A", sid, extracted, task_id=task_id)
    ln = ctx.get("linked_notes") or []
    check("协商上下文拿到这条资料", any(n["source"] == hit[0]["source_label"] for n in ln) and len(ln) >= 1, len(ln))
    check("上下文里这份资料带正文", any((n.get("fields") or {}).get("正文") == text for n in ln), "")
    ctx_nolink = agents._a_context(db2, "A", sid, extracted, task_id="")
    check("不传 task_id 时上下文不带资料", (ctx_nolink.get("linked_notes") or []) == [], "")

    # 8) 身份边界：资料/事项都不是本人的要拒绝
    other_ev = post("/api/photo/save", {
        "subject_id": "p_xiaoyu", "image_b64": "", "media_type": "image/png",
        "captured_by": "B", "confirmed_fields": {}, "read": {},
        "draft_text": "B 家记的", "happened_on": "", "attachments": [], "input_kinds": ["text"],
    })
    oid = other_ev.get("event_id")
    if oid:
        # 直接改库把这条挂到 B 名下，模拟别人的资料
        db2.execute("UPDATE life_event SET owner_id='B' WHERE event_id=?", (oid,))
        bad = post("/api/note/link-task", {"event_id": oid, "task_id": task_id})
        check("别人的资料不能挂到我的事项", bad.get("__http_error") in (400, 403, 409), bad)
    bad_task = post("/api/note/link-task", {"event_id": event_id, "task_id": "task_nope"})
    check("事项不存在时如实报错", bad_task.get("__http_error") in (400, 404, 409, 422), bad_task)

    # 9) 取消关联：资料不动
    un = post("/api/note/unlink-task", {"event_id": event_id, "task_id": task_id})
    check("可以取消关联", un.get("unlinked") is True, un)
    gone = get("/api/task/%s/linked-notes" % task_id)["notes"]
    check("取消后事项里不再出现", all(n["event_id"] != event_id for n in gone), len(gone))
    still = get("/api/life-events?subject_id=%s" % (tasks[0]["subject_id"] or "p_xiaoyu"))
    check("取消关联不删资料本身",
          any(text in json.dumps(e, ensure_ascii=False) for e in (still or {}).get("events", [])), "")

    ok = sum(1 for _, p, _ in checks if p)
    print("\n资料进入事项：%d/%d 通过" % (ok, len(checks)))
    for n, p, d in checks:
        if not p:
            print("  FAIL", n, repr(d)[:200])
    sys.exit(0 if ok == len(checks) else 1)
finally:
    srv.send_signal(signal.SIGTERM)
    try:
        srv.wait(timeout=8)
    except subprocess.TimeoutExpired:
        srv.kill(); srv.wait()
    log.close()
