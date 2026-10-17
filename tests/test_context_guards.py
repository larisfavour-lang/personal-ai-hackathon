"""跨页面需求卡的授权边界：字段级授权、时效、撤回、跨人不串、匿名导出。

不调模型、不连网络、不写业务库：用临时库 + 直接构造事项和设备凭证触发
extension_context，只验后端 guard。这样每次改动都能立刻重跑。

覆盖：
1. 对方授权 likes 时，需求卡给出家长记录的值，来源标为家长记录；
2. 撤销/过期/未授权该字段时，该项保持 pending，不进 search_terms 和 export_text；
3. 只授权查看未授权转述时，不进导出文本；
4. B 侧纠正后版本号变化，且新值进入需求/搜索/导出；
5. 换一个 B 侧同名人物不会串到别人家的记录（靠 person_link 绑定）；
6. 别人的事项 404，无凭证 401；
7. pending 项不留占位值；
8. 导出文本不含人名、电话、地址。

运行：/tmp/hack_venv/bin/python tests/test_context_guards.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db as dbm
from app import service

TMP = Path("/tmp/pa_context_guards.db")
results: list[tuple[str, bool, object]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def fresh():
    if TMP.exists():
        TMP.unlink()
    db = dbm.DB(TMP)
    dbm.seed(db)
    return db


def make_task(db, owner_id="A", subject_id="p_xiaoyu", stage="created"):
    task_id = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage,"
        " outcome, created_at) VALUES(?,?,?,?,?,?,?)",
        (task_id, owner_id, "给小雨准备生日礼物，300元以内，别重复",
         json.dumps({"subject_id": subject_id, "budget_max": 300,
                     "occasion": "生日", "avoid_repeat": True},
                    ensure_ascii=False),
         stage, "", dbm.now_iso()))
    return task_id


def make_device(db, owner_id="A", name="测试设备"):
    code = service.create_pair_code(db, owner_id, name)["code"]
    paired = service.pair_device(db, owner_id, code, name)
    return service.device_from_request(db, "Bearer " + paired["access_token"])


def ctx(db, device, task_id):
    return service.extension_context(db, device, task_id, "shopping_help")


def req_of(card, key):
    for r in card["requirements"]:
        if r["key"] == key:
            return r
    return {}


def main():
    # ---------- 1. 授权 likes：给出家长记录的值 ----------
    db = fresh()
    task_id = make_task(db)
    device = make_device(db)
    card = ctx(db, device, task_id)
    likes = req_of(card, "likes")
    check("授权字段给出家长记录的值", likes.get("state") == "known"
          and likes.get("value") == "画画", likes)
    src = {s["field"]: s["source_label"] for s in card["source_summary"]}
    check("来源标明是家长记录", "家长记录" in src.get("likes", ""), src.get("likes"))

    # ---------- 2. 撤销授权后：该项回到 pending ----------
    db.execute("DELETE FROM authorization WHERE scope_kind='share' AND subject_id='p_xiaoyu_b'")
    card2 = ctx(db, device, task_id)
    likes2 = req_of(card2, "likes")
    check("撤回授权后该项 pending", likes2.get("state") == "pending", likes2)
    check("撤回后不进搜索词", "画画" not in card2["search_terms"], card2["search_terms"])
    check("撤回后不进导出文本", "画画" not in card2["export_text"], card2["export_text"])

    # ---------- 3. 授权过期 ----------
    db = fresh()
    task_id = make_task(db)
    device = make_device(db)
    db.execute("UPDATE authorization SET valid_until='2020-01-01'"
               " WHERE scope_kind='share' AND subject_id='p_xiaoyu_b'")
    card3 = ctx(db, device, task_id)
    check("授权过期后该项 pending", req_of(card3, "likes").get("state") == "pending",
          req_of(card3, "likes"))

    # ---------- 3b. 授权备注写明「仅供查看」：不进导出与搜索 ----------
    db = fresh()
    task_id = make_task(db)
    device = make_device(db)
    db.execute("UPDATE authorization SET note=? WHERE subject_id='p_xiaoyu_b'",
               ("允许告诉 A 孩子的兴趣，仅供查看，不得外传",))
    card_v = ctx(db, device, task_id)
    check("仅查看授权后该项 pending",
          req_of(card_v, "likes").get("state") == "pending", req_of(card_v, "likes"))
    check("仅查看授权不进导出文本", "画画" not in card_v["export_text"],
          card_v["export_text"])
    check("仅查看授权不进搜索词", "画画" not in card_v["search_terms"],
          card_v["search_terms"])

    # ---------- 3c. 写清可用于本次选购的正例：可以外发 ----------
    db = fresh()
    task_id = make_task(db)
    device = make_device(db)
    db.execute("UPDATE authorization SET note=? WHERE subject_id='p_xiaoyu_b'",
               ("允许把孩子的兴趣告诉 A，用于准备礼物",))
    card_p = ctx(db, device, task_id)
    # 授权写明了购物用途：需求卡里能看到这个值（本人自己看，不外发）。
    check("写清购物用途后需求卡可见",
          req_of(card_p, "likes").get("state") == "known"
          and req_of(card_p, "likes").get("value") == "画画",
          req_of(card_p, "likes"))
    # 有购物用途许可时，短值偏好进入复制载荷——否则「换页面不用重讲需求」
    # 就落空了。但仍要拦长句子（性格描述、家长本次要求这类）。
    check("授权后偏好进入复制载荷", "画画" in card_p["export_text"],
          card_p["export_text"])
    check("预算保留", "300" in card_p["export_text"], card_p["export_text"])
    check("排除项进入复制载荷", "动物绘本" in card_p["export_text"],
          card_p["export_text"])
    # 长句子不带：B 侧的性格描述「初见陌生人时慢热…」是自由文本，不外发。
    check("长句自由文本不外发", "慢热" not in card_p["export_text"],
          card_p["export_text"])
    check("家长本次要求不外发", "不发声" not in card_p["export_text"],
          card_p["export_text"])

    # ---------- 4. 只授权部分字段：未授权的仍 pending ----------
    db = fresh()
    task_id = make_task(db)
    device = make_device(db)
    db.execute("UPDATE authorization SET attributes_json=? WHERE subject_id='p_xiaoyu_b'",
               (json.dumps(["喜欢颜色"]),))
    card4 = ctx(db, device, task_id)
    check("未授权字段 pending", req_of(card4, "likes").get("state") == "pending",
          req_of(card4, "likes"))

    # ---------- 5. B 侧纠正后版本变化且新值生效 ----------
    db = fresh()
    task_id = make_task(db)
    device = make_device(db)
    before = ctx(db, device, task_id)
    db.execute("UPDATE fact SET value=?, observed_on=? WHERE fact_id='f_xy_draw'",
               ("拼图", "2026-10-14"))
    after = ctx(db, device, task_id)
    check("纠正后版本号变化", before["context_revision"] != after["context_revision"],
          (before["context_revision"], after["context_revision"]))
    check("纠正后新值进入需求", req_of(after, "likes").get("value") == "拼图",
          req_of(after, "likes"))
    # 搜索词只保留结构化品类，兴趣这类自由文本默认不进。
    check("搜索词默认不含自由文本", "拼图" not in after["search_terms"],
          after["search_terms"])
    check("同样状态版本号稳定", ctx(db, device, task_id)["context_revision"]
          == after["context_revision"], "重复请求版本号应一致")

    # ---------- 6. pending 不留占位值 ----------
    check("品类 pending 时值为空", req_of(after, "category").get("value") == "",
          req_of(after, "category"))
    check("明确要求 pending 时值为空",
          req_of(after, "hard_constraints").get("value") == "",
          req_of(after, "hard_constraints"))

    # ---------- 7. 别人的事项 404 ----------
    try:
        ctx(db, device, "task_not_mine")
        check("别人的事项被拒", False, "没有抛出")
    except service.ExtError as exc:
        check("别人的事项被拒", exc.status == 404, exc.status)

    # 无凭证/错凭证的 401/403 由 tests/test_context_http.py 在真实 HTTP 栈上验，
    # 服务层单测不构造 None 凭证：那只会抛 TypeError，证明不了接口返回 401。

    # ---------- 9. 导出文本匿名：不含人名电话地址 ----------
    db = fresh()
    task_id = make_task(db)
    device = make_device(db)
    db.execute(
        "UPDATE task SET extracted_json=? WHERE task_id=?",
        (json.dumps({"subject_id": "p_xiaoyu", "budget_max": 300,
                     "occasion": "生日", "avoid_repeat": True,
                     "hard_constraints": ["别买会响的", "送到小雨家 3 栋 13800138000"]},
                    ensure_ascii=False), task_id))
    card9 = ctx(db, device, task_id)
    text = card9["export_text"]
    check("导出不含人名", "小雨" not in text, text)
    check("导出不含电话", "13800138000" not in text, text)
    check("导出不含地址门牌", "3 栋" not in text and "栋" not in text, text)
    check("导出用关系称呼", "朋友的孩子" in text, text)
    terms_text = " ".join(card9["search_terms"])
    check("搜索词不含人名电话地址",
          "小雨" not in terms_text and "13800138000" not in terms_text, terms_text)

    # ---------- 10. 同名不同人不串 ----------
    db = fresh()
    task_id = make_task(db, subject_id="p_xiaohan")
    device = make_device(db)
    card10 = ctx(db, device, task_id)
    # 种子数据里小宇的授权备注只写「允许告诉 A」，没写可用于本次选购，
    # 按「必须显式许可」的规则它是 pending。这里验的是不串人：
    # 既不是小雨的「画画」，也不会把 A 自己记的值当成对方授权的值。
    check("小宇的兴趣不串成小雨的画画", req_of(card10, "likes").get("value") != "画画",
          req_of(card10, "likes"))
    check("小宇的兴趣没有购物用途许可时 pending",
          req_of(card10, "likes").get("state") == "pending", req_of(card10, "likes"))

    # ---------- 10b. 同名但无显式关联：不串到别人家的记录 ----------
    db = fresh()
    # A 侧新建一个也叫「小雨」的人物，但不建 person_link。
    dup_id = dbm.new_id("person")
    db.execute(
        "INSERT INTO person(person_id, owner_id, name, relation, note, attributes_json)"
        " VALUES(?,?,?,?,?,?)",
        (dup_id, "A", "小雨", "friend_child", "另一位朋友的孩子", "{}"))
    task_dup = make_task(db, subject_id=dup_id)
    card_dup = ctx(db, task_dup and make_device(db), task_dup)
    check("同名无关联时不取对方记录",
          req_of(card_dup, "likes").get("state") == "pending", req_of(card_dup, "likes"))
    check("同名无关联时导出不含对方兴趣", "画画" not in card_dup["export_text"],
          card_dup["export_text"])
    # 反过来：断了关联的已关联人物也不该继续读到 B 侧记录。
    db.execute("UPDATE person_link SET status='revoked' WHERE person_id_a='p_xiaoyu'")
    task_rl = make_task(db, subject_id="p_xiaoyu")
    card_rl = ctx(db, make_device(db), task_rl)
    check("关联被撤销后不再读对方记录",
          req_of(card_rl, "likes").get("state") == "pending", req_of(card_rl, "likes"))

    # ---------- 11. 禁止优先：句子里同时有许可字样也必须拒绝 ----------
    for note, why in (
        ("不得用于购物，仅供查看", "句中含「用于购物」但被否定"),
        ("不可用于购买或转述", "句中含「用于购买」但被否定"),
    ):
        db = fresh()
        task_id = make_task(db)
        device = make_device(db)
        db.execute("UPDATE authorization SET note=? WHERE subject_id='p_xiaoyu_b'",
                   (note,))
        card_n = ctx(db, device, task_id)
        check("禁止优先：%s" % why,
              req_of(card_n, "likes").get("state") == "pending"
              and "画画" not in card_n["export_text"],
              {"req": req_of(card_n, "likes"), "why": why})

    # ---------- 12. 含糊用途：没写购物也不拒绝外的其他用途，一律 pending ----------
    for note in ("允许 A 查看兴趣", "仅用于活动安排", "可用于家庭内部沟通"):
        db = fresh()
        task_id = make_task(db)
        device = make_device(db)
        db.execute("UPDATE authorization SET note=? WHERE subject_id='p_xiaoyu_b'",
                   (note,))
        card_a = ctx(db, device, task_id)
        check("含糊用途不放行：%s" % note,
              req_of(card_a, "likes").get("state") == "pending", note)

    # ---------- 汇总 ----------
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + name
              + ((" | " + str(detail)) if not ok else ""), flush=True)
    print("需求卡授权边界：%d/%d 通过" % (passed, len(results)), flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
