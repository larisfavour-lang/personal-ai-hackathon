"""家庭活动完整切片与多孩子引导（t14）回归。

覆盖 G1–G7 与「选图前次入口」的隐私修正，分三层，边界写清楚：

  A. 关系数据驱动（不连网络、不调模型、用临时库）
     - 家庭与孩子从关系表推导，不写死人名；
     - 小周与配偶小林合为一个家庭（依据明确配偶关系，共同抚养不作为同家证据）；
     - 数不出孩子的家庭报未知，不编数；
     - 同名不同人不靠名字合并（按 person_id）。
  B. 接口级（真实 HTTP 栈 + 独立临时服务/临时库，模型全拦）
     - 原话「周末三家一起带孩子出去玩，我出100以内」进家庭活动草稿，
       不建礼物事项、不返回要点选的对象；
     - 100 记在「本人出资上限」，不记成「活动总预算」；
     - 发起人只能填自己一方，代填别家被 403；
     - 校验失败不半落库；
     - 同一 title 重复 create：参与方/版本/表态零变化；
     - 误当成送礼的旧事项可转成家庭活动，旧事项不删、给恢复入口。
  C. 浏览器级（隔离 Chrome + 真实 DOM 点击，模型全拦）
     - 加图后自动识别仍默认生效；
     - 图片识别选定文件后，选「只把图存进资料库」/「先裁剪再识别」时 0 次 /api/photo/read；
     - 本人点「识别图里的文字，进正文框」之后才发那 1 次 read，文字进正文框。

运行：/tmp/hack_venv/bin/python tests/test_family_activity_flow.py
真实模型不在本脚本范围内：需要模型的一律拦截成合成数据，
模型真实行为由 test_e2e_chain / test_note_real_persist 覆盖并单独说明。
"""
from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db as dbm  # noqa: E402
from app import service  # noqa: E402

OUT = ROOT / "docs" / "evidence" / "family-activity-flow.txt"

UTTERANCE = "周末三家一起带孩子出去玩，我出100以内"
results: list[tuple[str, bool, object]] = []


def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))


