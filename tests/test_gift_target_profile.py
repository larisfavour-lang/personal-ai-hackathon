"""送礼对象档案：关系分类、偏好/尺寸/忌讳/上次送过什么（t49）回归。

补回最初提出、在上轮主场景切换中丢掉的「送礼对象档案」能力。复用既有机制，
不建平行表：档案条目 = life_event(event_type='gift_profile')，更正走 supersede、
撤回走 tombstone、来源投影 + 授权走 event_grant、provenance 走 audit/source_fingerprint。
关系分类只存一份在 person.attributes_json，不进每条 payload、不占 person.relation 列
（relation 驱动家庭结构 配偶/同户 判断，混入 长辈/朋友 会污染那段逻辑）。

键名契约（两层，务必分开，t53 的通用人际关系分类会在此之上另起一层，不得占用/混合）：
  person.attributes_json = {
    "gift_target": {                       # 送礼对象关系分类层（本任务，gift 专用，命名空间隔离）
      "relationship": "长辈|晚辈|同龄人|朋友|同事",   # GIFT_RELATIONSHIP_CATEGORIES 之一
      "marked_at": "YYYY-MM-DD",
      "note": ""
    }
    # t53 的「通用人际关系分类」(亲人/长辈/晚辈/同龄人/朋友/工作伙伴) 是另一层，
    # 用各自独立的键/结构，本任务不预设、不占用、不混为一个字段。
  }
  life_event 档案条目：
    event_type="gift_profile", source_kind="gift_profile", subject_id=<person_id>,
    payload_json={"category": "preference|size|avoid|last_gift", "value": "..."},
    happened_on, provider, status/superseded_by。

覆盖（离线，不连网、不调业务模型；需要模型的一律 StubClient 拦截）：
  A. 档案增改撤、更正后旧值仅作来源可查、关系分组、重复提交幂等；
  B. gift_target_context 授权门：未授权条目一个字段都不进（可证伪：断言提示词正文
     不出现未授权 value）；已授权也只进本事项相关的那几条；
  C. 主对话接线：_a_context 带入已授权档案 + 上次送过什么；B 侧/跨家庭不外泄；
  D. 推荐依据来自实际档案；档案为空不得用常见款凑、不编造未知字段；
  E. 路由层（独立临时服务/临时库，纯数据路由，不触发模型）。

运行：python tests/test_gift_target_profile.py
状态：本文件为待实现规格；app/ 改动在 t43 录制完成后统一落地，落地前本套件会因
      缺 service/agents 新函数而失败，属预期。
"""
from __future__ import annotations

import json
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db as dbm  # noqa: E402
from app import service  # noqa: E402
from app import agents  # noqa: E402

OUT = ROOT / "docs" / "evidence" / "gift-target-profile.txt"
results: list[tuple[str, bool, object]] = []


def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))


class StubClient:
    """离线模型桩：记录发给模型的全部提示词，按提示词内容路由返回合成 JSON。"""

    def __init__(self, router=None):
        self.sent: list[list[str]] = []
        self.router = router or (lambda prompt: {})

    def chat_json(self, messages, **kw):
        parts = [m.get("content") or "" for m in messages]
        self.sent.append(parts)
        prompt = "\n".join(parts)
        out = self.router(prompt)
        if not isinstance(out, dict):
            out = {}
        return out


def fresh_db():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pa-gift-")) / "gift.db"
    db = dbm.DB(tmp)
    dbm.seed(db)
    return db


def all_prompt_text(stub):
    return "\n".join("\n".join(x) for x in stub.sent)


