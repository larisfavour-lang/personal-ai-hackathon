"""朋友旅行场景：多方协调引擎泛化（t56）回归。

把原家庭专用的多方协调引擎泛化：事项条件字段按场景配置（家庭活动保留原 8 键、
行为零变化；朋友旅行用旅行字段集），plan.kind 落场景，_plan_context/提示词/预算校验
按场景取字段；参与声明收人数与出席，并**由声明人自己决定**要不要说出随行是谁。
填了姓名的随行进共同事项；未配自己助手的随行者默认由声明人代决策，代决策单独记录、
不计入本人接受。

离线：临时库 + StubClient，0 业务模型调用。覆盖：
  A. 字段可按场景配置：家庭 8 键不变、拒绝未知键、出资口径不变；朋友字段集生效、
     拒绝家庭专属键；
  B. 意图：朋友旅行措辞识别，送礼句/家庭出游句不误判；
  C. 同一引擎跑完整链：交办→邀请→来宾自称→声明人数与随行→发起人认可
     →按当前条件重排→来宾对当前版本表态→改条件旧表态失效、重排新版；
  D. 随行姓名由声明人决定填不填；填了进共同事项；未配自己助手的由声明人代决策，
     代决策不计入「已约定」；私有档案（手机号等）不外泄；
  E. 演示种子：三个虚构朋友，不破坏既有 A/B/C 家庭演示数据。

运行：python tests/test_friend_trip_scenario.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db as dbm  # noqa: E402
from app import service  # noqa: E402
from app import agents  # noqa: E402

OUT = ROOT / "docs" / "evidence" / "friend-trip-scenario.txt"
results: list[tuple[str, bool, object]] = []


def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))


class StubClient:
    """离线模型桩：按提示词内容路由返回合成 JSON，记录发出的提示词。"""

    def __init__(self, content=None):
        self.sent = []
        self.content = content or {"目的地": "海边小城", "时间": "五一假期",
                                   "住宿标准": "经济型酒店", "交通": "高铁"}

    def chat_json(self, messages, **kw):
        prompt = "\n".join(m.get("content") or "" for m in messages)
        self.sent.append(prompt)
        return {"content": dict(self.content), "reason": "按各方已给条件排的一版",
                "conflicts": []}


def fresh_db():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pa-ftrip-")) / "ftrip.db"
    db = dbm.DB(tmp)
    dbm.seed(db)
    return db


def all_prompt(stub):
    return "\n".join(stub.sent)


# ---------------------------------------------------------------
# A. 字段按场景配置：家庭零变化 + 朋友字段集
# ---------------------------------------------------------------
def test_scenario_fields():
    # 家庭 8 键原样保留
    check("家庭场景仍是原 8 个条件键",
          service.PLAN_CONDITION_KEYS == ("时间", "地点", "活动总预算", "本人出资上限",
                                          "活动内容", "孩子安排", "交通", "备注"),
          service.PLAN_CONDITION_KEYS)
    # 家庭：未知键拒绝、出资口径不变
    out, unknown, confirm = service.normalize_conditions(
        {"时间": "周六", "孩子安排": "两家孩子都来", "瞎掰": "x"})
    check("家庭拒绝未知键", unknown == ["瞎掰"], unknown)
    check("家庭接受原 8 键", out.get("孩子安排") == "两家孩子都来" and out.get("时间") == "周六", out)
    out2, _, _ = service.normalize_conditions({"预算": "每家人不超过80元"})
    check("家庭出资口径不变：每家→本人出资上限", "本人出资上限" in out2, out2)
    out3, _, confirm3 = service.normalize_conditions({"预算": "100元"})
    check("家庭光数字口径不明→请本人确认", confirm3 == ["预算"], confirm3)

    # 朋友字段集
    fk = service.condition_keys("friend_trip")
    check("朋友字段集含 目的地/人均预算/本人预算上限/能否带伴侣 等",
          {"目的地", "人均预算", "本人预算上限", "能否带伴侣", "假期长度", "住宿标准"} <= set(fk), fk)
    fo, fun, _ = service.normalize_conditions({"目的地": "海边", "假期长度": "3天"}, scenario="friend_trip")
    check("朋友接受旅行字段", fo.get("目的地") == "海边" and fo.get("假期长度") == "3天", fo)
    _, ffamily, _ = service.normalize_conditions({"孩子安排": "x"}, scenario="friend_trip")
    check("朋友场景拒绝家庭专属键「孩子安排」", ffamily == ["孩子安排"], ffamily)
    fp, _, _ = service.normalize_conditions({"预算": "人均1500"}, scenario="friend_trip")
    check("朋友人均口径→人均预算", fp.get("人均预算") == "人均1500", fp)
    fc, _, _ = service.normalize_conditions({"预算": "我出3000以内"}, scenario="friend_trip")
    check("朋友本人上限口径→本人预算上限", fc.get("本人预算上限") == "我出3000以内", fc)


def test_context_scenario_isolation():
    db = fresh_db()
    # 家庭：上下文不带朋友字段
    fp = service.create_plan(db, "A", "周末三家一起带孩子去公园", "周末三家一起带孩子去公园")
    service.add_plan_participant(db, fp, "A", "我家", {"孩子安排": "都来", "时间": "周六"})
    fctx = agents._plan_context(db, fp)
    fblob = json.dumps(fctx, ensure_ascii=False)
    check("家庭上下文不含朋友旅行专属字段(目的地/人均预算/能否带伴侣)",
          not any(k in fblob for k in ("目的地", "人均预算", "能否带伴侣")), fblob[:120])
    # 朋友：上下文不带家庭专属字段
    tp = service.create_plan(db, "A", "朋友想去旅行", "朋友想去旅行", kind="friend_trip")
    service.add_plan_participant(db, tp, "A", "我", {"目的地": "海边"}, role="self")
    tctx = agents._plan_context(db, tp)
    tblob = json.dumps(tctx, ensure_ascii=False)
    check("朋友上下文标场景=朋友旅行", tctx.get("场景") == "朋友旅行")
    check("朋友上下文不含家庭专属字段(孩子安排/孩子出席/家庭)",
          not any(k in tblob for k in ("孩子安排", "孩子出席", '"家庭"')), tblob[:160])


def test_friend_trip_intent():
    check("朋友想去旅行→朋友旅行", service.friend_trip_intent("朋友想去旅行"))
    check("几个朋友约出去旅游→朋友旅行", service.friend_trip_intent("几个朋友约出去旅游"))
    check("送礼句不算朋友旅行", not service.friend_trip_intent("给三家一起出游的孩子们准备礼物"))
    check("家庭出游句不算朋友旅行", not service.friend_trip_intent("周末三家一起带孩子出去玩"))
    check("普通送礼句不算朋友旅行", not service.friend_trip_intent("给女儿买个生日礼物"))


# ---------------------------------------------------------------
# C+D. 完整链 + 授权边界（人数声明不含姓名、随行姓名不外泄）
# ---------------------------------------------------------------
def _run_chain(db):
    """同一引擎跑朋友旅行完整链，返回关键 id/状态。"""
    owner = "A"
    plan_id = service.create_plan(db, owner, "五一朋友去海边", "五一朋友去海边", kind="friend_trip")
    service.add_plan_participant(db, plan_id, owner, "我（发起人）",
                                 {"目的地": "海边", "假期长度": "3天"}, role="self")
    inv = service.invitation_create(db, owner, plan_id, "朋友位")
    acc = service.invitation_accept(db, inv["token"], "小奈")
    service.invitation_approve(db, owner, plan_id, inv["invitation_id"])
    # 小奈声明人数与出席，并自己决定说出随行是小陈；小陈没有自己的助手
    session = {"raw_token": acc["session_token"]}
    decl = service.declare_trip_party(db, session, {"adults": 2, "kids": 0, "tbd": 1},
                                      {"目的地": "海边", "本人预算上限": "3000 元以内"},
                                      [{"name": "小陈", "has_agent": False, "attendance": "coming"}])
    stub = StubClient()
    # 声明人数/条件后 needs_replan=1：按 acceptance「按全部当前条件重排」出第一版，
    # plan_replan 才会清掉待重排标记，各方才能对这一版表态。
    prop = agents.plan_replan(db, stub, plan_id)
    v1 = prop["version"]
    # 小奈对当前版本表态（只能由本人）
    service.decide_plan(db, plan_id, acc["participant_id"], v1, "accept", actor_id=acc["guest_id"])
    # 发起人改自己的一个条件 → 旧表态失效、需重排
    service.update_plan_condition(db, plan_id, owner, {"假期长度": "4天"})
    replan = agents.plan_replan(db, stub, plan_id)
    v2 = replan["version"]
    st = service.plan_status(db, plan_id)
    return {"plan_id": plan_id, "acc": acc, "decl": decl, "v1": v1, "v2": v2,
            "st": st, "stub": stub}


def test_full_chain_with_companions():
    db = fresh_db()
    r = _run_chain(db)
    st = r["st"]
    # 人数声明只收计数
    check("人数声明只收成人/孩子/待定计数", r["decl"]["headcount"] == {"adults": 2, "kids": 0, "tbd": 1},
          r["decl"])
    # 完整链走到重排新版
    check("完整链：交办→邀请→自称→人数声明→认可→提案→表态→改条件→重排新版",
          r["v1"] >= 1 and r["v2"] == r["v1"] + 1 and st["current_version"] == r["v2"],
          {"v1": r["v1"], "v2": r["v2"], "cur": st["current_version"]})
    # 改条件后旧接受失效：小奈对新版本还没回复（不能拿旧接受当约定）
    xiaonai = next(s for s in st["participants"] if s["owner_id"] == r["acc"]["guest_id"])
    check("改条件后旧表态失效：小奈对新版本未默认接受",
          xiaonai["state"] in ("pending", "stale") and not st["agreed"], xiaonai["state"])
    # 随行由声明人自己决定填不填：填了进共同事项，未配助手的标为代决策
    side = next(s for s in st["participants"] if s["owner_id"] == r["acc"]["guest_id"])
    check("人数声明收成人/孩子/待定计数",
          side["headcount"].get("adults") == 2 and side["headcount"].get("tbd") == 1,
          side["headcount"])
    names = [c["name"] for c in side["companions"]]
    check("声明人填了的随行姓名进共同事项", names == ["小陈"], names)
    check("未配自己助手的随行者标为代决策",
          side["delegated"] == ["小陈"] and
          next(c for c in side["companions"] if c["name"] == "小陈")["decision_mode"] == "delegated",
          side["companions"])
    # 本方状态只认本人表态：代决策行不把这一方显示成已同意
    check("本方状态不是 accept（代决策不算本人同意）", side["state"] != "accept", side["state"])


def test_no_private_profile_leak():
    db = fresh_db()
    pid = dbm.new_id("p")
    db.execute("INSERT INTO person(person_id,owner_id,name,relation,note) VALUES(?,?,?,?,?)",
               (pid, "A", "小奈", "朋友", "会和男友小陈一起去；手机 138-0000-0000"))
    plan_id = service.create_plan(db, "A", "国庆朋友自驾", "国庆朋友自驾", kind="friend_trip")
    service.add_plan_participant(db, plan_id, "A", "我", {"目的地": "山区"}, role="self")
    inv = service.invitation_create(db, "A", plan_id, "朋友位")
    acc = service.invitation_accept(db, inv["token"], "小奈")
    service.invitation_approve(db, "A", plan_id, inv["invitation_id"])
    # 小奈自己决定说出随行是小陈，且小陈没有自己的助手
    service.declare_trip_party(db, {"raw_token": acc["session_token"]},
                               {"adults": 2, "kids": 0, "tbd": 0}, {"目的地": "山区"},
                               [{"name": "小陈", "has_agent": False, "attendance": "coming"}])
    st = service.plan_status(db, plan_id)
    ctx = agents._plan_context(db, plan_id)
    for tag, blob in (("plan_status", json.dumps(st, ensure_ascii=False)),
                      ("模型上下文", json.dumps(ctx, ensure_ascii=False))):
        # 随行姓名是小奈自己填的，可以进共同事项
        check("%s 含声明人自己填的随行姓名" % tag, "小陈" in blob, blob[:160])
        # 但私有档案（手机号）不外泄
        check("%s 不含私有档案手机号" % tag, "138-0000-0000" not in blob, blob[:160])


def test_delegated_decision_not_consent():
    """代决策是单独一类：它让随行者的事情能推进，但永远不算他本人同意。"""
    db = fresh_db()
    plan_id = service.create_plan(db, "A", "朋友去旅行", "朋友去旅行", kind="friend_trip")
    service.add_plan_participant(db, plan_id, "A", "我", {}, role="self")
    inv = service.invitation_create(db, "A", plan_id, "朋友位")
    acc = service.invitation_accept(db, inv["token"], "阿远")
    service.invitation_approve(db, "A", plan_id, inv["invitation_id"])
    sid = acc["session_token"]
    service.declare_trip_party(db, {"raw_token": sid}, {"adults": 1, "kids": 0, "tbd": 0},
                               {}, [{"name": "小王", "has_agent": False, "attendance": "coming"}])
    # 声明后 needs_replan=1：按当前条件重排出第一版，各方才能对这一版表态
    agents.plan_replan(db, StubClient(), plan_id)
    st = service.plan_status(db, plan_id)
    v = st["current_version"]
    # 阿远本人接受：同时为未配助手的小王记一条代决策
    service.decide_plan(db, plan_id, st["participants"][-1]["participant_id"], v, "accept",
                        reason="本人接受", actor_id=acc["guest_id"])
    rows = db.rows("SELECT decided_by,reason,changes_json FROM plan_decision WHERE plan_id=? AND version=?", (plan_id, v))
    dele = [r for r in rows if r["decided_by"] == "delegate"]
    check("为未配助手的随行者记了代决策行", len(dele) == 1, rows)
    check("代决策行注明代谁决策", dele and "小王" in dele[0]["reason"], dele)
    st2 = service.plan_status(db, plan_id)
    # 这一方的本方状态来自本人表态，不是代决策
    side = next(s for s in st2["participants"] if s["owner_id"] == acc["guest_id"])
    check("本方状态来自本人表态", side["state"] == "accept", side["state"])
    # 代决策行不会让「已约定」判据把没有自己助手的一方算成本人同意
    check("代决策不冒充实数同意：仍有其他方待确认", not st2["agreed"], st2["agreed"])


def test_headcount_rejects_unknown_keys():
    db = fresh_db()
    plan_id = service.create_plan(db, "A", "朋友去旅行", "朋友去旅行", kind="friend_trip")
    service.add_plan_participant(db, plan_id, "A", "我", {}, role="self")
    inv = service.invitation_create(db, "A", plan_id, "朋友位")
    acc = service.invitation_accept(db, inv["token"], "阿远")
    service.invitation_approve(db, "A", plan_id, inv["invitation_id"])
    session = {"raw_token": acc["session_token"]}
    try:
        service.declare_trip_party(db, session, {"adults": 1, "姓名": "小王"}, {})
        check("人数声明仍拒绝未知键", False, "未抛错")
    except ValueError:
        check("人数声明仍拒绝未知键", True)
    # 随行姓名走 companions，不再夹在人数里
    r = service.declare_trip_party(db, session, {"adults": 1}, {},
                                   [{"name": "小王", "has_agent": False}])
    check("随行姓名走 companions 且默认代决策",
          r["companions"]["companions"][0]["decision_mode"] == "delegated", r["companions"])


def test_friend_seed_preserves_family():
    db = fresh_db()
    fam_before = {r["name"] for r in db.rows("SELECT name FROM person WHERE owner_id='A'")}
    res = service.seed_friend_trip_demo(db, "A")
    fam_after = {r["name"] for r in db.rows("SELECT name FROM person WHERE owner_id='A'")}
    check("演示种子新增三个虚构朋友", len(res["created"]) == 3, res["created"])
    check("既有家庭演示人物一个没动", fam_before <= fam_after and len(fam_after - fam_before) == 3,
          sorted(fam_after - fam_before))
    check("演示种子幂等（再调一次不新增）", service.seed_friend_trip_demo(db, "A")["created"] == [])


def main():
    for fn in (test_scenario_fields, test_context_scenario_isolation, test_friend_trip_intent,
               test_full_chain_with_companions, test_delegated_decision_not_consent,
               test_no_private_profile_leak, test_headcount_rejects_unknown_keys,
               test_friend_seed_preserves_family):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(fn.__name__ + "（异常）", False, repr(exc))
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    lines = ["t56 朋友旅行场景 测试退出=%d" % (0 if passed == total else 1),
             "通过 %d / %d" % (passed, total), ""]
    for name, ok, detail in results:
        lines.append("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                                    ("" if ok else "  <- " + repr(detail)[:300])))
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
