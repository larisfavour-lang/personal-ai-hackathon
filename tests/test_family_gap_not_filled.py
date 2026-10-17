"""无关成人不得被数成家庭、填掉第三家缺口（t71）回归。

边界缺陷：建送礼对象后，`start_group_activity` 把「没有家庭关系的成人」也数成家庭，
`missing_families` 从 1 变 0（家庭数 3→5），「周末三家」的第三家缺口被悄悄填掉。
这和产品明令的「第三家未知就保留缺口，不拿配偶凑数」是同一条边界——只是换成拿送礼对象凑数。

根因：`family_overview` 的规则 3「没有明确家庭关系的成人都照实单独列出」是**对**的
（展示要如实），但 `start_group_activity` 拿 `len(families)` 当「已知家庭户数」是错的：
一个没有配偶/同户关系的成人不是一户家庭。

修法：`family_overview` 额外返回 `household_count`（只加字段，不改 `families` 展示），
判据是「is_me（本人自己这一方，一个人也是一户）或组里有 same_family_confirmed 的成人
（并查集被 spouse_pairs 真正合并过）」；`start_group_activity` 改消费这个字段。

覆盖（离线，不连网、不调业务模型）：
  A. 3 个无家庭关系的成人（送礼对象/朋友），说要 3 家，仍报缺 1 家；
  B. 真实家庭户数达到 3 家时报不缺；
  C. 家庭 + 无关成人混合时，只数家庭；
  D. family_overview 展示语义未改（无关成人仍照实单独列出）；
  E. 既有「未知不造」「不拿配偶凑数」断言仍通过。

运行：/tmp/hack_venv/bin/python tests/test_family_gap_not_filled.py
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

OUT = ROOT / "docs" / "evidence" / "family-gap-not-filled.txt"
results: list[tuple[str, bool, object]] = []


def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))


class StubClient:
    """离线模型桩：start_group_activity 不走模型，这里只为签名占位。"""

    def chat_json(self, messages, **kw):
        return {}


def fresh_db():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pa-gap-")) / "gap.db"
    db = dbm.DB(tmp)
    dbm.seed(db)
    return db


def add_lone_adult(db, name, relation="other"):
    pid = dbm.new_id("p")
    db.execute("INSERT INTO person(person_id,owner_id,name,relation,note,aliases,"
               "attributes_json) VALUES(?,?,?,?,'','[]','{}')",
               (pid, "A", name, relation))
    return pid


def start3(db, text="周末三家一起带孩子出去玩，我出 100 以内"):
    return service.start_group_activity(db, StubClient(), "A", text)


# --------------------------------------------------------------------------
# A. 无家庭关系的成人不填缺口
# --------------------------------------------------------------------------
def test_lone_adults_do_not_fill():
    db = fresh_db()
    base = start3(db)
    check("基线：说了 3 家，缺口是 1", base["missing_families"] == 1,
          base["missing_families"])

    # 三个没有任何配偶/同户关系的成人（模拟建了三个送礼对象）
    for n in ("大姨", "老王", "李老师"):
        add_lone_adult(db, n)
    ov = service.family_overview(db, "A")
    check("family_overview 仍把无关成人照实单独列出（展示语义没改）",
          len(ov["families"]) > base.get("_families", 0) + 2
          if False else len(ov["families"]) >= 5,
          [f["label"] for f in ov["families"]])
    after = start3(db)
    check("A 加了 3 个无关成人后，缺口仍是 1（不被他们填掉）",
          after["missing_families"] == 1, after["missing_families"])
    check("A household_count 不把无关成人算成户",
          ov["household_count"] == 2, ov["household_count"])


# --------------------------------------------------------------------------
# B. 真实家庭户数够了才报不缺
# --------------------------------------------------------------------------
def test_real_households_count():
    db = fresh_db()
    # 再造两个真实家庭：各自由一对明确配偶组成
    for tag in ("x", "y"):
        a, b = add_lone_adult(db, "甲%s" % tag), add_lone_adult(db, "乙%s" % tag)
        db.execute("INSERT INTO relation(relation_id,owner_id,from_person_id,"
                   "relation_type,to_person_id,label,source_utterance,observed_on)"
                   " VALUES(?,?,?,?,?,'','t','2026-10-01')",
                   ("r_%s" % tag, "A", a, "配偶", b))
    ov = service.family_overview(db, "A")
    check("B 两个新增真实家庭后 household_count = 4（我家 + 小周家 + 2）",
          ov["household_count"] == 4, ov["household_count"])
    st = start3(db)
    check("B 真实户数 4 家 ≥ 说要 3 家，缺口为 0",
          st["missing_families"] == 0, st["missing_families"])


# --------------------------------------------------------------------------
# C. 家庭 + 无关成人混合，只数家庭
# --------------------------------------------------------------------------
def test_mixed():
    db = fresh_db()
    a, b = add_lone_adult(db, "丙"), add_lone_adult(db, "丁")
    db.execute("INSERT INTO relation(relation_id,owner_id,from_person_id,"
               "relation_type,to_person_id,label,source_utterance,observed_on)"
               " VALUES(?,?,?,?,?,'','t','2026-10-01')",
               ("r_cd", "A", a, "配偶", b))
    # 再来两个无关成人
    add_lone_adult(db, "戊")
    add_lone_adult(db, "己")
    ov = service.family_overview(db, "A")
    # 我家 + 小周家 + 丙丁家 = 3 户；戊己是无关成人，不算
    check("C 混合场景只数家庭：household_count = 3", ov["household_count"] == 3,
          ov["household_count"])
    check("C 展示里无关成人仍单独列出（条目数 > 户数）",
          len(ov["families"]) > ov["household_count"],
          {"families": len(ov["families"]),
           "households": ov["household_count"]})
    st = start3(db)
    check("C 要 3 家、真实户数 3 家，缺口 0", st["missing_families"] == 0,
          st["missing_families"])


# --------------------------------------------------------------------------
# D. 既有边界不断
# --------------------------------------------------------------------------
def test_existing_boundaries():
    db = fresh_db()
    st = start3(db)
    reply = st.get("reply") or ""
    check("D 回复仍说第三家没有记录、先留着待确认（不造家庭）",
          "先留着待确认" in reply or "没有记录" in reply, reply[:160])
    check("D 回复里不给「点一个孩子」这种单选引导",
          "点一个就行" not in reply and "选一个孩子" not in reply, reply[:160])
    # 不拿配偶凑数：A 只认识小周一家，说不出第三家
    check("D 缺口仍是 1（不拿认识的人凑数）", st["missing_families"] == 1,
          st["missing_families"])


def main():
    test_lone_adults_do_not_fill()
    test_real_households_count()
    test_mixed()
    test_existing_boundaries()
    ok = sum(1 for _, p, _ in results if p)
    lines = ["第三家缺口不被无关成人填掉：%d/%d 通过" % (ok, len(results)),
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