# --------------------------------------------------------------------------
# A. 档案增改撤 / supersede 只留来源 / 关系分组 / 幂等
# --------------------------------------------------------------------------
def test_crud_and_supersede():
    db = fresh_db()
    tgt = service.create_gift_target(db, "A", name="爸爸", relationship="长辈")
    pid = tgt["person_id"]
    check("新建送礼对象落到 person 且标记 attributes_json.gift_target",
          db.row("SELECT * FROM person WHERE person_id=?", (pid,)) is not None
          and json.loads(db.row("SELECT attributes_json FROM person WHERE person_id=?",
                                (pid,))["attributes_json"]).get("gift_target", {}).get("relationship") == "长辈")

    # 关系分类不占 person.relation 列（那是家庭结构用的）
    check("关系分类不写进 person.relation 列",
          db.row("SELECT relation FROM person WHERE person_id=?", (pid,))["relation"] != "长辈")

    e1 = service.add_gift_profile_entry(db, "A", pid, "preference", "喜欢黑色软底皮鞋")
    e2 = service.add_gift_profile_entry(db, "A", pid, "size", "42 码")
    check("新增偏好/尺寸两条档案条目",
          e1["category"] == "preference" and e2["category"] == "size")

    active = service.gift_target_entries(db, "A", pid)
    check("当前有效条目=2", len(active) == 2, [e["value"] for e in active])
    check("每条带来源/日期/提供者",
          all(e.get("provider") and e.get("happened_on") and e.get("event_id") for e in active))

    # 更正尺寸：旧值 superseded，仅作来源可查，不再当当前值
    fixed = service.correct_gift_profile_entry(db, "A", e2["event_id"], "43 码")
    active2 = service.gift_target_entries(db, "A", pid, category="size")
    check("更正后 size 当前值=43 码，仅一条", len(active2) == 1 and active2[0]["value"] == "43 码",
          [e["value"] for e in active2])
    hist = service.gift_target_entries(db, "A", pid, include_history=True)
    old = [e for e in hist if e["value"] == "42 码"]
    check("更正后旧值 42 码仍可查、状态 superseded、指向新记录",
          len(old) == 1 and old[0]["status"] == "superseded"
          and old[0].get("superseded_by") == fixed["event_id"],
          old)
    check("旧值不当成当前值（当前 size 只有 43 码）",
          all(e["value"] != "42 码" for e in service.gift_target_entries(db, "A", pid)))

    # 撤回偏好：tombstone，当前查询不可见
    service.revoke_gift_profile_entry(db, "A", e1["event_id"])
    after_revoke = service.gift_target_entries(db, "A", pid)
    check("撤回后偏好不再出现在当前条目",
          all(e["value"] != "喜欢黑色软底皮鞋" for e in after_revoke))
    check("撤回后条目仍留在历史里可查",
          any(e["value"] == "喜欢黑色软底皮鞋" and e["status"] == "revoked"
              for e in service.gift_target_entries(db, "A", pid, include_history=True)))


def test_relationship_grouping_and_enum():
    db = fresh_db()
    service.create_gift_target(db, "A", name="爸爸", relationship="长辈")
    service.create_gift_target(db, "A", name="大姨", relationship="长辈")
    service.create_gift_target(db, "A", name="老王", relationship="朋友")
    service.create_gift_target(db, "A", name="同桌小李", relationship="同龄人")
    listing = service.list_gift_targets(db, "A")
    groups = listing["groups"]
    check("按关系分类分组：长辈 2、朋友 1、同龄人 1",
          len(groups.get("长辈", [])) == 2 and len(groups.get("朋友", [])) == 1
          and len(groups.get("同龄人", [])) == 1, {k: len(v) for k, v in groups.items()})
    check("关系取值来自五个枚举之一",
          set(service.GIFT_RELATIONSHIP_CATEGORIES) == {"长辈", "晚辈", "同龄人", "朋友", "同事"})
    # 非法关系被拒
    try:
        service.set_gift_relationship(db, "A",
                                      service.create_gift_target(db, "A", name="临时", relationship="朋友")["person_id"],
                                      "不存在的类")
        check("非法关系分类被拒", False, "未抛错")
    except (ValueError, KeyError):
        check("非法关系分类被拒", True)


def test_idempotent_add():
    db = fresh_db()
    pid = service.create_gift_target(db, "A", name="爸爸", relationship="长辈")["person_id"]
    a = service.add_gift_profile_entry(db, "A", pid, "size", "42 码")
    b = service.add_gift_profile_entry(db, "A", pid, "size", "42 码")
    check("重复提交同一档案条目幂等（不新增第二条）",
          len(service.gift_target_entries(db, "A", pid, category="size")) == 1
          and a["event_id"] == b["event_id"], (a["event_id"], b["event_id"]))


# --------------------------------------------------------------------------
# B. 授权门：未授权条目一个字段都不进（可证伪：断言提示词正文无未授权 value）
# --------------------------------------------------------------------------
def test_authorization_gate_prompt():
    db = fresh_db()
    pid = service.create_gift_target(db, "A", name="爸爸", relationship="长辈")["person_id"]
    auth = service.add_gift_profile_entry(db, "A", pid, "preference", "偏好软底防滑")
    priv = service.add_gift_profile_entry(db, "A", pid, "avoid", "忌讳送烟酒")

    # A（所有者）看自己：两条都在
    ctx_a = service.gift_target_context(db, "A", pid, viewer_id="A")
    vals_a = [e["value"] for e in ctx_a["entries"]]
    check("所有者上下文含全部已授权档案", "偏好软底防滑" in vals_a and "忌讳送烟酒" in vals_a, vals_a)

    # B（另一家庭，未授权）看同一个人：一条字段都不该有
    ctx_b = service.gift_target_context(db, "A", pid, viewer_id="B")
    check("未授权查看者上下文为空", ctx_b["entries"] == [] and ctx_b["basis_count"] == 0, ctx_b["entries"])
    # 可证伪：拼进提示词的正文里不出现未授权 value
    prompt_b = agents._gift_profile_prompt(ctx_b)
    check("未授权条目 value 不出现在提示词正文（可证伪）",
          "偏好软底防滑" not in prompt_b and "忌讳送烟酒" not in prompt_b, prompt_b[:200])

    # 给 B 显式授权只看 preference 的 value 字段
    service.grant_gift_profile_field(db, "A", auth["event_id"], "B", ["value"])
    ctx_b2 = service.gift_target_context(db, "A", pid, viewer_id="B")
    vals_b2 = [e["value"] for e in ctx_b2["entries"]]
    check("授权后 B 只看到被授权的那一条", vals_b2 == ["偏好软底防滑"], vals_b2)
    prompt_b2 = agents._gift_profile_prompt(ctx_b2)
    check("授权后提示词只含被授权 value、不含未授权 value",
          "偏好软底防滑" in prompt_b2 and "忌讳送烟酒" not in prompt_b2, prompt_b2[:200])


