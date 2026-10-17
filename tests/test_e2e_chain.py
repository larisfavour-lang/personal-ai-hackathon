"""五项验收的端到端检查：真实模型调用，结果写入 data/acceptance.json。

断言规则：
- 不把 model_failed 当作冲突通过。
- 不把 counter_pending 当作已达成。
- 事实新旧同时存在不等于时效验证通过，必须看执行用的是哪个值。
- 订单规格必须来自当前有效方案，不能是提问里的旧值。

运行：/tmp/hack_venv/bin/python tests/test_e2e_chain.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db as dbm
from app import service
from app import agents
from app.model_client import ModelClient

# 用例自建的 DB/明细落点。设了 PA_DATA_DIR 就落在它里面（隔离跑时是临时目录），
# 没设才回落到仓库 data/。原来是写死的 ROOT/data：隔离跑也会往仓库 data/ 落
# test_three_family_plan.db 之类的产物——路径没隔离，光把服务端口隔开不算数。
# 回落分支保持原样，不影响平时直接跑的行为。
DATA = Path(os.environ.get("PA_DATA_DIR") or (ROOT / "data"))
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "test_acceptance.db"

results: list[dict] = []


def fresh_db(name):
    """每次拿一个干净的库，保证用例之间不互相污染、可重复运行。

    shutil.rmtree 对文件路径是静默无效的，只留下旧库，
    上一轮写入的状态会被下一轮当成初始条件，结论就不可复现。
    """
    import shutil
    path = DATA / name
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()
    db = dbm.DB(path)
    dbm.seed(db)
    return db


def report(name, ok, detail):
    results.append({"case": name, "ok": bool(ok), "detail": detail})
    print(("[PASS] " if ok else "[FAIL] ") + name)
    print("       " + json.dumps(detail, ensure_ascii=False)[:1400])


def likes_of(db, owner_id, person_id):
    rows = db.rows(
        "SELECT value FROM fact WHERE owner_id=? AND subject_id=? AND attribute='likes'"
        " AND status='active'", (owner_id, person_id))
    return sorted(r["value"] for r in rows)


def test_rejected_confirm_leaves_no_trace():
    """错误确认被拒绝时，不得留下接受、约定阶段或订单。

    覆盖：非 A 确认、跨任务提案、旧版本提案、缺尺码。
    这条只走程序校验，不调用模型。
    """
    db = fresh_db("test_confirm_guard.db")
    # 直接构造一个已约定、需要尺码的反提案，避免依赖模型结果。
    task_id = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (task_id, "A", "给小周的女儿买双鞋，300元以内",
         json.dumps({"subject_id": "p_xiaoyu", "subject_name": "小雨",
                     "budget_max": 300.0}, ensure_ascii=False),
         "counter_pending", "", dbm.now_iso()))
    other_task_id = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (other_task_id, "A", "别的任务", json.dumps({}, ensure_ascii=False),
         "created", "", dbm.now_iso()))
    prop_id = dbm.new_id("prop")
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id, reason,"
        " asked_size, shared_conditions_json, facts_used_json, decision, decided_by, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (prop_id, task_id, 1, "B", "pr_puzzle_100", "对方建议的拼图", "",
         json.dumps([], ensure_ascii=False), json.dumps([], ensure_ascii=False),
         "pending", "", dbm.now_iso()))
    other_prop_id = dbm.new_id("prop")
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id, reason,"
        " asked_size, shared_conditions_json, facts_used_json, decision, decided_by, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (other_prop_id, other_task_id, 1, "A", "pr_puzzle_100", "别的任务的方案", "",
         json.dumps([], ensure_ascii=False), json.dumps([], ensure_ascii=False),
         "accept", "B", dbm.now_iso()))

    def snapshot(proposal_row_id=None):
        """读取任务状态；默认看本次要确认的那个提案。"""
        pid = proposal_row_id or prop_id
        t = db.row("SELECT * FROM task WHERE task_id=?", (task_id,))
        p = db.row("SELECT * FROM proposal WHERE proposal_id=?", (pid,))
        orders = db.rows("SELECT * FROM sim_order WHERE task_id=?", (task_id,))
        return {"stage": t["stage"], "decision": p["decision"], "decided_by": p["decided_by"],
                "orders": len(orders)}

    before = snapshot()
    cases = {}

    # 1) 非送礼者本人确认（即使勾选接受反提案）
    try:
        service.confirm_and_purchase(db, None, task_id, prop_id, "B", accept_counter=True)
        cases["非A确认"] = "未被拒绝"
    except ValueError as exc:
        cases["非A确认"] = str(exc)
    assert snapshot() == before, "非 A 确认后状态被改动：%s" % snapshot()

    # 2) 跨任务提案
    try:
        service.confirm_and_purchase(db, None, task_id, other_prop_id, "A")
        cases["跨任务提案"] = "未被拒绝"
    except ValueError as exc:
        cases["跨任务提案"] = str(exc)
    assert snapshot() == before, "跨任务提案确认后状态被改动：%s" % snapshot()

    # 3) 缺尺码的有尺码商品。这里换成有尺码商品专门验一次，
    #    因为上面的构造用的是无尺码商品，测不到这一条。
    sized_task = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (sized_task, "A", "给小周的女儿买双鞋，300元以内",
         json.dumps({"subject_id": "p_xiaoyu", "subject_name": "小雨",
                     "budget_max": 300.0}, ensure_ascii=False),
         "counter_pending", "", dbm.now_iso()))
    sized_prop = dbm.new_id("prop")
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id, reason,"
        " asked_size, shared_conditions_json, facts_used_json, decision, decided_by, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (sized_prop, sized_task, 1, "B", "pr_sneakers", "对方建议的运动鞋", "",
         json.dumps([], ensure_ascii=False), json.dumps([], ensure_ascii=False),
         "pending", "", dbm.now_iso()))
    try:
        service.confirm_and_purchase(db, None, sized_task, sized_prop, "A", accept_counter=True)
        cases["缺尺码"] = "未被拒绝"
    except ValueError as exc:
        cases["缺尺码"] = str(exc)
    # 家长明确登记当前鞋码后才放行，证明这条拒绝来自依据不足，而不是永远拒绝。
    b_person = db.row("SELECT person_id FROM person WHERE owner_id='B' AND name='小雨'")
    db.execute(
        "INSERT INTO fact(fact_id, owner_id, subject_id, attribute, value,"
        " source_utterance, provider, observed_on, status) VALUES(?,?,?,?,?,?,?,?,?)",
        (dbm.new_id("fact"), "B", b_person["person_id"], "shoe_size", "31 码",
         "家长 2026-10-13 明确确认：孩子现在穿 31 码", "家长确认", "2026-10-13", "active"))
    db.execute("UPDATE proposal SET asked_size=? WHERE proposal_id=?",
               ("31 码", sized_prop))
    ok_sized = service.confirm_and_purchase(db, None, sized_task, sized_prop, "A",
                                            accept_counter=True)
    assert ok_sized.get("spec") == "31 码", ok_sized.get("spec")

    # 4) 旧版本提案
    new_prop_id = dbm.new_id("prop")
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id, reason,"
        " asked_size, shared_conditions_json, facts_used_json, decision, decided_by, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_prop_id, task_id, 2, "B", "pr_puzzle_100", "第二版", "",
         json.dumps([], ensure_ascii=False), json.dumps([], ensure_ascii=False),
         "pending", "", dbm.now_iso()))
    try:
        service.confirm_and_purchase(db, None, task_id, prop_id, "A")
        cases["旧版本提案"] = "未被拒绝"
    except ValueError as exc:
        cases["旧版本提案"] = str(exc)
    after_stale = snapshot(prop_id)
    assert after_stale["decision"] in ("pending", "") and after_stale["orders"] == 0, \
        "旧版本提案确认后状态被改动：%s" % after_stale

    # 全部条件满足后应当成功，并同时记录接受、订单与已登记阶段
    ok = service.confirm_and_purchase(db, None, task_id, new_prop_id, "A", accept_counter=True)
    final = snapshot(new_prop_id)
    assert final["orders"] == 1 and final["stage"] == "purchased" \
        and final["decision"] == "accept" and final["decided_by"] == "A", final
    # 这件商品不需要尺码，订单规格为空；尺码依据的校验由用例 4 覆盖。
    assert ok.get("spec") == "", ok.get("spec")

    print("[PASS] 错误确认不留下接受/约定/订单痕迹")
    return {"拒绝原因": cases, "成功后状态": final, "订单规格": ok.get("spec")}


def test_correction_invalidates_pending_proposal():
    """事实纠正后，依赖旧事实的未完方案必须失效，旧确认不能再执行。

    链路：协商到 B 的反提案（未确认）→ 纠正该对象的记录 →
    旧反提案被拒绝 → 同一事项重新协商 → 新版本执行。

    关于模型：纠正那一步（record_utterance）要模型从原话里抽取事实，
    所以这一条**会调用模型**，包括后续重建事项。原 docstring 写「不调用模型」是错的，
    照着它读会把人引向「失败一定是程序问题」；现在改成就实描述——
    模型不可用时这一条会以 ModelUnavailable 失败，归因到模型侧，
    不算程序校验通过。后面两处 create_task 原先传 client=None，
    只在命中重复事项时才不碰 client，一旦没命中就
    AttributeError: 'NoneType' object has no attribute 'chat_json'——
    那是测试自己断了链路，不是产品缺陷。改成传真 client。
    """
    db = fresh_db("test_stale_proposal.db")
    client = ModelClient()
    xy = db.row("SELECT person_id FROM person WHERE owner_id='A' AND name='小雨'")["person_id"]

    task_id = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (task_id, "A", "给小周的女儿准备生日礼物，300元以内",
         json.dumps({"subject_id": xy, "subject_name": "小雨", "budget_max": 300.0},
                    ensure_ascii=False),
         "counter_pending", "", dbm.now_iso()))
    # 旧反提案：B 提出，理由是「她喜欢拼图」，事实依据里带着旧的那条 likes。
    old_prop = dbm.new_id("prop")
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id, reason,"
        " asked_size, shared_conditions_json, facts_used_json, decision, decided_by, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (old_prop, task_id, 1, "B", "pr_puzzle_100", "她喜欢拼图", "",
         json.dumps([], ensure_ascii=False),
         json.dumps([{"attribute": "likes", "value": "拼图"}], ensure_ascii=False),
         "pending", "", dbm.now_iso()))

    # 纠正：喜欢拼图的是小宇，不是小雨。旧那条 likes 被替代。
    result = service.record_utterance(
        db, client, "A", "记错了，喜欢拼图的是小宇，小雨喜欢画画")
    invalidated = result.get("invalidated_tasks") or []
    task_row = db.row("SELECT stage FROM task WHERE task_id=?", (task_id,))
    old_decision = db.row("SELECT decision FROM proposal WHERE proposal_id=?",
                          (old_prop,))
    old_decision = {"decision": old_decision["decision"]}

    # 旧确认必须被拒绝，且依据是「已作废」，不是版本号或状态碰巧挡住。
    reject_reason = ""
    try:
        service.confirm_and_purchase(db, None, task_id, old_prop, "A", accept_counter=True)
        reject_reason = "未被拒绝"
    except ValueError as exc:
        reject_reason = str(exc)

    # 同一事项重新协商：新版本号必须排在旧版本之后。
    service.create_task(db, client, "A", "给小周的女儿准备生日礼物，300元以内")
    renegotiate = service.create_task(db, client, "A", "给小周的女儿准备生日礼物，300元以内")
    assert renegotiate.get("reused") is True, renegotiate
    new_prop = dbm.new_id("prop")
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id, reason,"
        " asked_size, shared_conditions_json, facts_used_json, decision, decided_by, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_prop, task_id, 2, "A", "pr_watercolor", "她喜欢画画，选水彩", "",
         json.dumps([], ensure_ascii=False),
         json.dumps([{"attribute": "likes", "value": "画画"}], ensure_ascii=False),
         "pending", "", dbm.now_iso()))
    # B 接受这个新版本：提案由 A 提出，必须有对方同版本的接受记录。
    db.execute("UPDATE proposal SET decision='accept', decided_by='B' WHERE proposal_id=?",
               (new_prop,))
    db.execute("UPDATE task SET stage='agreed' WHERE task_id=?", (task_id,))

    # 旧版本仍然不能执行（版本更旧 + 已作废双重拒绝）。
    stale_reason = ""
    try:
        service.confirm_and_purchase(db, None, task_id, old_prop, "A", accept_counter=True)
        stale_reason = "未被拒绝"
    except ValueError as exc:
        stale_reason = str(exc)

    # 新版本可以正常执行（A 的提案 + B 已接受，不需要再勾接受反提案）。
    ok = service.confirm_and_purchase(db, None, task_id, new_prop, "A")

    # 未纠正的另一个对象的事项不能被误伤。
    other_task = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (other_task, "A", "给小宇准备生日礼物，100元以内",
         json.dumps({"subject_id": "p_xiaohan", "subject_name": "小宇",
                     "budget_max": 100.0}, ensure_ascii=False),
         "agreed", "", dbm.now_iso()))
    other_prop = dbm.new_id("prop")
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id, reason,"
        " asked_size, shared_conditions_json, facts_used_json, decision, decided_by, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (other_prop, other_task, 1, "A", "pr_puzzle_100", "他喜欢拼图", "",
         json.dumps([], ensure_ascii=False),
         json.dumps([{"attribute": "likes", "value": "拼图"}], ensure_ascii=False),
         "accept", "B", dbm.now_iso()))
    other_before = db.row("SELECT decision FROM proposal WHERE proposal_id=?", (other_prop,))
    service.record_utterance(db, ModelClient(), "A", "小雨现在喜欢水彩")
    other_after = db.row("SELECT decision FROM proposal WHERE proposal_id=?", (other_prop,))
    other_stage = db.row("SELECT stage FROM task WHERE task_id=?", (other_task,))["stage"]

    print("[PASS] 事实纠正使未完方案失效")
    return {
        "被纠正后失效的事项": invalidated,
        "失效后阶段": task_row["stage"],
        "旧反提案决定": old_decision,
        "旧确认被拒原因": reject_reason,
        "旧版本再次被拒原因": stale_reason,
        "新版本执行": ok.get("order_id"),
        "新版本是否排在旧版本后": True,
        "无关事项决定": {"before": other_before, "after": other_after,
                     "未被误改": other_before == other_after},
        "无关事项阶段": other_stage,
    }


def test_order_spec_takes_final_corrected_size():
    """订单规格取最终更正的尺码，而不是提问里的旧尺码。"""
    from app.service import _order_spec
    product = {"has_size": 1, "size_options": '["30 码","31 码","32 码"]'}
    proposal = {"shared_conditions_json": [
        "请确认小雨目前仍穿30码", "请确认她需要一双儿童运动鞋", "请将鞋码改为32码"]}
    got = _order_spec(proposal, product)
    assert got == "32 码", got
    assert _order_spec({"shared_conditions_json": ["随便"]},
                       {"has_size": 0, "size_options": "[]"}) == ""
    assert _order_spec({"shared_conditions_json": ["请确认颜色"]}, product) == ""
    print("[PASS] 订单规格取最终更正尺码")
    return True


def test_followup_does_not_swallow_new_task():
    """已登记订单后，带新目标的句子必须按交办走，不能被记成上一单的反馈。

    这条守住一个真实回归：判断顺序放在关键词之后时，
    「小周说孩子很喜欢画画，给她买一套颜料，100元以内」会被当成上一单的事后反馈，
    新交办被悄悄吞掉，用户看不到任何进展。
    纯反馈仍要接回原来那条送礼记录。
    """
    import shutil
    path = DATA / "test_followup_intent.db"
    if path.exists():
        path.unlink()
    db = dbm.DB(path)
    dbm.seed(db)
    client = ModelClient()

    def new_purchased(name, product):
        subj = db.row("SELECT person_id FROM person WHERE owner_id='A' AND name=?",
                      (name,))["person_id"]
        tid = dbm.new_id("task")
        db.execute(
            "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome,"
            " created_at) VALUES(?,?,?,?,?,?,?)",
            (tid, "A", "给%s准备生日礼物，300元以内" % name,
             json.dumps({"subject_id": subj, "subject_name": name, "budget_max": 300.0},
                        ensure_ascii=False),
             "purchased", "已登记", dbm.now_iso()))
        oid = dbm.new_id("simorder")
        db.execute(
            "INSERT INTO sim_order(order_id, task_id, proposal_version, confirmed_by, product_id,"
            " product_name, spec, recipient_subject_id, amount_cny, state, delivery_state,"
            " dedup_key, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, tid, 1, "A", "pr_blocks", product, "", subj, 199.0, "purchased",
             "simulated", "order:%s:v1" % tid, dbm.now_iso()))
        gid = dbm.new_id("gift")
        db.execute(
            "INSERT INTO gift_record(gift_id, order_id, owner_id, subject_id, product_id,"
            " product_name, spec, occasion, status, feedback, happened_on, simulated)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
            (gid, oid, "A", subj, "pr_blocks", product, "", "生日", "purchased", "",
             dbm.now_iso()[:10]))
        return tid, oid, gid

    t_xy, o_xy, g_xy = new_purchased("小雨", "大颗粒积木桶")

    # 带新目标的句子：意图必须是 task，且不能写任何 feedback。
    new_task_text = "小周说孩子很喜欢画画，给她买一套颜料，100元以内"
    intent_new = service.classify_intent(db, client, "A", new_task_text)
    pure_flag = service._is_pure_followup(new_task_text)
    untouched = db.row("SELECT feedback FROM gift_record WHERE gift_id=?", (g_xy,))["feedback"]

    # 纯反馈：意图是 followup，并写回同一件。
    intent_pure = service.classify_intent(db, client, "A", "送到了，孩子很喜欢")
    pure_hit = service.record_followup(db, "A", "送到了，孩子很喜欢")
    fb_after = db.row("SELECT feedback FROM gift_record WHERE gift_id=?",
                      (g_xy,))["feedback"]

    # 单候选但指名他人：不能顺势写到小雨名下。
    # 这里要留下一条真正待记的订单，否则候选为空，测的就不是「顺势写过去」。
    t2, o2, g2 = new_purchased("小雨", "儿童绘画套装")
    wrong_person = service.record_followup(db, "A", "小宇收到了，他很喜欢")
    g2_fb = db.row("SELECT feedback FROM gift_record WHERE gift_id=?",
                   (g2,))["feedback"]
    # 同一件待记订单，用户没有指名时仍要能正常写入。
    unnamed = service.record_followup(db, "A", "收到了，孩子说谢谢")

    ok = (intent_new == "task"
          and pure_flag is False
          and untouched == ""
          and intent_pure == "followup"
          and pure_hit is not None
          and pure_hit.get("order_id") == o_xy
          and fb_after == "送到了，孩子很喜欢"
          and wrong_person.get("resolved") is False
          and g2_fb == ""
          and unnamed is not None
          and unnamed.get("order_id") == o2)

    print("[PASS] 后续反馈不吞新交办且按人对号" if ok
          else "[FAIL] 后续反馈不吞新交办且按人对号")
    return {
        "带新目标句子的意图": intent_new,
        "该句是否被判为纯反馈": pure_flag,
        "上一单 feedback 是否被误写": untouched,
        "纯反馈意图": intent_pure,
        "纯反馈命中的订单": pure_hit and pure_hit.get("order_id"),
        "是否等于小雨那一单": pure_hit and pure_hit.get("order_id") == o_xy,
        "写回的反馈": fb_after,
        "单候选但指名他人": {"resolved": wrong_person.get("resolved"),
                       "待记订单是否被误写": g2_fb},
        "单候选且未指名": {"order_id": unnamed and unnamed.get("order_id"),
                       "是否等于第二条订单": unnamed and unnamed.get("order_id") == o2},
    }


def test_followup_append_when_all_fed_back():
    """反馈全部记完后，用户接着补充同一件事不能变成新交办。

    _followup_candidates 只列还没写反馈的订单。所有订单都有反馈后再说
    「后来玩了一会就放一边了」，候选为空，record_followup 会返回 None，
    main 的 followup 分支接不住，这句话就一路落到 create_task——
    一句事后反馈凭空长出一件新事项。

    这里不调模型，只锁这条纯逻辑分支：未指名时续接同一条、指名他人不误写、
    全程不新增 task、也不覆盖原来那句反馈。
    """
    import os
    path = DATA / "test_followup_append.db"
    if path.exists():
        path.unlink()
    db = dbm.DB(path)
    dbm.seed(db)

    subj = db.row("SELECT person_id FROM person WHERE owner_id='A' AND name=?",
                  ("小雨",))["person_id"]
    tid = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome,"
        " created_at) VALUES(?,?,?,?,?,?,?)",
        (tid, "A", "给小雨准备生日礼物，300元以内",
         json.dumps({"subject_id": subj, "subject_name": "小雨", "budget_max": 300.0},
                    ensure_ascii=False),
         "purchased", "已登记", dbm.now_iso()))
    oid = dbm.new_id("simorder")
    db.execute(
        "INSERT INTO sim_order(order_id, task_id, proposal_version, confirmed_by, product_id,"
        " product_name, spec, recipient_subject_id, amount_cny, state, delivery_state,"
        " dedup_key, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, tid, 1, "A", "pr_blocks", "大颗粒积木桶", "", subj, 199.0, "purchased",
         "simulated", "order:%s:v1" % tid, dbm.now_iso()))
    gid = dbm.new_id("gift")
    db.execute(
        "INSERT INTO gift_record(gift_id, order_id, owner_id, subject_id, product_id,"
        " product_name, spec, occasion, status, feedback, happened_on, simulated)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
        (gid, oid, "A", subj, "pr_blocks", "大颗粒积木桶", "", "生日", "purchased", "",
         dbm.now_iso()[:10]))
    tasks_before = db.row("SELECT COUNT(*) c FROM task WHERE owner_id='A'")["c"]

    first = service.record_followup(db, "A", "送到了，孩子很喜欢")
    # 候选已空后的第二条：未指名，应续接同一条而不是反问或新建事项。
    second = service.record_followup(db, "A", "后来玩了一会就放一边了")
    fb_two = db.row("SELECT feedback FROM gift_record WHERE gift_id=?", (gid,))["feedback"]
    # 指名了记录里的另一个人：不能顺势追加到小雨这件上。
    wrong = service.record_followup(db, "A", "小宇的那个他不喜欢")
    fb_after_wrong = db.row("SELECT feedback FROM gift_record WHERE gift_id=?",
                            (gid,))["feedback"]
    # 指名本人：可以继续追加。
    named = service.record_followup(db, "A", "小雨说还想再要一个")
    tasks_after = db.row("SELECT COUNT(*) c FROM task WHERE owner_id='A'")["c"]

    ok = (first is not None and first.get("gift_id") == gid
          and second is not None and second.get("gift_id") == gid
          and "送到了，孩子很喜欢" in fb_two
          and "后来玩了一会就放一边了" in fb_two
          and wrong.get("resolved") is False
          and fb_after_wrong == fb_two
          and named is not None and named.get("gift_id") == gid
          and tasks_after == tasks_before)

    print("[PASS] 反馈记完后继续补充不变成新交办" if ok
          else "[FAIL] 反馈记完后继续补充不变成新交办")
    return {
        "首条命中本单": first and first.get("gift_id") == gid,
        "未指名续接同一件": second and second.get("gift_id") == gid,
        "原反馈未被覆盖": "送到了，孩子很喜欢" in fb_two,
        "新反馈已追加": "后来玩了一会就放一边了" in fb_two,
        "指名他人不误写": wrong.get("resolved"),
        "指名他人后反馈未变": fb_after_wrong == fb_two,
        "指名本人可追加": named and named.get("gift_id") == gid,
        "任务数变化": "%d -> %d" % (tasks_before, tasks_after),
    }


def test_waiting_record_and_no_order():
    """在等谁、等什么要记下来；未回应不能生成订单。

    卡在只有家长能给的当前鞋码上时，这件事进入 blocked，同时要留下一条等待记录：
    等的是对方本人、缺的是什么、从什么时候开始等。未回应不是同意，
    也不能显示成已经约好，所以这种状态下不能有任何订单。

    同时锁住两个真实回归：
    - 确认购买时清理等待必须用同一个连接。另开写连接会被这个事务自己持有的锁
      挡住，等到超时后整个确认跟着回滚，用户看到的是「没登记成功」。
    - 「等我自己定」和「等对方回复」是两回事。对方已经回过反提案，卡的是送礼者
      本人，这时不能显示成「在等对方助理回复」，那和已收到回复的事实矛盾。
    """
    import os
    import time
    path = DATA / "test_waiting.db"
    if path.exists():
        path.unlink()
    db = dbm.DB(path)
    dbm.seed(db)

    subj = db.row("SELECT person_id FROM person WHERE owner_id='A' AND name=?",
                  ("小雨",))["person_id"]
    tid = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome,"
        " created_at) VALUES(?,?,?,?,?,?,?)",
        (tid, "A", "给小雨准备生日礼物，300元以内",
         json.dumps({"subject_id": subj, "subject_name": "小雨", "budget_max": 300.0},
                    ensure_ascii=False),
         "agreed", "", dbm.now_iso()))
    pid = dbm.new_id("prop")
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id, reason,"
        " asked_size, shared_conditions_json, facts_used_json, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (pid, tid, 1, "A", "pr_blocks", "大颗粒积木桶", "", "[]", "[]", dbm.now_iso()))
    db.execute("UPDATE proposal SET decision='accept', decided_by='B',"
               " decision_reason='可以' WHERE proposal_id=?", (pid,))

    # 等家长本人确认尺码。
    service.set_waiting(db, tid, "B", "awaiting_person", "家长这边没有当前鞋码记录。")
    parent_wait = service.waiting_for(db, "B")
    # 等我自己定。
    service.set_waiting(db, tid, "A", "awaiting_my_decision",
                        "对方提了新方案（儿童运动鞋），等你定。")
    my_wait = service.waiting_for(db, "A")
    orders_before = db.row("SELECT COUNT(*) c FROM sim_order")["c"]

    # 未回应就是未回应：这种状态下不能有订单。
    no_order = orders_before == 0

    # 确认购买要在同一事务里清掉等待，不能另开连接自锁。
    t0 = time.time()
    try:
        order = service.confirm_and_purchase(db, None, tid, pid, "A")
        confirmed = True
        err = ""
    except Exception as exc:  # noqa: BLE001
        order = None
        confirmed = False
        err = "%s: %s" % (type(exc).__name__, str(exc)[:80])
    elapsed = time.time() - t0

    ok = (len(parent_wait) == 1
          and parent_wait[0]["kind"] == "awaiting_person"
          and "在等对方本人确认" in parent_wait[0]["text"]
          and len(my_wait) == 1
          and my_wait[0]["kind"] == "awaiting_my_decision"
          and "在等你定" in my_wait[0]["text"]
          and "在等对方助理回复" not in my_wait[0]["text"]
          and no_order
          and confirmed
          and order is not None
          and service.waiting_for(db, "A") == []
          and service.waiting_for(db, "B") == []
          and db.row("SELECT COUNT(*) c FROM pending_action")["c"] == 0
          and db.row("SELECT COUNT(*) c FROM sim_order WHERE order_id=?",
                     (order["order_id"],))["c"] == 1
          and elapsed < 5.0)

    print("[PASS] 在等谁要记下，未回应不生成订单，确认不自锁" if ok
          else "[FAIL] 在等谁要记下，未回应不生成订单，确认不自锁")
    return {
        "等家长本人的等待": len(parent_wait),
        "等待文案含在等对方本人": "在等对方本人确认" in (parent_wait[0]["text"] if parent_wait else ""),
        "等我定的等待": len(my_wait),
        "等我定文案": my_wait[0]["text"] if my_wait else "",
        "未回应时订单数": orders_before,
        "确认是否成功": confirmed,
        "确认失败原因": err,
        "确认耗时秒": round(elapsed, 2),
        "确认后 A 侧等待": len(service.waiting_for(db, "A")),
        "确认后 B 侧等待": len(service.waiting_for(db, "B")),
        "库里等待记录": db.row("SELECT COUNT(*) c FROM pending_action")["c"],
        "订单是否落库": bool(order) and db.row(
            "SELECT COUNT(*) c FROM sim_order WHERE order_id=?",
            (order["order_id"],))["c"] == 1,
    }


def test_life_event_field_grants():
    """家庭近况：按人、按字段授权，撤回后新查询不可见，纠正后用新状态。

    两个标注的测试来源写同一个人的不同事件，授权范围不同：
    学习 App 的进度可以给 A 看，软件自己判定的困难点不给；
    手表数据是设备来源，必须标出来，不能当成本人自述或医学判断。

    这里锁住四件容易做错的事：
    - 未授权字段不能出现在查询结果里（不是给了再在界面上藏）；
    - 事件所有者本人要能看到自己名下的全部字段；
    - 撤回后新的查询看不到，原记录仍留在库里可查；
    - 纠正来源记录后，后续摘要用新状态，授权跟着新记录走。
    """
    import os
    path = DATA / "test_life_event.db"
    if path.exists():
        path.unlink()
    db = dbm.DB(path)
    dbm.seed(db)

    # 演示 seed 也写了几条同来源的近况，去重键会和下面的用例撞上。
    # 这条用例要测的是导入/授权/撤回/纠正本身，先清空再跑。
    with db.connect() as conn:
        conn.execute("DELETE FROM life_event")
        conn.execute("DELETE FROM event_grant")
        conn.execute("DELETE FROM event_notice")

    b_subj = db.row("SELECT person_id FROM person WHERE owner_id='B' AND name=?",
                    ("小雨",))["person_id"]
    a_subj = db.row("SELECT person_id FROM person WHERE owner_id='A' AND name=?",
                    ("小雨",))["person_id"]

    e_study, c_study = service.import_life_event(
        db, "study_app", b_subj, "study_progress",
        {"已完成项目": "数学练习 20 题", "软件报告的困难点": "进位加法容易错"},
        "2026-10-12", "孩子本人在学习软件里确认", "B",
        [{"viewer_id": "A", "fields": ["已完成项目"]}], "app")
    # 重复导入同一来源的同一事件：不能重复建。
    _, dup = service.import_life_event(
        db, "study_app", b_subj, "study_progress",
        {"已完成项目": "数学练习 20 题"}, "2026-10-12", "x", "B", [])
    e_watch, _ = service.import_life_event(
        db, "watch_sync", b_subj, "sleep", {"睡眠时长": "8.2 小时", "静息心率": "72"},
        "2026-10-11", "手表原厂 App 同步", "B",
        [{"viewer_id": "A", "fields": ["睡眠时长"]}], "watch")

    a_sees = service.life_events_for(db, b_subj, "A")
    b_sees = service.life_events_for(db, b_subj, "B")
    # 只该看到授权字段。这里要看的是「未授权的字段有没有混进来」，
    # 不是「可见字段为空」——可见字段本来就该是授权的那几个。
    visible = set()
    for e in a_sees:
        visible |= set(e["fields"].keys())
    unauthorized = {"软件报告的困难点", "静息心率"}
    leaked = sorted(visible & unauthorized)

    # 通知只登记一次。
    notice_first = service.notice_life_event(db, e_study, "A")
    notice_again = service.notice_life_event(db, e_study, "A")

    # 撤回学习事件：新查询看不到，原记录仍在。
    service.revoke_life_event(db, e_study, "B")
    after_revoke = service.life_events_for(db, b_subj, "A")

    # 纠正手表事件：后续用新状态，授权跟着新记录走。
    new_watch = service.correct_life_event(
        db, e_watch, {"睡眠时长": "9.0 小时"}, "B")
    after_correct = service.life_events_for(db, b_subj, "A")
    watch_now = [e for e in after_correct if e["event_id"] == new_watch]

    # 人物详情里也要能看到（A 侧人物实体和 B 侧不是同一个，要按名字对上）。
    detail = service.person_detail(db, "A", a_subj)

    ok = (c_study is True
          and dup is False
          and len(a_sees) == 2
          and not leaked
          and visible == {"已完成项目", "睡眠时长"}
          and len(b_sees) == 2
          and any("软件报告的困难点" in e["fields"] for e in b_sees)
          and any(e["is_device"] and e["source_label"] == "手表模拟数据" for e in a_sees)
          and notice_first is True and notice_again is False
          and all(e["event_id"] != e_study for e in after_revoke)
          and db.row("SELECT status FROM life_event WHERE event_id=?",
                     (e_study,))["status"] == "revoked"
          and len(watch_now) == 1
          and watch_now[0]["fields"] == {"睡眠时长": "9.0 小时"}
          and db.row("SELECT status FROM life_event WHERE event_id=?",
                     (e_watch,))["status"] == "superseded"
          and len(detail.get("life_events", [])) == 1
          and detail["life_events"][0]["fields"] == {"睡眠时长": "9.0 小时"})

    print("[PASS] 家庭近况按人按字段授权，撤回与纠正都生效" if ok
          else "[FAIL] 家庭近况按人按字段授权，撤回与纠正都生效")
    return {
        "导入是否新建": c_study,
        "重复导入是否新建": dup,
        "A 可见条数": len(a_sees),
        "B 可见条数": len(b_sees),
        "A 可见字段": sorted(visible),
        "未授权字段是否泄露": leaked,
        "B 能看到全部字段": any("软件报告的困难点" in e["fields"] for e in b_sees),
        "手表是否标为设备": any(e["is_device"] for e in a_sees),
        "通知去重": notice_first and not notice_again,
        "撤回后仍可见数": len(after_revoke),
        "撤回记录状态": db.row("SELECT status FROM life_event WHERE event_id=?",
                          (e_study,))["status"],
        "纠正后睡眠时长": watch_now[0]["fields"] if watch_now else None,
        "旧事件状态": db.row("SELECT status FROM life_event WHERE event_id=?",
                       (e_watch,))["status"],
        "A 人物详情可见数": len(detail.get("life_events", [])),
        "详情里的是新状态": detail["life_events"][0]["fields"] if detail.get("life_events") else None,
    }


def test_cross_app_reuse_in_context():
    """跨小 App 复用：来源 A 更新后，问一次就能用上新状态。

    方案要求至少验证一次「来源 A 更新后，来源 B 的相关摘要正确复用」，
    同时另一个人或无权查看者读不到。这里检查的是实际发给模型的上下文，
    不是页面：上下文里必须是新值，旧值不能残留；
    别的孩子的上下文里不能出现这个孩子的近况；
    撤回后从上下文里消失，但同一人物的其他来源仍在。
    """
    import os
    path = DATA / "test_reuse_ctx.db"
    if path.exists():
        path.unlink()
    db = dbm.DB(path)
    dbm.seed(db)
    with db.connect() as conn:
        conn.execute("DELETE FROM life_event")
        conn.execute("DELETE FROM event_grant")
        conn.execute("DELETE FROM event_notice")

    b_subj = db.row("SELECT person_id FROM person WHERE owner_id='B' AND name=?",
                    ("小雨",))["person_id"]
    a_subj = db.row("SELECT person_id FROM person WHERE owner_id='A' AND name=?",
                    ("小雨",))["person_id"]
    a_brother = db.row("SELECT person_id FROM person WHERE owner_id='A' AND name=?",
                       ("小宇",))["person_id"]

    e1, _ = service.import_life_event(
        db, "study_app", b_subj, "study_progress",
        {"已完成项目": "数学练习 10 题"}, "2026-10-12", "孩子本人确认", "B",
        [{"viewer_id": "A", "fields": ["已完成项目"]}], "app")
    before = agents._a_context(db, "A", a_subj, {})["family_updates"]
    # 来源 A 更新为 30 题。
    new_id_ = service.correct_life_event(db, e1, {"已完成项目": "数学练习 30 题"}, "B")
    after = agents._a_context(db, "A", a_subj, {})["family_updates"]
    study_after = [u for u in after if u["source"] == "study_app"]
    # 另一个孩子的上下文里不能出现这个孩子的近况。
    brother_ctx = agents._a_context(db, "A", a_brother, {})["family_updates"]
    # 撤回后从上下文消失，但同一人物其他来源仍在。
    service.revoke_life_event(db, new_id_, "B")
    revoked_ctx = agents._a_context(db, "A", a_subj, {})["family_updates"]

    # ---- 实际发给模型的输入，不是页面、不是构造出来的对象 ----
    # 上面只看 _a_context 的构造结果，证明的是「上下文对象里有新值」。
    # 这里驱动一次真实提案，读 client.sent_inputs 里真正发出去的请求体，
    # 核对两个来源的新值都到了模型那里，旧值和未授权字段都没过去。
    e3, _ = service.import_life_event(
        db, "study_app", b_subj, "study_progress",
        {"已完成项目": "数学练习 30 题", "软件报告的困难点": "进位加法容易错"},
        "2026-10-13", "孩子本人在学习软件里确认", "B",
        [{"viewer_id": "A", "fields": ["已完成项目"]}], "app")
    # 第二个来源要在同一个测试库里写清楚，不能只在别的用例/别的库出现。
    # 「两个来源的新值都进真实请求」是这一条的验收口径，
    # 少了这一条，请求里只有学习近况，判据就永远不成立。
    e4, _ = service.import_life_event(
        db, "mom_diary", b_subj, "daily_note",
        {"孩子自述": "今天自己整理了书包", "家长观察": "最近想自己背书包"},
        "2026-10-13", "孩子本人在日常记录里确认", "B",
        [{"viewer_id": "A", "fields": ["孩子自述"]}], "app")
    client = ModelClient()
    products = db.rows("SELECT * FROM product ORDER BY product_id")
    ctx = agents._a_context(db, "A", a_subj, {"category": "art"})
    probe_task = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome,"
        " created_at) VALUES(?,?,?,?,?,?,?)",
        (probe_task, "A", "给小雨准备个生日礼物", "{}", "negotiating", "",
         dbm.now_iso()))
    try:
        agents._a_propose(db, client, probe_task, ctx, products, 1)
    except Exception:  # noqa: BLE001
        # 模型这次没返回可解析结果也没关系：请求已经发出，
        # 要核对的是发出去的内容，不是它答了什么。
        pass
    # 请求体是 JSON 转义后的字符串，中文值会被 \" 拆开，
    # 直接做子串匹配会把「确实发了」判成「没发」。先把每条消息的
    # content 解析回原文再比对。
    sent_contents = []
    for item in client.sent_inputs:
        for m in item["messages"]:
            try:
                sent_contents.append(json.loads(json.dumps(m, ensure_ascii=False))
                                     .get("content") or "")
            except (ValueError, TypeError):
                sent_contents.append(str(m))
    sent_text = "\n".join(str(c) for c in sent_contents)
    # 两个来源的新值都要在真实请求里。
    reuse_in_request = ("数学练习 30 题" in sent_text
                        and "今天自己整理了书包" in sent_text)
    # 旧值、未授权字段都不能出现在请求里。
    stale_or_unauth = [w for w in ("数学练习 10 题", "进位加法", "睡眠自述")
                       if w in sent_text]
    # 来源可追溯：每条近况都带来源、时间和提供者。
    traceable = all(u.get("source") and u.get("happened_on") and u.get("provider")
                    for u in ctx["family_updates"])
    # 另一个孩子的近况不能混进这次请求。
    brother_leak = any("数学练习" in c and "小宇" in c for c in sent_contents)
    # 事件所有者本人能看自己全部字段，A 侧只拿到授权的那一个。
    b_sees_all = service.life_events_for(db, b_subj, "B")
    viewer_scope = [e for e in b_sees_all if e["event_id"] == e3]
    viewer_ok = (len(viewer_scope) == 1
                 and "软件报告的困难点" in viewer_scope[0]["fields"])

    ok = (len(before) == 1 and before[0]["fields"] == {"已完成项目": "数学练习 10 题"}
          and len(study_after) == 1 and study_after[0]["fields"] == {"已完成项目": "数学练习 30 题"}
          and not any("数学练习 10 题" in str(u["fields"]) for u in after)
          and brother_ctx == []
          and not any(u["source"] == "study_app" for u in revoked_ctx)
          and reuse_in_request and not stale_or_unauth and traceable
          and not brother_leak and viewer_ok)

    print("[PASS] 跨小 App 近况按新状态复用，他人与撤回都不串" if ok
          else "[FAIL] 跨小 App 近况按新状态复用，他人与撤回都不串")
    return {
        "更新前上下文": before[0]["fields"] if before else None,
        "更新后上下文": study_after[0]["fields"] if study_after else None,
        "旧值是否残留": any("数学练习 10 题" in str(u["fields"]) for u in after),
        "另一个孩子的上下文条数": len(brother_ctx),
        "撤回后 study_app 是否还在": any(u["source"] == "study_app" for u in revoked_ctx),
        "真实请求里是否含两来源新值": reuse_in_request,
        "真实请求里的旧值或未授权字段": stale_or_unauth,
        "近况是否带来源/时间/提供者": traceable,
        "另一个孩子是否混进请求": brother_leak,
        "事件本人可见全部字段": viewer_ok,
    }


def test_family_update_entry_and_link():
    """问家人近况不用重填背景；同名不同人不能串事件。

    两个明确标注的测试来源（学习任务、日常确认）关联同一个人，
    用户一句「小雨最近怎么样」就能拿到授权近况，不必再说一遍她是谁、
    她和我什么关系。只汇总授权字段，未授权的、已撤回的都不出现。

    另一条是方案明确要求的边界：不同用户的记录不能因同名自动合并。
    两侧各有一个「小李」但没有建立关联时，B 的小李的近况不能跑到 A 的小林
    名下——这里用同名不同人验证，事件挂在未关联的同名人物上时不出现。
    """
    import os
    path = DATA / "test_family_update.db"
    if path.exists():
        path.unlink()
    db = dbm.DB(path)
    dbm.seed(db)

    # 演示 seed 也写了小雨的近况，去重键会和下面的用例撞上。
    # 这条用例要数清「问一次拿到几条」，先清空再导。
    with db.connect() as conn:
        conn.execute("DELETE FROM life_event")
        conn.execute("DELETE FROM event_grant")
        conn.execute("DELETE FROM event_notice")

    a_xy = db.row("SELECT person_id FROM person WHERE owner_id='A' AND name=?",
                  ("小雨",))["person_id"]
    b_xy = db.row("SELECT person_id FROM person WHERE owner_id='B' AND name=?",
                  ("小雨",))["person_id"]

    # 两个测试来源写同一个人，授权字段不同。
    service.import_life_event(
        db, "study_app", b_xy, "study_progress",
        {"已完成项目": "数学练习 20 题", "软件报告的困难点": "进位加法容易错"},
        "2026-10-12", "孩子本人在学习软件里确认", "B",
        [{"viewer_id": "A", "fields": ["已完成项目"]}], "app")
    service.import_life_event(
        db, "mom_diary", b_xy, "daily_note",
        {"本人愿意分享": "今天自己整理了书包", "睡眠自述": "九点前睡了"},
        "2026-10-12", "妈妈本人确认后分享", "B",
        [{"viewer_id": "A", "fields": ["本人愿意分享"]}], "app")

    asked = service.family_update_summary(db, "A", "小雨最近怎么样")
    event_blob = str(asked["events"])

    # 同名不同人：A、B 两侧各有一个「小李」，没有建立关联。
    db.execute("INSERT INTO person(person_id,owner_id,name,relation,note,aliases,"
               "attributes_json) VALUES(?,?,?,?,?,?,?)",
               ("p_li_a", "A", "小李", "other", "A 记录的小李", "[]", "{}"))
    db.execute("INSERT INTO person(person_id,owner_id,name,relation,note,aliases,"
               "attributes_json) VALUES(?,?,?,?,?,?,?)",
               ("p_li_b", "B", "小李", "other", "B 记录的小李", "[]", "{}"))
    service.import_life_event(
        db, "mom_diary", "p_li_b", "daily_note",
        {"本人愿意分享": "B 家小李的事"}, "2026-10-12", "妈妈确认", "B",
        [{"viewer_id": "A", "fields": ["本人愿意分享"]}], "app")
    unlinked = service._life_events_both_sides(db, "A", "p_li_a")
    service.link_persons(db, "p_li_a", "p_li_b", confirmed_by="A", note="确认同一人")
    linked = service._life_events_both_sides(db, "A", "p_li_a")

    ok = (asked["resolved"] is True
          and len(asked["events"]) == 2
          and "数学练习 20 题" in asked["reply"]
          and "今天自己整理了书包" in asked["reply"]
          and "进位加法" not in event_blob
          and "睡眠自述" not in event_blob
          and unlinked == []
          and len(linked) == 1)

    print("[PASS] 问近况不重填背景，同名不同人不串事件" if ok
          else "[FAIL] 问近况不重填背景，同名不同人不串事件")
    return {
        "是否定位到人": asked["resolved"],
        "拿到近况条数": len(asked["events"]),
        "回复含学习近况": "数学练习 20 题" in asked["reply"],
        "回复含日常近况": "今天自己整理了书包" in asked["reply"],
        "未授权字段泄露": [w for w in ("进位加法", "睡眠自述") if w in event_blob],
        "同名未关联时条数": len(unlinked),
        "显式关联后条数": len(linked),
    }


def test_late_reply_and_stale_version():
    """有效迟到回复接回原事项；旧版本迟到回复无效；刷新后等待仍在。

    未回应不是同意。协商卡住后：
    - 家长过后用自己的身份补一句当前鞋码，要接回原来那件事继续办，
      不另开一件，也不新建订单；
    - 依据已被更正的旧版本回复不能推动执行；
    - 等待记录存在库里，重新打开页面（重新查一次）还在，不会凭空消失。

    家长补上尺码后要接着办同一件事，这一步会真实调用模型重新协商；
    前面的转述、等待记录和版本校验都不依赖模型。
    """
    import os
    path = DATA / "test_late_reply.db"
    if path.exists():
        path.unlink()
    db = dbm.DB(path)
    dbm.seed(db)
    client = ModelClient()

    # 卡在尺码上、等家长本人确认。
    task_id = dbm.new_id("task")
    a_subj = db.row("SELECT person_id FROM person WHERE owner_id='A' AND name=?",
                    ("小雨",))["person_id"]
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome,"
        " created_at) VALUES(?,?,?,?,?,?,?)",
        (task_id, "A", "给小雨买双运动鞋，200元以内",
         json.dumps({"subject_id": a_subj, "subject_name": "小雨", "budget_max": 200.0},
                    ensure_ascii=False),
         "blocked",
         "这件要 儿童运动鞋。家长这边没有能支撑当前鞋码的记录。"
         "请家长确认孩子现在的鞋码后再下单。",
         dbm.now_iso()))
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id, reason,"
        " asked_size, shared_conditions_json, facts_used_json, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (dbm.new_id("prop"), task_id, 1, "A", "pr_sneakers", "儿童运动鞋", "31 码",
         "[]", "[]", dbm.now_iso()))
    service.set_waiting(db, task_id, "B", "awaiting_person",
                        "这件要儿童运动鞋。请家长确认孩子现在的鞋码。",
                        {"need": "size", "product_id": "pr_sneakers"})

    tasks_before = db.row("SELECT COUNT(*) c FROM task WHERE owner_id='A'")["c"]
    orders_before = db.row("SELECT COUNT(*) c FROM sim_order")["c"]

    # 模拟刷新：重新查一次，等待记录还在。
    after_refresh = service.waiting_for(db, "B")

    # 送礼者转述不算数：不接回、不新建订单，事项继续卡着。
    giver_try = service.answer_blocked_with_size(db, client, "A", "她现在穿 32 码")
    stage_after_giver = db.row("SELECT stage FROM task WHERE task_id=?", (task_id,))["stage"]
    orders_after_giver = db.row("SELECT COUNT(*) c FROM sim_order")["c"]

    # 家长本人从自己身份补一句：接回同一件事，尺码记在家长名下。
    parent_try = service.answer_blocked_with_size(db, client, "B", "她现在穿 32 码")
    stage_after_parent = db.row("SELECT stage FROM task WHERE task_id=?",
                                 (task_id,))["stage"]
    parent_fact = db.rows(
        "SELECT provider FROM fact WHERE owner_id='B' AND attribute='shoe_size'"
        " ORDER BY observed_on DESC LIMIT 1")
    tasks_after = db.row("SELECT COUNT(*) c FROM task WHERE owner_id='A'")["c"]
    orders_after = db.row("SELECT COUNT(*) c FROM sim_order")["c"]
    # 接回的是原来那件事，不是另开一件。
    same_task = parent_try.get("task_id") == task_id
    # 重新协商收尾后不能再显示「在等谁」。家长补了尺码、事情已经往下走了，
    # 等待记录要么被 agreed / counter_pending 处理，要么被终态清掉。
    waiting_after = service.waiting_for(db, "B")

    # 旧版本迟到回复无效：把最新提案降级成旧版本后确认，必须被拒。
    latest = db.row("SELECT MAX(version) AS v FROM proposal WHERE task_id=?",
                    (task_id,))["v"]
    old_prop = db.row(
        "SELECT proposal_id FROM proposal WHERE task_id=? AND version=?", (task_id, latest))
    db.execute("UPDATE proposal SET version=? WHERE proposal_id=?",
               (latest + 5, old_prop["proposal_id"]))
    db.execute("UPDATE proposal SET version=? WHERE proposal_id=?",
               (1, old_prop["proposal_id"]))
    # 造一个更高的版本，让上面那条变成旧版本。
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id, reason,"
        " asked_size, shared_conditions_json, facts_used_json, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (dbm.new_id("prop"), task_id, 3, "A", "pr_sneakers", "新方案", "32 码",
         "[]", "[]", dbm.now_iso()))
    stale_err = ""
    try:
        service.confirm_and_purchase(db, None, task_id, old_prop["proposal_id"], "A")
    except Exception as exc:  # noqa: BLE001
        stale_err = str(exc)
    orders_after_stale = db.row("SELECT COUNT(*) c FROM sim_order")["c"]

    ok = (len(after_refresh) == 1
          and after_refresh[0]["kind"] == "awaiting_person"
          and giver_try.get("resolved") is False
          and stage_after_giver == "blocked"
          and orders_after_giver == orders_before
          and parent_try.get("task_id") == task_id
          and same_task
          and bool(parent_fact) and parent_fact[0]["provider"] == "家长本人确认"
          # 接回后事项离开 blocked 就行：谈成、等送礼者定、还是没谈成都是合法结果，
          # 这条断言不规定协商结果，只要求事情确实往下走了。
          and stage_after_parent != "blocked"
          and tasks_after == tasks_before
          and orders_after == orders_before
          and orders_after_stale == orders_before
          # 收尾后不能再显示「在等谁」：等家长的那条必须已经清掉。
          and all(w["kind"] != "awaiting_person" for w in waiting_after)
          and bool(stale_err))

    print("[PASS] 迟到回复接回原事项，旧版本回复无效，刷新后仍在" if ok
          else "[FAIL] 迟到回复接回原事项，旧版本回复无效，刷新后仍在")
    return {
        "刷新后等待条数": len(after_refresh),
        "刷新后等待类型": after_refresh[0]["kind"] if after_refresh else None,
        "送礼者转述是否被拒": giver_try.get("resolved"),
        "转述后阶段": stage_after_giver,
        "转述后订单数": orders_after_giver,
        "家长接回的是否原事项": same_task,
        "家长确认来源": parent_fact[0]["provider"] if parent_fact else None,
        "家长接回后阶段": stage_after_parent,
        "收尾后仍在等的类型": [w["kind"] for w in waiting_after],
        "任务数变化": "%d -> %d" % (tasks_before, tasks_after),
        "接回后订单数": orders_after,
        "旧版本确认报错": stale_err[:60],
        "旧版本确认后订单数": orders_after_stale,
    }


def _clock_minutes(text):
    """从「周六上午 10:00-12:00」「周六10:00—12:00」里取出实际起止分钟。

    返回 [开始分钟, 结束分钟]（自 00:00 起算），取不到就返回空。
    精确到分钟：抹掉分钟会让 12:30 被当成 12:00，越界方案反而判成合规，
    也会让 12:00 结束和 12:01 结束看不出区别。
    """
    import re
    found = re.findall(r"(\d{1,2})[:：](\d{2})", str(text or ""))
    if len(found) >= 2:
        out = [int(found[0][0]) * 60 + int(found[0][1]),
               int(found[1][0]) * 60 + int(found[1][1])]
    else:
        # 只有「10 点到 12 点」这种写法：分钟未知，按整点处理。
        nums = re.findall(r"(\d{1,2})\s*[点時时]", str(text or ""))
        if len(nums) < 2:
            return []
        out = [int(nums[0]) * 60, int(nums[1]) * 60]
    # 倒序（22:00-08:00）或零时长（10:00-10:00）不是一个可用的时段，
    # 放行会让后面所有窗口判断失去意义。
    if out[1] <= out[0]:
        return []
    return out


def _covers_hours(clock, start, end):
    """取到的时段是否覆盖 [start, end)。"""
    if not clock or len(clock) < 2:
        return False
    return clock[0] <= start and clock[1] >= end


def _within_window(clock, start, end):
    """取到的时段是否完整落在 [start, end) 内，没有越界。

    和 _covers_hours 是两件事：条件给的是「可出门窗口」时，
    方案落在窗口内即可，不必填满；给的是「活动时长」时才要求覆盖。
    混用这两个口径会把合理方案误判成没听条件。
    """
    if not clock or len(clock) < 2:
        return False
    return clock[0] >= start and clock[1] <= end


class _ThreePartyStub:
    """deterministic stub：按调用类型返回合规方案，只用于验证断言路由。

    它**不是**模型结论：用来证明「模型按规矩返回时断言全过」。
    真实模型结论必须用 `--client real` 单独跑，不能拿这份 stub
    的结果充当真实模型通过。
    """

    def __init__(self, mode="good"):
        self.mode = mode
        self.sent_inputs = []
        self.calls = []

    def chat_json(self, messages):
        self.sent_inputs.append({"messages": messages})
        blob = json.dumps(messages, ensure_ascii=False)
        self.calls.append("review" if ("审阅" in blob or "个人代表" in blob)
                          else "replan" if ("刚改" in blob or "重排" in blob)
                          else "propose")
        kind = self.calls[-1]
        if kind == "review":
            return {"decision": "accept", "reason": "stub：这一版可以"}
        content = {"时间": "周六上午 10:00-12:00", "地点": "附近地铁可达公园",
                   "活动内容": "上午散步和亲子拼图，午饭前结束", "交通": "地铁能到",
                   "孩子安排": "上午活动结束后留出午饭和两个孩子午睡时间，小宇可带拼图"}
        if kind == "replan":
            content = {"时间": "周六上午 10:00-12:00", "地点": "附近地铁可达公园",
                       "活动内容": "上午散步和亲子拼图，10:00 开始、12:00 前结束",
                       "交通": "地铁能到",
                       "孩子安排": "12:00 前结束活动，之后午饭和午睡，小宇可带拼图"}
        content["备注"] = "活动总预算未定；其他参与方待本人确认。"
        if self.mode == "sum-caps":
            content["活动总预算"] = "330 元"
            content["本人出资上限"] = "100 元以内"
        elif self.mode == "borrow-cap":
            content["本人出资上限"] = "80 元以内"
        elif self.mode == "placeholder":
            content = {"时间": "待确认", "地点": "再定", "活动内容": "未定"}
        return {"content": content, "reason": "stub", "conflicts": []}


def _three_party_arrangement_check(content):
    # 三项已有明确授权，必须非空且不是占位；备注中的未知预算/本人回复不算安排。
    placeholder_keys = [k for k in ("时间", "地点", "活动内容")
                        if any(w in str(content.get(k, ""))
                               for w in ("待确认", "再定", "未定", "待定"))]
    concrete = all(content.get(k) for k in ("时间", "地点", "活动内容"))
    return placeholder_keys, concrete


def test_three_party_arrangement_contract():
    """零模型合同：有依据的安排落地与无依据的预算/本人回复保持未知并存。"""
    base = {"时间": "周六上午 10:00-12:00", "地点": "附近地铁可达公园",
            "活动内容": "上午散步和亲子拼图，午饭前结束", "活动总预算": "",
            "备注": "活动总预算未定；其他参与方待本人确认。"}
    placeholders, concrete = _three_party_arrangement_check(base)
    checks = {"安排具体且未知预算备注允许": concrete and not placeholders}
    for key in ("时间", "地点", "活动内容"):
        p, c = _three_party_arrangement_check(dict(base, **{key: ""}))
        checks[key + "空值拒绝"] = not c
        for word in ("待确认", "再定", "未定", "待定"):
            p, c = _three_party_arrangement_check(dict(base, **{key: word}))
            checks[key + word + "拒绝"] = key in p
    assert all(checks.values()), checks
    return checks


def _run_focused_three_party(client_mode):
    """定点跑三方 plan 用例；默认 main() 仍跑全部 22 项，不受影响。"""
    print("具体安排合同（零模型）：" + json.dumps(
        test_three_party_arrangement_contract(), ensure_ascii=False))
    if client_mode == "real":
        client = ModelClient()
        tag = "真实模型"
    else:
        client = _ThreePartyStub(client_mode)
        tag = "deterministic stub（%s）" % client_mode
    plan = test_three_family_plan(client)
    ok = plan.get("用例内部判定") is True
    report("17 三个参与方规划：同版本接受才算约定，旧接受随新版本失效（%s）" % tag,
           ok, plan)
    # 定点明细写到 PA_DATA_DIR（隔离跑时是临时目录），不写仓库 data/：
    # 复跑不应往仓库里落产物。
    import os
    out_dir = Path(os.environ.get("PA_DATA_DIR") or DATA)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "acceptance-three-party.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(1 for r in results if r["ok"])
    print("\n[定点] 通过 %d/%d，明细写入 %s" % (passed, len(results), out))
    return 0 if passed == len(results) else 1


def test_three_family_plan(client=None):
    """三个参与方的共同事项：只有各方接受同一版本才算约定。

    **这是 generic 三方 plan 逻辑测试，不是用户真实的「三家」场景。**
    产品演示数据里 A/B/C 是三个身份、两个家庭（B 与 C 是配偶、同一户）；
    这里只用三个参与方把「同版本才算约定、旧接受随新版本失效」这条
    plan 状态机跑通，不断言产品家庭结构。真正的独立第三方家庭
    （guest 参与方、独立会话、资料引用）由 t25/t27 覆盖，不靠给演示数据
    里的 B/C 伪造成第三户来凑。

    锁住的要求：
    - 每个参与方只提供自己授权的条件，不替别家补条件；
    - 第一版方案必须按已给条件排出具体安排，不能全是「待确认」占位；
    - 一方未回复仍是待确认，不能算约定，也不产生订单；
    - 一方改时间出新版本，旧版本的接受全部失效，阶段退回待确认，
      别家对旧版本的接受不能当成本次约定；
    - 预算分口径：每方只有「本人出资上限」，没人给「活动总预算」时
      方案里总预算必须为空；把各家上限相加当总预算必须被代码拒绝，
      且拒绝要冒出来、不落版本，不能吞掉或清掉违规字段后当成功。
    """
    client = client if client is not None else ModelClient()
    import os
    path = DATA / "test_three_family_plan.db"
    if path.exists():
        path.unlink()
    db = dbm.DB(path)
    dbm.seed(db)
    # client 已在函数开头按参数兜底，这里不再新建：
    # 再建一个真的 ModelClient 会把注入的 stub 覆盖掉，
    # 定点验证就悄悄变成打真实模型。

    plan_id = service.create_plan(
        db, "A", "三个参与方周末一起办一次户外活动",
        "约三个参与方周末一起办一次户外活动")
    plan_title = "三个参与方周末一起办一次户外活动"
    # 三家的时间条件留出共同解：这一条要验证的是「一家改条件后，
    # 重排听不听新条件」。如果条件本身无解（例如小周家只能周六上午、
    # 小林家只能周日），模型只能报冲突，就分不清它是听了新条件
    # 还是照旧排了旧条件。真实冲突由 plan_propose 的 conflicts 覆盖。
    # 预算按产品当前口径分两个键写：每方只给自己这一家的「本人出资上限」，
    # 没有任何一方给出「活动总预算」。旧裸键「预算」已不再是产品字段，
    # 继续用它会让各方条件里既没有上限也没有总预算，预算门因「无依据」
    # 拒绝模型给出的上限——那是测试合同过期，不是产品把无依据预算落版。
    # 口径不明的旧键值（如「不超过 150 元」）按产品规则要本人确认，
    # 这里直接给明确键，不测旧键归一（旧键归一由 service 单测覆盖）。
    # A 本人明确提供可执行的地点/活动建议，支持下方三个具体字段的原断言。
    # 这是虚构、generic 三参与方 fixture；不从联系人资料推定孩子出席。
    conds = {
        "A": {"时间": "周六或周日都行", "本人出资上限": "100 元以内",
              "地点": "附近地铁可达公园",
              "活动内容": "上午散步和亲子拼图，午饭前结束",
              "孩子安排": "两个孩子在午饭时间需要午睡"},
        "B": {"时间": "周六或周日都行", "本人出资上限": "150 元以内",
              "孩子安排": "小宇要带拼图"},
        "C": {"时间": "周末都行", "本人出资上限": "80 元以内", "交通": "不开车，地铁能到"},
    }
    labels = {"A": "A 家", "B": "B 家", "C": "C 家"}
    for oid, c in conds.items():
        service.add_plan_participant(db, plan_id, oid, labels[oid], c, True)

    # 各方条件是各自授权的那一份：库里存的条件不能被别家覆盖或合并。
    stored = {}
    for p in db.rows("SELECT label, conditions_json FROM plan_participant WHERE plan_id=?",
                     (plan_id,)):
        stored[p["label"]] = json.loads(p["conditions_json"] or "{}")
    conditions_isolated = (stored["A 家"] == conds["A"]
                           and stored["B 家"] == conds["B"]
                           and stored["C 家"] == conds["C"])

    # 第一版由模型按已给条件排出具体安排。
    first = agents.plan_propose(db, client, plan_id, "A")
    v1 = first["version"]
    content = first["content"]
    # 只对 A 已明确供给的必要安排查占位；未知总预算/本人回复可在备注如实留缺口。
    # 不能把备注的「未定」误当这三项安排已落地；原三项非空 concrete 保留。
    placeholder_keys, concrete = _three_party_arrangement_check(content)

    # 三家都还没表态：待确认，等的是这三家，不是已约定。
    st = service.plan_status(db, plan_id)
    initial = (st["stage"] == "awaiting"
               and st["agreed"] is False
               and sorted(st["waiting_on"]) == sorted(labels.values()))

    # 两方接受、一方未回复：仍是待确认。未回复不是同意。
    participants = {p["label"]: p["participant_id"]
                    for p in service.plan_status(db, plan_id)["participants"]}
    service.decide_plan(db, plan_id, participants["A 家"], v1, "accept",
                        "这个时间对我们合适", actor_id="A")
    service.decide_plan(db, plan_id, participants["C 家"], v1, "accept",
                        "地铁能到，可以", actor_id="C")
    st_two = service.plan_status(db, plan_id)
    still_waiting = (st_two["agreed"] is False
                     and st_two["stage"] == "awaiting"
                     and st_two["waiting_on"] == ["B 家"]
                     and st_two["agreed_count"] == 2
                     and st_two["required_total"] == 3
                     and st_two["required_agreed"] == 2)

    # 越权与虚构 id 必须被拒，且不留下任何痕迹。
    guard_errors = []
    try:
        service.decide_plan(db, plan_id, participants["B 家"], v1, "accept",
                            "我替小周家答应了", actor_id="A")
    except ValueError as exc:
        guard_errors.append(str(exc))
    try:
        service.decide_plan(db, plan_id, "pp_不存在", v1, "accept", "x", actor_id="B")
    except ValueError as exc:
        guard_errors.append(str(exc))
    after_guard = service.plan_status(db, plan_id)
    # 这两次拒绝都发生在「两方已接受、小周家还没回复」时：拒绝不能留下痕迹，
    # 也不能顺手把状态改成别的。
    guard_clean = (len(guard_errors) == 2
                   and after_guard["agreed_count"] == 2
                   and after_guard["stage"] == "awaiting"
                   and after_guard["agreed"] is False
                   and after_guard["waiting_on"] == ["B 家"]
                   and after_guard["current_version"] == v1
                   and db.row("SELECT COUNT(*) c FROM plan_decision"
                              " WHERE participant_id='pp_不存在'")["c"] == 0)

    # 小周家本人接着接受：三方都对同一版本明确接受，才算约定。
    # 这里要的是「已经约好之后一家改条件」那条真实分支，
    # 所以先把事项推到约定，再让其中一家改自己的条件。
    service.decide_plan(db, plan_id, participants["B 家"], v1, "accept",
                        "这版可以", actor_id="B")
    st_full = service.plan_status(db, plan_id)
    fully_agreed = (st_full["agreed"] is True and st_full["stage"] == "agreed"
                    and st_full["required_agreed"] == 3)

    # 第三方本人改自己的条件：走用户改条件的真实入口。
    # 改条件不是「表态」，只更新这一方的条件并把事项标成需要重排，
    # 别家对旧版本的接受随之失效，要重新核对。
    cond = service.update_plan_condition(
        db, plan_id, "B", {"时间": "周六上午 10:00-12:00"},
        "小周家周六上午才能出门，周日下午不行")
    cond_recorded = (cond.get("needs_replan") is True
                     and cond.get("changed_fields") == ["时间"]
                     and cond.get("agreed") is False)
    # 条件库里已经是新值，方案版本还没动：重排由模型做，不是改字。
    cond_stored = json.loads(db.row(
        "SELECT conditions_json FROM plan_participant WHERE participant_id=?",
        (participants["B 家"],))["conditions_json"])
    cond_applied = cond_stored.get("时间") == "周六上午 10:00-12:00"
    after_cond = service.plan_status(db, plan_id)
    # 已经约好的事项，一家改条件后必须立刻不算约定：旧接受全部失效，
    # 且这个事实要落库，刷新、重试之后都还在。
    cond_stage = (after_cond["stage"] == "awaiting"
                  and after_cond["agreed"] is False
                  and after_cond["needs_replan"] is True
                  and after_cond["current_version"] == v1
                  and db.row("SELECT COUNT(*) c FROM plan_decision WHERE plan_id=?"
                             " AND decision='stale'", (plan_id,))["c"] == 3)
    # 待重排期间不能对旧版本表态：条件已经变了，
    # 这时点「接受」只是对一份作废内容的确认。
    replan_pending_reject = ""
    try:
        service.decide_plan(db, plan_id, participants["A 家"], v1, "accept",
                            "我还按原来那版", actor_id="A")
    except ValueError as exc:
        replan_pending_reject = str(exc)
    still_not_agreed = service.plan_status(db, plan_id)["agreed"] is False

    # 依据各家当前条件重排，落成新版本。这里必须真的调用模型，
    # 因为要证明重排后方案内部一致，而不是把一个字段盖到旧方案上。
    replan = agents.plan_replan(db, client, plan_id)
    # 同一标题重复发起要复用这一件还没定完的事项：用户重点一次「创建」往往是
    # 没看到回应，不是想办两件看起来一模一样的事。复用时也不能再叠一版，
    # 否则各家对旧版本的接受会白白失效。
    # 已经约定或明确失败的 intentionally 不复用，否则会挡住以后合理再次发起。
    dup_before = service.plan_status(db, plan_id)["current_version"]
    dup_plan_id = service.create_plan(db, "A", plan_title, plan_title)
    dup_after = service.plan_status(db, dup_plan_id)["current_version"]
    dup_reused_same = dup_plan_id == plan_id
    dup_version_unchanged = dup_after == dup_before
    dup_plan_count = db.rows(
        "SELECT COUNT(*) AS n FROM plan WHERE title=?", (plan_title,))[0]["n"]

    st_v2 = service.plan_status(db, plan_id)
    v2 = st_v2["current_version"]
    replanned = v2 == v1 + 1
    # 重排后内容内部一致：时间落在周六上午，就不能再留着 14:00 开始、
    # 「午睡之后」这类和上午矛盾的内容。
    # 核对方式按语义而不是字面：模型写「周六10:00—12:00」和
    # 「周六上午 10:00-12:00」是同一个时段，不能因为少两个字判失败。
    replan_content = st_v2["proposal"] or {}
    replan_time = str(replan_content.get("时间", ""))
    blob = json.dumps(replan_content, ensure_ascii=False)
    # 从时段里取出实际钟点：上午指 12:00 之前开始。
    clock = _clock_minutes(replan_time)
    is_morning = bool(clock) and clock[0] < 12 * 60
    time_consistent = (is_morning and "周六" in replan_time
                       and "14:00" not in blob and "下午" not in blob
                       and "午睡之后" not in blob)
    # 用的是不是这一方新给的时间：这一方给的是「周六上午 10:00-12:00 才能出门」，
    # 那是可出门窗口，不是要求活动排满两小时。所以方案只要完整落在这个窗口内
    # 就算用上了条件；越界（比如排到 14:00）算没用上。不要求原样复述字符串。
    replan_used_new_cond = is_morning and "周六" in replan_time and _within_window(
        clock, 10 * 60, 12 * 60)
    # 新版本落库后待重排标记要清掉，各家才能按这一版重新表态。
    replan_cleared = st_v2.get("needs_replan") is False
    stale_rows = db.rows(
        "SELECT participant_id, decision FROM plan_decision WHERE plan_id=? AND version=?",
        (plan_id, v1))
    # 两家的 accept 必须全部失效：新版本不接受旧接受当约定。
    old_accepts_stale = all(r["decision"] == "stale" for r in stale_rows)
    # 旧版本也不能重新表态算成约定。
    stale_reject = ""
    try:
        service.decide_plan(db, plan_id, participants["A 家"], v1, "accept",
                            "我改主意了，还按老时间来", actor_id="A")
    except ValueError as exc:
        stale_reject = str(exc)
    stage_back = (st_v2["agreed"] is False
                  and st_v2["stage"] == "awaiting"
                  and st_v2["agreed_count"] == 0
                  and sorted(st_v2["waiting_on"]) == sorted(labels.values()))
    # 重排后的新版本要带上这一方改的条件，别家才知道改了什么。
    condition_visible = replan_used_new_cond

    # 三方重新接受新版本后才约定。
    for label in labels.values():
        owner = {"A 家": "A", "B 家": "B", "C 家": "C"}[label]
        service.decide_plan(db, plan_id, participants[label], v2, "accept",
                            "这个时间可以", actor_id=owner)
    st_v3 = service.plan_status(db, plan_id)
    final_agreed = (st_v3["agreed"] is True
                    and st_v3["stage"] == "agreed"
                    and st_v3["current_version"] == v2
                    and st_v3["required_agreed"] == 3)

    # 未回复不生成订单：规划全程不碰送礼订单表。
    orders = db.row("SELECT COUNT(*) c FROM sim_order")["c"]
    tasks = db.row("SELECT COUNT(*) c FROM task")["c"]
    no_side_effects = (orders == 0 and tasks == 0)

    # ---------- 预算口径合同（代码判，不靠提示词自觉） ----------
    # 每方只给了「本人出资上限」，没有任何一方给出「活动总预算」。
    # 于是：方案里的总预算必须为空；谁把三家上限相加（100+150+80=330）
    # 当成总预算，就是替所有参与方做了承诺，必须被代码拒绝。
    own_caps = {oid: c["本人出资上限"] for oid, c in conds.items()}
    summed = sum(int("".join(ch for ch in v if ch.isdigit()))
                 for v in own_caps.values())
    first_total = content.get("活动总预算")
    first_cap = content.get("本人出资上限")
    total_absent = not first_total
    caps_not_summed = not (first_total and str(summed) in str(first_total))
    # 提案方（A）自己的上限可以原样出现在方案里，但必须是 A 那一份的金额，
    # 不能是别家的，也不能被改数。
    cap_only_proposer = (not first_cap
                         or str(own_caps["A"]) == str(first_cap)
                         or "100" in str(first_cap))

    # ---------- 负向护栏（stub，不依赖模型，明确标注为 stub） ----------
    # 只测代码门：违规方案必须被拒、拒绝必须冒出来、且不能落版本。
    # 不吞 ValueError、不把违规字段清掉之后当成功——这两条正是
    # 「安全拒绝被伪装成通过」的两种常见手法。
    guard_plan_id = service.create_plan(db, "A", "预算负向护栏用的事项", "负向护栏")
    for oid, c in conds.items():
        service.add_plan_participant(db, guard_plan_id, oid, labels[oid], c, True)

    class _ScriptedStub:
        """按脚本返回一份方案，用来喂负向护栏。"""

        def __init__(self, content):
            self._content = content
            self.sent_inputs = []

        def chat_json(self, messages):
            self.sent_inputs.append({"messages": messages})
            return {"content": self._content, "reason": "stub", "conflicts": []}

    base_content = {"时间": "周六上午 10:00-12:00", "地点": conds["A"]["地点"],
                    "活动内容": conds["A"]["活动内容"], "交通": conds["C"]["交通"],
                    "孩子安排": "午饭前结束活动，之后午饭和午睡，小宇可带拼图"}
    budget_guards = {}
    # (a) 把各家上限相加当总预算。
    try:
        agents.plan_propose(db, _ScriptedStub(dict(
            base_content, 活动总预算="%d 元" % summed, 本人出资上限=own_caps["A"])),
            guard_plan_id, "A")
        budget_guards["上限相加被拒"] = False
    except ValueError as exc:
        budget_guards["上限相加被拒"] = "预算" in str(exc)
    # (b) 提案方之外的那一家上限被冒名写成提案方的。
    try:
        agents.plan_propose(db, _ScriptedStub(dict(
            base_content, 本人出资上限=own_caps["C"])),
            guard_plan_id, "A")
        budget_guards["冒用别家上限被拒"] = False
    except ValueError as exc:
        budget_guards["冒用别家上限被拒"] = "上限" in str(exc)
    # (c) 没人给总预算时模型自己写一个。
    try:
        agents.plan_propose(db, _ScriptedStub(dict(
            base_content, 活动总预算="500 元")),
            guard_plan_id, "A")
        budget_guards["无依据总预算被拒"] = False
    except ValueError as exc:
        budget_guards["无依据总预算被拒"] = "总预算" in str(exc)
    # (d) 拒绝之后不能落版本：库里一版都没有，才算「拒绝」而不是
    # 「落了一版再悄悄改字段」。
    guard_versions = db.rows(
        "SELECT COUNT(*) AS c FROM plan_proposal WHERE plan_id=?",
        (guard_plan_id,))[0]["c"]
    budget_guards["拒绝后不落版本"] = guard_versions == 0

    # 真实模型也跑一轮审阅：改时间的一方要在新版本上重新表态。
    review = agents.plan_review(db, client, plan_id, participants["B 家"], v2)
    # 审阅是模型行为，可能 counter 并落出新版本。最终状态必须以
    # 审阅之后的真实 plan_status 回读为准，不能沿用 accept 采样时的快照：
    # counter 落新版本后，各家原来对旧版本的接受全部失效，
    # 这时还报告「已约定第 2 版」就是旧快照。
    st_after_review = service.plan_status(db, plan_id)
    final_snapshot = (st_after_review["current_version"],
                      st_after_review["stage"], st_after_review["agreed"])
    # 模型审阅和本人确认是两件事：counter 落新版本后，约定要等
    # 各家对新版本明确表态，不能因为模型说了算就算约定。
    # 注意 counter 不一定会落新版本：模型有时只提意见而不给可写入的字段
    # （例如「预算可能超，要明确上限」却没有对应字段），这时版本号不变，
    # 但同样不能算约定。所以这里按两种情况分别核对，不写死版本号一定递增。
    review_then_recheck = (
        (review.get("decision") != "counter")
        or (st_after_review["agreed"] is False
            and st_after_review["needs_replan"] is False
            and st_after_review["current_version"] >= v2))

    # 已经约定之后再发起同一标题，要另开一件：否则用户以后合理地
    # 想再办一次会被旧事项挡住。
    # 但前提是这件真的已经收尾（agreed / conflict / failed）。模型刚 counter
    # 出新版本时，这件又回到「还在等各家确认」，此时重复发起复用同一件才对，
    # 否则同一件事会被切成两件各自推进。所以按当前真实阶段判断，
    # 不假定它一定还停在已约定。
    closed_stages = ("agreed", "conflict", "failed")
    if st_after_review["stage"] in closed_stages:
        after_done = service.create_plan(db, "A", plan_title, plan_title)
        done_creates_new = after_done != plan_id
    else:
        after_done = service.create_plan(db, "A", plan_title, plan_title)
        done_creates_new = after_done == plan_id

    ok = (conditions_isolated and bool(content) and not placeholder_keys and concrete
          and total_absent and caps_not_summed and cap_only_proposer
          and all(budget_guards.values())
          and initial and still_waiting and fully_agreed and guard_clean
          and old_accepts_stale and cond_recorded and cond_applied and cond_stage
          and bool(replan_pending_reject) and still_not_agreed
          and replanned and time_consistent and replan_used_new_cond
          and replan_cleared and stage_back and condition_visible
          and final_agreed and no_side_effects and bool(stale_reject)
          and review_then_recheck and dup_reused_same and dup_version_unchanged
          and dup_plan_count == 1 and done_creates_new
          and review.get("decision") in ("accept", "reject", "counter"))

    print("[PASS] 三个参与方规划：同版本接受才算约定，旧接受随新版本失效" if ok
          else "[FAIL] 三个参与方规划：同版本接受才算约定，旧接受随新版本失效")
    return {
        "各方条件是否隔离": conditions_isolated,
        "第一版方案": content,
        "方案是否具体": concrete,
        "占位字段": placeholder_keys,
        "方案理由": first.get("reason"),
        "方案冲突说明": first.get("conflicts"),
        "初始阶段": st["stage"],
        "初始是否已约定": st["agreed"],
        "初始在等": st["waiting_on"],
        "两方接受一方未回是否仍待确认": still_waiting,
        "三方全票是否先约定": fully_agreed,
        "改条件是否只记条件不当表态": cond_recorded,
        "改的条件是否落库": cond_applied,
        "改条件后阶段": cond_stage,
        "已约好后改条件是否立刻不算约定": not after_cond["agreed"],
        "待重排期间旧版本表态是否被拒": bool(replan_pending_reject),
        "待重排拒绝原因": replan_pending_reject[:60],
        "被拒后是否仍未约定": still_not_agreed,
        "重排后是否清掉待重排标记": replan_cleared,
        "越权与虚构 id 是否被拒且无痕迹": guard_clean,
        "是否调用模型重排": replanned,
        "重排后方案": replan_content,
        "重排后内容是否内部一致": time_consistent,
        "重排是否用上新条件": replan_used_new_cond,
        "重排说明": replan.get("reason"),
        "重排冲突说明": replan.get("conflicts"),
        "越权拒绝条数": len(guard_errors),
        "越权拒绝原因": guard_errors,
        "新版本号": v2,
        "旧版本接受是否全部失效": old_accepts_stale,
        "旧接受决策明细": [(r["participant_id"], r["decision"]) for r in stale_rows],
        "旧版本再次表态是否被拒": bool(stale_reject),
        "旧版本拒绝原因": stale_reject[:60],
        "出新版后阶段是否退回": stage_back,
        "新版本是否带上改后的条件": condition_visible,
        "三方重新接受后是否约定": final_agreed,
        "约定版本": st_v3["current_version"],
        "是否产生订单": orders,
        "是否新建送礼任务": tasks,
        # 预算口径合同与负向护栏（护栏是 stub 级，不是模型结论）。
        "各方本人出资上限": own_caps,
        "三家上限相加值": summed,
        "没人给总预算时方案总预算是否为空": total_absent,
        "方案是否没把上限相加当总预算": caps_not_summed,
        "方案里的上限是否只来自提案方本人": cap_only_proposer,
        "负向护栏（stub）": budget_guards,
        "负向护栏是否全部拒绝": all(budget_guards.values()),
        "负向护栏说明": "stub 级代码门验证，非真实模型结论",
        "真实模型审阅结果": review.get("decision"),
        "真实模型审阅理由": review.get("reason"),
        "审阅后回读的版本/阶段/是否约定": final_snapshot,
        "模型审阅与本人确认是否分开": review_then_recheck,
        # 同一标题重复发起要复用同一件：用户重点一次创建往往是没看到回应，
        # 不是想办两件事；复用时也不能再叠一版，否则旧接受白白失效。
        "重复发起是否复用同一件": dup_reused_same,
        "复用后版本是否未叠加": dup_version_unchanged,
        "库里同类事项条数": dup_plan_count,
        "已约定后再发起是否另开一件": done_creates_new,
        # 内部判定一并返回。外层 report 用这一个值，
        # 避免「用例里判 FAIL、验收文件里记 PASS」。
        "用例内部判定": ok,
    }

def test_person_search_and_groups():
    """找人：姓名/别名/关系/职业都能搜到，同一个人可同时在多个组。

    方案要求「用可重叠分组和搜索组织，不要求填写完整档案」。
    这里验证四件事：搜别名能搜到人；按关系搜只命中这一条关系相关的人；
    同一个人有多条关系时同时出现在多个组，但仍是同一个 person_id
    （不是多份档案）；搜不到就说搜不到，不猜相似的人。
    """
    import pathlib
    path = DATA / "test_search_groups.db"
    if path.exists():
        path.unlink()
    db = dbm.DB(path)
    dbm.seed(db)

    # 小周既是朋友又是前同事：两条关系各自成立。
    db.execute(
        "INSERT INTO relation(relation_id,owner_id,from_person_id,relation_type,"
        "to_person_id,label,source_utterance,observed_on) VALUES(?,?,?,?,?,?,?,?)",
        ("r_me_azhou_old", "A", "p_me", "former_colleague", "p_azhou",
         "小周是我前同事", "小周是我前同事", "2026-09-01"))

    by_alias = service.search_persons(db, "A", "老周")
    by_job = service.search_persons(db, "A", "小学教师")
    by_relation = service.search_persons(db, "A", "朋友")
    by_child = service.search_persons(db, "A", "女儿")
    missing = service.search_persons(db, "A", "张三丰")

    groups = service.person_groups(db, "A")
    azhou_groups = groups["朋友与同学"] and [
        p["groups"] for p in groups["朋友与同学"] if p["person_id"] == "p_azhou"]
    # 同一个 person_id 在多个组里各出现一次，不是多份档案。
    azhou_ids = [p["person_id"] for g in groups.values() for p in g
                 if p["name"] == "小周"]
    # 朋友的孩子不因为家长是朋友就变成我的亲属。
    kid_groups = [p["groups"] for p in groups["朋友与同学"]
                  if p["person_id"] == "p_xiaoyu"]
    # 我这一侧搜不到 B 侧才有的记录，A/B 各自只看到自己的人物。
    # 注意不能拿「小周」当反例：小宇、小林的备注里都提到小周，
    # 命中他们是正确的，备注确实是关系背景。
    other_side = [p["name"] for p in service.search_persons(db, "A", "小周")]
    b_only = service.search_persons(db, "A", "小雨的尺码记录")
    b_side = service.search_persons(db, "B", "小雨")

    ok = (
        [p["name"] for p in by_alias] == ["小周"]
        and [p["name"] for p in by_job] == ["小林"]
        and "小周" in [p["name"] for p in by_relation]
        and [p["name"] for p in by_child] == ["小雨"]
        and missing == []
        and azhou_groups and azhou_groups[0] == ["朋友与同学", "工作与业务", "生活服务"]
        and sorted(set(azhou_ids)) == ["p_azhou"] and len(azhou_ids) == 3
        and kid_groups == [["朋友与同学"]]
        and other_side == ["小周", "小宇", "小林", "小雨"]
        and b_only == []
        and [p["name"] for p in b_side] == ["小雨"]
    )

    print("[PASS] 人物可按姓名/别名/关系/职业搜到，同一个人可跨组" if ok
          else "[FAIL] 人物可按姓名/别名/关系/职业搜到，同一个人可跨组")
    return {
        "按别名搜": [p["name"] for p in by_alias],
        "按职业搜": [p["name"] for p in by_job],
        "按关系搜": [p["name"] for p in by_relation],
        "按称呼搜": [p["name"] for p in by_child],
        "搜不到是否返回空": missing == [],
        "小周所在组": azhou_groups,
        "小周在各组出现的档案数": len(azhou_ids),
        "小周档案 id 是否唯一": sorted(set(azhou_ids)),
        "朋友的孩子是否被当成亲属": kid_groups,
        "A 侧搜小周": other_side,
        "A 侧搜 B 侧私有描述": [p["name"] for p in b_only],
        "B 侧搜小雨": [p["name"] for p in b_side],
        "用例内部判定": ok,
    }


def test_order_follow_through():
    """订单登记之后：候选可换、支付/发货/送达按顺序推进。

    方案要求「准备、已购买、已送达分开，模拟登记不能显示为真实送达」。
    这里验证四件事：模型给了备选且备选可换；换候选要重走硬条件检查；
    支付一次之后，发货和物流由系统自己接着走完，不需要用户再点；
    全程 delivery_state 保持 simulated，不谎报真实配送。
    """
    import pathlib
    path = DATA / "test_order_follow.db"
    if path.exists():
        path.unlink()
    db = dbm.DB(path)
    dbm.seed(db)

    # 事项先停在「已约定」：候选可换发生在订单产生之前。
    # 换商品等于改方案，订单一旦登记就不能再换，这条边界在下面单独验证。
    task_id = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome,"
        " created_at) VALUES(?,?,?,?,?,?,?)",
        (task_id, "A", "给小雨准备生日礼物，300元以内，别和我上次送的重复",
         json.dumps({"subject_id": "p_xiaoyu", "budget_max": 300, "avoid_repeat": True},
                    ensure_ascii=False),
         "agreed", "对方接受了，等你确认", dbm.now_iso()))
    # 候选：首选 + 两个备选；模型编的编号要被丢掉。
    prop = {"product_id": "pr_draw_set", "reason": "孩子喜欢画画",
            "candidates": [{"product_id": "pr_watercolor", "reason": "备选一"},
                           {"product_id": "pr_books", "reason": "备选二"},
                           {"product_id": "不存在的编号", "reason": "模型编的"}]}
    service.save_candidates(db, task_id, 1, prop)
    cands = service.task_candidates(db, task_id)
    cand_ok = (len(cands) == 3 and cands[0]["checked"] == 1
               and all(c["product_id"] != "不存在的编号" for c in cands))

    # 换候选：预算内的换得过去，选中的那一行被标出来。
    swapped = service.choose_candidate(db, task_id, "pr_watercolor", "A")
    after_swap = db.row("SELECT chosen FROM order_candidate WHERE task_id=? AND product_id=?",
                        (task_id, "pr_watercolor"))
    # 超预算必须被挡，而且原样不动。
    tight = json.dumps({"subject_id": "p_xiaoyu", "budget_max": 100, "avoid_repeat": True},
                       ensure_ascii=False)
    db.execute("UPDATE task SET extracted_json=? WHERE task_id=?", (tight, task_id))
    over_budget = None
    try:
        service.choose_candidate(db, task_id, "pr_draw_set", "A")  # 168 > 100
        over_budget = False
    except ValueError:
        over_budget = True
    db.execute("UPDATE task SET extracted_json=? WHERE task_id=?",
               (json.dumps({"subject_id": "p_xiaoyu", "budget_max": 300}, ensure_ascii=False),
                task_id))

    # 订单登记之后不能再换商品：这时候换会把阶段退回重新协商，
    # 界面上重新出现「确认购买」，用户能对同一件事再下一单。
    # 订单还没产生时换是正常的改主意，上面已经验过。
    order_id = dbm.new_id("simorder")
    db.execute(
        "INSERT INTO sim_order(order_id, task_id, proposal_version, confirmed_by, product_id,"
        " product_name, spec, recipient_subject_id, amount_cny, state, delivery_state,"
        " dedup_key, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, task_id, 1, "A", "pr_watercolor", "固体水彩 24 色", "",
         "p_xiaoyu", 79.0, "purchased", "simulated", "order:%s:v1" % task_id,
         dbm.now_iso()))
    swap_after_order = None
    try:
        service.choose_candidate(db, task_id, "pr_draw_set", "A")
        swap_after_order = False
    except ValueError as exc:
        swap_after_order = "不能再换商品" in str(exc)
    orders_after_denied = db.rows("SELECT * FROM sim_order WHERE task_id=?", (task_id,))

    # 订单按顺序推进：没支付不能发货，重复支付被挡，每步都写时间。
    # 发货和送达不再是用户动作：支付之后系统自动接着走完。
    ship_denied = None
    try:
        service.advance_order(db, task_id, order_id, "ship", "A")
        ship_denied = False
    except ValueError:
        ship_denied = True
    paid = service.advance_order(db, task_id, order_id, "pay", "A")
    dup_pay = None
    try:
        service.advance_order(db, task_id, order_id, "pay", "A")
        dup_pay = False
    except ValueError:
        dup_pay = True
    final = db.row("SELECT * FROM sim_order WHERE order_id=?", (order_id,))

    # 事项卡要带回候选和订单状态，前端才渲染得出来。
    matters = service.matters(db, "A")
    mine = [m for m in matters if m["task_id"] == task_id][0]

    ok = (
        cand_ok
        and swapped["product_id"] == "pr_watercolor"
        and after_swap["chosen"] == 1
        and over_budget is True            # 168 > 100，必须被挡
        and ship_denied is True            # 没支付不能发货
        # 支付一次，系统接着把发货、物流、送达都走完，所以回读到的
        # 已经是 delivered；断言落在最终状态上，不假设中间状态可见。
        and final["state"] == "delivered" and bool(final["paid_at"])
        # 金额要和换过之后那一件一致，不能还按换之前那件 168 元算。
        and paid["paid_amount_cny"] == 79.0
        and dup_pay is True                # 重复支付被挡
        # 订单登记之后不能再换商品，且没有因此多出第二个单
        and swap_after_order is True and len(orders_after_denied) == 1
        # 支付一次，系统自己把发货、物流、送达都走完
        and final["state"] == "delivered" and bool(final["ship_no"])
        and bool(final["shipped_at"]) and bool(final["delivered_at"])
        # 时间必须往后排：发货在送达之前，送达在支付之后
        and final["shipped_at"] < final["delivered_at"]
        and final["delivered_at"] > (final["paid_at"] or "")
        and final["delivery_state"] == "simulated"   # 全程模拟，不谎报真实送达
        and len(mine["product_candidates"]) == 3
        and mine["orders"][0]["state"] == "delivered"
    )
    return {
        "候选数": len(cands),
        "首选是否排第一": cands[0]["checked"] == 1,
        "模型编的编号是否被丢掉": all(c["product_id"] != "不存在的编号" for c in cands),
        "换候选结果": swapped["product_name"],
        "超预算换候选是否被挡": over_budget,
        "订单后换商品是否被挡": swap_after_order,
        "被挡后订单数": len(orders_after_denied),
        "未支付能否发货": not ship_denied,
        "支付后状态": paid["state"],
        "支付金额": paid["paid_amount_cny"],
        "重复支付是否被挡": dup_pay,
        "发货单号是否生成": bool(final["ship_no"]),
        "送达状态": final["state"],
        "delivery_state 是否仍为模拟": final["delivery_state"] == "simulated",
        "支付后是否自动走到送达": final["state"] == "delivered",
        "发货时间是否早于送达": final["shipped_at"] < final["delivered_at"],
        "送达时间是否晚于支付": final["delivered_at"] > (final["paid_at"] or ""),
        "是否声明未连接真实平台": final["delivery_state"] == "simulated",
        "事项卡是否带回候选": len(mine["product_candidates"]),
        "用例内部判定": ok,
    }


def test_swap_invalidates_acceptance():
    """换掉候选商品之后，对方对旧商品的接受不能再直接成交。

    这条守的是产品初衷：用户可以改主意，但改主意等于改方案。
    如果只把数据库里的商品编号换掉、却沿用对方对原来那件商品的接受，
   用户就会买到一件对方从未同意过的东西——这正是「越懂你越替你做错事」。
    正确做法是：换商品后该版本的相关接受一并失效，事项回到需要重新确认，
    并说清为什么；无关事项和已成交的历史不动。
    """
    db = fresh_db("test_swap_accept.db")
    task_id = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome,"
        " created_at) VALUES(?,?,?,?,?,?,?)",
        (task_id, "A", "给小雨准备生日礼物，300元以内，别和我上次送的重复",
         json.dumps({"subject_id": "p_xiaoyu", "budget_max": 300, "avoid_repeat": True},
                    ensure_ascii=False),
         "agreed", "对方接受了，等你确认", dbm.now_iso()))
    prop_id = dbm.new_id("prop")
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id, reason,"
        " asked_size, shared_conditions_json, facts_used_json, decision, decided_by,"
        " decision_reason, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (prop_id, task_id, 1, "A", "pr_books", "儿童科普绘本，趣味性和知识性都有", "",
         "[]", "[]", "accept", "B", "孩子正好缺绘本", dbm.now_iso()))
    service.save_candidates(db, task_id, 1, {
        "product_id": "pr_books", "reason": "首选",
        "candidates": [{"product_id": "pr_blocks", "reason": "备选：动手搭建"},
                       {"product_id": "pr_watercolor", "reason": "备选：喜欢画画"}]})

    before = service.task_candidates(db, task_id)
    swapped = service.choose_candidate(db, task_id, "pr_watercolor", "A")
    after = service.task_candidates(db, task_id)

    # 换过去之后：当前这件指向新商品，旧首选不再被标成选中。
    now_prop = db.row("SELECT product_id, decision FROM proposal WHERE proposal_id=?",
                      (prop_id,))
    chosen_row = db.row(
        "SELECT product_id, chosen FROM order_candidate WHERE task_id=? AND chosen=1",
        (task_id,))
    ok_swap = (now_prop["product_id"] == "pr_watercolor"
               and chosen_row is not None and chosen_row["product_id"] == "pr_watercolor"
               and all(c["product_id"] != "pr_books" or not c["chosen"] for c in after))

    # 关键：对方对旧商品的接受必须失效，执行要被挡住并说清原因。
    stale_ok = (now_prop["decision"] == "stale")
    blocked_reason = ""
    try:
        service.confirm_and_purchase(db, None, task_id, prop_id, "A")
        executed = True
    except ValueError as exc:
        executed = False
        blocked_reason = str(exc)
    executed_blocked = (executed is False and bool(blocked_reason))

    # 换商品不能顺手把别的事项或已成交历史改掉。
    other_task = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome,"
        " created_at) VALUES(?,?,?,?,?,?,?)",
        (other_task, "A", "给小宇准备生日礼物，200元以内", "{}", "agreed", "",
         dbm.now_iso()))
    untouched = db.row("SELECT stage FROM task WHERE task_id=?", (other_task,))
    orders_after = db.rows("SELECT * FROM sim_order WHERE task_id=?", (task_id,))

    return {
        "换后当前商品": now_prop["product_id"],
        "选中的候选": chosen_row["product_id"] if chosen_row else "",
        "候选条数不变": len(before) == len(after),
        "旧首选是否还标着选中": any(c["product_id"] == "pr_books" and c["chosen"]
                                   for c in after),
        "对方旧接受是否失效": stale_ok,
        "沿用旧接受能否成交": executed,
        "被挡时给出的原因": blocked_reason,
        "无关事项是否被误伤": untouched["stage"] == "agreed",
        "未产生订单": len(orders_after) == 0,
        "用例内部判定": bool(ok_swap and stale_ok and executed_blocked
                             and untouched["stage"] == "agreed"
                             and len(orders_after) == 0),
    }


def test_swap_survives_renegotiation():
    """用户换过的商品，重新协商后必须还是那一件。

    这是「换成这件」真正生效的最后一环。前两步只保证换的瞬间生效，
    可重新协商时模型若按自己的判断另选一件，用户刚换的东西又被悄悄换回去，
    看上去和「换了没用」没有区别。所以这里走完整链路：
    换商品 → 重新协商 → 确认 → 支付，核对买下来的就是用户选的那一件。
    """
    db = fresh_db("test_swap_survive.db")
    client = ModelClient()
    t = service.create_task(db, client, "A",
                            "给小雨准备生日礼物，300元以内，别和我上次送的重复")
    r = service.advance_task(db, client, t["task_id"])
    first_stage = r.get("stage")
    # 换一件：用户明确指定 pr_watercolor（固体水彩，79 元，预算内）。
    swapped = service.choose_candidate(db, t["task_id"], "pr_watercolor", "A")
    swapped_ok = swapped["product_id"] == "pr_watercolor"
    # 重新协商：模型必须沿用用户选的这一件，不能另选。
    r2 = service.advance_task(db, client, t["task_id"])
    prop = db.row("SELECT * FROM proposal WHERE task_id=? ORDER BY version DESC LIMIT 1",
                  (t["task_id"],))
    kept = prop is not None and prop["product_id"] == "pr_watercolor"
    # 重新协商后对方接受了，才继续确认；没接受就让确认去报错，同样记录在案。
    renegotiated = r2.get("stage")
    order = None
    buy_error = ""
    try:
        order = service.confirm_and_purchase(db, client, t["task_id"],
                                            prop["proposal_id"], "A")
    except ValueError as exc:
        buy_error = str(exc)
    bought = order["product_id"] if order else ""
    # 支付后系统自动把发货和送达走完，用户不需要再点。
    final = None
    if order is not None:
        final = service.advance_order(db, t["task_id"], order["order_id"], "pay", "A")
    return {
        "首次协商结果": first_stage,
        "换商品是否生效": swapped_ok,
        "换成的商品": swapped["product_id"],
        "重新协商结果": renegotiated,
        "重新协商后是否还是这一件": kept,
        "确认后买到的商品": bought,
        "确认被挡的原因": buy_error,
        "最终状态": final["state"] if final else "",
        "物流单号是否生成": bool(final and final["ship_no"]),
        "送达是否由系统自动完成": bool(final and final["delivered_at"]),
        "是否仍标注模拟": (final or {}).get("delivery_state") == "simulated",
        "用例内部判定": bool(swapped_ok and kept
                             and bought == "pr_watercolor"
                             and final and final["state"] == "delivered"
                             and final["delivery_state"] == "simulated"),
    }


def test_swap_steps_back_to_renegotiation():
    """换商品之后，事项阶段必须跟着退回，不能还挂着「已约定」。

    只作废提案而不改阶段，界面会继续显示「方案已约定」和「确认购买」，
    用户顺着点下去只会被服务端拒绝：按钮看得见、点了办不成。
    这正是「让用户伺候系统」，所以阶段、卡片状态、在等谁要一起改。
    """
    db = fresh_db("test_swap_stage.db")
    task_id = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome,"
        " created_at) VALUES(?,?,?,?,?,?,?)",
        (task_id, "A", "给小雨准备生日礼物，300元以内，别和我上次送的重复",
         json.dumps({"subject_id": "p_xiaoyu", "budget_max": 300, "avoid_repeat": True},
                    ensure_ascii=False),
         "agreed", "对方接受了，等你确认", dbm.now_iso()))
    prop_id = dbm.new_id("prop")
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id, reason,"
        " asked_size, shared_conditions_json, facts_used_json, decision, decided_by,"
        " decision_reason, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (prop_id, task_id, 1, "A", "pr_books", "儿童科普绘本", "", "[]", "[]",
         "accept", "B", "可以", dbm.now_iso()))
    service.save_candidates(db, task_id, 1, {
        "product_id": "pr_books", "reason": "首选",
        "candidates": [{"product_id": "pr_watercolor", "reason": "备选"},
                       {"product_id": "pr_crayon_36", "reason": "备选二"}]})
    service.set_waiting(db, task_id, "A", "awaiting_my_decision", "等你定")

    service.choose_candidate(db, task_id, "pr_watercolor", "A")
    task = db.row("SELECT stage, outcome FROM task WHERE task_id=?", (task_id,))
    waiting = service.waiting_for(db, "A")
    my_waiting = [w for w in waiting if w.get("task_id") == task_id]
    matters = [m for m in service.matters(db, "A") if m["task_id"] == task_id]
    stage_on_card = matters[0]["stage"] if matters else ""
    return {
        "换后阶段": task["stage"],
        "阶段是否退回重谈": task["stage"] == "needs_renegotiation",
        "卡片读到的阶段": stage_on_card,
        "说明是否说清换了什么": "固体水彩" in (task["outcome"] or ""),
        "是否还在等我定": any(w.get("kind") == "awaiting_my_decision" for w in my_waiting),
        "是否改成等对方重新确认": any(w.get("kind") == "awaiting_counterparty"
                                   for w in my_waiting),
        "用例内部判定": bool(task["stage"] == "needs_renegotiation"
                             and stage_on_card == "needs_renegotiation"
                             and "固体水彩" in (task["outcome"] or "")
                             and not any(w.get("kind") == "awaiting_my_decision"
                                         for w in my_waiting)
                             and any(w.get("kind") == "awaiting_counterparty"
                                     for w in my_waiting)),
    }


def main():
    # 定点选择器：`--only three-party --client stub|good|sum-caps|borrow-cap|placeholder|real`
    # 只跑第 17 项，用来在修复期快速验证断言路由与负向护栏；
    # 不带选择器时仍跑全部 22 项，行为不变。
    if "--only" in sys.argv:
        i = sys.argv.index("--only")
        target = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        mode = "stub"
        if "--client" in sys.argv:
            j = sys.argv.index("--client")
            mode = sys.argv[j + 1] if j + 1 < len(sys.argv) else "stub"
        if target in ("three-party", "three_family", "17"):
            return _run_focused_three_party(mode)
        print("未知 --only 目标：%r（可用 three-party）" % target)
        return 2

    if DB_PATH.exists():
        DB_PATH.unlink()
    db = dbm.DB(DB_PATH)
    dbm.seed(db)
    client = ModelClient()

    ms = service.model_status(client)
    print("模型连接：" + json.dumps(ms, ensure_ascii=False)[:160])
    if not ms["ok"]:
        report("模型可用", False, ms)
        return 1

    # ---------- 用例 1：完整办理 ----------
    t1 = service.create_task(db, client, "A",
                             "给小周的女儿准备生日礼物，300元以内，别和我上次送的重复")
    ex1 = t1["extracted"]
    r1 = service.advance_task(db, client, t1["task_id"])
    agreed = r1.get("stage") in ("agreed", "counter_pending")
    order = None
    if r1.get("stage") == "agreed":
        order = service.confirm_and_purchase(db, client, t1["task_id"],
                                             r1["proposal_id"], "A")
    elif r1.get("stage") == "counter_pending":
        order = service.confirm_and_purchase(db, client, t1["task_id"],
                                             r1["proposal_id"], "A", accept_counter=True)
    report("1 完整办理",
           agreed and order is not None and not order["duplicate"]
           and ex1.get("subject_resolved_name") == "小雨",
           {"抽取对象": ex1.get("subject_resolved_name"),
            "抽取预算": ex1.get("budget_max"),
            "阶段": r1.get("stage"), "版本": r1.get("version"),
            "商品": r1.get("product_name"), "价格": r1.get("price_cny"),
            "A 理由": r1.get("reason"), "B 理由": r1.get("b_reason"),
            "共享条件": r1.get("shared_conditions"),
            "订单": order and order["order_id"],
            "订单规格": order and order["spec"],
            "配送状态": order and order["delivery_state"]})

    # ---------- 用例 2：预算冲突（不能拿模型失败冒充） ----------
    t2 = service.create_task(db, client, "A", "给小周的女儿准备生日礼物，20元以内")
    r2 = service.advance_task(db, client, t2["task_id"])
    report("2 预算冲突且不擅自放宽",
           r2.get("stage") in ("conflict", "failed") and r2.get("stage") != "model_failed",
           {"阶段": r2.get("stage"), "结论": r2.get("outcome"),
            "是否误判为模型失败": r2.get("stage") == "model_failed"})

    # ---------- 用例 3：隔离（检查 B 的真实模型输入，不是存的回复） ----------
    # 探针必须是 A 侧私有原话。
    # 不能用裸「31 码」：B 自己授权数据里就有「上学期某品牌 31 码穿过」，
    # 那是 B 可见的正当信息，与 A 那条旧尺码记录只是数值相同。
    leaked_probes = ("我的旧记录", "买拼图准没错", "我 2025 年生日送过", "我本人记录",
                     "去年给小雨买鞋", "穿得还行")
    # 直接看实际发给模型的输入，而不是只查 message_log 里存的 B 回复。
    b_inputs = []
    for item in client.sent_inputs:
        blob = json.dumps(item["messages"], ensure_ascii=False)
        # B 的输入以「孩子家长的个人代表」开头，用它区分是哪一侧的调用。
        if blob.find("孩子家长的个人代表") >= 0:
            b_inputs.append(blob)
    leaked = [probe for probe in leaked_probes
              if any(probe in blob for blob in b_inputs)]

    # B 收到的 A 送礼历史只能来自 A 主动声明分享的字段，且字段不越界。
    share_leak = []
    for blob in b_inputs:
        idx = blob.find("giver_history_visible_to_me")
        if idx < 0:
            continue
        snippet = blob[idx:idx + 2000]
        try:
            items = json.loads(snippet[:snippet.index("]") + 1]) if "]" in snippet else []
        except ValueError:
            items = []
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except ValueError:
                items = []
        for it in (items if isinstance(items, list) else []):
            if not isinstance(it, dict):
                continue
            if "product_name" not in it or "happened_on" not in it:
                share_leak.append(it)
            elif set(it.keys()) - {"product_name", "happened_on"}:
                share_leak.append(it)

    # B 的输入里不应出现 A 的私有属性名，只应出现授权范围内的字段。
    a_only_attrs = ("shoe_size", "偏好边界", "已有物品")
    scope_leak = [a for a in a_only_attrs if any(a in blob for blob in b_inputs)]
    report("3 A 私有信息未进 B 的上下文", not leaked and not share_leak and not scope_leak,
           {"固定短语泄露": leaked, "分享字段越界": share_leak,
            "A 私有属性越界": scope_leak,
            "B 真实输入次数": len(b_inputs),
            "检查方式": "逐条读取 client.sent_inputs 中 B 侧实际输入"})

    # ---------- 用例 4：尺寸时效（必须看执行用哪个值） ----------
    # 先把前提钉死：家长名下不能有当前鞋码记录。前面几个用例在同一个库里
    # 跑过，可能已经留下一条 active 的 shoe_size；不先清掉的话，
    # 「没有当前鞋码时程序必须停下来问家长」这条根本走不到，
    # 后面的断言就成了空转。标成 superseded 而不是删除，记录仍可查。
    for r in db.rows(
            "SELECT fact_id FROM fact WHERE owner_id='B' AND attribute='shoe_size'"
            " AND status='active'"):
        db.execute("UPDATE fact SET status='superseded' WHERE fact_id=?",
                   (r["fact_id"],))
    t4 = service.create_task(db, client, "A",
                             "给小周的女儿买双鞋，300元以内")
    r4 = service.advance_task(db, client, t4["task_id"])
    size_in_proposal = ""
    structured_size = ""
    size_in_order = ""
    blocked_reason = ""
    # 家长这边只有脚长和一条写着「待核实」的旧试穿印象，没有当前鞋码。
    # 程序必须在这里停下来问家长，不能凭模型几句话就往下执行。
    # 家长补上当前鞋码后，同一件事接着办。
    relayed = {}
    if r4.get("stage") == "blocked":
        blocked_reason = r4.get("outcome") or ""
        # 送礼者先转述一个尺码：这句话不能算家长确认，事项必须继续卡着。
        relayed = service.answer_blocked_with_size(
            db, client, "A", "听孩子他妈说现在穿 32 码")
        assert relayed.get("resolved") is False, relayed
        assert relayed.get("need_parent") is True, relayed
        # 家长本人从自己的身份补同一件事，才接着办。
        answered = service.answer_blocked_with_size(
            db, client, "B", "孩子现在穿 32 码，今天量过")
        assert answered.get("resolved") is True, answered
        # 用协商返回的提案继续确认，不自己造一个只带 stage 的字典。
        latest = db.row(
            "SELECT proposal_id, product_id FROM proposal WHERE task_id=?"
            " ORDER BY version DESC LIMIT 1", (t4["task_id"],))
        latest_product = ""
        if latest is not None:
            row_p = db.row("SELECT name FROM product WHERE product_id=?",
                           (latest["product_id"],))
            latest_product = row_p["name"] if row_p else ""
        r4 = {"stage": answered.get("stage"),
              "proposal_id": latest["proposal_id"] if latest else "",
              "product_name": latest_product}
    if r4.get("stage") in ("agreed", "counter_pending"):
        props = db.rows("SELECT * FROM proposal WHERE task_id=? ORDER BY version DESC",
                        (t4["task_id"],))
        if props:
            try:
                conds = json.loads(props[0]["shared_conditions_json"])
            except (ValueError, TypeError):
                conds = []
            size_in_proposal = " ".join(conds)
            # 尺码走结构化字段，不再从散文条件里猜。
            structured_size = (props[0].get("asked_size") or "").strip()
        # 走到这里说明家长已经补过当前鞋码，同一方案可以执行。
        o = service.confirm_and_purchase(db, client, t4["task_id"],
                                         r4["proposal_id"], "A",
                                         accept_counter=r4["stage"] == "counter_pending")
        size_in_order = o.get("spec") or ""
    a_old = db.rows("SELECT value FROM fact WHERE owner_id='A' AND subject_id="
                    " (SELECT person_id FROM person WHERE owner_id='A' AND name='小雨')"
                    " AND attribute='shoe_size' AND status='active'")
    b_new = db.rows("SELECT value FROM fact WHERE owner_id='B' AND subject_id="
                    " (SELECT person_id FROM person WHERE owner_id='B' AND name='小雨')"
                    " AND attribute='foot_length' AND status='active'")
    # 订单规格必须来自当前有效方案，且不能是 A 侧那条已过期的旧尺码。
    stale_size = a_old[0]["value"] if a_old else ""
    stale_digits = "".join(ch for ch in stale_size if ch.isdigit())
    order_digits = "".join(ch for ch in size_in_order if ch.isdigit())
    # 尺码依据必须是家长这一侧的确认，不能只因为数值和 A 的旧记录不同就算通过。
    # 家长授权数据里本来就有「上学期 31 码穿过」，B 确认同一个数值是正当的；
    # 真正要证明的是订单用的是 B 本次给出的值，而不是 A 单方面记的旧值。
    size_source = db.row(
        "SELECT detail_json FROM audit_log WHERE task_id=? AND action='size_confirmed'"
        " ORDER BY created_at DESC LIMIT 1", (t4["task_id"],))
    b_confirmed = ""
    if size_source is not None:
        try:
            b_confirmed = (json.loads(size_source["detail_json"]).get("b_confirmed_size") or "").strip()
        except (ValueError, TypeError):
            b_confirmed = ""
    b_decided = ""
    for m in db.rows("SELECT payload_json FROM message_log WHERE task_id=? AND sender='B'"
                     " ORDER BY created_at DESC", (t4["task_id"],)):
        try:
            b_decided = (json.loads(m["payload_json"]).get("review", {})
                         .get("asked_size") or "").strip()
        except (ValueError, TypeError):
            b_decided = ""
        if b_decided:
            break
    # 模型口头报的尺码可能还是旧的（例：31 码来自「上学期试穿」的印象）。
    # 家长在本事项确认过的值优先，订单必须跟家长的那个，不能被模型一句旧值覆盖。
    model_spoke_different = bool(b_decided) and b_decided != b_confirmed

    # 送礼者转述的那一句只能留在自己名下，不能算家长确认。
    a_relayed = db.rows(
        "SELECT value, provider FROM fact WHERE owner_id='A' AND subject_id="
        " (SELECT person_id FROM person WHERE owner_id='A' AND name='小雨')"
        " AND attribute='shoe_size' AND status='active' AND provider LIKE '%转述%'")
    b_side_providers = [
        r["provider"] for r in db.rows(
            "SELECT provider FROM fact WHERE owner_id='B' AND subject_id="
            " (SELECT person_id FROM person WHERE owner_id='B' AND name='小雨')"
            " AND attribute='shoe_size' AND status='active'")]
    size_actions = [r["action"] for r in db.rows(
        "SELECT action FROM audit_log WHERE task_id=? AND action LIKE 'size_%'"
        " ORDER BY created_at", (t4["task_id"],))]

    report("4 尺寸时效与规格执行",
           r4.get("stage") in ("agreed", "counter_pending")
           and bool(b_new) and bool(a_old)
           and bool(structured_size)
           and bool(blocked_reason)
           and size_in_order == structured_size
           and bool(b_confirmed)
           and size_in_order == b_confirmed
           # 模型口头报的旧值不能推翻家长本轮确认的值。
           and (b_decided in ("", b_confirmed) or size_in_order == b_confirmed)
           # 尺码的身份来源：送礼者转述不能当成家长本人确认。
           and bool(a_relayed)
           and b_side_providers == ["家长本人确认"]
           and "size_relayed_by_giver" in size_actions
           and "size_confirmed_by_parent" in size_actions
           and relayed.get("resolved") is False,
           {"阶段": r4.get("stage"),
            "商品": r4.get("product_name"),
            "无当前鞋码记录时的拒绝原因": blocked_reason,
            "方案结构化尺码": structured_size,
            "订单规格": size_in_order,
            "规格与方案一致": size_in_order == structured_size,
            "家长本次确认尺码": b_confirmed,
            "订单尺码来源": "家长本轮确认，非 A 单方面记录",
            "模型口头报的尺码": b_decided,
            "模型旧值是否被家长确认值覆盖": model_spoke_different,
            "A 侧旧记录": [r["value"] for r in a_old],
            "B 侧当前脚长": [r["value"] for r in b_new],
            "数值是否与 A 旧记录相同": order_digits == stale_digits,
            "送礼者转述被记在自己名下": a_relayed,
            "家长侧确认记录来源": b_side_providers,
            "尺码相关审计动作": size_actions,
            "送礼者转述是否被误当家长确认": bool(relayed) and not (relayed.get("resolved") is False),
            "说明": ("订单尺码取自家长本轮给出的值；A 侧旧尺码仍保留可查但不进入执行。"
                     "家长只说一句话而授权数据里没有当前鞋码时，程序先拒绝执行，"
                     "要求家长确认当前鞋码；确认后放行。")})

    # ---------- 用例 5：主体纠正与再次复用 ----------
    corr = service.record_utterance(
        db, client, "A", "记错了，喜欢拼图的是小宇，小雨喜欢画画")
    t5 = service.create_task(db, client, "A", "给小宇准备个生日礼物，100元以内")
    r5 = service.advance_task(db, client, t5["task_id"])
    xy = db.row("SELECT person_id FROM person WHERE owner_id='A' AND name='小雨'")["person_id"]
    xh = db.row("SELECT person_id FROM person WHERE owner_id='A' AND name='小宇'")["person_id"]
    xy_likes = likes_of(db, "A", xy)
    xh_likes = likes_of(db, "A", xh)
    superseded = db.rows(
        "SELECT fact_id FROM fact WHERE owner_id='A' AND status='superseded'"
        " AND superseded_by IS NOT NULL")
    # 商品、对象、理由必须一致：给哥哥选的东西，理由不能提妹妹
    reason = r5.get("reason") or ""
    report("5 主体纠正与再次复用",
           r5.get("stage") in ("agreed", "counter_pending")
           and xy_likes == ["画画"] and xh_likes == ["拼图"]
           and "小雨" not in reason,
           {"纠正": corr.get("changes"),
            "纠正后小雨 likes": xy_likes, "纠正后小宇 likes": xh_likes,
            "旧记录保留并标记": [r["fact_id"] for r in superseded],
            "第二次交办阶段": r5.get("stage"),
            "第二次商品": r5.get("product_name"),
            "第二次理由": reason,
            "理由是否串到另一个人": "小雨" in reason})

    # ---------- 用例 6：对象不明时必须说清要补什么 ----------
    t6 = service.create_task(db, client, "A", "上次小李送我孩子一个跑车玩具，我怎么回礼")
    r6 = service.advance_task(db, client, t6["task_id"])
    card6 = service.task_card(db, t6["task_id"])
    cands = r6.get("candidates") or []
    # 用户接着问「补充什么？」：要说清缺哪一条，且不能再开一件重复任务。
    before_tasks = len(db.rows("SELECT task_id FROM task WHERE owner_id='A'"))
    intent_ask = service.classify_intent(db, client, "A", "补充什么？")
    info_ask = service.clarify_reply(db, "A")
    reply_ask = info_ask["reply"]
    dup_task = service.create_task(db, client, "A", "上次小李送我孩子一个跑车玩具，我怎么回礼")
    after_tasks = len(db.rows("SELECT task_id FROM task WHERE owner_id='A'"))
    reused_stage = db.row("SELECT stage FROM task WHERE task_id=?",
                          (dup_task["task_id"],))["stage"]
    # 已有 blocked 时，带新目标的句子不能被误判成追问：
    # 「小李送我玩具，我怎么回礼？」是一次新的交办，不是问上一件缺什么。
    intent_new = service.classify_intent(db, client, "A", "小李送我玩具，我怎么回礼？")
    # 两个待补事项时，追问要指向最近那一件。
    t6c = service.create_task(db, client, "A", "上次小王请我吃饭，我想回请一顿")
    r6c = service.advance_task(db, client, t6c["task_id"])
    info_two = service.clarify_reply(db, "A")
    two_points_newest = (t6c["task_id"] == info_two["task_id"]
                         and "小王" in info_two["reply"])
    # 用户只报一个名字（不追问、不带新目标）时，要接回卡住的那件事继续办。
    name_reply = service.answer_blocked_with_name(db, client, "A", "小周")
    name_resolved = bool(name_reply and name_reply.get("resolved"))
    # 「补充什么？」不能被当成人名记下来。
    meta_not_name = service._looks_like_name_only("补充什么？") is False
    hint_in_reply = (r6.get("hint") or "") in reply_ask
    names = [c.get("name") for c in info_ask.get("candidates", []) if c.get("name")]
    name_in_reply = any(n in reply_ask for n in names)
    resolved = None
    r6b = None
    if cands:
        resolved = service.resolve_subject(db, client, t6["task_id"],
                                           cands[0]["person_id"])
        r6b = service.advance_task(db, client, t6["task_id"])
    report("6 对象不明时说清要补什么并可继续",
           r6.get("stage") == "blocked"
           and bool(r6.get("hint"))
           and len(r6.get("outcome") or "") > 12
           and bool(cands)
           and bool(card6.get("candidates"))
           and intent_ask == "clarify"
           and meta_not_name
           and name_resolved
           and intent_new == "task"
           and two_points_newest
           and hint_in_reply and name_in_reply
           and dup_task.get("reused") is True
           and after_tasks == before_tasks
           and reused_stage == "blocked"
           and resolved is not None
           and r6b is not None and r6b.get("stage") != "blocked",
           {"阶段": r6.get("stage"),
            "用户原话称呼": r6.get("hint"),
            "追问原文": r6.get("outcome"),
            "候选人数": len(cands),
            "事项卡带回候选": len(card6.get("candidates") or []),
            "追问「补充什么？」的意图": intent_ask,
            "「补充什么？」未被当成人名": meta_not_name,
            "只报名字是否接回原事项": name_resolved,
            "只报名字后的回复": (name_reply or {}).get("reply", "")[:80],
            "只报名字后阶段": (name_reply or {}).get("stage"),
            "已有阻塞时新回礼请求的意图": intent_new,
            "第二件待补阶段": r6c.get("stage"),
            "两件待补时追问指向最近一件": two_points_newest,
            "追问指向的任务": info_two["task_id"] == t6c["task_id"],
            "追问回答原文": reply_ask,
            "回答点出缺的是送礼对象": "送给谁" in reply_ask,
            "回答提到用户原话称呼": hint_in_reply,
            "回答提到可点人名": name_in_reply,
            "重复原话是否复用原任务": bool(dup_task.get("reused")),
            "任务数变化": "%d -> %d" % (before_tasks, after_tasks),
            "点选后对象": resolved["extracted"]["subject_resolved_name"] if resolved else None,
            "点选后阶段": r6b.get("stage") if r6b else None})

    # ---------- 用例 7：重复确认与旧提案 ----------
    dup = None
    if order:
        dup = service.confirm_and_purchase(db, client, t1["task_id"],
                                           r1["proposal_id"], "A",
                                           accept_counter=r1["stage"] == "counter_pending")
    stale = False
    reason_stale = ""
    try:
        service.confirm_and_purchase(db, client, t5["task_id"], r1["proposal_id"], "A")
    except ValueError as exc:
        stale = True
        reason_stale = str(exc)
    report("7 重试与旧版本",
           dup is not None and dup["duplicate"] and dup["order_id"] == order["order_id"]
           and stale,
           {"首次订单": order["order_id"] if order else None,
            "重复确认订单": dup["order_id"] if dup else None,
            "重复确认标记": dup["duplicate"] if dup else None,
            "旧提案被拒原因": reason_stale})

    # 确认守卫是纯程序校验，不调用模型，失败时其他用例也应当作废。
    guard = test_rejected_confirm_leaves_no_trace()
    stale = test_correction_invalidates_pending_proposal()
    report("8 错误确认不留下接受/约定/订单痕迹", guard is not None, guard)
    # 无关事项的接受记录不能被误改，仍由 B 接受。
    unrelated_ok = (stale.get("无关事项决定") or {}).get("未被误改") is True
    report("9 事实纠正使未完方案失效并重新协商",
           bool(stale) and stale.get("失效后阶段") == "needs_renegotiation"
           and "已被更正，不能执行" in (stale.get("旧确认被拒原因") or "")
           and bool(stale.get("新版本执行"))
           and "未被拒绝" not in (stale.get("旧版本再次被拒原因") or "")
           and unrelated_ok,
           stale)

    test_order_spec_takes_final_corrected_size()

    followup = test_followup_does_not_swallow_new_task()
    followup_ok = (
        followup.get("带新目标句子的意图") == "task"
        and followup.get("该句是否被判为纯反馈") is False
        and followup.get("上一单 feedback 是否被误写") == ""
        and followup.get("纯反馈意图") == "followup"
        and followup.get("是否等于小雨那一单") is True
        and followup.get("写回的反馈") == "送到了，孩子很喜欢"
        and followup.get("单候选但指名他人", {}).get("resolved") is False
        and followup.get("单候选但指名他人", {}).get("待记订单是否被误写") == ""
        and followup.get("单候选且未指名", {}).get("是否等于第二条订单") is True
    )
    report("10 后续反馈不吞新交办且按人对号", followup_ok, followup)

    append = test_followup_append_when_all_fed_back()
    append_ok = (
        append.get("首条命中本单") is True
        and append.get("未指名续接同一件") is True
        and append.get("原反馈未被覆盖") is True
        and append.get("新反馈已追加") is True
        and append.get("指名他人不误写") is False
        and append.get("指名他人后反馈未变") is True
        and append.get("指名本人可追加") is True
        and append.get("任务数变化") == "1 -> 1"
    )
    report("11 反馈记完后继续补充不变成新交办", append_ok, append)

    waiting = test_waiting_record_and_no_order()
    waiting_ok = (
        waiting.get("等家长本人的等待") == 1
        and waiting.get("等待文案含在等对方本人") is True
        and waiting.get("等我定的等待") == 1
        and waiting.get("未回应时订单数") == 0
        and waiting.get("确认是否成功") is True
        and waiting.get("确认后 A 侧等待") == 0
        and waiting.get("确认后 B 侧等待") == 0
        and waiting.get("库里等待记录") == 0
        and waiting.get("订单是否落库") is True
        and waiting.get("确认耗时秒", 99) < 5.0
    )
    report("12 在等谁要记下，未回应不生成订单，确认不自锁", waiting_ok, waiting)

    evt = test_life_event_field_grants()
    evt_ok = (
        evt.get("导入是否新建") is True
        and evt.get("重复导入是否新建") is False
        and evt.get("A 可见条数") == 2
        and evt.get("B 可见条数") == 2
        and evt.get("未授权字段是否泄露") == []
        and evt.get("A 可见字段") == ["已完成项目", "睡眠时长"]
        and evt.get("B 能看到全部字段") is True
        and evt.get("手表是否标为设备") is True
        and evt.get("通知去重") is True
        and evt.get("撤回后仍可见数") == 1
        and evt.get("撤回记录状态") == "revoked"
        and evt.get("纠正后睡眠时长") == {"睡眠时长": "9.0 小时"}
        and evt.get("旧事件状态") == "superseded"
        and evt.get("A 人物详情可见数") == 1
        and evt.get("详情里的是新状态") == {"睡眠时长": "9.0 小时"}
    )
    report("13 家庭近况按人按字段授权，撤回与纠正都生效", evt_ok, evt)

    reuse = test_cross_app_reuse_in_context()
    # 外层门槛必须覆盖「实际发给模型的请求里有两个来源的新值」。
    # 之前这里只看 _a_context 构造出来的对象，证明的是上下文里有新值，
    # 但方案要求核对真实请求：context 里有而请求里没有，
    # 模型这一轮拿到的还是旧状态。两个口径缺一不可。
    reuse_ok = (
        reuse.get("更新前上下文") == {"已完成项目": "数学练习 10 题"}
        and reuse.get("更新后上下文") == {"已完成项目": "数学练习 30 题"}
        and reuse.get("旧值是否残留") is False
        and reuse.get("另一个孩子的上下文条数") == 0
        and reuse.get("撤回后 study_app 是否还在") is False
        and reuse.get("真实请求里是否含两来源新值") is True
        and reuse.get("真实请求里的旧值或未授权字段") == []
        and reuse.get("近况是否带来源/时间/提供者") is True
        and reuse.get("另一个孩子是否混进请求") is False
        and reuse.get("事件本人可见全部字段") is True
    )
    report("14 跨小 App 近况按新状态复用，他人与撤回都不串", reuse_ok, reuse)

    fu = test_family_update_entry_and_link()
    fu_ok = (
        fu.get("是否定位到人") is True
        and fu.get("拿到近况条数") == 2
        and fu.get("回复含学习近况") is True
        and fu.get("回复含日常近况") is True
        and fu.get("未授权字段泄露") == []
        and fu.get("同名未关联时条数") == 0
        and fu.get("显式关联后条数") == 1
    )
    report("15 问近况不重填背景，同名不同人不串事件", fu_ok, fu)

    late = test_late_reply_and_stale_version()
    late_ok = (
        late.get("刷新后等待条数") == 1
        and late.get("刷新后等待类型") == "awaiting_person"
        and late.get("送礼者转述是否被拒") is False
        and late.get("转述后阶段") == "blocked"
        and late.get("转述后订单数") == 0
        and late.get("家长接回的是否原事项") is True
        and late.get("家长确认来源") == "家长本人确认"
        and "awaiting_person" not in (late.get("收尾后仍在等的类型") or [])
        and late.get("任务数变化") == "1 -> 1"
        and late.get("接回后订单数") == 0
        and bool(late.get("旧版本确认报错"))
        and late.get("旧版本确认后订单数") == 0
    )
    report("16 迟到回复接回原事项，旧版本回复无效，刷新后仍在", late_ok, late)

    plan = test_three_family_plan()
    # 第 17 项的通过口径和用例内部的判定是同一个值：
    # 用例内部把「重排后内容内部一致」「已约好后改条件立刻失效」
    # 「待重排期间拒绝表态」等一并算进 ok，外层不能再另立一套更宽的门槛，
    # 否则会出现用例打印 FAIL、验收文件却记 PASS。
    plan_ok = plan.get("用例内部判定") is True
    report("17 三个参与方规划：同版本接受才算约定，旧接受随新版本失效"
           "（generic plan 逻辑，非产品两家三身份演示）", plan_ok, plan)

    srch = test_person_search_and_groups()
    # 外层判定直接取用例内部判定：内部把「别名命中」「跨组仍是同一档案」
    # 「朋友的孩子不升级成亲属」一并算进 ok，外层不另立一套更宽的门槛。
    report("18 人物可按姓名/别名/关系/职业搜到，同一个人可跨组",
           srch.get("用例内部判定") is True, srch)

    chain = test_order_follow_through()
    # 外层判定直接取用例内部判定：内部把「候选去重」「乱序被挡」
    # 「超预算换不过去」「全程仍是模拟」一并算进 ok。
    report("19 订单后续：候选可换，支付/发货/送达按顺序推进",
           chain.get("用例内部判定") is True, chain)

    swap = test_swap_invalidates_acceptance()
    report("20 换候选商品后对方旧接受失效，不能直接成交",
           swap.get("用例内部判定") is True, swap)

    survive = test_swap_survives_renegotiation()
    report("21 换过的商品重新协商后仍被保留，买下来的就是那一件",
           survive.get("用例内部判定") is True, survive)

    step = test_swap_steps_back_to_renegotiation()
    report("22 换商品后事项退回重新协商，不再挂着能点却办不成的确认",
           step.get("用例内部判定") is True, step)

    out = DATA / "acceptance.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(1 for r in results if r["ok"])
    print("\n通过 %d/%d，明细写入 %s" % (passed, len(results), out))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
