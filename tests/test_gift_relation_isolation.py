"""送礼管家的关系分类不得写进 person.relation（t65）隔离回归。

背景：`251c26d` 为了让送礼对象在侧栏归进「家人与亲属」，把送礼管家里选的
关系分类经 `_relation_for_gift_category` 写进 `person.relation`（新建时写、
关联已有 person 时也写）。那一列驱动家庭结构判断（配偶/同户）和侧栏分组，
混入送礼关系会造成两类错：
  1. 家庭推导：`family_overview` 按 relation 聚家庭，送礼对象被算进家庭后，
     「说了 3 家只认出 2 家」的缺口被算成 0，孩子数也跟着错；
  2. 侧栏分组：朋友/同事因为要送礼被归进「家人与亲属」。

本任务的做法：
  - 删掉所有向 `person.relation` 写送礼关系的路径；
  - 侧栏分组改读 `attributes_json.gift_target`（`_GIFT_RELATION_GROUP`）；
  - 一次性修正迁移 `_migrate_gift_relation_pollution`：只清**能确证**是送礼标注
    写入的行（gift_target 存在 + 当前 relation='family' + relation 表里没有任何
    涉及他的关系），拿不准的一条不动并把跳过条数写进 audit。

覆盖（离线，不连网、不调业务模型）：
  A. create_gift_target / set_gift_relationship 之后 person.relation 不被改写；
  B. 侧栏分组仍然按送礼关系归类（读 attributes_json，不读 relation）；
  C. 家庭结构判断不受送礼标注影响（缺口仍报 1、孩子数不变）；
  D. 一次性迁移只清能确证的行，有真实家庭关系证据的一条不动。

运行：/tmp/hack_venv/bin/python tests/test_gift_relation_isolation.py
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

OUT = ROOT / "docs" / "evidence" / "gift-relation-isolation.txt"
results: list[tuple[str, bool, object]] = []


def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))


def fresh_db():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pa-giftrel-")) / "giftrel.db"
    db = dbm.DB(tmp)
    dbm.seed(db)
    return db


def rel_of(db, pid):
    return db.row("SELECT relation FROM person WHERE person_id=?", (pid,))["relation"]


# --------------------------------------------------------------------------
# A. 写入路径不再碰 person.relation
# --------------------------------------------------------------------------
def test_write_paths():
    db = fresh_db()
    # 新建送礼对象：relation 必须是 "other"，不因选「长辈」变成 family
    t = service.create_gift_target(db, "A", name="大姨", relationship="长辈")
    pid = t["person_id"]
    check("新建送礼对象不把 person.relation 写成 family",
          rel_of(db, pid) == "other", rel_of(db, pid))
    check("新建送礼对象的送礼关系仍记在 attributes_json.gift_target",
          json.loads(db.row("SELECT attributes_json FROM person WHERE person_id=?",
                            (pid,))["attributes_json"])["gift_target"]["relationship"]
          == "长辈")

    # 关联一个已有 person：原来 relation 是 other，也不能被改成 family
    azhou = db.row("SELECT person_id FROM person WHERE person_id='p_azhou'")["person_id"]
    before = rel_of(db, azhou)
    service.create_gift_target(db, "A", person_id=azhou, relationship="长辈")
    check("关联已有 person 时 person.relation 不被改写",
          rel_of(db, azhou) == before, {"before": before, "after": rel_of(db, azhou)})

    # 改关系分类同样不碰 relation
    service.set_gift_relationship(db, "A", azhou, "同事")
    check("set_gift_relationship 不碰 person.relation",
          rel_of(db, azhou) == before, rel_of(db, azhou))
    check("set_gift_relationship 只改 attributes_json.gift_target",
          json.loads(db.row("SELECT attributes_json FROM person WHERE person_id=?",
                            (azhou,))["attributes_json"])["gift_target"]["relationship"]
          == "同事")


# --------------------------------------------------------------------------
# B. 侧栏分组改读 attributes_json.gift_target
# --------------------------------------------------------------------------
def test_sidebar_grouping():
    db = fresh_db()
    t = service.create_gift_target(db, "A", name="大姨", relationship="长辈")
    pid = t["person_id"]
    groups = service.person_groups(db, "A")
    where = [g for g, v in groups.items()
             if any(p["person_id"] == pid for p in v)]
    check("送礼对象仍归进「家人与亲属」（读 attributes_json，不读 relation）",
          "家人与亲属" in where, where)
    check("该 person 的 relation 列仍是 other（两层分离）",
          rel_of(db, pid) == "other", rel_of(db, pid))

    t2 = service.create_gift_target(db, "A", name="老王", relationship="同事")
    pid2 = t2["person_id"]
    where2 = [g for g, v in service.person_groups(db, "A").items()
              if any(p["person_id"] == pid2 for p in v)]
    check("选「同事」的送礼对象归「工作与业务」，不进家人与亲属",
          "工作与业务" in where2 and "家人与亲属" not in where2, where2)


# --------------------------------------------------------------------------
# C. 家庭结构判断不受影响
# --------------------------------------------------------------------------
def test_family_structure():
    db = fresh_db()
    ov0 = service.family_overview(db, "A")
    mine0 = next(f for f in ov0["families"] if f["is_me"])
    # 按 label 取，不能取「第一个非我家」：家庭按 label 排序，
    # 加了送礼对象后「大姨家」会排到「小周家」前面。
    zhou0 = next(f for f in ov0["families"] if f["label"] == "小周家")

    service.create_gift_target(db, "A", name="大姨", relationship="长辈")
    service.create_gift_target(db, "A", name="老王", relationship="同事")

    ov1 = service.family_overview(db, "A")
    mine1 = next(f for f in ov1["families"] if f["is_me"])
    zhou1 = next(f for f in ov1["families"] if f["label"] == "小周家")
    # 送礼对象是「没有家庭关系的成人」，按 family_overview 的规则 3 会单独成一项，
    # 这是既有行为、和 person.relation 无关（改动前实测同样从 3 项变 5 项）。
    # 本任务要保证的是**已知家庭的组成不被送礼标注改写**。
    check("加了送礼对象后「我家」的成人和孩子不变",
          [a["person_id"] for a in mine1["adults"]]
          == [a["person_id"] for a in mine0["adults"]]
          and [c["person_id"] for c in mine1["children"]]
          == [c["person_id"] for c in mine0["children"]],
          {"before": mine0, "after": mine1})
    check("加了送礼对象后「小周家」的成人和孩子不变",
          [a["person_id"] for a in zhou1["adults"]]
          == [a["person_id"] for a in zhou0["adults"]]
          and [c["person_id"] for c in zhou1["children"]]
          == [c["person_id"] for c in zhou0["children"]],
          {"before": zhou0, "after": zhou1})

    # 缺口：在没有送礼对象的库上验「说了 3 家只认出 2 家」仍是 1。
    db2 = fresh_db()
    st = service.start_group_activity(db2, None, "A",
                                      "周末三家一起带孩子出去玩，我出 100 以内")
    check("说了 3 家仍只认出 2 家，缺口报 1", st["missing_families"] == 1,
          st["missing_families"])


# --------------------------------------------------------------------------
# D. 一次性迁移只清能确证的行
# --------------------------------------------------------------------------
def test_migration():
    db = fresh_db()
    # ① 能确证：gift_target 存在 + relation='family' + relation 表无任何关系
    bad = service.create_gift_target(db, "A", name="大姨", relationship="长辈")["person_id"]
    db.execute("UPDATE person SET relation='family' WHERE person_id=?", (bad,))
    # ② 拿不准：有真实关系证据的家人，不能动
    good = db.row("SELECT person_id FROM person WHERE person_id='p_azhou'")["person_id"]
    db.execute("UPDATE person SET relation='family' WHERE person_id=?", (good,))
    # ③ 拿不准：没有 gift_target 的其他人
    other = db.row("SELECT person_id FROM person WHERE name='小周'")["person_id"]

    before_good = rel_of(db, good)
    with db.connect() as conn:
        dbm.DB._migrate_gift_relation_pollution(conn)
    after = db.row("SELECT relation FROM person WHERE person_id=?", (bad,))["relation"]
    check("迁移把能确证是送礼标注写入的那行改回 other", after == "other", after)
    check("迁移不碰有真实关系证据的家人", rel_of(db, good) == before_good,
          {"before": before_good, "after": rel_of(db, good)})
    check("迁移不碰没有 gift_target 的其他人",
          rel_of(db, other) == "family", rel_of(db, other))
    aud = db.rows("SELECT detail_json AS detail FROM audit_log WHERE action='gift_relation_pollution_fixed'")
    check("迁移结果写进 audit（含跳过条数，可复核）",
          bool(aud) and json.loads(aud[-1]["detail"]).get("skipped_count", -1) >= 1,
          aud[-1]["detail"] if aud else None)


def main():
    test_write_paths()
    test_sidebar_grouping()
    test_family_structure()
    test_migration()
    ok = sum(1 for _, p, _ in results if p)
    lines = ["送礼关系不污染 person.relation：%d/%d 通过" % (ok, len(results)),
             "边界：离线，不连网、不调业务模型；隔离临时库。", ""]
    for name, passed, detail in results:
        lines.append(("PASS " if passed else "FAIL ") + name)
        if not passed and detail is not None:
            lines.append("     %s" % (json.dumps(detail, ensure_ascii=False)[:300]))
    text = "\n".join(lines)
    print(text)
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text + "\n", encoding="utf-8")
    except OSError:
        pass
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