# --------------------------------------------------------------------------
# C. 主对话接线：_a_context 带入已授权档案 + 上次送过什么；B 侧不外泄
# --------------------------------------------------------------------------
def _extract_router(subject_name):
    def router(prompt):
        if "整理成结构化记录变更" in prompt or "extract" in prompt.lower():
            return {"subject_name": subject_name, "product": "鞋", "budget": "", "avoid_repeat": False}
        if "候选" in prompt or "方案" in prompt or "推荐" in prompt:
            return {"content": "（合成候选）", "candidates": [], "reason": "依据档案", "basis": []}
        return {"content": "（合成）", "reason": "", "conflicts": []}
    return router


def test_main_conversation_brings_authorized_profile():
    db = fresh_db()
    pid = service.create_gift_target(db, "A", name="爸爸", relationship="长辈")["person_id"]
    service.add_gift_profile_entry(db, "A", pid, "preference", "走路多，要软底")
    service.add_gift_profile_entry(db, "A", pid, "last_gift", "去年送了羊毛护膝")
    service.add_gift_profile_entry(db, "A", pid, "size", "42 码")

    # _a_context 带入 gift_target_profile 块
    ctx = agents._a_context(db, "A", pid, {"product": "鞋"}, task_id="")
    block = ctx.get("gift_target_profile") or {}
    check("_a_context 带入送礼对象档案块", block.get("is_gift_target") is True, block.get("is_gift_target"))
    vals = [e["value"] for e in block.get("entries", [])]
    check("档案块含偏好/尺寸/上次送过什么",
          "走路多，要软底" in vals and "42 码" in vals and "去年送了羊毛护膝" in vals, vals)
    check("档案块带出上次送过什么", (block.get("last_gift") or {}).get("value") == "去年送了羊毛护膝",
          block.get("last_gift"))

    # 拼进模型提示词（端到端捕获）：授权值在、无编造
    stub = StubClient(_extract_router("爸爸"))
    prompt_text = agents._gift_profile_prompt(block)
    check("主对话提示词含已授权档案依据",
          "走路多，要软底" in prompt_text and "去年送了羊毛护膝" in prompt_text, prompt_text[:200])
    check("主对话提示词明示还缺什么（avoid 缺失，据实问不编造）",
          "还缺" in prompt_text, prompt_text[:200])

    # 缺尺寸类信息时据实标缺，不编造
    db2 = fresh_db()
    pid2 = service.create_gift_target(db2, "A", name="妈妈", relationship="长辈")["person_id"]
    service.add_gift_profile_entry(db2, "A", pid2, "preference", "喜欢喝茶")
    ctx2 = service.gift_target_context(db2, "A", pid2, viewer_id="A")
    check("缺的类别如实进 missing（size/avoid/last_gift 缺失）",
          set(ctx2["missing"]) >= {"size", "avoid", "last_gift"}, ctx2["missing"])
    prompt2 = agents._gift_profile_prompt(ctx2)
    check("有缺失时提示词明示要问、不替对方编",
          any(k in prompt2 for k in ("还缺", "不知道", "待确认", "据实", "不要编", "问")))


def test_no_leak_to_other_family():
    db = fresh_db()
    pid = service.create_gift_target(db, "A", name="爸爸", relationship="长辈")["person_id"]
    service.add_gift_profile_entry(db, "A", pid, "avoid", "忌讳送烟酒")
    # B 侧上下文（未经授权）绝不含 A 的私有档案
    b_ctx = agents._b_context(db, pid, {"content": "（A 的提案）"}, {})
    b_text = json.dumps(b_ctx, ensure_ascii=False)
    check("B 侧协商上下文不含 A 未授权档案 value", "忌讳送烟酒" not in b_text, b_text[:200])


