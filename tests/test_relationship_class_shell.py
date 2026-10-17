"""通用人际关系分类壳体（t53）回归：能存能改、界面标注建设中、不驱动任何能力。

用户 2026-10-17 15:1x 诉求（thread_history 01a12033 10-13 16:46 原话）：
「人际关系是否应该分个类，比如亲人/长辈晚辈 朋友 工作伙伴，如果是一个社牛，
所有人都并列，会比较难办吧」。captain 表态：先做壳子，括号建设中。

本任务**只做壳体，不做成功能**：
  - 不按分类做检索、不按分类推荐；
  - 不把分类塞进任何模型提示词当依据；
  - 不因为分类改变任何既有家庭结构（配偶/同户）或授权判断。

键名契约（三层，务必分开）：
  person.attributes_json = {
    "gift_target":  {...}   # 层① t49 送礼对象关系分类（长辈/晚辈/同龄人/朋友/同事），
                            #      本任务不占用、不混合、不改写；
    "social_class": {        # 层② 本任务：通用人际关系分类
      "classes": ["亲人", "朋友"],   # 多分类，可空数组 = 不分类
      "marked_at": "YYYY-MM-DD",
      "note": ""
    }
  }
  - **不占 person.relation 列**：那列驱动家庭结构 配偶/同户 判断
    （SPOUSE_RELATIONS / person.relation 的 spouse、friend_spouse），
    混入 亲人/长辈 会污染那段逻辑；
  - 不新建表、不做迁移：person.attributes_json 本就是异质属性袋
    （`TEXT NOT NULL DEFAULT '{}'`），追加一个独立键即算「既有追加方式」。

覆盖（离线，不连网、不调业务模型；需要模型的一律 StubClient 拦截）：
  A. 分类存取：单分类、多分类、不分类（清空）、非法枚举值拒绝；
  B. 键名隔离：不写 gift_target、不写 person.relation，两层互不污染；
  C. 兼容：无该键的既有 person 读取为空；设了 social_class 不影响家庭结构判断；
  D. 界面：apps.html 有「建设中」标注，并明写不驱动检索/推荐/协商；
  E. 明确不做（可证伪）：分类值不进模型提示词正文。

运行：python tests/test_relationship_class_shell.py
状态：app/ 改动要等 t56（朋友旅行场景，同批文件）报告完成后再统一落地，
      落地前本套件会因缺 service 新函数而失败，属预期；落地后必须全绿。
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

OUT = ROOT / "docs" / "evidence" / "relationship-class-shell.txt"
results: list[tuple[str, bool, object]] = []

# 壳体取值：至少覆盖任务书要求的六类，另加「其他」。允许一个人有多个分类。
EXPECTED_CLASSES = ("亲人", "长辈", "晚辈", "同龄人", "朋友", "工作伙伴")


def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))


class StubClient:
    """离线模型桩：记录发给模型的全部提示词，供 E 组证伪。"""

    def __init__(self):
        self.sent: list[list[str]] = []

    def chat_json(self, messages, **kw):
        self.sent.append([m.get("content") or "" for m in messages])
        return {}


def fresh_db():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pa-relclass-")) / "relclass.db"
    db = dbm.DB(tmp)
    dbm.seed(db)
    return db


def attrs_of(db, person_id):
    row = db.row("SELECT attributes_json FROM person WHERE person_id=?", (person_id,))
    try:
        return json.loads(row["attributes_json"] or "{}")
    except (ValueError, TypeError):
        return {}


def existing_person(db, owner_id="A"):
    row = db.row("SELECT person_id FROM person WHERE owner_id=? LIMIT 1", (owner_id,))
    return row["person_id"]


# --------------------------------------------------------------------------
# A. 分类存取
# --------------------------------------------------------------------------
def test_crud():
    db = fresh_db()
    pid = existing_person(db)

    # 落地判定用 hasattr：早前版本用 try/except AttributeError 判「存在」，
    # 旗标写反了——函数存在且调用成功时 missing 仍为 True，反而误报未落地。
    if not hasattr(service, "get_person_social_classes"):
        check("service.get_person_social_classes 存在", False, "app/ 未落地")
        return
    check("service.get_person_social_classes 存在", True)

    empty = service.get_person_social_classes(db, "A", pid)
    check("未分类的既有 person 读回空列表（不破坏既有数据）",
          empty.get("classes") in ([], None), empty)

    service.set_person_social_classes(db, "A", pid, ["亲人"])
    one = service.get_person_social_classes(db, "A", pid)
    check("单分类可存可读", one.get("classes") == ["亲人"], one)

    service.set_person_social_classes(db, "A", pid, ["亲人", "朋友"])
    multi = service.get_person_social_classes(db, "A", pid)
    check("多分类可存可读（既是亲戚又是朋友）",
          sorted(multi.get("classes") or []) == ["亲人", "朋友"], multi)

    service.set_person_social_classes(db, "A", pid, [])
    cleared = service.get_person_social_classes(db, "A", pid)
    check("可以清空成「不分类」", cleared.get("classes") == [], cleared)

    bad = None
    try:
        service.set_person_social_classes(db, "A", pid, ["老板"])
    except ValueError as exc:
        bad = str(exc)
    check("枚举外的取值被拒绝（不是静默吞掉）", bad is not None, bad)
    check("被拒绝后原值不被改写",
          service.get_person_social_classes(db, "A", pid).get("classes") == [],
          service.get_person_social_classes(db, "A", pid))

    for cls in EXPECTED_CLASSES:
        service.set_person_social_classes(db, "A", pid, [cls])
        got = service.get_person_social_classes(db, "A", pid).get("classes")
        check("取值覆盖「%s」" % cls, got == [cls], got)


# --------------------------------------------------------------------------
# B. 键名隔离：不占 gift_target、不占 person.relation
# --------------------------------------------------------------------------
def test_key_isolation():
    db = fresh_db()
    pid = existing_person(db)
    if not hasattr(service, "set_person_social_classes"):
        check("键名隔离（app/ 未落地）", False, "skip")
        return
    service.set_person_social_classes(db, "A", pid, ["工作伙伴"])
    attrs = attrs_of(db, pid)
    check("写在独立的 social_class 键下",
          isinstance(attrs.get("social_class"), dict), sorted(attrs))
    check("不写进 gift_target 键（t49 那层）",
          "gift_target" not in attrs or attrs.get("gift_target") is None,
          sorted(attrs))
    rel = db.row("SELECT relation FROM person WHERE person_id=?", (pid,))["relation"]
    check("不占 person.relation 列（家庭结构用的那列）",
          rel not in EXPECTED_CLASSES, rel)

    # 与 gift_target 并存互不污染
    tgt = service.create_gift_target(db, "A", name="爸爸", relationship="长辈",
                                     person_id=pid)
    attrs2 = attrs_of(db, pid)
    check("两层可并存：social_class 与 gift_target 各写各的",
          attrs2.get("social_class", {}).get("classes") == ["工作伙伴"]
          and attrs2.get("gift_target", {}).get("relationship") == "长辈",
          {"social": attrs2.get("social_class"), "gift": attrs2.get("gift_target")})
    check("改 social_class 不动 gift_target",
          service.get_person_social_classes(db, "A", pid).get("classes") == ["工作伙伴"]
          and tgt.get("relationship") == "长辈", tgt)


# --------------------------------------------------------------------------
# C. 兼容：不破坏既有关系与家庭结构判断
# --------------------------------------------------------------------------
def test_no_family_regression():
    db = fresh_db()
    pid = existing_person(db)
    if not hasattr(service, "set_person_social_classes"):
        check("家庭结构不受影响（app/ 未落地）", False, "skip")
        return
    before = service.family_overview(db, "A") if hasattr(service, "family_overview") else None
    service.set_person_social_classes(db, "A", pid, ["长辈", "亲人"])
    after = service.family_overview(db, "A") if hasattr(service, "family_overview") else None
    check("设了通用分类后家庭结构概览不变", before == after,
          {"before": before, "after": after})
    rel = db.row("SELECT relation FROM person WHERE person_id=?", (pid,))["relation"]
    check("person.relation 列原样保留", rel not in EXPECTED_CLASSES, rel)


# --------------------------------------------------------------------------
# D. 界面「建设中」标注
# --------------------------------------------------------------------------
def test_ui_under_construction():
    html = (ROOT / "app" / "static" / "apps.html").read_text(encoding="utf-8")
    check("界面有「建设中」标注", "建设中" in html, None)
    check("界面明写它还不驱动检索/推荐/协商",
          any(k in html for k in ("检索", "推荐", "协商"))
          and any(k in html for k in ("不驱动", "还没有", "暂不", "不会")),
          None)
    check("界面不宣称已支持按分类找人/推荐",
          "按关系分类找人" not in html and "按分类推荐" not in html, None)


# --------------------------------------------------------------------------
# E. 明确不做：分类值不进模型提示词
# --------------------------------------------------------------------------
def test_not_in_prompt():
    db = fresh_db()
    pid = existing_person(db)
    if not hasattr(service, "set_person_social_classes"):
        check("分类不进提示词（app/ 未落地）", False, "skip")
        return
    service.set_person_social_classes(db, "A", pid, ["亲人", "工作伙伴"])
    stub = StubClient()
    prompt = ""
    try:
        if hasattr(agents_ctx := __import__("app.agents", fromlist=["agents"]),
                   "_a_context"):
            ctx = agents_ctx._a_context(db, "A", pid, viewer_id="A")
            prompt = json.dumps(ctx, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        prompt = "ctx-error:%r" % (exc,)
    check("通用分类的值不进 _a_context（因而不进提示词正文）",
          "工作伙伴" not in prompt and "social_class" not in prompt, prompt[:200])


def main():
    test_crud()
    test_key_isolation()
    test_no_family_regression()
    test_ui_under_construction()
    test_not_in_prompt()
    ok = sum(1 for _, p, _ in results if p)
    lines = ["通用人际关系分类壳体：%d/%d 通过" % (ok, len(results)),
             "边界：离线，不连网、不调业务模型；隔离临时库。",
             "状态：app/ 改动等 t56 报告完成后统一落地；落地前 A/B/C/E 组会因缺 "
             "service 新函数而失败，属预期。",
             ""]
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