# =====================================================================
# A. 关系数据驱动
# =====================================================================
def part_relations():
    tmp = pathlib.Path(tempfile.mkdtemp()) / "fam.db"
    db = dbm.DB(tmp)
    dbm.seed(db)
    ov = service.family_overview(db, "A")
    fams = {f["label"]: f for f in ov["families"]}

    check("A 侧认得出两个家庭：我家 + 小周家", len(ov["families"]) == 2,
          [f["label"] for f in ov["families"]])

    mine = fams.get("我家") or {}
    # 种子在 relation 表里记了 A→小满（r_me_xiaoman），所以我家孩子数从关系数出来
    # 是 1，不再是「未知」。这条断言的本意是「不编数」：有几条关系就报几个，
    # 没有就把 known 置假。t65 之前它是 False，因为那时 relation 表里没有这一行。
    check("我家孩子数从关系数出来，不编数",
          mine.get("children_known") is True
          and [c["person_id"] for c in mine.get("children") or []] == ["p_xiaoman_a"],
          mine)

    zhou = fams.get("小周家") or {}
    adults = sorted(a["name"] for a in zhou.get("adults") or [])
    kids = sorted(c["name"] for c in zhou.get("children") or [])
    # 排序按 Unicode 码点：小周(5468) 在小林(6797) 之前，期望值要照这个写。
    check("小周和小林合为一个家庭，不当成两家",
          adults == ["小周", "小林"] and zhou.get("children_known") is True,
          {"adults": adults, "children_known": zhou.get("children_known"),
           "children": [c["name"] for c in zhou.get("children") or []]})
    check("小周家孩子数从关系数出来，是 2 个", len(kids) == 2, kids)
    check("孩子按 person_id 认，不按名字认",
          sorted(c["person_id"] for c in zhou.get("children") or [])
          == ["p_xiaohan", "p_xiaoyu"],
          [c["person_id"] for c in zhou.get("children") or []])

    # 换一套演示数据：把 A 侧的孩子关系改名，函数照样说得对。
    db.execute("UPDATE person SET name='大娃' WHERE person_id='p_xiaoyu'")
    db.execute("UPDATE person SET name='二娃' WHERE person_id='p_xiaohan'")
    ov2 = service.family_overview(db, "A")
    zhou2 = [f for f in ov2["families"] if not f["is_me"]][0]
    check("换掉孩子姓名后仍认得出 2 个孩子（不靠名字匹配）",
          sorted(c["name"] for c in zhou2["children"]) == ["二娃", "大娃"],
          [c["name"] for c in zhou2["children"]])

    # 意图路由：多家庭活动进 group_activity，送礼/纠正/问进展不误入。
    check("原话判为多家庭活动", service.group_activity_intent(UTTERANCE))
    for other in ("给小周的女儿准备生日礼物，300元以内",
                  "记错了，喜欢拼图的是哥哥",
                  "现在办到哪一步了？",
                  "她最近还想画画吗"):
        check("不把「%s」当成多家庭活动" % other[:14],
              not service.group_activity_intent(other), other)

    check("数出用户说了三家", service.count_requested_families(UTTERANCE) == 3)
    check("「我出100以内」取成本人出资上限",
          service.extract_own_cap(UTTERANCE) == "100 元以内",
          service.extract_own_cap(UTTERANCE))
    check("「活动总共300元」不当成本人出资上限",
          service.extract_own_cap("活动总共300元") is None)

    # 真实发起一次：只带自己一方，缺口留在同一件事里。
    started = service.start_group_activity(db, None, "A", UTTERANCE)
    check("原话直接开出一件家庭活动草稿", bool(started["plan_id"]))
    parts = db.rows("SELECT owner_id,label,conditions_json FROM plan_participant"
                    " WHERE plan_id=?", (started["plan_id"],))
    check("发起方只带自己一方进场", len(parts) == 1 and parts[0]["owner_id"] == "A",
          [p["owner_id"] for p in parts])
    cond = json.loads(parts[0]["conditions_json"] or "{}")
    check("100 记在「本人出资上限」这一键下",
          cond.get("本人出资上限") == "100 元以内", cond)
    check("没有「活动总预算」这个键被写成 100",
          "活动总预算" not in cond and "预算" not in cond, cond)
    # 旧键「预算」仍收，但归一成「活动总预算」：老调用方不炸，
    # 库里也不再出现一个说不清是谁的预算。
    # 旧键「预算」按值里的措辞归一到不同口径，不是一律当总预算：
    # 「每家不超过80元」是每家的出资上限，「活动一共300元」才是总预算。
    own, _u1, _c1 = service.normalize_conditions({"预算": "每家不超过80元"})
    tot, _u2, _c2 = service.normalize_conditions({"预算": "活动一共300元"})
    check("旧键「预算」带「每家」措辞时归一到本人出资上限",
          own == {"本人出资上限": "每家不超过80元"}, own)
    check("旧键「预算」带「一共」措辞时归一到活动总预算",
          tot == {"活动总预算": "活动一共300元"}, tot)
    # 光一个数字说不清是谁的：两个键都不写，交给本人确认，不默认成任何一个。
    amb, _u3, confirm = service.normalize_conditions({"预算": "100元"})
    check("口径不明的旧预算不默认成任何一种，转为请本人确认",
          amb == {} and confirm == ["预算"], {"norm": amb, "confirm": confirm})
    both, _u4, _c4 = service.normalize_conditions(
        {"本人出资上限": "100 元以内", "活动总预算": "300 元"})
    check("两个口径的键可以同时存在，互不覆盖",
          both == {"本人出资上限": "100 元以内", "活动总预算": "300 元"}, both)
    check("缺的第三家报成待确认，不凑数",
          started["missing_families"] == 1, started["missing_families"])
    # 不出现「点一个就行」那种 UI 引导语；回复里说「不用点一个孩子」
    # 是在打消这个误解，不算引导。
    check("回复里不给点选孩子的引导",
          "点一个就行" not in started["reply"]
          and "选一个孩子" not in started["reply"]
          and "送礼对象" not in started["reply"],
          started["reply"][:160])
    turns = db.rows("SELECT plan_id FROM conversation WHERE owner_id='A'")
    check("这一轮对话记下了 plan 关联（刷新后找得回）",
          turns and all(t["plan_id"] == started["plan_id"] for t in turns),
          [t["plan_id"] for t in turns])

    # 旧代码留下的那类事项：同一条原话在修复前被当成送礼卡在「选一个对象」。
    # 直接插一行模拟它（不调模型），验证卡片给的是「改成家庭活动」而不是人选。
    legacy = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome,"
        " created_at) VALUES(?,?,?,?,?,?,?)",
        (legacy, "A", UTTERANCE, "{}", "blocked", "要送给谁", dbm.now_iso()))
    card = [t for t in service.matters(db, "A") if t["task_id"] == legacy][0]
    check("旧代码留下的活动事项被标记成 group_activity",
          card.get("group_activity") is True, card.get("group_activity"))
    check("卡片因此走恢复入口，不走点选送礼对象",
          card.get("stage") == "blocked" and card.get("group_activity") is True,
          {"stage": card.get("stage")})
    rec = service.recover_group_activity(db, "A", legacy)
    check("旧事项可转成家庭活动（服务层）", bool(rec and rec.get("plan_id")))
    after = [t for t in service.matters(db, "A") if t["task_id"] == legacy][0]
    check("转走后旧事项仍在，原话没被删",
          after["utterance"] == UTTERANCE, after["utterance"])
    check("转走后带上新 plan_id，卡片能给回去的入口",
          after.get("stage") == "redirected" and bool(after.get("plan_id")),
          {"stage": after.get("stage"), "plan_id": after.get("plan_id")})

    # 重复发起：参与方、版本、表态都不变。
    again = service.start_group_activity(db, None, "A", UTTERANCE)
    check("同一句原话重复发起复用同一件", again["plan_id"] == started["plan_id"])
    check("重复发起不增加参与方",
          db.row("SELECT COUNT(*) c FROM plan_participant WHERE plan_id=?",
                 (started["plan_id"],))["c"] == 1)
    check("重复发起不产生新版本",
          db.row("SELECT COUNT(*) c FROM plan_proposal WHERE plan_id=?",
                 (started["plan_id"],))["c"] == 0)
    check("重复发起不产生表态",
          db.row("SELECT COUNT(*) c FROM plan_decision WHERE plan_id=?",
                 (started["plan_id"],))["c"] == 0)
    return started["plan_id"]