# --------------------------------------------------------------------------
# D. 推荐依据来自实际档案；空档案不得用常见款凑
# --------------------------------------------------------------------------
def test_recommendation_basis_and_no_fabrication():
    db = fresh_db()
    pid = service.create_gift_target(db, "A", name="爸爸", relationship="长辈")["person_id"]
    service.add_gift_profile_entry(db, "A", pid, "preference", "喜欢爬山")
    service.add_gift_profile_entry(db, "A", pid, "last_gift", "去年送了登山杖")
    ctx = service.gift_target_context(db, "A", pid, viewer_id="A")
    check("有档案时依据非空、可指回具体条目",
          ctx["basis_count"] >= 2 and all("event_id" in e for e in ctx["entries"]))
    prompt = agents._gift_profile_prompt(ctx)
    check("推荐提示词把档案/上次送过什么作为依据带入",
          "喜欢爬山" in prompt and "去年送了登山杖" in prompt, prompt[:200])

    # 空档案：basis 空、四类全缺；不得用常见款凑、不编造
    db2 = fresh_db()
    pid2 = service.create_gift_target(db2, "A", name="陌生人", relationship="同事")["person_id"]
    ctx2 = service.gift_target_context(db2, "A", pid2, viewer_id="A")
    check("空档案 basis=0、missing 含全部四类",
          ctx2["basis_count"] == 0 and set(ctx2["missing"]) >= set(service.GIFT_PROFILE_CATEGORIES),
          ctx2["missing"])
    prompt2 = agents._gift_recommendation_prompt(ctx2)
    check("空档案推荐提示词要求「不知道/据实问」，且不含任何常见款商品名",
          any(k in prompt2 for k in ("不知道", "据实", "不要", "常见", "凑", "待确认", "问"))
          and not any(w in prompt2 for w in ("茶叶", "保温杯", "剃须刀", "围巾", "坚果")),
          prompt2[:200])


# --------------------------------------------------------------------------
# E. 路由层（独立临时服务/临时库，纯数据路由，不触发模型）
# --------------------------------------------------------------------------
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _http(method, url, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:200]}


def test_routes():
    port = _free_port()
    db_dir = tempfile.mkdtemp(prefix="pa-gift-iso-")
    env = dict(__import__("os").environ)
    env["PA_DATA_DIR"] = db_dir
    env["PA_BIND"] = "127.0.0.1"
    env["PA_DEMO_OWNER"] = "A"
    log = open("/tmp/pa-gift-%d.log" % port, "wb")
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=log)
    base = "http://127.0.0.1:%d" % port
    try:
        # 纯数据路由就绪即认为服务起（不调 /api/health，避免碰模型）
        ready = False
        for _ in range(80):
            try:
                code, _ = _http("GET", base + "/api/gift-targets")
                ready = code == 200
                if ready:
                    break
            except Exception:
                time.sleep(0.3)
        check("隔离服务可启动（纯数据路由就绪，未调模型）", ready)

        code, created = _http("POST", base + "/api/gift-targets",
                              {"name": "爸爸", "relationship": "长辈"})
        check("POST /api/gift-targets 建对象", code == 200 and created.get("person_id"), created)
        pid = created.get("person_id", "")

        code, entry = _http("POST", base + "/api/gift-targets/%s/entries" % pid,
                            {"category": "size", "value": "42 码"})
        check("POST 增档案条目", code == 200 and entry.get("event_id"), entry)
        eid = entry.get("event_id", "")

        code, listing = _http("GET", base + "/api/gift-targets")
        check("GET 列表按关系分组含长辈", code == 200
              and any(t.get("person_id") == pid for t in (listing.get("groups", {}).get("长辈") or [])),
              listing.get("groups", {}).keys())

        code, fixed = _http("POST", base + "/api/gift-targets/entries/%s/correct" % eid,
                            {"value": "43 码"})
        check("POST 更正条目", code == 200 and fixed.get("value") == "43 码", fixed)

        code, _ = _http("POST", base + "/api/gift-targets/entries/%s/revoke" % fixed.get("event_id", eid))
        check("POST 撤回条目", code == 200)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()


def main():
    for fn in (test_crud_and_supersede, test_relationship_grouping_and_enum,
               test_idempotent_add, test_authorization_gate_prompt,
               test_main_conversation_brings_authorized_profile,
               test_no_leak_to_other_family,
               test_recommendation_basis_and_no_fabrication, test_routes):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(fn.__name__ + "（异常）", False, repr(exc))
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    lines = ["t49 送礼对象档案 测试退出=%d" % (0 if passed == total else 1),
             "通过 %d / %d" % (passed, total), ""]
    for name, ok, detail in results:
        lines.append("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                                    ("" if ok else "  <- " + repr(detail)[:300])))
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