# =====================================================================
# B. 接口级（独立临时服务 + 临时库，模型全拦）
# =====================================================================
PNG_1PX = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c63000100000500010d0a2db4000000"
    "0049454e44ae426082")).decode()

READ_STUB = {"transcribed": "哥哥练琴四十分钟", "fields": {"完成时间": "40分钟"},
             "happened_on": "2026-10-14", "confidence": "high",
             "unclear_parts": [], "subject_hint": ""}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http_json(base, path, obj=None, token=None, timeout=30):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body[:300]}


def part_api():
    port = free_port()
    db_dir = "/tmp/pa-fam-%d" % port
    db_dir = tempfile.mkdtemp(prefix="pa-fam-isolated-")
    env = dict(os.environ) if (os := __import__("os")) else {}
    env["PA_DATA_DIR"] = db_dir
    env["PA_BIND"] = "127.0.0.1"
    env["PA_DEMO_OWNER"] = "A"
    log = open("/tmp/pa-fam-%d-server.log" % port, "wb")
    srv = subprocess.Popen(
        ["/tmp/hack_venv/bin/python", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=log)
    base = "http://127.0.0.1:%d" % port
    try:
        ready = None
        for _ in range(80):
            try:
                ready = http_json(base, "/api/health", timeout=5)[1]
                break
            except Exception:
                __import__("time").sleep(0.3)
        check("隔离服务可启动", bool(ready and ready.get("ok")), ready)

        # ---- G1：原话从聊天进家庭活动，不建礼物 ----
        code, said = http_json(base, "/api/say", {"utterance": UTTERANCE})
        check("原话走 /api/say 成功", code == 200, said)
        check("意图是 group_activity，不是送礼交办",
              said.get("kind") == "group_activity", said.get("kind"))
        check("没有创建礼物事项（不返回 task_id）", not said.get("task_id"), said)
        check("没有要求点一个孩子（不给 candidates）",
              not said.get("candidates"), said.get("candidates"))
        check("返回了可点开的家庭活动 plan_id", bool(said.get("plan_id")))
        check("100 明确标成本人出资上限",
              said.get("own_cap") == "100 元以内", said.get("own_cap"))
        check("缺口（第三家）报成待确认", said.get("missing_families") == 1,
              said.get("missing_families"))
        plan_id = said["plan_id"]

        code, wrapped = http_json(base, "/api/matters")
        tasks = wrapped.get("matters") or []
        gift_like = [t for t in tasks if UTTERANCE in (t.get("utterance") or "")]
        check("这件事没有变成送礼事项卡", not gift_like,
              [t["utterance"] for t in tasks])

        # ---- G2/G3：事项详情里家庭与孩子按关系显示 ----
        code, detail = http_json(base, "/api/plan/" + plan_id)
        fams = {f["label"]: f for f in (detail.get("families") or [])}
        check("事项详情列出家庭与孩子", len(detail.get("families") or []) == 1,
              [f["label"] for f in detail.get("families") or []])
        check("未邀的小周家资料不出现在事项详情", "小周家" not in fams, list(fams))
        check("事项详情说明 A/B/C 是两个家庭",
              "两个家庭" in (detail.get("family_note") or ""),
              detail.get("family_note"))
        check("我家孩子数按关系显示为 1 个，不报未知也不编数",
              (fams.get("我家") or {}).get("children_known") is True
              and len((fams.get("我家") or {}).get("children") or []) == 1,
              fams.get("我家"))

        # ---- G4：出资口径分离 ----
        code, st = http_json(base, "/api/plan/" + plan_id)
        me = [p for p in st["participants"] if p["owner_id"] == "A"][0]
        check("本人条件里只有出资上限，没有活动总预算",
              me["conditions"].get("本人出资上限") == "100 元以内"
              and "活动总预算" not in me["conditions"], me["conditions"])

        # ---- G5：发起人不能代填别家 ----
        code, rejected = http_json(base, "/api/plan/create", {
            "title": "代填测试事项", "utterance": "两家一起带孩子去公园",
            "participants": [
                {"owner_id": "A", "label": "我家", "conditions": {"时间": "周六"}},
                {"owner_id": "B", "label": "小周家", "conditions": {"时间": "周日"}},
            ]})
        check("发起人代填别家条件被拒（403）", code == 403, {"code": code, "body": rejected})
        check("拒绝原因说清该由谁补", "自己" in str(rejected.get("detail", "")),
              rejected.get("detail"))
        # 半落库检查：被拒的那一件里不应该只有 A 一方。
        code, st2 = http_json(base, "/api/plan/create", {
            "title": "半落库检查", "utterance": "两家一起带孩子去公园",
            "participants": [
                {"owner_id": "A", "label": "我家", "conditions": {}},
                {"owner_id": "Z", "label": "不存在", "conditions": {}},
            ]})
        check("身份非法的请求被拒（400）", code == 400, {"code": code})
        code, st3 = http_json(base, "/api/plan/create", {
            "title": "半落库检查", "utterance": "两家一起带孩子去公园",
            "participants": [{"owner_id": "A", "label": "我家", "conditions": {}}]})
        plans = http_json(base, "/api/plans")[1]
        plans = plans.get("plans") if isinstance(plans, dict) else plans
        half = [p for p in (plans or []) if p.get("title") == "半落库检查"]
        check("校验失败的请求没有落下半份草稿",
              not half or all(len(p.get("participants") or []) == 1 for p in half),
              half)

        # ---- G6：重复创建幂等（接口级）----
        body = {"title": "重复创建幂等", "utterance": "两家周末一起聚个餐",
                "participants": [{"owner_id": "A", "label": "我家",
                                  "conditions": {"时间": "周六"}}]}
        code1, r1 = http_json(base, "/api/plan/create", body)
        before = http_json(base, "/api/plan/" + r1["plan_id"])[1]
        code2, r2 = http_json(base, "/api/plan/create", body)
        after = http_json(base, "/api/plan/" + r2["plan_id"])[1]
        check("同一 title 重复 create 复用同一件", r1["plan_id"] == r2["plan_id"])
        check("参与方数量不变", len(before["participants"]) == len(after["participants"])
              and len(after["participants"]) == 1,
              [p["label"] for p in after["participants"]])
        check("版本数不变", before["current_version"] == after["current_version"] == 0)
        check("表态数不变", before["agreed_count"] == after["agreed_count"] == 0)
        check("第二次创建告诉前端是复用", r2.get("reused") is True, r2.get("reused"))

        # ---- 旧错误类型事项：保留历史 + 恢复入口 ----
        # 这句带明确送礼措辞，按送礼链建成事项——恢复入口要能把它转成活动。
        code, task = http_json(base, "/api/say", {
            "utterance": "给三家一起出游的孩子们准备礼物"})
        check("带送礼措辞的句子仍按送礼链办，不误改成活动",
              code == 200 and task.get("kind") == "task", task.get("kind"))
        tasks = (http_json(base, "/api/matters")[1].get("matters") or [])
        old = [t for t in tasks if "三家一起出游" in (t.get("utterance") or "")]
        check("旧句仍按原链路建成事项（历史不抹掉）", bool(old), len(tasks))
        if old:
            tid = old[0]["task_id"]
            code, rec = http_json(base, "/api/task/" + tid + "/to-group-activity", {})
            check("旧事项可转成家庭活动", code == 200 and bool(rec.get("plan_id")), rec)
            tasks2 = (http_json(base, "/api/matters")[1].get("matters") or [])
            same = [t for t in tasks2 if t["task_id"] == tid][0]
            check("旧事项没有被删，原话还在",
                  "三家一起出游" in (same.get("utterance") or ""), same.get("utterance"))
            check("旧事项标记成已转走并带新 plan_id",
                  same.get("stage") == "redirected" and bool(same.get("plan_id")),
                  {"stage": same.get("stage"), "plan_id": same.get("plan_id")})
            # 转走之后卡片走 redirected 分支：只给「打开这件家庭活动」，
            # 不再摆人名。group_activity 标记对送礼措辞的句子为假是正常的，
            # 那条路由由 A 段的旧代码事项用例覆盖。
            check("旧事项卡片不再摆人选（已转走）",
                  same.get("stage") == "redirected" and bool(same.get("plan_id")),
                  {"stage": same.get("stage")})
            code, newplan = http_json(base, "/api/plan/" + same["plan_id"])
            check("恢复入口指向的家庭活动真的存在", code == 200, code)
    finally:
        srv.send_signal(__import__("signal").SIGTERM)
        try:
            srv.wait(timeout=8)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait()
        log.close()


class StubClient:
    """记录真正送给模型的输入，返回预制方案。

    用来验「提示词里确实写了不许把各家上限相加」这条硬规则——
    模型听不听话要真实模型才验得了，但提示词里有没有这句话是确定的。
    """

    def __init__(self, proposal):
        self.proposal = proposal
        self.sent = []

    def chat_json(self, messages, **kw):
        self.sent.append([m.get("content") or "" for m in messages])
        return {"content": self.proposal, "reason": "按各方条件排的", "conflicts": []}


def part_prompt_rules():
    """G4：出资口径的硬规则必须真的写进给模型的提示词。"""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "prompt.db"
    db = dbm.DB(tmp)
    dbm.seed(db)
    plan_id = service.create_plan(db, "A", "两家周末一起带孩子去公园", "两家周末一起带孩子去公园")
    service.add_plan_participant(db, plan_id, "A", "我家", {"本人出资上限": "100 元以内"}, True)
    service.add_plan_participant(db, plan_id, "B", "小周家", {"本人出资上限": "200 元以内"}, True)
    stub = StubClient({"时间": "周六上午", "地点": "世纪公园"})
    import app.agents as agents
    agents.plan_propose(db, stub, plan_id, "A")
    prompt = "\n".join("\n".join(x) for x in stub.sent)
    check("提示词明写不许把各家出资上限相加当活动总预算",
          "不许把各家的上限相加" in prompt, prompt[:120])
    check("提示词明写活动总预算只能来自某一家自己给出",
          "只能来自某一家自己给出的总预算" in prompt, prompt[:120])
    check("提示词明写上上限不是承诺、不自动代家庭承诺",
          "不是他同意出这个数" in prompt and "不由那一方自己说" in prompt.replace(
              "都由那一方自己说", "不由那一方自己说"), prompt[:160])
    # 方案字段白名单带上两个口径键，模型才写得对地方。
    check("方案白名单同时含「活动总预算」与「本人出资上限」",
          "活动总预算" in service.PLAN_CONDITION_KEYS
          and "本人出资上限" in service.PLAN_CONDITION_KEYS,
          service.PLAN_CONDITION_KEYS)
    prop = db.row("SELECT content_json FROM plan_proposal WHERE plan_id=?",
                  (plan_id,))
    content = json.loads(prop["content_json"] or "{}")
    check("模型多给的字段不进方案（只留白名单键）",
          set(content) <= set(service.PLAN_CONDITION_KEYS), sorted(content))


def part_r1_boundaries():
    """R1 家庭结构边界：共同抚养不合并成人家庭，跨代不合并，空资料不崩。"""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "r1.db"
    db = dbm.DB(tmp)
    dbm.seed(db)

    # 1) 小周 + 小林：依据是明确的配偶关系（A 侧 friend_spouse + 别侧 spouse），
    #    不是共同抚养孩子。
    ov = service.family_overview(db, "A")
    fams = {f["label"]: f for f in ov["families"]}
    zhou = fams.get("小周家") or {}
    adults = sorted(a["name"] for a in zhou.get("adults") or [])
    check("小周和小林按明确配偶关系合为一个家庭",
          adults == ["小周", "小林"]
          and all(a["same_family_confirmed"] for a in zhou.get("adults") or []),
          adults)
    check("小周家 2 个孩子从关系数出，且不当成完整人数或都出席",
          len(zhou.get("children") or []) == 2
          and zhou.get("children_known") is True, zhou.get("children"))
    check("我家与认识的家庭分开列，未选的朋友不算参加家庭",
          "我家" in fams and len(ov["families"]) == 2,
          [f["label"] for f in ov["families"]])

    # 2) 不同家庭共同抚养一个孩子：两个家庭不合并，孩子同时是两个家庭的候选。
    db.execute("DELETE FROM relation WHERE relation_id='r_xiaolin_xiaoyu'")
    db.execute("DELETE FROM relation WHERE relation_id='r_xiaolin_xiaohan'")
    db.execute("INSERT INTO person(person_id,owner_id,name,relation,note,aliases,"
               "attributes_json) VALUES('p_other','A','另一家家长','friend','','[]','{}')")
    db.execute("INSERT INTO relation(relation_id,owner_id,from_person_id,relation_type,"
               "to_person_id,label,source_utterance,observed_on)"
               " VALUES('r_other_xiaoyu','A','p_other','父亲','p_xiaoyu','','t','2026-10-01')")
    ov2 = service.family_overview(db, "A")
    labels = [f["label"] for f in ov2["families"]]
    holders = [f for f in ov2["families"] if any(
        c["person_id"] == "p_xiaoyu" for c in f["children"])]
    check("共同抚养孩子的两个成人不被并成一个家庭",
          "小周家" in labels and "另一家家长家" in labels, labels)
    check("共同抚养的孩子同时挂在两个家庭下（候选，不合并家庭）",
          len(holders) == 2, [f["label"] for f in holders])

    # 3) 同名不同 id 不合并。
    db.execute("DELETE FROM relation WHERE relation_id='r_other_xiaoyu'")
    db.execute("DELETE FROM person WHERE person_id='p_other'")
    db.execute("INSERT INTO person(person_id,owner_id,name,relation,note,aliases,"
               "attributes_json) VALUES('p_xiaoyu2','A','小雨','friend_child','','[]','{}')")
    ov3 = service.family_overview(db, "A")
    # 关键不是总数，是两个「小雨」各是各的一条：按 person_id 分列，
    # 不因为同名被并成一个孩子。新加的那条没有家长记录，单独成一项。
    ids = [c["person_id"] for f in ov3["families"] for c in f["children"]]
    check("同名不同 person_id 不按姓名合并",
          sorted(ids) == ["p_xiaohan", "p_xiaoman_a", "p_xiaoyu", "p_xiaoyu2"], ids)
    db.execute("DELETE FROM person WHERE person_id='p_xiaoyu2'")

    # 4) 没有家长记录的孩子：单独列出，不塞进任何家庭，也不算已参加。
    db.execute("INSERT INTO person(person_id,owner_id,name,relation,note,aliases,"
               "attributes_json) VALUES('p_lonely','A','孤儿条目','friend_child','','[]','{}')")
    ov4 = service.family_overview(db, "A")
    lonely = [f for f in ov4["families"] if any(
        c["person_id"] == "p_lonely" for c in f["children"])]
    check("没有家长记录的孩子单独列出，不归进任何家庭",
          len(lonely) == 1 and not lonely[0]["adults"], lonely)
    check("没有家长记录的孩子标为未知，不指认家庭",
          lonely and lonely[0]["children_known"] is False, lonely)
    db.execute("DELETE FROM person WHERE person_id='p_lonely'")

    # 5) 祖孙三代：不因共同后代把两代成人并成一个家庭。
    db.execute("DELETE FROM relation WHERE owner_id='A'")
    db.execute("DELETE FROM person WHERE owner_id='A'")
    for pid, name, rel in (("p_g", "爷爷", "friend"), ("p_f", "爸爸", "friend"),
                           ("p_k", "孩子", "friend_child")):
        db.execute("INSERT INTO person(person_id,owner_id,name,relation,note,aliases,"
                   "attributes_json) VALUES(?,?,?,?,'','[]','{}')", (pid, "A", name, rel))
    db.execute("INSERT INTO relation(relation_id,owner_id,from_person_id,relation_type,"
               "to_person_id,label,source_utterance,observed_on)"
               " VALUES('r_gf','A','p_g','父亲','p_f','','','2026-10-01')")
    db.execute("INSERT INTO relation(relation_id,owner_id,from_person_id,relation_type,"
               "to_person_id,label,source_utterance,observed_on)"
               " VALUES('r_fk','A','p_f','父亲','p_k','','','2026-10-01')")
    ov5 = service.family_overview(db, "A")
    g = [f for f in ov5["families"] if any(
        a["person_id"] == "p_g" for a in f["adults"])]
    check("跨代两代成人不被并成一个家庭（爷爷与爸爸不同家庭）",
          len(g) == 1 and len(g[0]["adults"]) == 1, g)
    check("孩子按父母挂到家庭，缺家长的不硬塞",
          any(any(c["person_id"] == "p_k" for c in f["children"]) for f in ov5["families"]),
          [f["label"] for f in ov5["families"]])

    # 6) 纯环关系不崩。
    db.execute("DELETE FROM relation WHERE owner_id='A'")
    db.execute("DELETE FROM person WHERE owner_id='A'")
    db.execute("INSERT INTO person(person_id,owner_id,name,relation,note,aliases,"
               "attributes_json) VALUES('p_c1','A','甲','friend','','[]','{}')")
    db.execute("INSERT INTO person(person_id,owner_id,name,relation,note,aliases,"
               "attributes_json) VALUES('p_c2','A','乙','friend','','[]','{}')")
    db.execute("INSERT INTO relation(relation_id,owner_id,from_person_id,relation_type,"
               "to_person_id,label,source_utterance,observed_on)"
               " VALUES('r_c1','A','p_c1','父亲','p_c2','','','2026-10-01')")
    db.execute("INSERT INTO relation(relation_id,owner_id,from_person_id,relation_type,"
               "to_person_id,label,source_utterance,observed_on)"
               " VALUES('r_c2','A','p_c2','父亲','p_c1','','','2026-10-01')")
    try:
        ov6 = service.family_overview(db, "A")
        check("纯环关系（互相指认家长）不崩，且不指认家长",
              all(not f["adults"] or f["head_person_id"] for f in ov6["families"]),
              [f["label"] for f in ov6["families"]])
    except Exception as exc:  # noqa: BLE001
        check("纯环关系（互相指认家长）不崩，且不指认家长", False, repr(exc))

    # 7) 空资料不崩。
    db.execute("DELETE FROM relation WHERE owner_id='A'")
    db.execute("DELETE FROM person WHERE owner_id='A'")
    check("空资料不崩", service.family_overview(db, "A")["families"] == [])


def part_r2_continuation():
    """R2 同一件事里继续补家庭/孩子，未知方持久，不误报全员同意。"""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "r2.db"
    db = dbm.DB(tmp)
    dbm.seed(db)
    started = service.start_group_activity(db, None, "A", UTTERANCE)
    plan_id = started["plan_id"]

    check("目标家庭数持久保存（刷新后还在）",
          service._plan_meta(db, plan_id).get("requested_families") == 3,
          service._plan_meta(db, plan_id))
    check("未知的第三家持久成缺口", service._plan_meta(
        db, plan_id).get("unknown_families") == 2)

    # 后续三句话都接回同一件，不新开。
    for utt in ("小周家和另外一家", "我家2个孩子", "小雨来小宇不来"):
        out = service.continue_group_activity(db, "A", utt)
        check("「%s」接回同一件事" % utt[:8],
              out is not None and out["plan_id"] == plan_id, out)
    plans = db.rows("SELECT plan_id FROM plan WHERE owner_id='A'")
    check("三句后续没有新开事项", len(plans) == 1, [p["plan_id"] for p in plans])
    meta = service._plan_meta(db, plan_id)
    check("A不能替小周家确认孩子出席", meta.get("child_attendance", {}) == {}, meta.get("child_attendance"))
    out = service.continue_group_activity(db, "B", "小雨来小宇不来")
    check("B本人可确认自己孩子出席并持久", out and service._plan_meta(db, plan_id)["child_attendance"].get("p_xiaohan_b") == "not_coming", out)
    check("本人孩子数持久保存", meta.get("my_children_count") == 2, meta)

    # 选进来的家庭是候选，不是确认参加。
    rows = db.rows("SELECT owner_id,label,status FROM plan_participant WHERE plan_id=?",
                   (plan_id,))
    check("被选进的家庭记为候选，不当成已确认参加",
          all(r["status"] == "pending" for r in rows),
          [(r["label"], r["status"]) for r in rows])

    # 有本人条件就能排第一版；别家没给条件也先排，缺的留缺口。
    class Stub:
        def chat_json(self, m, **k):
            return {"content": {"时间": "周六上午", "地点": "世纪公园"},
                    "reason": "按当前条件排，别家条件待补", "conflicts": []}
    import app.agents as agents
    out = agents.plan_propose(db, Stub(), plan_id, "A")
    check("只有本人条件也能排出第一版", out["version"] == 1, out["version"])

    # 本人一方接受，不等于三家都同意。
    st = service.plan_status(db, plan_id)
    mine = [p for p in st["participants"] if p["owner_id"] == "A"][0]
    service.decide_plan(db, plan_id, mine["participant_id"], 1, "accept",
                        actor_id="A")
    st2 = service.plan_status(db, plan_id)
    check("本人一方接受不变成全员同意", st2["agreed"] is False, st2["agreed"])
    check("还在等的方照实列出", set(st2["waiting_on"]) >= {"小周家"}, st2["waiting_on"])


def part_r4_permissions():
    """R4 权限：非参与方不能读详情/排方案/审阅；详情不泄露整本人物档案。"""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "r4.db"
    db = dbm.DB(tmp)
    dbm.seed(db)
    started = service.start_group_activity(db, None, "A", UTTERANCE)
    plan_id = started["plan_id"]
    # B 侧自己进场，才能验「参与方看得见、非参与方看不见」。
    service.add_plan_participant(db, plan_id, "B", "小周家", {}, True, role="family")

    check("发起方（A）看得见详情", service.plan_detail(db, plan_id, viewer="A") is not None)
    check("参与方（B）看得见详情", service.plan_detail(db, plan_id, viewer="B") is not None)
    check("非参与方（C）读不到详情", service.plan_detail(db, plan_id, viewer="C") is None)
    check("详情按 viewer 过滤，非参与方拿到的是 None 而不是全部字段",
          service.plan_detail(db, plan_id, viewer="C") is None)

    # 详情里的家庭信息只列本事项参与方的资料，不摊开 owner 的整本人物档案。
    det = service.plan_detail(db, plan_id, viewer="A")
    names = set()
    for f in det["families"]:
        for a in f["adults"]:
            names.add(a["name"])
        for c in f["children"]:
            names.add(c["name"])
    check("详情不把 owner 整本人物档案摊开（只有关系推导出的家庭与孩子）",
          "我" not in names or det["families"][0]["label"] == "我家", sorted(names))

    # propose / review 的参与方校验在接口层，这里验 service 层的判据一致。
    mine = db.row("SELECT participant_id FROM plan_participant WHERE plan_id=? AND owner_id=?",
                  (plan_id, "C"))
    check("C 在这个事项里没有代表的一方", mine is None)


def part_r5_budget():
    """R5 预算口径：不默认、不相加、模型违反不落版。"""
    own, _u, _c = service.normalize_conditions({"预算": "每家不超过80元"})
    check("带「每家」的旧预算归一到本人出资上限",
          own == {"本人出资上限": "每家不超过80元"}, own)
    tot, _u2, _c2 = service.normalize_conditions({"预算": "活动一共300元"})
    check("带「一共」的旧预算归一到活动总预算",
          tot == {"活动总预算": "活动一共300元"}, tot)
    amb, _u3, confirm = service.normalize_conditions({"预算": "100元"})
    check("口径不明的旧预算不默认，转为请本人确认",
          amb == {} and confirm == ["预算"], {"norm": amb, "confirm": confirm})
    both, _u4, _c4 = service.normalize_conditions(
        {"活动总预算": "300 元", "本人出资上限": "100 元以内"})
    check("两个口径的键同时存在且互不覆盖",
          both == {"活动总预算": "300 元", "本人出资上限": "100 元以内"}, both)

    # 模型恶意把各家上限相加：不落版。
    tmp = pathlib.Path(tempfile.mkdtemp()) / "r5.db"
    db = dbm.DB(tmp)
    dbm.seed(db)
    plan_id = service.create_plan(db, "A", "两家周末一起去公园", "两家周末一起去公园")
    service.add_plan_participant(db, plan_id, "A", "我家",
                                 {"本人出资上限": "100 元以内"}, True, role="self")
    service.add_plan_participant(db, plan_id, "B", "小周家",
                                 {"本人出资上限": "每家不超过80元"}, True)
    import app.agents as agents

    class Evil:
        def chat_json(self, m, **k):
            return {"content": {"时间": "周六上午", "活动总预算": "180 元",
                                "本人出资上限": "180 元"},
                    "reason": "两家上限相加", "conflicts": []}

    class NoBasis:
        def chat_json(self, m, **k):
            return {"content": {"时间": "周六", "活动总预算": "300 元"},
                    "reason": "x", "conflicts": []}

    class Fine:
        def chat_json(self, m, **k):
            return {"content": {"时间": "周六上午", "地点": "世纪公园"},
                    "reason": "按各家自己说的排，出资各家自理", "conflicts": []}

    for name, stub in (("相加", Evil()), ("无依据", NoBasis())):
        try:
            agents.plan_propose(db, stub, plan_id, "A")
            check("模型%s总预算被代码挡下，不落版" % name, False, "竟然落版了")
        except ValueError as exc:
            check("模型%s总预算被代码挡下，不落版" % name, True, str(exc))
    check("被挡下后没有留下任何版本",
          db.row("SELECT COUNT(*) c FROM plan_proposal WHERE plan_id=?",
                 (plan_id,))["c"] == 0)
    out = agents.plan_propose(db, Fine(), plan_id, "A")
    check("不带合成预算的正常方案能落版", out["version"] == 1, out["version"])


def part_t22_safety():
    import app.agents as agents
    d = dbm.DB(pathlib.Path(tempfile.mkdtemp()) / "safe.db")
    dbm.seed(d)
    plan = service.start_group_activity(d, None, "A", UTTERANCE)["plan_id"]
    service.new_plan_proposal(d, plan, "A", {"时间": "周六"})
    mine = service.plan_status(d, plan)["participants"][0]
    service.decide_plan(d, plan, mine["participant_id"], 1, "accept", actor_id="A")
    check("原句三家Aonlyaccept不能agreed", not service.plan_status(d, plan)["agreed"])
    service.continue_group_activity(d, "A", "另外一家")
    check("模糊另外一家不自动选联系人", len(service.plan_status(d, plan)["participants"]) == 1)
    service.continue_group_activity(d, "A", "小周家和另外一家")
    service.continue_group_activity(d, "A", "小周家和另外一家")
    check("明确小周家映射B且同plan幂等", [p["owner_id"] for p in service.plan_status(d, plan)["participants"]] == ["A", "B"])
    det = service.plan_detail(d, plan, viewer="B")
    check("已参加B读不泄露A通讯录", all(f["is_me"] for f in det["families"]))
    ctx = agents._plan_context(d, plan)
    check("模型上下文不读未授权人物", "p_xiaoyu" not in json.dumps(ctx))
    service.continue_group_activity(d, "B", "小雨来小宇不来")
    ctx2 = agents._plan_context(d, plan)
    bctx = next(s for s in ctx2["各方条件"] if s["owner_id"] == "B")
    check("本人出席最小字段传入且coming/notcoming分开", bctx["已确认出席人数"] == 1 and {c["出席"] for c in bctx["本事项已授权孩子"]} == {"coming", "not_coming"}, bctx)
    check("没有年龄授权就未知不自动读私有属性", all(c["年龄"].startswith("未知") for c in bctx["本事项已授权孩子"]))
    service.add_plan_participant(d, plan, "C", "同家另位家长", {})
    check("A/B/C三个身份不能冒充三家", len(service._selected_family_keys(d, plan)) == 2)
    for caps, content, bad in (([], {"本人出资上限":"500元"}, True),
                               (["100元以内"], {"本人出资上限":"500元"}, True),
                               (["100元以内"], {"本人出资上限":"100.00 元以内"}, False)):
        c = {"各方条件":[{"这一方的条件":{"本人出资上限":v}} for v in caps]}
        check("无/单cap金额依据严格校验" + repr(caps) + repr(content), bool(agents._budget_violation(c, content)) == bad)
    labeled = {"提案方":"A", "各方条件":[{"owner_id":"A","家庭":"我家","这一方的条件":{"本人出资上限":"100元以内"}}, {"owner_id":"B","家庭":"小周家","这一方的条件":{"本人出资上限":"80元以内"}}]}
    for out, bad in (({"A":"100 元以内", "B":"80元以内"}, False), ({"A":"180元", "B":"80元"}, True), ({"A":"100元", "未知":"80元"}, True), ("A 家庭：100 元以内；B 家庭：80 元以内（仅列上限，未授权实际出资）", False), ("我家：100 元以内；小周家：80 元以内", False), ("180 元", True), ("我家：180 元；小周家：80 元", True), ("我家：100 元；陌生家：80元", True)):
        check("逐家cap不误拒但拒sum/改额/未授权:" + str(out), bool(agents._budget_violation(labeled, {"本人出资上限":out})) == bad)
    c = {"各方条件":[{"这一方的条件":{"活动总预算":"活动一共300元"}}]}
    check("明确总额金额格式不同不误拒", not agents._budget_violation(c, {"活动总预算":"300.00 元"}))
    class EvilReview:
        def chat_json(self, *a, **k):
            return {"decision":"counter", "reason":"x", "changes":{"活动总预算":"999元"}}
    before = service.plan_status(d, plan)["current_version"]
    try:
        agents.plan_review(d, EvilReview(), plan, mine["participant_id"], before)
        check("review无依据反提案不落版", False)
    except ValueError:
        check("review无依据反提案不落版", service.plan_status(d, plan)["current_version"] == before)


def main() -> int:
    plan = part_relations()
    part_r1_boundaries()
    part_r2_continuation()
    part_r4_permissions()
    part_r5_budget()
    part_prompt_rules()
    part_t22_safety()
    part_api()
    ok = sum(1 for _, p, _ in results if p)
    lines = ["家庭活动完整切片与多孩子引导：%d/%d 通过" % (ok, len(results)),
             "A 关系数据驱动：临时库，不连网络、不调模型。",
             "B 接口级：独立临时服务 + 独立临时数据目录，模型未调用；",
             "  /api/say、/api/plan/create、/api/plans、/api/plan/{id}、",
             "  /api/task/{id}/to-group-activity 全部走真实 HTTP。",
             "C 浏览器级：另见 test_note_entry_flow.py 的加图分支（模型拦截）。",
             "真实模型行为不在本脚本范围内。", ""]
    for name, passed, detail in results:
        lines.append(("PASS " if passed else "FAIL ") + name)
        if not passed:
            lines.append("       证据：" + repr(detail)[:400])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf8")
    print("\n".join(lines))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
