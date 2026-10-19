"""业务服务：录入与纠正、任务推进、模拟订单与回读。

边界：
- owner_id 由服务端演示会话决定，模型输出不能切换身份。
- 模拟订单有去重键；同一次确认重复提交返回同一订单。
- 纠正不删除旧记录：旧事实标记 superseded 并指向新记录。
"""

from __future__ import annotations

import json
import re
import uuid
import secrets
import hashlib
import ipaddress
from urllib.parse import urlsplit
import httpx
from datetime import datetime, timedelta, timezone

from . import agents, db as dbm
from .model_client import ModelUnavailable


def _person_by_name(db, owner_id, name):
    if not name:
        return None
    rows = db.rows("SELECT * FROM person WHERE owner_id=? AND name=?", (owner_id, name))
    if rows:
        return rows[0]
    rows = db.rows("SELECT * FROM person WHERE owner_id=? AND name LIKE ?",
                   (owner_id, "%" + name + "%"))
    if len(rows) == 1:
        return rows[0]
    return None


def _resolve_subject_pair(db, owner_id, subject_id=None, subject_name=None):
    """按 owner 视角确定主体，并找到 B 侧同一孩子的记录。"""
    subject = None
    if subject_id:
        subject = db.row("SELECT * FROM person WHERE person_id=? AND owner_id=?", (subject_id, owner_id))
    if subject is None and subject_name:
        subject = _person_by_name(db, owner_id, subject_name)
    if subject is None:
        return None, None
    other = "B" if owner_id == "A" else "A"
    b_side = db.row("SELECT * FROM person WHERE owner_id=? AND name=?", (other, subject["name"]))
    return subject, b_side


def record_utterance(db, client, owner_id, utterance):
    """自然语言录入：新增人物、新增事实，或一句纠正（可同时纠正多个人）。"""
    persons = db.rows("SELECT * FROM person WHERE owner_id=? ORDER BY name", (owner_id,))
    person_lines = "\n".join(
        "- %s | person_id=%s | %s%s" % (
            p["name"], p["person_id"], p["relation"], ("；" + p["note"]) if p["note"] else "")
        for p in persons) or "（暂无）"
    facts = db.rows(
        "SELECT f.*, p.name AS subject_name FROM fact f JOIN person p ON p.person_id=f.subject_id"
        " WHERE f.owner_id=? ORDER BY f.observed_on DESC LIMIT 40", (owner_id,))
    fact_lines = "\n".join(
        "- fact_id=%s | 关于%s的%s：%s（提供者 %s，%s，%s）" % (
            r["fact_id"], r["subject_name"], r["attribute"], r["value"], r["provider"],
            r["observed_on"], r["status"])
        for r in facts) or "（暂无）"
    system = (
        "你是个人记录整理员。把用户这句话整理成结构化记录变更。只输出一个 JSON 对象。\n"
        "首先阅读已知人物列表。用户提到的人物必须原样使用列表中的姓名。\n"
        "字段：\n"
        "  kind: 字符串，只能是 new_fact / correction / new_person / note。\n"
        "  changes: 数组，每项包含 subject_name、attribute、value、corrects_fact_id。\n"
        "          一句话同时纠正多个人时，每人都给一项。\n"
        "  new_person_name: 字符串，新人物姓名；不是新增人物填空字符串。\n"
        "  new_person_relation: 字符串，新人物关系。\n"
        "  new_person_note: 字符串，新人物的说明。\n"
        "  reason: 字符串，一句话说明你做了什么判断。\n"
        "规则：这句话在改正以前记错的内容时 kind=correction，并在对应项给出被改的 fact_id；\n"
        "是补充新信息时 kind=new_fact；是介绍新人物时 kind=new_person；\n"
        "只是普通备注时 kind=note。不要删除旧记录，只指出新记录替代哪条。"
    )
    user = ("已知人物：\n" + person_lines + "\n\n已有记录：\n" + fact_lines +
            "\n\n用户这句话：" + utterance)
    data = client.chat_json([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])

    kind = data.get("kind") if isinstance(data.get("kind"), str) else "note"
    if kind not in ("new_fact", "correction", "new_person", "note"):
        kind = "note"
    result = {
        "kind": kind,
        "utterance": utterance,
        "reason": data.get("reason") if isinstance(data.get("reason"), str) else "",
        "changes": [],
        "ambiguous_subjects": [],
        "created_person_id": "",
    }

    if kind in ("new_fact", "correction"):
        raw_changes = data.get("changes")
        if not isinstance(raw_changes, list):
            raw_changes = []
        for change in raw_changes[:6]:
            if not isinstance(change, dict):
                continue
            subject, _ = _resolve_subject_pair(db, owner_id, None, change.get("subject_name"))
            value = change.get("value") if isinstance(change.get("value"), str) else ""
            if subject is None:
                if change.get("subject_name"):
                    result["ambiguous_subjects"].append(change.get("subject_name"))
                continue
            attribute = change.get("attribute") if isinstance(change.get("attribute"), str) else "note"
            new_fid = dbm.new_id("fact")
            # 纠正时把该主体该属性下所有有效旧记录一并替代，避免旧值继续生效。
            supersede_ids = []
            if kind == "correction":
                named = change.get("corrects_fact_id") if isinstance(change.get("corrects_fact_id"), str) else ""
                if named:
                    named_row = db.row("SELECT * FROM fact WHERE fact_id=? AND owner_id=?",
                                       (named, owner_id))
                    if named_row is not None:
                        supersede_ids.append(named_row["fact_id"])
                for r in db.rows(
                        "SELECT fact_id FROM fact WHERE owner_id=? AND subject_id=? AND attribute=?"
                        " AND status='active'",
                        (owner_id, subject["person_id"], attribute)):
                    if r["fact_id"] not in supersede_ids:
                        supersede_ids.append(r["fact_id"])
            if not value:
                continue
            # 已有等价有效记录时不重复写入，避免一次纠正产生多条相同事实。
            same = db.row(
                "SELECT fact_id FROM fact WHERE owner_id=? AND subject_id=? AND attribute=?"
                " AND value=? AND status='active'",
                (owner_id, subject["person_id"], attribute, value))
            if same is not None:
                target = same["fact_id"]
                for fid in supersede_ids:
                    if fid != target:
                        db.execute(
                            "UPDATE fact SET status='superseded', superseded_by=? WHERE fact_id=?",
                            (target, fid))
                # 已经记过同样的值：这是重复陈述，不要再向用户汇报一次「已改为」。
                result["changes"].append({
                    "subject": subject["name"], "attribute": attribute, "value": value,
                    "created_fact_id": target,
                    "superseded_fact_id": supersede_ids[0] if supersede_ids else "",
                    "superseded_fact_ids": [f for f in supersede_ids if f != target],
                    "unchanged": True,
                })
                continue
            db.execute(
                "INSERT INTO fact(fact_id, owner_id, subject_id, attribute, value, source_utterance,"
                " provider, observed_on, status) VALUES(?,?,?,?,?,?,?,?,?)",
                (new_fid, owner_id, subject["person_id"], attribute, value,
                 utterance, "我本人", dbm.now_iso()[:10], "active"))
            for fid in supersede_ids:
                db.execute("UPDATE fact SET status='superseded', superseded_by=? WHERE fact_id=?",
                           (new_fid, fid))
            result["changes"].append({
                "subject": subject["name"], "attribute": attribute, "value": value,
                "created_fact_id": new_fid,
                "superseded_fact_id": supersede_ids[0] if supersede_ids else "",
                "superseded_fact_ids": [f for f in supersede_ids if f != new_fid],
            })
        db.audit("", owner_id, "record_" + kind, result, owner_id=owner_id)
        # 事实变了，依赖旧事实的未完事项要失效，否则旧确认还会拿着改过的依据执行。
        invalidated = invalidate_stale_proposals(db, owner_id, result.get("changes") or [])
        if invalidated:
            result["invalidated_tasks"] = invalidated

    elif kind == "new_person":
        name = data.get("new_person_name") if isinstance(data.get("new_person_name"), str) else ""
        if name and _person_by_name(db, owner_id, name) is None:
            pid = dbm.new_id("p")
            relation = data.get("new_person_relation") if isinstance(data.get("new_person_relation"), str) else "other"
            note = data.get("new_person_note") if isinstance(data.get("new_person_note"), str) else ""
            db.execute("INSERT INTO person(person_id,owner_id,name,relation,note) VALUES(?,?,?,?,?)",
                       (pid, owner_id, name, relation, note or "由自然语言录入"))
            result["created_person_id"] = pid
            result["changes"].append({"subject": name, "created_person_id": pid})
            db.audit("", owner_id, "record_new_person", result, owner_id=owner_id)
    return result


def _person_ids_for_subject(db, owner_id, name):
    """同一事物在两位用户侧各自的 person_id。

    家庭里两边用各自的记录：A 侧 p_xiaoyu，B 侧 p_xiaoyu_b，名字相同。
    家长纠正自己那边的记录时，也要能命中 A 侧那件未完的事。
    """
    if not name:
        return []
    rows = db.rows(
        "SELECT person_id FROM person WHERE owner_id IN ('A','B') AND name=?", (name,))
    return [r["person_id"] for r in rows]


def invalidate_stale_proposals(db, owner_id, changed):
    """人物事实被纠正后，让依赖旧事实的未完事项失效。

    匹配按「主体 + 属性 + 旧值」对应，而不是只比值：
    - 纠正发生在哪一侧、记的是谁，都按名字映射到两位用户侧的同一个人；
    - 方案里用过的那条事实，其属性和旧值都要和本次被替代的记录对上，
      避免把属性不同、值恰好相同的其他记录误伤，也避免漏掉真正依赖的那条。

    规则：
    - 只看还没走完的事项（created / negotiating / agreed / counter_pending /
      blocked / needs_renegotiation）；
    - 命中后该事项退回「需要重新协商」，已约定的接受一并作废；
    - 与本次纠正无关的事项、已经登记或已收尾的事项不动。
    """
    # 本次被替代的旧记录：主体名字、属性、旧值。
    superseded = []
    for change in changed or []:
        for fid in change.get("superseded_fact_ids") or []:
            row = db.row(
                "SELECT f.*, p.name AS subject_name FROM fact f"
                " JOIN person p ON p.person_id=f.subject_id WHERE f.fact_id=?", (fid,))
            if row is None:
                continue
            superseded.append({
                "fact_id": row["fact_id"], "name": row["subject_name"],
                "attribute": row["attribute"], "value": row["value"],
            })
    if not superseded:
        return []
    affected = []
    open_stages = ("created", "negotiating", "agreed", "counter_pending",
                   "blocked", "needs_renegotiation")
    # 纠正发生在哪一侧，都要能命中另一侧名下未完的那件事。
    # 家庭里两边各存一份关于同一个孩子的记录：B 更正了女儿的兴趣，
    # A 名下那件正按旧兴趣办的事必须一起失效，否则 A 还会拿着旧依据下单。
    for t in db.rows(
            "SELECT task_id, utterance, stage, extracted_json FROM task"
            " WHERE stage IN (%s)" % ",".join("?" * len(open_stages)),
            tuple(open_stages)):
        try:
            extracted = json.loads(t["extracted_json"] or "{}")
        except (ValueError, TypeError):
            extracted = {}
        subject_id = extracted.get("subject_id") or ""
        if not subject_id:
            continue
        subject_name_row = db.row(
            "SELECT name FROM person WHERE person_id=?", (subject_id,))
        if subject_name_row is None:
            continue
        subject_name = subject_name_row["name"]
        # 本次纠正是否涉及这个送礼对象（按名字对应到两侧记录）。
        relevant = [r for r in superseded if r["name"] == subject_name]
        if not relevant:
            continue
        relied = []
        proposals = db.rows(
            "SELECT proposal_id, version, facts_used_json FROM proposal WHERE task_id=?",
            (t["task_id"],))
        for p in proposals:
            try:
                used = json.loads(p["facts_used_json"] or "[]")
            except (ValueError, TypeError):
                used = []
            for f in used:
                if not isinstance(f, dict):
                    continue
                if f.get("attribute") in [r["attribute"] for r in relevant] \
                        and f.get("value") in [r["value"] for r in relevant]:
                    relied.append({"proposal_id": p["proposal_id"],
                                   "attribute": f.get("attribute"),
                                   "value": f.get("value")})
                    break
        # 还没开始协商的事项同样受影响：下一步就会按新事实重新协商。
        if not relied and not proposals:
            relied = [{"proposal_id": "", "attribute": "", "value": ""}]
        if not relied:
            continue
        # 作废该事项下所有未执行的接受记录，旧确认不能再推动执行。
        db.execute(
            "UPDATE proposal SET decision='stale', decided_by='', decision_reason=?"
            " WHERE task_id=? AND decision IN ('accept','pending')",
            ("事实已更正，此版本作废，需要重新协商", t["task_id"]))
        db.execute("UPDATE task SET stage='needs_renegotiation', outcome=? WHERE task_id=?",
                   ("关于%s的记录刚被更正，之前的方案依据已变，需要重新协商。" % subject_name,
                    t["task_id"]))
        db.audit(t["task_id"], owner_id, "proposals_invalidated",
                 {"subject_id": subject_id, "subject_name": subject_name,
                  "relied_on": relied,
                  "changed_facts": [
                      {"subject": c.get("subject"), "attribute": c.get("attribute"),
                       "value": c.get("value")} for c in (changed or [])]},
                 owner_id=owner_id)
        affected.append(t["task_id"])
    return affected


# ---------------------------------------------------------------------------
# 家庭近况：来自标注测试来源的事件，按人、按字段授权给指定的人看。
#
# 设计要点：
# - 事件带自己的来源标识和发生时间，不并进 person.attributes_json。
#   同一家人从学习 App 报进度、从手表报睡眠，是两件事，
#   合并成一份就分不清谁说的、什么时候说的、授权范围是什么。
# - 「没上报」不等于「没发生」。手表没同步的那几晚，不能推断睡得好或不好。
# - 设备来源的数据要标出来，不让它看起来像本人自述。
DEVICE_SOURCE_KINDS = ("watch", "health", "device")

# 配图授权标记。event_grant 的 fields_json 里出现这个值，才表示
# 这条记录的配图也可以给该查看者看。事件授权按字段给，图是整张原图，
# 默认不随字段授权一起放行。
ATTACHMENT_GRANT_MARK = "__attachments__"


def import_life_event(db, source_id, subject_id, event_type, payload, happened_on,
                      provider, owner_id="B", viewer_grants=None, source_kind="app"):
    """导入一条家庭近况事件。同一来源的同一事件重复导入只留一条。

    viewer_grants 是 [{"viewer_id": "A", "fields": ["完成项目"], "valid_until": ""}, ...]，
    决定这条事件的哪些字段能给谁看。没授权的字段，查的时候直接不给，
    不是给了再在界面上藏起来。

    返回 (event_id, created)。created=False 表示已经导入过，没有重复建。
    """
    subject = db.row("SELECT * FROM person WHERE person_id=?", (subject_id,))
    if subject is None:
        raise ValueError("事件主体不存在")
    # 主体必须属于本人记录。获准查看别人的孩子不等于能往那个孩子名下
    # 写自己的事件：普通入口拿 B 的 person_id 也能建一条 A 来源的记录，
    # 那条记录会带着 A 授权的字段出现在 B 的孩子身上。
    if subject["owner_id"] != owner_id:
        raise ValueError("这个人物不在你的记录里，不能以你的名义记他的近况")
    # 去重键：来源 + 主体 + 类型 + 发生时间。同一条事件重复推送很常见
    # （App 重连、手表补传），不能每次都算一条新的。
    existing = db.row(
        "SELECT event_id FROM life_event WHERE source_id=? AND subject_id=?"
        " AND event_type=? AND happened_on=?", (source_id, subject_id, event_type, happened_on))
    if existing is not None:
        return existing["event_id"], False
    event_id = dbm.new_id("evt")
    db.execute(
        "INSERT INTO life_event(event_id, source_id, source_kind, subject_id, owner_id,"
        " event_type, payload_json, happened_on, provider, status, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, source_id, source_kind, subject_id, owner_id, event_type,
         json.dumps(payload, ensure_ascii=False), happened_on, provider, "active",
         dbm.now_iso()))
    for g in viewer_grants or []:
        db.execute(
            "INSERT INTO event_grant(grant_id, event_id, viewer_id, fields_json,"
            " valid_until, note, created_at) VALUES(?,?,?,?,?,?,?)",
            (dbm.new_id("grant"), event_id, g["viewer_id"],
             json.dumps(g.get("fields") or [], ensure_ascii=False),
             g.get("valid_until") or "", g.get("note") or "", dbm.now_iso()))
    db.audit("", owner_id, "life_event_imported",
             {"event_id": event_id, "source_id": source_id, "subject_id": subject_id,
              "event_type": event_type}, owner_id=owner_id)
    return event_id, True


def _event_visible_fields(db, event_id, viewer_id):
    """这条事件授权给 viewer 看的字段列表。没授权或已过期就返回空。"""
    # 记忆所有者看自己的记录不需要额外授权：这条事件本来就记在他名下。
    row = db.row("SELECT owner_id FROM life_event WHERE event_id=?", (event_id,))
    if row is not None and row["owner_id"] == viewer_id:
        try:
            payload = json.loads(
                db.row("SELECT payload_json FROM life_event WHERE event_id=?",
                       (event_id,))["payload_json"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        return sorted(payload.keys())
    today = dbm.now_iso()[:10]
    fields: list[str] = []
    for g in db.rows("SELECT * FROM event_grant WHERE event_id=? AND viewer_id=?",
                     (event_id, viewer_id)):
        if g["valid_until"] and g["valid_until"] < today:
            continue
        try:
            fields.extend(json.loads(g["fields_json"]))
        except (ValueError, TypeError):
            continue
    return sorted(set(fields))


def life_events_for(db, subject_id, viewer_id, limit=20):
    """某人授权给 viewer 看的近况，按发生时间倒序。

    只返回授权字段内的内容，未授权字段不出现——不是给了再藏。
    已撤回的事件不再返回；旧副本不承诺能远程删除，只保证新查询看不到。
    """
    rows = db.rows(
        "SELECT * FROM life_event WHERE subject_id=? AND status='active'"
        " ORDER BY happened_on DESC, created_at DESC LIMIT ?", (subject_id, limit))
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        allowed = _event_visible_fields(db, r["event_id"], viewer_id)
        visible = {k: v for k, v in payload.items() if k in allowed}
        # 一条记录留下的条件：有授权可见的字段，或者有可见的配图。
        # 少了后半个条件，只存图、没有文字字段的记录会被
        # 「一个字段都没授权」整条丢掉——用户明明存下了一张图，
        # 回查时这条记录连同那张图一起凭空消失。
        # 配图仍按当前查看者的权限判，不是无条件放行。
        if not visible and not event_attachments(db, r["event_id"], viewer_id):
            continue
        is_device = r["source_kind"] in DEVICE_SOURCE_KINDS
        out.append({
            "event_id": r["event_id"], "source_id": r["source_id"],
            "source_kind": r["source_kind"],
            "subject_id": r["subject_id"], "event_type": r["event_type"],
            "happened_on": r["happened_on"], "provider": r["provider"],
            # 设备来的数据要标明，不让它看起来像本人自述或医生判断。
            "is_device": is_device,
            "source_label": ("手表模拟数据" if r["source_kind"] == "watch"
                             else r["source_id"]),
            "fields": visible,
            "granted_fields": allowed,
        })
    return out


# 「下一步建议」这次目的需要的字段。近况里还有别的内容（睡眠自述、
# 健康备注、家长私下记的话），那些和给出学习安排无关：
# 本人能读不等于该为这件事发给模型，按目的收窄再送出去。
_SUGGESTION_FIELDS = ("进度", "已完成项目", "完成时间", "本人愿意分享", "睡眠时长",
                      "软件报告的困难点", "评价")

# 本人核对后写下的正文也属于本次用途可用的内容，但只在它是这条记录的
# 最终采用值时才送。模型读到的原文已经在上面那些字段里，正文是本人改过的
# 那一版；不带它的话，用户把「25 分钟」纠正成「四十分钟」之后，
# 下一次建议拿到的还是旧进度，纠正等于白做。
# 仍然按目的收窄：只有这条记录本来就有本次可用字段、或正文确实短到像一条
# 事实时才送，长段私话（性格描述、家长叮嘱）继续排除，不因为这个键存在就全送。
_SUGGESTION_TEXT_KEY = "正文"
_SUGGESTION_TEXT_MAX = 60


def _suggestion_inputs(sources):
    """按本次目的挑字段。返回 (送模型的近况, 被略过的字段说明)。"""
    kept, skipped = [], []
    for e in sources:
        fields = e.get("fields") or {}
        use = {k: v for k, v in fields.items() if k in _SUGGESTION_FIELDS}
        # 本人核对后写下的正文，按本次用途决定带不带。
        # 判据不是长度，是这条记录本来就在说本次用途这件事：
        # 只有它已经有本次可用字段（进度、完成时间之类）时才带正文，
        # 因为那些字段的存在就说明这条记录是关于学习安排的。
        # 一条只有正文、没有结构化字段的记录不带——它可能是家长私话、
        # 健康备注，与「下一步做什么」无关，短也不是理由。
        # 长度上限只是第二道闸：防止误带进来的长段内容占满上下文。
        text = str(fields.get(_SUGGESTION_TEXT_KEY) or "").strip()
        relevant = bool({k for k in fields if k in _SUGGESTION_FIELDS})
        if text and relevant and len(text) <= _SUGGESTION_TEXT_MAX:
            use[_SUGGESTION_TEXT_KEY] = text
        else:
            text = ""
        drop = sorted(set(fields) - set(use))
        if drop:
            skipped.append({"source": e.get("source_label") or e.get("source"),
                            "happened_on": e.get("happened_on"), "fields": drop})
        # 结构化进度和本人纠正过的正文可能说同一件事却给了不同值
        # （字段还是「十分钟」，正文写「四十分钟」）。两个都送给模型，
        # 它会挑一个，用户不知道最后信了哪个，纠正就白做了。
        # 这里显式标出冲突，让建议模块要求采用哪一个，不静默用旧值。
        conflict = ""
        if not text:
            pass
        else:
            # 「完成时间」和「进度」都要比。进度也是同一条记录在说时间：
            # 字段写「哥哥自己练了十分钟」，本人核对时把正文改成「四十分钟」，
            # 这两个值都说的是同一次练习的时长。只比完成时间的话这里不报冲突，
            # 两个互相矛盾的时长一起送给模型，它挑一个，用户不知道最后信了哪个，
            # 这一次纠正就白做了。
            if use.get("完成时间") and _times_conflict(use["完成时间"], text):
                conflict = "完成时间"
            elif use.get("进度") and _times_conflict(use["进度"], text):
                conflict = "进度"
        if conflict:
            use["_conflict"] = conflict
        if use:
            kept.append({
                # event_id 要给到界面上：冲突卡片要能指出「是哪一条记录」，
                # 并把用户带到那条记录的纠正入口。只给 source 和日期的话，
                # 用户看到的是一句说不清指向谁的话，还是要自己翻列表。
                "source": e.get("source_label") or e.get("source"),
                "event_id": e.get("event_id") or "",
                "event_type": e.get("event_type"),
                "happened_on": e.get("happened_on"),
                "provider": e.get("provider"),
                "is_device": e.get("is_device"),
                "fields": use,
            })
    return kept, skipped


def _times_conflict(structured, text):
    """结构化「完成时间」和本人正文里的说法是不是一回事。

    中文数字也要认。「四十分钟」和字段里的「10 分钟」说的是同一件事，
    只按阿拉伯数字取的话两边一个取到 10、一个什么也取不到，
    冲突就被漏掉了，用户改过的值依旧被旧字段压过去。
    正文里完全没有数量说法（例如「练完了，没要人催」）时不算冲突——
    那句话没有和字段矛盾的数值，只是补充。
    """
    a = _numbers_in(str(structured or ""))
    b = _numbers_in(str(text or ""))
    # 一方有数量、另一方没有时无法比较，不当成冲突。
    if not a or not b:
        return False
    return a != b


_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _numbers_in(s):
    """把一段中文或数字混排的文字里出现的数量取出来，统一成数字集合。

    「四十分钟」取 {40}，「10 分钟」取 {10}，「两幅拼图」取 {2}。
    必须把连续的中文数字当成一个整体来读。逐字取的话「四十」会被拆成
    「四」和「十」两个数，得到 {4,10}，而字段里的「10 分钟」是 {10}，
    两个集合不相等——明明一致的写法被判成冲突，用户会看到一个
    莫名其妙的「你说的时间对不上」。
    只处理常见的十以内组合，不解析「一百三十五」这类复杂写法：
    记录里几乎不会出现，而解析错比不解析更危险。
    """
    out = set(int(x) for x in re.findall(r"\d+", s))
    for m in re.finditer(r"[零一二两三四五六七八九十]+", s):
        val = _cn_number(m.group(0))
        if val is not None:
            out.add(val)
    return out


def _cn_number(tok):
    """把一个连续的中文数字串读成一个数。「四十」->40，「十」->10，「两」->2。"""
    if not tok:
        return None
    if "十" in tok:
        left, _, right = tok.partition("十")
        tens = _CN_NUM.get(left, 1) if left else 1
        ones = _CN_NUM.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(tok) == 1:
        return _CN_NUM.get(tok)
    # 「四十五」这类十以上没有「十」字的写法不猜，交给调用方当成读不出来。
    return None


def app_suggestion(db, client, owner_id, subject_id):
    """第二个小应用自己的产出：按这个人的授权近况给一条下一步建议。

    和主界面的送礼协商是两件事：这里只回答「接下来做什么」，
    每条建议都要能追到它依据的近况（来源、时间），不重填人物背景。

    模型不可用时如实报 model_failed，不返回一句空话冒充成功——
    「没连上模型」和「模型说不需要建议」对用户是两种完全不同的信息。
    """
    subject = db.row("SELECT * FROM person WHERE person_id=?", (subject_id or "",))
    if subject is None:
        raise ExtError("no_such_person", "找不到这个人物", status=404)
    # 对象必须在本人的记录里，或经本人确认关联到本人记录里的某个人。
    # 只检查「存在」不够：那样传任何一个别人的 person_id，这里也会返回
    # 他的名字并去查近况。归属不能靠前端下拉列表限制——那是可绕过的。
    mine = {r["person_id"] for r in
            db.rows("SELECT person_id FROM person WHERE owner_id=?", (owner_id,))}
    allowed = mine | {pid for pid in linked_person_ids(db, subject_id)
                      if pid in mine}
    if subject_id not in allowed:
        raise ExtError("no_such_person", "找不到这个人物，或它不在你的记录里",
                       status=404)
    events = _life_events_both_sides(db, owner_id, subject_id)
    if not events:
        # 一条授权近况都没有：这时候给建议只能是编。
        # 明确说现在没有可用依据，不调模型硬凑一条。
        return {
            "subject_id": subject_id,
            "subject_name": subject["name"] or "",
            "status": "no_data",
            "suggestion": "",
            "sources": [],
            "note": "这个人名下还没有授权给你的近况，没有可依据的内容，先记一条再说。",
        }
    sources = [{"source": e["source_label"], "event_id": e["event_id"],
                "event_type": e["event_type"],
                "happened_on": e["happened_on"], "provider": e["provider"],
                "is_device": e["is_device"], "fields": e["fields"]}
               for e in events]
    # 只把这次目的要用的字段送进模型。本人可读的其余字段不带出去。
    model_inputs, skipped = _suggestion_inputs(sources)
    # 结构化字段和本人纠正过的正文给了不同值时，不静默取旧值：
    # 明确说哪里对不上，让本人先定用哪个，再谈下一步。
    conflicts = []
    for m in model_inputs:
        c = (m.get("fields") or {}).pop("_conflict", "")
        if c:
            conflicts.append({"source": m.get("source") or "",
                              "event_id": m.get("event_id") or "",
                              "happened_on": m.get("happened_on") or "",
                              "field": c})
    if not model_inputs:
        return {
            "subject_id": subject_id,
            "subject_name": subject["name"] or "",
            "status": "no_data",
            "suggestion": "",
            "sources": sources,
            "note": "有近况，但都和「下一步做什么」无关，没有可依据的内容。",
        }
    if conflicts:
        return {
            "subject_id": subject_id,
            "subject_name": subject["name"] or "",
            "status": "needs_confirm",
            "suggestion": "",
            "sources": sources,
            "conflicts": conflicts,
            "note": "记录里的完成时间和你后来写的不一致，先定用哪个，"
                    "再给下一步建议。",
        }
    system = "\n".join([
        "你是家庭小助手里的「下一步建议」模块。只根据下面给出的近况记录，给出一条具体的下一步建议。",
        "要求：",
        "  suggestion: 字符串，一句话，说清接下来做什么，别写空话；",
        "  based_on: 数组，这条建议依据了哪些记录，每项写来源和日期，只能从给出的记录里选，不能引用没给过的内容；",
        "  confidence: 字符串，high / medium / low，依据不足就写 low。",
        "不要补充记录里没有的情况，不要替人下医学或学习能力判断，设备数据和本人自述要分开说。只输出 JSON。",
        "近况记录：",
        json.dumps(model_inputs, ensure_ascii=False),
    ])
    try:
        data = client.chat_json([
            {"role": "system", "content": system},
            {"role": "user", "content": "请给出下一步建议。"},
        ], max_tokens=1200)
    except ModelUnavailable as exc:
        return {
            "subject_id": subject_id,
            "subject_name": subject["name"] or "",
            "status": "model_failed",
            "suggestion": "",
            "sources": sources,
            "note": "没连上模型，这次没有建议。%s" % (str(exc)[:120],),
        }
    # 模型说的 based_on 要和真实来源对得上：对不上的条目剔掉，
    # 并在返回里说明「依据未经核对」，不能拿它当已验证的引用。
    real = {(e.get("source") or "", e.get("happened_on") or "") for e in model_inputs}
    claimed = [b for b in (data.get("based_on") or []) if isinstance(b, str) and b.strip()]
    matched = [b for b in claimed
               if any((src in b or b in src) and (day in b or not day)
                      for src, day in real)]
    text = (data.get("suggestion") or "").strip()
    if not text:
        return {
            "subject_id": subject_id,
            "subject_name": subject["name"] or "",
            "status": "model_failed",
            "suggestion": "",
            "sources": sources,
            "note": "模型没有给出可用的建议，这次不返回结论。",
        }
    return {
        "subject_id": subject_id,
        "subject_name": subject["name"] or "",
        "status": "ok",
        "suggestion": text,
        "based_on": matched,
        "unverified_based_on": [b for b in claimed if b not in matched],
        "confidence": data.get("confidence") or "",
        # sources 给界面显示全部授权近况；送进模型的只有 model_inputs，
        # 界面上要能看出这个差别，否则「依据」看着比实际多。
        "sources": sources,
        "used_sources": model_inputs,
        "skipped_fields": skipped,
    }


def app_source_view(db, source_id, owner_id):
    """一个小应用自己写的记录，以及它授权出去的部分。

    这一页给应用自己看：它记了什么、每个字段分别给了谁、有效期到什么时候，
    还有别人实际能读到的字段。没授权的字段在这里标出来，
    让「授权范围」不是一句口头承诺。

    owner_id 直接进 SQL：先按 source_id 读出全部记录再在调用方筛，
    别人的 payload 和授权明细已经出了这个模块，筛掉也来不及；
    「共有几条别人的记录」本身也是不该给的信息。
    """
    rows = db.rows(
        "SELECT * FROM life_event WHERE source_id=? AND owner_id=?"
        " ORDER BY happened_on DESC, created_at DESC LIMIT 50", (source_id or "", owner_id))
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        grants = []
        for g in db.rows("SELECT * FROM event_grant WHERE event_id=? ORDER BY grant_id",
                         (r["event_id"],)):
            try:
                fields = json.loads(g["fields_json"] or "[]")
            except (ValueError, TypeError):
                fields = []
            grants.append({
                "viewer_id": g["viewer_id"], "fields": fields,
                "valid_until": g["valid_until"] or "",
            })
        shared_fields = sorted({f for g in grants for f in g["fields"]})
        # 别人实际读到的字段按 viewer 逐个算：同一份记录，
        # 不同查看者看到的内容可以不一样。这里不预筛选，交给调用方按 viewer 读。
        per_viewer = {}
        for g in grants:
            try:
                per_viewer.setdefault(g["viewer_id"], set()).update(g["fields"])
            except (ValueError, TypeError):
                continue
        subject = db.row("SELECT name FROM person WHERE person_id=?", (r["subject_id"],))
        out.append({
            "event_id": r["event_id"], "source_id": r["source_id"],
            "source_kind": r["source_kind"],
            "subject_id": r["subject_id"], "subject_name": (subject or {}).get("name") or "",
            "event_type": r["event_type"], "happened_on": r["happened_on"],
            "provider": r["provider"], "status": r["status"],
            "is_device": r["source_kind"] in DEVICE_SOURCE_KINDS,
            "payload": payload,
            "shared_fields": shared_fields,
            "private_fields": sorted(set(payload.keys()) - set(shared_fields)),
            "grants": grants,
            "viewers": {k: sorted(v) for k, v in per_viewer.items()},
            "superseded_by": r["superseded_by"] or "",
        })
    return {"source_id": source_id, "events": out}


def revoke_life_event(db, event_id, owner_id):
    """撤回一条事件。之后的新查询看不到它。

    已经导出的旧副本不在承诺范围内：本地 demo 没有远程删除能力，
    这一点要对用户说清，不能假装撤回了就谁都看不到了。
    """
    row = db.row("SELECT * FROM life_event WHERE event_id=?", (event_id,))
    if row is None:
        raise ValueError("事件不存在")
    # 只有记录的所有者能撤回。获准查看不等于获准编辑：
    # A 被授权看 B 孩子的事件，不等于 A 能让那条事件对所有人消失。
    if row["owner_id"] != owner_id:
        raise ValueError("这条记录不是你记的，不能撤回")
    db.execute("UPDATE life_event SET status='revoked', superseded_by=? WHERE event_id=?",
               ("revoked_by:%s" % owner_id, event_id))
    db.audit("", owner_id, "life_event_revoked", {"event_id": event_id})
    invalidate_event_plans(db, event_id)
    return True


def correct_life_event(db, event_id, payload, owner_id):
    """纠正一条事件的内容：旧记录标为被替代，写入新内容。

    后续摘要必须用新状态。纠正来源记录后，不能还按旧内容总结。
    """
    row = db.row("SELECT * FROM life_event WHERE event_id=?", (event_id,))
    if row is None:
        raise ValueError("事件不存在")
    # 同上：只有记录的所有者能纠正内容。授权查看只给了读，
    # 没有给改别人写下的近况。
    if row["owner_id"] != owner_id:
        raise ValueError("这条记录不是你记的，不能更正")
    if row["status"] != "active":
        raise ValueError("只能更正当前有效的资料")
    original = json.loads(row["payload_json"] or "{}")
    if not isinstance(payload, dict):
        raise ValueError("更正须为字段对象")
    original.update(payload)
    payload = original
    new_id_ = dbm.new_id("evt")
    db.execute(
        "INSERT INTO life_event(event_id, source_id, source_kind, subject_id, owner_id,"
        " event_type, payload_json, happened_on, provider, status, superseded_by, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id_, row["source_id"], row["source_kind"], row["subject_id"],
         row["owner_id"], row["event_type"], json.dumps(payload, ensure_ascii=False),
         row["happened_on"], row["provider"] + "（已更正）", "active", "", dbm.now_iso()))
    db.execute("UPDATE life_event SET status='superseded', superseded_by=? WHERE event_id=?",
               (new_id_, event_id))
    # 授权跟着新记录走，否则纠正后查看者反而看不到更新后的内容。
    for g in db.rows("SELECT * FROM event_grant WHERE event_id=?", (event_id,)):
        db.execute(
            "INSERT INTO event_grant(grant_id, event_id, viewer_id, fields_json,"
            " valid_until, note, created_at) VALUES(?,?,?,?,?,?,?)",
            (dbm.new_id("grant"), new_id_, g["viewer_id"], g["fields_json"],
             g["valid_until"], g["note"], dbm.now_iso()))
    db.audit("", owner_id, "life_event_corrected",
             {"old_event_id": event_id, "new_event_id": new_id_})
    if row['source_kind'] == 'connector':
        db.execute("UPDATE connector_record SET event_id=?,authorized_fields_json='[]' WHERE source_id=?", (new_id_, row['source_id']))
    invalidate_event_plans(db, event_id)
    return new_id_


# 拍照识别的抽取提示。只让模型报它从图里读到的事实，
# 不让他判断这是谁、也不能替谁确认。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VISION_EXTRACT_SYSTEM = (
    "你是照片文字识别助手。用户拍到的是一份手写或打印的记录。"
    "只输出一个 JSON 对象，不要解释、不要代码块。\n"
    "字段：\n"
    "  transcribed: 字符串，图里实际出现的文字，逐字抄录，不要改写、不要补充。\n"
    "  subject_hint: 字符串，图里提到的孩子名字或称呼；图里没写就填空串。\n"
    "  fields: 对象，从文字里能确定读到的项目，键用中文（如「已完成项目」"
    "「完成时间」「评价」），值照抄原文。读不到的键不要出现，不要猜、不要填默认值。\n"
    "  happened_on: 字符串，图里的日期，格式 YYYY-MM-DD；图里没写日期就填空串。\n"
    "  confidence: 字符串，high / medium / low，字迹模糊或字段不全时如实降低。\n"
    "  unclear_parts: 数组，列出看不清或不确定的地方，逐条说明。\n"
    "严格要求：看不清就说不看清，不要根据常见内容编造；"
    "没有出现的字段一律不输出。"
)


def read_photo_record(db, client, image_b64, media_type="image/jpeg"):
    """用 Step 的多模态能力读一张照片，返回抽到的字段。

    这一步只做识别，不写任何记忆：识别结果先给本人核对，
    核对过才由调用方决定记到谁名下。不让模型顺手判断「这是谁」，
    主体由调用方明确指定。
    """
    try:
        # 参数顺序必须是 (提示词, base64, media_type)。
        # 顺序写错时 URL 会拼成 data:<base64>;base64,<提示词>，
        # 代理返回 400 "url invalid"，看起来像图片不被支持，
        # 其实是自己把两个参数对调了。
        data = client.chat_vision_json(
            _VISION_EXTRACT_SYSTEM, image_b64, media_type,
            user_prompt="请识别这张照片里的记录内容。")
    except Exception as exc:
        if type(exc).__name__ == "ModelUnavailable":
            raise
        raise ModelUnavailable("图片识别失败：%s" % type(exc).__name__) from exc
    fields = data.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    # 只留字符串值：模型偶尔会返回数字或嵌套对象，直接进库会让界面难读。
    clean_fields = {}
    for k, v in fields.items():
        if isinstance(k, str) and isinstance(v, (str, int, float)):
            clean_fields[k] = str(v)
    unclear = data.get("unclear_parts")
    if not isinstance(unclear, list):
        unclear = []
    return {
        "transcribed": str(data.get("transcribed") or ""),
        "subject_hint": str(data.get("subject_hint") or ""),
        "fields": clean_fields,
        "happened_on": str(data.get("happened_on") or ""),
        "confidence": str(data.get("confidence") or ""),
        "unclear_parts": [str(x) for x in unclear if isinstance(x, (str, int, float))],
    }


def linkable_tasks(db, owner_id):
    """可以拿来关联的本人事项：未完的，且已有明确办事对象。

    只列本人名下的。已买到、已办完的不列：资料是给「接下来还要办的事」
    用的，挂到一件已经结束的事上没有意义，还会让用户在清单里翻找。
    没有明确对象的事项也列出来但标出来——它缺的就是对象，
    用户可能正打算用这条资料把对象补上；但服务端不替他选。
    """
    rows = db.rows(
        "SELECT task_id, utterance, stage, outcome, extracted_json FROM task"
        " WHERE owner_id=? ORDER BY created_at DESC", (owner_id,))
    out = []
    for r in rows:
        if r["stage"] in ("purchased", "closed", "cancelled"):
            continue
        try:
            extracted = json.loads(r["extracted_json"] or "{}")
        except (ValueError, TypeError):
            extracted = {}
        out.append({
            "task_id": r["task_id"],
            "utterance": r["utterance"],
            "stage": r["stage"],
            "stage_text": STAGE_TEXT.get(r["stage"], r["stage"]),
            "subject_name": extracted.get("subject_resolved_name") or "",
            "subject_id": extracted.get("subject_id") or "",
        })
    return out


def link_event_to_task(db, owner_id, event_id, task_id, note=""):
    """把一条资料明确用于某件事项。本人点「用于这件事」才建立。

    关联只是记录「这次办事我用了哪条资料」，不授予任何额外权限：
    不因此可以对外分享、不因此建立对他人的承诺、也不因此能执行。
    重复关联返回同一条 link_id，不叠记录。
    """
    ev = db.row("SELECT * FROM life_event WHERE event_id=?", (event_id,))
    if ev is None:
        raise ValueError("这条资料不存在")
    # 资料和事项都必须是本人的：拿别人的资料挂到自己的事项上，
    # 或者反向，都会把没授权的内容带进协商上下文。
    if ev["owner_id"] != owner_id:
        raise ValueError("这条资料不是你的，不能用于你的事项")
    task = db.row("SELECT * FROM task WHERE task_id=?", (task_id,))
    if task is None:
        raise ValueError("这件事不存在")
    if task["owner_id"] != owner_id:
        raise ValueError("这件事不是你的")
    have = db.row(
        "SELECT link_id FROM event_task_link WHERE event_id=? AND task_id=?",
        (event_id, task_id))
    if have is not None:
        return {"link_id": have["link_id"], "created": False}
    link_id = dbm.new_id("lnk")
    db.execute(
        "INSERT INTO event_task_link(link_id, event_id, task_id, owner_id, note, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (link_id, event_id, task_id, owner_id, note, dbm.now_iso()))
    db.audit(task_id, owner_id, "event_linked",
             {"event_id": event_id, "note": note})
    return {"link_id": link_id, "created": True}


def unlink_event_from_task(db, owner_id, event_id, task_id):
    """取消关联。资料本身不动，只是不再用于这件事。"""
    ev = db.row("SELECT owner_id FROM life_event WHERE event_id=?", (event_id,))
    task = db.row("SELECT owner_id FROM task WHERE task_id=?", (task_id,))
    if ev is None or task is None or ev["owner_id"] != owner_id or task["owner_id"] != owner_id:
        raise ValueError("这条资料或这件事不是你的")
    db.execute("DELETE FROM event_task_link WHERE event_id=? AND task_id=?", (event_id, task_id))
    return {"unlinked": True}


def linked_events(db, task_id, viewer_id):
    """这件事用到过哪些资料。按查看者身份过滤：不是人人都看得到。

    这里不把 base64 带出来，图片本体走 /api/event/{id}/attachment/{aid}，
    服务端在那里按身份校验——列表里直接塞图等于绕过那道校验。
    """
    rows = db.rows(
        "SELECT l.link_id, l.event_id, l.note, l.created_at, l.owner_id"
        " FROM event_task_link l WHERE l.task_id=? ORDER BY l.created_at", (task_id,))
    out = []
    for r in rows:
        if r["owner_id"] != viewer_id:
            continue
        ev = db.row("SELECT * FROM life_event WHERE event_id=?", (r["event_id"],))
        if ev is None:
            continue
        try:
            payload = json.loads(ev["payload_json"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        if ev['source_kind'] == 'connector':
            payload = _connector_model_fields(db, ev, payload)
            if not payload:
                continue
        out.append({
            "link_id": r["link_id"],
            "event_id": ev["event_id"],
            "source_label": ev["source_id"],
            "event_type": ev["event_type"],
            "happened_on": ev["happened_on"],
            "provider": ev["provider"],
            "linked_at": r["created_at"],
            "fields": payload,
            "attachments": event_attachment_index(db, ev["event_id"], viewer_id),
        })
    return out


def _normalize_read(read):
    """把识别结果补成完整的一套键。缺的键给空值，不抛异常。

    模型返回的字段个数本来就不固定，调用方也可能只带一部分过来
    （例如「只作配图，不识别」时只表示这里没有识别文字）。
    下游要按固定字段写审计、判日期，缺一个键就让整次保存失败是不可接受的：
    那意味着用户写好的正文和挑好的配图一起丢。
    """
    base = {"transcribed": "", "fields": {}, "happened_on": "",
            "confidence": "", "unclear_parts": [], "subject_hint": ""}
    if not isinstance(read, dict):
        return base
    out = dict(base)
    out.update({k: v for k, v in read.items() if v is not None})
    # 字段值必须是标量：审计和 payload 都按字符串落库，
    # 塞一个 dict 进去会让 json.dumps 之后存成奇怪的结构，回读时对不上。
    fields = out.get("fields")
    out["fields"] = fields if isinstance(fields, dict) else {}
    if not isinstance(out.get("unclear_parts"), list):
        out["unclear_parts"] = []
    for k in ("transcribed", "happened_on", "confidence", "subject_hint"):
        if not isinstance(out.get(k), str):
            out[k] = "" if out.get(k) is None else str(out[k])
    return out


def save_photo_record(db, client, owner_id, subject_id, image_b64="",
                      media_type="image/jpeg", captured_by="", grants=None,
                      confirmed_fields=None, read=None, attachments=None,
                      draft_text=None, happened_on=None, input_kinds=None,
                      voice_text=""):
    """拍照记录接入记忆底座：本人核对后才落库。

    owner_id 是记忆归在谁名下（这条记录的提供者），subject_id 是记录说的是谁，
    captured_by 是实际按下快门的人。三者分开存：家长拍的孩子记录，
    和妈妈自己报的学习进度，来源不同，不能混成一种。

    识别结果先给本人看，confirmed_fields 是本人核对后确认采用的字段；
    模型读到但本人没确认的字段不写入，避免把看不清的猜测当事实。

    attachments 是本人在核对界面里明确点了「保留为配图」的图片。
    识别用的原图不自动成为配图：默认不长期存图，用户说要留才留。
    """
    person = db.row("SELECT * FROM person WHERE person_id=?", (subject_id,))
    if person is None:
        raise ValueError("记录主体不存在")
    # 本人核对后改过的正文单独放一个键，和模型读到的原文分开存。
    # 界面显示的是本人最终采用的那版，但来源必须分得清：
    # 改过的内容不能冒记为模型读的原样，也不能算成孩子自述。
    final_text = (draft_text or "").strip()
    # 语音转写并进正文：转写是草稿，本人改过才算数，所以追加在正文后面，
    # 不单独占一个字段——回看时它就是这条记录的一部分。
    voice = (voice_text or "").strip()
    if voice:
        final_text = (final_text + "\n" + voice).strip() if final_text else voice
    # 有图才识别。纯文字记录不该为了一次模型调用多等几十秒，
    # 也不该因为模型连不上就存不进去——文字是本人写的，本来就不需要识别。
    has_image = bool(image_b64)
    # 前端已经调过 /api/photo/read 把结果给本人看过一遍了，这里不再识别第二遍：
    # 同一张图跑两次多模态，用户要多等几十秒，而且核对的动作就白做了。
    # 万一 read 没传进来（直接调接口），才退回识别一次。
    if read is None and has_image:
        read = read_photo_record(db, client, image_b64, media_type)
    # read 归一化成完整的一套键，再看要不要识别。
    # 只判 None 不够：调用方给一份缺键的部分字典时，下面按 read["fields"]、
    # read["confidence"] 直接取，会抛 KeyError 把整次保存打成 500——
    # 用户点「保存资料」之后记录和配图一起丢，界面上只有一句「没保存」。
    # 「只作配图，不识别」这条路正是由调用方给一份不完整的 read，
    # 所以这里必须补全，而不是假设它一定带全套键。
    read = _normalize_read(read)
    picked = confirmed_fields if confirmed_fields is not None else read["fields"]
    payload = {}
    for k, v in (picked or {}).items():
        if isinstance(k, str) and isinstance(v, (str, int, float)):
            payload[k] = str(v)
    # 本人改过的正文随字段一起进 payload，但用独立的键：
    # 读回时能分清「模型读到的原文」和「本人最终采用的正文」，
    # 界面上也据此标明这条记录经过本人核对。
    if final_text:
        payload["正文"] = final_text
    # 字段和正文都空才算真的没内容。
    # 顺序很关键：正文必须先并入 payload 再判空。反过来判的话，
    # 用户把读出来的字段全删掉、只留一段自己写的正文，会被
    # 「核对后没有可记入的字段」挡下——他明明写清了要记的事。
    # 不强迫保留 OCR 字段：识别只是草稿，本人写的才算要记的内容。
    # 有配图时字段和正文都可以空：一张奖状、一张手工作品本来就没有
    # 要读的文字，用户走的就是「只存这张图」。这里再要求有文字，
    # 等于逼他为一张没有文字的图编一句话。
    if not payload and not attachments:
        raise ValueError("核对后没有可记入的字段")
    # 日期必须是真的读清了才写。模型报的「202 -10-12」是模糊字迹，
    # 日期错了，之后按时间回忆、按时间提醒都会错。
    # 读不清就留空，不拿今天冒充事实发生时间：图里常写「10月12日」而
    # 没有年份，补一个年份就是替事实补内容，之后按日期回忆会把这条
    # 安排到错误的那一年。今天只作这条记录的保存时间（created_at），
    # 发生日期保持未知，界面标出来由本人决定要不要补。
    # 日期取本人核对后的最终值，不取模型读到的那个：用户在核对界面
    # 改过日期时，用 read 里的就会把他说清的日期又盖回去。
    # 读不清时是空串，空串表示发生日期未知，不拿今天冒充。
    raw_day = (happened_on if happened_on is not None
               else read["happened_on"] or "").strip()
    if _DATE_RE.match(raw_day):
        happened_on = raw_day
    else:
        happened_on = ""
    # 发生日期读不清时单独标出来，界面和审计都能看出这条待补日期，
    # 不是所有记录都默认「今天发生」。
    date_unknown = not happened_on
    # 来源写成「用什么记的 + 谁记的 + 核对没核对」，一眼能看出这条不是本人自述。
    # 记事本式录入下同一条草稿可能只有文字、只有图、或两者都有，
    # 一律写「拍照记录」会让只打了字的用户回看时以为图丢了。
    who = captured_by or owner_id
    kinds = [k for k in (input_kinds or []) if k in ("text", "image", "voice")]
    if not kinds:
        kinds = ["image"] if has_image else ["text"]
    label = {"text": "文字", "image": "图片", "voice": "语音"}
    used = "+".join(label[k] for k in kinds)
    provider = "%s记录（%s 录入，本人核对后采用）" % (used, who)
    # 来源 ID 必须由内容本身决定，不能每次新生成一个随机值。
    # import_life_event 的去重键是「来源 + 主体 + 类型 + 发生日期」，
    # 随机来源会让去重永远命中不了：同一条草稿手抖存两次就变成两条记录，
    # 之后按人回忆、按来源统计都会重复计数，本人还得自己去删。
    # 取实际内容的摘要：有图按图算（同一张图永远同一个 ID），
    # 纯文字按正文算——同一个人在更正前后写同一句话，本来也该算同一条。
    digest_src = image_b64 if has_image else ("text:" + final_text)
    rec_digest = hashlib.sha1(
        (digest_src or "").encode("utf-8", "replace")).hexdigest()[:16]
    source_id = "photo_%s" % rec_digest
    event_id, created = import_life_event(
        db, source_id, subject_id, "photo_record", payload, happened_on,
        provider, owner_id=owner_id,
        viewer_grants=grants or [],
        source_kind="photo")
    # 模型原文单独记进审计，不混进正文：正文键里只放本人最终采用的值，
    # 否则「本人核对后改过」和「模型就读成这样」在记录里分不出来。
    text_edited = bool(final_text) and final_text != (read.get("transcribed") or "").strip()
    db.audit("", owner_id, "photo_record_saved",
             {"event_id": event_id, "subject_id": subject_id,
              "captured_by": who, "model_confidence": read["confidence"],
              "fields_used": sorted(payload.keys()),
              "fields_model_read": sorted(read["fields"].keys()),
              "model_transcribed": (read.get("transcribed") or "").strip(),
              "final_text": final_text, "text_edited": text_edited,
             "unclear_parts": read["unclear_parts"],
             "happened_on": happened_on, "date_unknown": date_unknown})
    out = {"event_id": event_id, "created": created, "provider": provider,
           "happened_on": happened_on, "date_unknown": date_unknown,
           "fields": payload}
    # 正文被本人改过要说出来，界面据此标明这是核对后的版本。
    out["text_edited"] = text_edited
    out["read"] = read
    # 配图落库。只认本次事件，owner_id 跟事件一致，取图时按本人身份校验。
    # 重复保存不重复插：同一条事件上同样的图片只留一份，
    # 否则用户手抖点两次「保留」就多出两张一模一样的配图。
    att = _save_event_attachments(db, event_id, owner_id, attachments)
    out["attachments"] = att["kept"]
    # 有配图没存进去就说清楚，别让界面显示「已保存」而图其实丢了。
    # 记录本身已经落库，用户能重试补图；但不告诉他就是瞒。
    if att["failed"]:
        out["attachment_failed"] = att["failed"]
    return out


def _save_event_attachments(db, event_id, owner_id, attachments):
    """把本人明确保留的配图落到 event_attachment，返回可回读的清单。

    保存是幂等的：同一事件上同样的 base64 只插一条，重复点击不重复记。
    """
    if not attachments:
        return {"kept": [], "failed": 0}
    kept = []
    failed = 0
    for a in attachments:
        if not isinstance(a, dict):
            continue
        b64 = str(a.get("b64") or "")
        if not b64:
            continue
        media = str(a.get("media") or "image/jpeg")
        # 视频也走同一个附件机制：t61 起对话输入区可以带视频附件。
        # 视频只存档，不进任何模型提示词——见 docs/evidence/chat-input-modes-20261017.md。
        if media not in ("image/jpeg", "image/png", "image/webp",
                         "video/mp4", "video/quicktime", "video/webm"):
            media = "image/jpeg"
        have = db.row(
            "SELECT attachment_id FROM event_attachment WHERE event_id=? AND b64=?",
            (event_id, b64))
        if have is not None:
            kept.append({"attachment_id": have["attachment_id"], "media": media})
            continue
        try:
            aid = dbm.new_id("att")
            db.execute(
                "INSERT INTO event_attachment(attachment_id, event_id, owner_id,"
                " media, b64, note, created_at) VALUES(?,?,?,?,?,?,?)",
                (aid, event_id, owner_id, media, b64,
                 str(a.get("note") or ""), dbm.now_iso()))
            kept.append({"attachment_id": aid, "media": media})
        except Exception:
            # 存不进去必须说出来。把异常咽掉、只返回成功的那几张，
            # 界面会显示「已保存」而用户以为配图也存下了——
            # 下次打开记录发现图没了，他不知道该怪谁。
            # 计数返回给调用方，由它决定是报失败还是提示重试。
            failed += 1
            continue
    return {"kept": kept, "failed": failed}


def event_attachments(db, event_id, viewer_id):
    """按本人身份取一条事件的配图。看不到这条事件的人，也拿不到它的图。"""
    row = db.row("SELECT owner_id FROM life_event WHERE event_id=?", (event_id,))
    if row is None:
        return []
    if row["owner_id"] != viewer_id:
        # 不是记忆所有者：默认看不到配图。
        # 「授权了一个字段」不等于「授权了整张图」——事件授权按字段给，
        # 配图是这条记录的原始图像，里面往往包含没授权的其他内容。
        # 只在这条事件的授权里明确写了「允许看配图」时才放行，
        # 而不是拿字段可见当全图许可。
        if not _event_grants_attachment(db, event_id, viewer_id):
            return []
    return [dict(r) for r in db.rows(
        "SELECT attachment_id, media, b64, note, created_at"
        " FROM event_attachment WHERE event_id=? ORDER BY created_at", (event_id,))]


def _event_grants_attachment(db, event_id, viewer_id):
    """这条事件的授权里有没有明确允许查看者看配图。

    授权结构沿用 event_grant 的 fields_json，用固定标记 ATTACHMENT_GRANT_MARK
    表示「这条记录的配图可以看」。没有这个标记就只有文字字段可见，
    不因为「看得到一个字段」就连图一起给出去。
    """
    today = dbm.now_iso()[:10]
    for g in db.rows("SELECT * FROM event_grant WHERE event_id=? AND viewer_id=?",
                     (event_id, viewer_id)):
        if g["valid_until"] and g["valid_until"] < today:
            continue
        try:
            fields = json.loads(g["fields_json"])
        except (ValueError, TypeError):
            continue
        if ATTACHMENT_GRANT_MARK in fields:
            return True
    return False


def notice_life_event(db, event_id, viewer_id):
    """登记「这条事件已经通知过这个查看者」。重复导入不重复打扰。"""
    have = db.row(
        "SELECT notice_id FROM event_notice WHERE event_id=? AND viewer_id=?",
        (event_id, viewer_id))
    if have is not None:
        return False
    db.execute(
        "INSERT INTO event_notice(notice_id, event_id, viewer_id, kind, created_at)"
        " VALUES(?,?,?,?,?)",
        (dbm.new_id("notice"), event_id, viewer_id, "imported", dbm.now_iso()))
    return True


# ---------------------------------------------------------------------------
# 多人事项：多个家庭共同决定一次本地活动。
#
# 规则和送礼协商一致，但参与方更多：
# - 每个家庭只提供自己授权的条件，不替别家决定；
# - 约定必须各方都接受同一版本，缺一个必要参与方就仍待确认；
# - 一方改时间，受影响的旧接受失效，要重新核对；
# - 未回复就是未回复，不能默认同意。

# 条件字段和方案字段用同一组键：界面、模型、库里都按这些键读写，
# 不接受任意键，避免同一个意思出现三种写法后对不上。
# 口径分开：「本人出资上限」是这一家自己愿意出的最多多少钱，
# 「活动总预算」是整场活动一共要花多少。两者不是一个数。
# 用户原话「我出 100 以内」是前者；把它写成后者，等于替其他家庭
# 把整场活动的预算定成了 100，正是用户明确防的坑。
# 界面、模型、库里都按这两个键读写，不接受把两者混成一个「预算」。
# 事项条件字段按场景配置：家庭活动与朋友旅行各一组键，互不串味。
# 家庭场景保留原有 8 个键（一个都不能少、不能改名），行为零变化；
# 朋友旅行用新字段集，能表达「每个人想去的不一样」。校验、未知键拒绝、
# 出资口径、来源投影与授权对两个场景都成立，不只对新场景生效。
PLAN_SCENARIOS = {
    "group_activity": {
        "label": "家庭活动",
        "condition_keys": ("时间", "地点", "活动总预算", "本人出资上限",
                           "活动内容", "孩子安排", "交通", "备注"),
        # 改动这些键，这一版方案的内容就变了，要重排；
        # 只改「本人出资上限」影响的是这一家自己能不能接受，方案内容没变；
        # 只改「备注」是补充说明，两样都不影响。
        "plan_driving_keys": ("时间", "地点", "活动总预算", "活动内容",
                              "孩子安排", "交通"),
        "self_only_keys": ("本人出资上限",),
        "context_keys": ("备注",),
        "own_cap_key": "本人出资上限",
        "total_budget_key": "活动总预算",
        "percap_key": None,
        "family_flavored": True,
    },
    "friend_trip": {
        "label": "朋友旅行",
        "condition_keys": ("时间", "目的地", "假期长度", "人均预算", "本人预算上限",
                           "住宿标准", "交通", "能否带伴侣", "忌口或体力", "备注"),
        "plan_driving_keys": ("时间", "目的地", "假期长度", "住宿标准",
                              "能否带伴侣", "忌口或体力"),
        "self_only_keys": ("本人预算上限",),
        "context_keys": ("备注",),
        "own_cap_key": "本人预算上限",
        "total_budget_key": None,
        "percap_key": "人均预算",
        "family_flavored": False,
    },
}
# 家庭场景的键与口径说明单独留名：既有界面/模型/测试照旧引用，行为不变。
PLAN_CONDITION_KEYS = PLAN_SCENARIOS["group_activity"]["condition_keys"]

# 出资口径的说明，给界面和模型用同一句，避免两边说法不一致。
OWN_CAP_HINT = "「本人出资上限」只是这一家自己最多出多少，不是活动总预算"

# 旧的「预算」键留着当别名，但**不一律**归一成「活动总预算」。
# 直接删掉这个键，老调用方（含既有的契约测试和早期界面）会当场 400，
# 用户看到的是「条件字段不对」而不是「这个字段现在叫什么」。
# 一律归一成总预算更是错的那一种：e2e 用例 17 的「每家人不超过80元」
# 是**每家的出资上限**，归一成总预算就把 80 说成了整场活动的预算，
# 正是用户明确防的坑。所以按值里的措辞判断口径，判不出来的请本人确认。
LEGACY_BUDGET_KEY = "预算"
PER_FAMILY_MARKERS = ("每家", "每户", "各自", "每方", "一方", "本家", "我家")
TOTAL_MARKERS = ("总共", "一共", "合计", "总预算", "整体", "全场", "总费用")
# 人均口径：朋友旅行里「人均/每人」说的是每个人的份额，不是整场总预算，
# 也不是某一方的上限。三者分开，不能混成一个「预算」。
PER_CAP_MARKERS = ("人均", "每人", "每个人", "每一位", "每位")


def condition_keys(scenario):
    """这个场景允许的条件字段。未知场景退回家庭活动的键，不抛错。"""
    cfg = PLAN_SCENARIOS.get(scenario) or PLAN_SCENARIOS["group_activity"]
    return cfg["condition_keys"]


def own_cap_hint(scenario):
    """这个场景的「本人上限 ≠ 总预算/人均」口径说明，界面和模型共用一句。"""
    cfg = PLAN_SCENARIOS.get(scenario) or PLAN_SCENARIOS["group_activity"]
    if cfg.get("family_flavored", True):
        return OWN_CAP_HINT
    return "「本人预算上限」只是你自己最多出多少，不是人均预算，也不是替所有人定的价"


def scenario_label(scenario):
    cfg = PLAN_SCENARIOS.get(scenario) or PLAN_SCENARIOS["group_activity"]
    return cfg["label"]


def budget_calibre(value):
    """这句预算说的是哪一口径。返回 own / total / unknown，不猜。"""
    t = str(value or "")
    per = any(m in t for m in PER_FAMILY_MARKERS)
    tot = any(m in t for m in TOTAL_MARKERS)
    if per and not tot:
        return "own"
    if tot and not per:
        return "total"
    if per and tot:
        return "unknown"
    # 光一个数字（「100元」）说不清是谁的：可能是这一家的上限，
    # 也可能是整场的预算。请本人确认，不默认成任何一个。
    return "unknown"


def normalize_conditions(conditions, scenario="group_activity"):
    """把条件归一到带口径的键上。按场景取字段集。

    返回 (归一后的条件, 不认识的键列表, 要本人确认的键列表)。
    第三个列表非空时，调用方必须停下来问本人，不能挑一个口径默认。
    家庭场景（默认）行为与之前完全一致；朋友旅行按 friend_trip 字段集，
    「预算」旧键按 人均/本人上限 口径归一，判不出就请本人确认。
    """
    cfg = PLAN_SCENARIOS.get(scenario) or PLAN_SCENARIOS["group_activity"]
    keys = cfg["condition_keys"]
    out = {}
    unknown = []
    confirm = []
    for k, v in (conditions or {}).items():
        key = str(k)
        val = str(v)
        if key == LEGACY_BUDGET_KEY:
            if cfg.get("family_flavored", True):
                cal = budget_calibre(val)
                if cal == "own":
                    key = cfg["own_cap_key"]
                elif cal == "total":
                    key = cfg["total_budget_key"]
                else:
                    # 口径不明：两个键都不写，交给本人确认。默认成任何一个，
                    # 都是替他把话说清楚，而这句话本来是他自己的条件。
                    confirm.append(LEGACY_BUDGET_KEY)
                    continue
            else:
                # 朋友旅行：人均/每人是每个人的份额；我出/我家/各自等是本人上限；
                # 判不出（光一个数、或多种口径同时出现）就请本人确认。
                per_cap = any(m in val for m in PER_CAP_MARKERS)
                own = any(m in val for m in ("我出", "我最多", "我个人", "自己出", "我这",
                                             "本家", "我家", "各自", "我方"))
                if per_cap and not own:
                    key = cfg["percap_key"]
                elif own and not per_cap:
                    key = cfg["own_cap_key"]
                else:
                    confirm.append(LEGACY_BUDGET_KEY)
                    continue
        if key not in keys:
            unknown.append(str(k))
            continue
        # 同一个键给两个口径时，后写的那个不覆盖前一个，两个都留下让本人看。
        out[key] = val
    return out, unknown, confirm


def find_open_plan(db, owner_id, title):
    """同一家人名下、还没定完的同名草稿。没有就 None。

    创建和复用判断都走这一个函数：两处各写一遍 SQL，
    改一处漏一处，就会出现「创建时复用了、判断时又说新建」。
    """
    return db.row(
        "SELECT plan_id FROM plan WHERE owner_id=? AND title=?"
        " AND stage IN ('draft','awaiting') ORDER BY created_at DESC LIMIT 1",
        (owner_id, (title or "").strip()))


def create_plan(db, owner_id, title, utterance="", kind="group_activity"):
    """发起一个多人事项。

    同一句原话重复发起时，接着原来那件还没定完的事办，不再开一件看起来
    一模一样的。用户重点一次「创建」往往是没看到回应，不是想办两件事。
    已经约定或明确失败的不在此列，否则会挡住以后合理再次发起。
    """
    text = (utterance or title or "").strip()
    if text:
        dup = find_open_plan(db, owner_id, title)
        if dup is not None:
            return dup["plan_id"]
    plan_id = dbm.new_id("plan")
    db.execute(
        "INSERT INTO plan(plan_id, owner_id, title, utterance, kind, stage, outcome,"
        " created_at) VALUES(?,?,?,?,?,?,?,?)",
        (plan_id, owner_id, title, utterance, kind, "draft", "", dbm.now_iso()))
    return plan_id


def plan_current_version(db, title, owner_id):
    """这件未完结的共同事项当前排到第几版。没有就返回 None。

    创建时用它判断是新建还是复用，避免复用时又生成一版方案。
    """
    row = find_open_plan(db, owner_id, title)
    if row is None:
        return None
    latest = db.row("SELECT MAX(version) AS v FROM plan_proposal WHERE plan_id=?",
                    (row["plan_id"],))
    return (latest["v"] if latest and latest["v"] else 0) or None


# 家庭结构从关系表推导，不写死任何人名。
# 用户案例里「每家几个孩子」必须来自已知的有效关系：小周家两个孩子是从
# 「小周是父亲」「小林是母亲」这几条关系数出来的，不是从「小雨」「小宇」
# 这两个名字匹配出来的。换一套演示数据，同一个函数照样说得对。
PARENT_RELATIONS = ("父亲", "母亲", "家长", "父母", "爸爸", "妈妈")
# person.relation 里标明「这是个孩子」的取值。没有家长关系记录时，
# 靠它判断谁是孩子，避免把一个缺家长记录的孩子当成一家之主。
CHILD_RELATIONS = ("child", "own_child", "friend_child", "son", "daughter",
                   "孩子", "儿子", "女儿")
SPOUSE_RELATIONS = ("配偶", "夫妻", "妻子", "丈夫", "老公", "老婆")


def _person_index(db, owner_id):
    rows = db.rows("SELECT person_id, name, relation FROM person WHERE owner_id=?"
                   " ORDER BY name", (owner_id,))
    return {r["person_id"]: r for r in rows}


def _identity_classes(db, person_ids):
    """按 person_link 把两侧记的是同一个人的 id 并成一个等价类。

    不按名字合：两个用户记录里同名的人不一定是同一个人
    （person_link 的注释早就写明了）。
    """
    parent = {r["person_id"]: r["person_id"] for r in db.rows("SELECT person_id FROM person")}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        if x not in parent or y not in parent:
            return
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for row in db.rows("SELECT person_id_a, person_id_b FROM person_link"
                       " WHERE status='active'"):
        union(row["person_id_a"], row["person_id_b"])
    return {pid: find(pid) for pid in parent}


def _spouse_pairs(db, owner_id, persons):
    """明确的配偶/同家庭关系对。三条来源，**没有一条是「共同抚养孩子」**。

    1. relation 表里 relation_type 属于 SPOUSE_RELATIONS 的行；
    2. person_link 关联到别侧记录时，别侧那条明确的配偶关系也算数——
       A 记「小林是朋友的配偶」，别侧记「小周是我的配偶」，两条合起来才是证据；
    3. person.relation 写成 spouse / friend_spouse 的：spouse 是本人的配偶；
       friend_spouse 是「某位朋友的配偶」，那位朋友取与本人物共享已记录孩子的
       唯一成人。取不到唯一值就不配对——宁可少聚一家，也不能把离异或未同住的
       两位家长并成一个家庭。
    """
    pairs = []          # [(a_pid, b_pid, relation_id, observed_on)]
    ident = _identity_classes(db, list(persons))
    by_class = {}
    for pid, cls in ident.items():
        by_class.setdefault(cls, []).append(pid)

    # 1 + 2：跨 owner 读 relation，按等价类归位。
    rows = db.rows(
        "SELECT relation_id, owner_id, from_person_id, relation_type, to_person_id,"
        " observed_on FROM relation WHERE status='active'")
    for r in rows:
        if r["relation_type"] not in SPOUSE_RELATIONS:
            continue
        a_cls, b_cls = ident.get(r["from_person_id"]), ident.get(r["to_person_id"])
        if a_cls is None or b_cls is None or a_cls == b_cls:
            continue
        for a in by_class.get(a_cls, []):
            for b in by_class.get(b_cls, []):
                pairs.append((a, b, r["relation_id"], r["observed_on"]))

    # 3：person.relation 直接写明的配偶关系。
    parent_of = {}
    for r in db.rows("SELECT from_person_id, relation_type, to_person_id FROM relation"
                     " WHERE owner_id=? AND status='active'", (owner_id,)):
        if (r["relation_type"] in PARENT_RELATIONS
                and r["from_person_id"] in persons and r["to_person_id"] in persons):
            parent_of.setdefault(r["to_person_id"], set()).add(r["from_person_id"])
    me_ids = [pid for pid, r in persons.items() if r["relation"] == "self"]
    for pid, row in persons.items():
        rel = row.get("relation") or ""
        if rel == "spouse" and me_ids:
            pairs.append((pid, me_ids[0], "person.relation:spouse", ""))
        elif rel == "friend_spouse":
            # 这一方和哪位朋友共享已记录的孩子，且只有那一位，才认定是同家庭。
            shared = set()
            for child, parents in parent_of.items():
                if pid in parents:
                    shared |= (parents - {pid})
            adults = {m for m in shared if m not in parent_of}
            if len(adults) == 1:
                pairs.append((pid, adults.pop(), "person.relation:friend_spouse", ""))
    return pairs


def family_overview(db, owner_id):
    """按本人可见的**明确**关系，列出家庭与每家相关的孩子。

    四条硬规则（t17 修；旧实现把共同抚养的孩子当成夫妻证据、把所有单身人物
    都算成一个家庭，纯环关系还会让 head 取空崩掉）：
    1. 成人只按**明确的配偶/同家庭关系**聚成一个家庭。共同抚养一个孩子
       **不构成**同家证据——离异、未同住、跨代帮忙带的父母都会因此被错误
       合并成一个家庭；
    2. 孩子按父母挂到各个家庭下面，可以同时属于多个家庭（候选），
       **但不充当家庭之间的桥**：不把两个家庭并成一个；
    3. 没有家长记录的孩子、没有明确家庭关系的成人都照实单独列出，
       不硬塞进某个家庭，也不算「已参加」；
    4. 家长信息不齐就标未知，不拿已知的那几个孩子当完整人数、更不当成
       都出席。全程按 person_id，不按姓名合并。
    """
    persons = _person_index(db, owner_id)
    if not persons:
        return {"owner_id": owner_id, "families": [], "known_family_count": 0}

    me_ids = {pid for pid, r in persons.items() if r["relation"] == "self"}
    parent_of = {}      # child_id -> [(parent_id, relation_id, observed_on)]
    for r in db.rows(
            "SELECT relation_id, from_person_id, relation_type, to_person_id, observed_on"
            " FROM relation WHERE owner_id=? AND status='active'", (owner_id,)):
        if (r["relation_type"] in PARENT_RELATIONS
                and r["from_person_id"] in persons and r["to_person_id"] in persons):
            parent_of.setdefault(r["to_person_id"], []).append(
                (r["from_person_id"], r["relation_id"], r["observed_on"]))

    spouse_pairs = _spouse_pairs(db, owner_id, persons)

    # 是不是孩子：relation 表里有家长算，person.relation 写明是孩子也算。
    # 后者重要——一个记着「friend_child」但还没有家长关系的人，
    # 不能因为缺家长就被当成一家之主。
    def is_child(pid):
        return (pid in parent_of
                or (persons[pid].get("relation") or "") in CHILD_RELATIONS)

    # 成人分组：只走明确的配偶/同家庭关系。**孩子不参与并查集**。
    adults_all = [pid for pid in persons if not is_child(pid)]
    group = {pid: pid for pid in adults_all}

    def find(x):
        while group[x] != x:
            group[x] = group[group[x]]
            x = group[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            group[ry] = rx

    for a, b, _rid, _on in spouse_pairs:
        if a in group and b in group:
            union(a, b)

    buckets = {}
    for pid in adults_all:
        buckets.setdefault(find(pid), []).append(pid)

    def kids_of(members):
        """这一组挂哪些孩子：成员（含作为孩子成员的长辈）是已记录家长的都算。

        往下传一代：爸爸在爷爷那个组里，孩子挂在爸爸名下，也就跟着进这一组。
        只往一个方向挂，不做并集——共同抚养的两个家庭仍然各列各的，
        同一个孩子可以同时出现在两个组里（候选），不因此合并两个组。
        """
        members = set(members)
        out = set()
        frontier = set(members)
        while frontier:
            nxt = set()
            for c, ps in parent_of.items():
                if c in out or c in members:
                    continue
                if any(pid in frontier for pid, _rid, _on in ps):
                    out.add(c)
                    nxt.add(c)
            frontier = nxt
        return sorted(out)

    def head_of(adults):
        for m in adults:
            if m in me_ids:
                return m
        linked = [m for m in adults
                  if any(m in (a, b) for a, b, _r, _o in spouse_pairs)]
        return linked[0] if linked else (adults[0] if adults else None)

    families = []
    claimed = set()
    for root, members in buckets.items():
        kids = kids_of(members)
        claimed.update(kids)
        head = head_of(members)
        kid_rows = [{"person_id": c, "name": persons[c]["name"],
                     "evidence": [{"relation_id": rid, "parent_id": pid,
                                   "observed_on": on}
                                  for pid, rid, on in parent_of.get(c, [])
                                  if pid in members]}
                    for c in kids]
        label = "我家" if head in me_ids else (persons[head]["name"] + "家")
        families.append({
            "key": "f_" + head,
            "label": label,
            "is_me": head in me_ids,
            "head_person_id": head,
            "adults": [{"person_id": m, "name": persons[m]["name"],
                        "relation": persons[m]["relation"],
                        # 只有这一组里有明确家庭关系的成人才算「同家庭已确认」，
                        # 单独一个朋友不算。
                        "same_family_confirmed": any(
                            m in (a, b) for a, b, _r, _o in spouse_pairs)}
                       for m in members],
            "children": kid_rows,
            # 0 个孩子不等于没有孩子：没有关系能证明有，就是未知。
            "children_known": bool(kid_rows),
            "children_note": ("授权关系里记着 %d 个孩子" % len(kid_rows)) if kid_rows
                             else "还没有能证明这一家有几个孩子的关系记录",
            "evidence": [{"relation_id": rid, "a": a, "b": b, "observed_on": on}
                         for a, b, rid, on in spouse_pairs
                         if a in members and b in members],
        })

    # 没有任何已记录家长的孩子：单独列出，不归到任何家庭，也不算已参加。
    leftover = [pid for pid in persons if is_child(pid) and pid not in claimed]
    for child in leftover:
        ps = parent_of.get(child, [])
        families.append({
            "key": "f_" + child, "label": "未定家庭", "is_me": False,
            "head_person_id": "", "adults": [],
            "children": [{"person_id": child, "name": persons[child]["name"],
                          "evidence": [{"relation_id": rid, "parent_id": pid,
                                        "observed_on": on}
                                       for pid, rid, on in ps]}],
            "children_known": False,
            "children_note": "这个孩子的家长还没有记录，先不归到任何家庭",
            "evidence": [],
        })

    families.sort(key=lambda f: (not f["is_me"], f["label"]))
    # 真实家庭户数：只有这两种算一户——
    #   ① 本人自己这一方（is_me），一个人也是一户；
    #   ② 组里有明确配偶/同户关系的成人（same_family_confirmed），
    #      也就是并查集被 spouse_pairs 真正合并过的组。
    # 单人成组、没有任何配偶/同户关系的成人（例如只为送礼建的人物）**不算一户**：
    # 他不是「另一家」，只是我认识的一个人。规则 3 照实单独列出仍然保留，
    # 这里只是另外给一个「户数」供「说了几家／还缺几家」消费。
    # 原来拿 len(families) 当户数，会把送礼对象、朋友这些都算成家庭，
    # 「周末三家」的第三家缺口被悄悄填掉——那和「不拿配偶凑数」是同一条边界。
    household = [f for f in families
                 if f["is_me"]
                 or any(a["same_family_confirmed"] for a in f["adults"])]
    return {"owner_id": owner_id, "families": families,
            "known_family_count": len(families),
            "household_count": len(household)}


def count_requested_families(text):
    """用户这句话里说了几个家庭。没说就返回 None，不猜。"""
    import re
    t = text or ""
    cn = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
    # 不用 \b：中文字符在 Python 里也算 \w，「家」后面紧跟汉字时
    # 永远找不到边界，三家、两个家庭 一个都匹配不上。
    m = re.search(r"([0-9一二两三四五六])\s*(?:个)?(?:家庭|家人|家)", t)
    if m:
        tok = m.group(1)
        return int(tok) if tok.isdigit() else cn.get(tok)
    if "几家" in t or "多少家" in t:
        return None
    return None


def group_activity_intent(text):
    """这句话是在约一场多家庭活动，不是要给谁送礼。

    判据两条同时满足：说了多个家庭，又在说一起做什么。
    「给小周的女儿准备生日礼物」没有多家庭措辞，不会误入；
    「周末三家一起带孩子出去玩」两条都中。
    用关键词而不是只靠模型：这条入口错了，用户原话会被当成送礼交办，
    接着被要求「点一个孩子」——那正是用户明确不要的。
    """
    t = (text or "").strip()
    if not t:
        return False
    multi = bool(count_requested_families(t)) or any(
        k in t for k in ("几家", "多少个家庭", "多少户"))
    doing = any(k in t for k in (
        "出去玩", "出游", "聚会", "一起", "组团", "公园", "活动", "爬山",
        "露营", "吃饭", "自驾", "遛娃", "带娃"))
    # 明确在说给某一个人准备东西的，仍是送礼，不因为提到「三家一起出游」
    # 就改成活动——「给三家一起出游的孩子们准备礼物」要办的是礼物。
    gifting = any(k in t for k in ("礼物", "送礼", "买给", "生日礼", "买什么"))
    return bool(multi and doing and not gifting)


def extract_own_cap(text):
    """从本人这句话里取出「我这一方最多出多少」，不当成活动总预算。

    「我出 100 以内」「我这半边 200」是本家出资上限；
    「总共 300」「活动一共 500」才是活动总预算。两者分开记，
    因为把一家的上限写成总预算，等于替其他家庭把整场活动定了价。
    """
    import re
    t = (text or "").replace(" ", "")
    own = re.search(r"我(?:们)?(?:家|这边|这半边)?(?:出|承担|负责)([0-9]+(?:\.\d+)?)"
                    r"(?:元|块|人民币)?(?:以内|之内|以下|左右|最多)?", t)
    if own:
        return own.group(1) + " 元以内"
    total = re.search(r"(?:总共|一共|合计|活动)(?:预算)?(?:是|为)?([0-9]+(?:\.\d+)?)"
                      r"(?:元|块|人民币)", t)
    if total:
        return None
    return None


def _plan_meta(db, plan_id):
    row = db.row("SELECT meta_json FROM plan WHERE plan_id=?", (plan_id,))
    try:
        return json.loads((row or {}).get("meta_json") or "{}")
    except (ValueError, TypeError):
        return {}


def _save_plan_meta(db, plan_id, meta):
    db.execute("UPDATE plan SET meta_json=? WHERE plan_id=?",
               (json.dumps(meta, ensure_ascii=False), plan_id))


def start_group_activity(db, client, owner_id, utterance):
    """从一句闲聊直接开一场家庭活动草稿，不走送礼链。

    做五件事，都不替别人决定：
    1. 建（或接着原有的）家庭活动草稿，本人作为发起方只带自己一方进场；
    2. 把「我出 100 以内」记成本人出资上限，不记成活动总预算；
    3. 按已知关系列出家庭与孩子，数不出来的明说未知；
    4. 把「说了几家 / 还缺几家 / 哪些孩子说来说不来」**持久**到 plan.meta_json，
       刷新、从别处跟进都还在，不会退回成一个空草稿；
    5. 回复说清缺什么、等谁，不给「点一个孩子」这种单选。
    """
    text = (utterance or "").strip()
    title = text[:120] or "家庭活动"
    # 复用判断必须在创建之前：创建之后这件草稿当然存在，
    # 拿「存在不存在」当依据，每次都报「已存在」，前端就永远不生成第一版方案。
    reused = find_open_plan(db, owner_id, title) is not None
    plan_id = create_plan(db, owner_id, title, text)
    cap = extract_own_cap(text)
    conditions = {}
    if cap:
        conditions["本人出资上限"] = cap
    mine = db.row("SELECT participant_id FROM plan_participant WHERE plan_id=? AND owner_id=?",
                  (plan_id, owner_id))
    if mine is None:
        add_plan_participant(db, plan_id, owner_id, "我家", conditions, True,
                             role="self")
    elif cap:
        # 本人条件更新走 condition 入口，那里有 actor 校验；这里只补空缺，
        # 不覆盖他已经改过的值。
        cur = db.row("SELECT conditions_json FROM plan_participant"
                     " WHERE plan_id=? AND owner_id=?", (plan_id, owner_id))
        have = json.loads(cur["conditions_json"] or "{}")
        if "本人出资上限" not in have:
            have["本人出资上限"] = cap
            db.execute("UPDATE plan_participant SET conditions_json=? WHERE plan_id=?"
                       " AND owner_id=?",
                       (json.dumps(have, ensure_ascii=False), plan_id, owner_id))

    fams = family_overview(db, owner_id)
    want = count_requested_families(text)
    # 「说了几家 / 还缺几家」按**真实家庭户数**算，不按 family_overview 列出的条目数。
    # 列出的条目里包含「没有家庭关系的成人」（规则 3 照实单独列出，展示是对的），
    # 但那些不是家庭；拿他们填缺口等于拿送礼对象凑数。
    households = fams.get("household_count", len(fams["families"]))
    # known 仍取全部条目：回复里「按已记下的关系，知道的有：…」要如实列出
    # 认识的人（含没有家庭关系的成人），展示口径和 family_overview 一致。
    known = fams["families"]
    missing = max(0, (want or 0) - households) if want else 0
    meta = _plan_meta(db, plan_id)
    if want:
        meta["requested_families"] = want
    meta.setdefault("invited", [])
    meta["unknown_families"] = max(0, (want or 0) - len(_selected_family_keys(db, plan_id)))
    meta.setdefault("child_attendance", {})
    _save_plan_meta(db, plan_id, meta)
    reply = _group_activity_reply(text, cap, known, missing)
    save_turn(db, owner_id, "system", reply, plan_id=plan_id)
    return {"plan_id": plan_id, "reused": reused, "title": title,
            "own_cap": cap or "", "families": known,
            "missing_families": missing,
            "requested_families": want or 0,
            "reply": reply}


def _family_owner(db, fam):
    """Resolve a contact family through explicit linked adult/spouse records."""
    ids = _identity_classes(db, [])
    classes = {ids.get(a["person_id"]) for a in fam.get("adults", [])}
    matches = []
    for r in db.rows("SELECT owner_id FROM person WHERE relation='self'"):
        own = next((f for f in family_overview(db, r["owner_id"])["families"] if f["is_me"]), None)
        own_classes = {ids.get(a["person_id"]) for a in (own or {}).get("adults", [])}
        if (classes - {None}) & (own_classes - {None}):
            matches.append(r["owner_id"])
    return matches[0] if len(matches) == 1 else None


def _own_children(db, owner_id):
    persons = _person_index(db, owner_id)
    me = {pid for pid, p in persons.items() if p["relation"] == "self"}
    child_ids = {p["person_id"] for p in persons.values() if p["relation"] in ("own_child", "child", "son", "daughter")}
    for r in db.rows("SELECT from_person_id,to_person_id,relation_type FROM relation WHERE owner_id=? AND status='active'", (owner_id,)):
        if r["from_person_id"] in me and r["relation_type"] in PARENT_RELATIONS:
            child_ids.add(r["to_person_id"])
    return [persons[pid] for pid in child_ids if pid in persons]


def _selected_family_keys(db, plan_id):
    """Count families, not identities; explicit spouses count once."""
    identity = _identity_classes(db, [])
    keys = set()
    for p in db.rows("SELECT owner_id,role FROM plan_participant WHERE plan_id=?", (plan_id,)):
        if p["role"] in ("retired", "guest_pending", "guest_revoked"):
            continue
        # The demo contract explicitly defines C as B's same-family second adult.
        actor = "B" if p["owner_id"] == "C" and any(u[0] == "C" and u[2] == "same_family_second_adult" for u in dbm.DEMO_USERS) else p["owner_id"]
        own = family_overview(db, actor)["families"]
        mine = next((f for f in own if f["is_me"]), None)
        members = [identity.get(a["person_id"], a["person_id"]) for a in (mine or {}).get("adults", [])]
        keys.add(min(members) if members else "owner:" + p["owner_id"])
    return keys


def _activity_gaps(db, plan_id, meta):
    requested = meta.get("requested_families") or 0
    gaps = []
    missing = max(0, requested - len(_selected_family_keys(db, plan_id)))
    if missing:
        gaps.append("还有 %d 家未明确进入事项" % missing)
    for g in db.rows("SELECT role,label,attendance_json FROM plan_participant WHERE plan_id=? AND role LIKE 'guest%'", (plan_id,)):
        a = json.loads(g["attendance_json"])
        if g["role"] != "guest":
            gaps.append(g["label"] + " 尚未获得本事项入场认可或已撤回")
        elif a.get("children_count") is None or any(c["attendance"] == "unknown" for c in a.get("guest_children", [])):
            gaps.append(g["label"] + " 孩子人数或出席仍待本人确认")
    if not requested:
        return gaps
    counts = meta.get("children_counts", {})
    for p in db.rows("SELECT owner_id,label,role,attendance_json FROM plan_participant WHERE plan_id=? AND required=1 AND role<>'retired'", (plan_id,)):
        kids = _own_children(db, p["owner_id"])
        if p.get("role", "").startswith("guest") or p.get("role") == "retired":
            continue
        attendance = json.loads(p["attendance_json"] or "{}")
        n = counts.get(p["owner_id"])
        if not kids and n != 0:
            gaps.append(p["label"] + "孩子人数与出席待本人确认")
        elif n is not None and n != len(kids):
            gaps.append(p["label"] + "孩子名单与人数尚未对齐")
        elif any(attendance.get(c["person_id"]) not in ("coming", "not_coming") for c in kids):
            gaps.append(p["label"] + "孩子出席待本人确认")
    return gaps


def continue_group_activity(db, owner_id, text):
    """Continue latest participating activity; each side alone confirms its children."""
    text = (text or "").strip()
    if not text:
        return None
    row = db.row("SELECT p.plan_id,p.owner_id FROM plan p JOIN plan_participant s ON s.plan_id=p.plan_id"
                 " WHERE s.owner_id=? AND p.kind='group_activity' AND p.stage IN ('draft','awaiting')"
                 " ORDER BY p.created_at DESC LIMIT 1", (owner_id,))
    if row is None:
        return None
    plan_id = row["plan_id"]
    meta = _plan_meta(db, plan_id)
    fams = family_overview(db, owner_id)["families"]
    changed = []
    if row["owner_id"] == owner_id:
        for fam in fams:
            if not fam["is_me"] and fam["head_person_id"] and fam["label"] in text:
                actor = _family_owner(db, fam)
                if actor is None:
                    changed.append(fam["label"] + "尚无明确的本人入口，仍待确认")
                    continue
                add_plan_participant(db, plan_id, actor, fam["label"], {}, True, role="family")
                if fam["key"] not in meta.setdefault("invited", []):
                    meta["invited"].append(fam["key"])
                changed.append("把%s选进事项（候选，等这一方自己回应）" % fam["label"])
        if any(x in text for x in ("另外一家", "另一家", "再找一家")):
            changed.append("另外一家身份待明确，不拿联系人凑数")
    mine = db.row("SELECT participant_id,attendance_json FROM plan_participant WHERE plan_id=? AND owner_id=?", (plan_id, owner_id))
    own = {c["person_id"] for c in _own_children(db, owner_id)}
    attendance = json.loads(mine["attendance_json"] or "{}")
    import re
    for c in _person_index(db, owner_id).values():
        if c["relation"] not in CHILD_RELATIONS:
            continue
        m = re.search(re.escape(c["name"]) + r"\s*(不来|不去|缺席|来|去|参加)", text)
        if not m:
            continue
        if c["person_id"] not in own:
            changed.append(c["name"] + "出席待其家长本人确认，未代填")
            continue
        state = "not_coming" if m.group(1) in ("不来", "不去", "缺席") else "coming"
        attendance[c["person_id"]] = state
        meta.setdefault("child_attendance", {})[c["person_id"]] = state
        changed.append(c["name"] + (" 不来" if state == "not_coming" else " 来"))
    m = re.search(r"我家\s*([0-9一二两三四五零])\s*个?孩子", text)
    if m:
        tok = m.group(1)
        n = int(tok) if tok.isdigit() else {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5}[tok]
        meta.setdefault("children_counts", {})[owner_id] = n
        if row["owner_id"] == owner_id:
            meta["my_children_count"] = n
        changed.append("本人这一方 %d 个孩子" % n)
    if not changed:
        return None
    meta["unknown_families"] = max(0, (meta.get("requested_families") or 0) - len(_selected_family_keys(db, plan_id)))
    db.execute("UPDATE plan_participant SET attendance_json=? WHERE participant_id=?", (json.dumps(attendance, ensure_ascii=False), mine["participant_id"]))
    _save_plan_meta(db, plan_id, meta)
    if attendance != json.loads(mine["attendance_json"] or "{}") or m:
        db.execute("UPDATE plan_decision SET decision='stale' WHERE plan_id=?", (plan_id,))
        if db.row("SELECT COUNT(*) c FROM plan_proposal WHERE plan_id=?", (plan_id,))["c"]:
            db.execute("UPDATE plan SET needs_replan=1 WHERE plan_id=?", (plan_id,))
    refresh_plan_stage(db, plan_id)
    return {"plan_id": plan_id, "changes": changed, "meta": meta,
            "reply": "已记下：" + "；".join(changed) + "。都还在同一件事里。"}


def _group_activity_reply(text, cap, families, missing):
    # 说明写短。事实一条不少：出资上限只是本人这一方、别家自己说；
    # 已知家庭有哪些；说的几家只认出几家、缺的留着待确认；
    # 各家条件各家自己补。不堆「不会…也不会…」那种多重否定。
    lines = ["这件事按家庭活动记下了。"]
    if cap:
        lines.append("「%s」记在**本人出资上限**里，只是你家最多出多少；"
                     "别家出多少由别家自己说。" % cap)
    else:
        lines.append("还没说你这一方出多少。那是你自己的条件，别人替不了你。")
    if families:
        parts = []
        for f in families:
            if f["children_known"]:
                parts.append("%s（%s）" % (f["label"],
                                          "、".join(c["name"] for c in f["children"])))
            else:
                parts.append("%s（有几个孩子还没记录）" % f["label"])
        lines.append("按已记下的关系，知道的有：" + "；".join(parts) + "。")
    if missing:
        lines.append("你说了 %d 家，只认得出 %d 家。剩下 %d 家没有记录，先留着待确认。"
                     % (len(families) + missing, len(families), missing))
    lines.append("各家的条件各家自己补；齐了我排一版方案给大家确认。")
    return "\n".join(lines)


def add_plan_participant(db, plan_id, owner_id, label, conditions, required=True,
                         role="family", attendance=None):
    """加一个参与方。conditions 是这一方自己授权提供的条件。

    同一方在同一件事里只占一行：按 (plan_id, owner_id) 查重。
    不查重的后果是「同 title 重复 create」——用户重点一次创建往往是没看到
    回应——参与方被插第二行，条件、表态、版本跟着翻倍，
    已经约好的事看起来又冒出几个没回复的人。2026-10-14 复核记的缺陷 2。
    查重命中时**原样返回已有那行**，不覆盖条件、不改状态：
    改条件走 /api/plan/{id}/condition，那里有 actor 校验。
    """
    have = db.row(
        "SELECT participant_id FROM plan_participant WHERE plan_id=? AND owner_id=?",
        (plan_id, owner_id))
    if have is not None:
        return have["participant_id"]
    pid = dbm.new_id("pp")
    db.execute(
        "INSERT INTO plan_participant(participant_id, plan_id, owner_id, label,"
        " conditions_json, required, status, role, attendance_json, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (pid, plan_id, owner_id, label, json.dumps(conditions, ensure_ascii=False),
         1 if required else 0, "pending", role,
         json.dumps(attendance or {}, ensure_ascii=False), dbm.now_iso()))
    return pid


def new_plan_proposal(db, plan_id, proposer, content, reason=""):
    """新一版方案。版本号从该事项已有最大版本之后接着排。

    新版本一出，比它旧的接受全部作废：一方改了时间之后，
    不能拿别家对旧版本的接受当成本次约定。
    """
    latest = db.row("SELECT MAX(version) AS v FROM plan_proposal WHERE plan_id=?", (plan_id,))
    version = (latest["v"] or 0) + 1
    ppid = dbm.new_id("pprop")
    db.execute(
        "INSERT INTO plan_proposal(plan_proposal_id, plan_id, version, proposer,"
        " content_json, reason, created_at) VALUES(?,?,?,?,?,?,?)",
        (ppid, plan_id, version, proposer, json.dumps(content, ensure_ascii=False),
         reason, dbm.now_iso()))
    db.execute(
        "UPDATE plan_decision SET decision='stale', reason=? WHERE plan_id=? AND version<?",
        ("方案已更新到第 %d 版，旧版本接受不再有效" % version, plan_id, version))
    # 新版本一出，之前的「已约定」立刻不算数：阶段要跟着退回待确认，
    # 否则用户看到的是已经约好，其实方案刚被人改过。
    db.execute("UPDATE plan_proposal SET provenance_json=? WHERE plan_proposal_id=?", (json.dumps(plan_provenance(db, plan_id), ensure_ascii=False), ppid))
    refresh_plan_stage(db, plan_id)
    return ppid, version


def decide_plan(db, plan_id, participant_id, version, decision, reason="", changes=None,
                actor_id="", decided_by="person"):
    """参与方对某一版表态。未回复时不写这条记录。

    actor_id 是这次表态的调用身份（演示身份）。本地演示只有 A/B 两个身份，
    不是生产多用户认证，但同样要拦住三类越权：
    decided_by 记这条表态的来源：person 是本人点的，
    representative 是这一方的个人代表审阅出来的。两者都只代表这一方，
    但来源必须分开，不能把代表审阅显示成本人确认。
    - 这个参与方不属于传入的这个事项；
    - 表态的版本不存在，或不是当前版本（旧版本接受不能重新算成约定）；
    - 调用身份不是这一方的代表本人（不能替别家表态）。
    拒绝时不留 decision、不改阶段，也不新建版本。
    """
    if decision not in ("accept", "reject", "counter"):
        raise ValueError("表态只能是接受、拒绝或反提案")
    guest = db.row("SELECT role FROM plan_participant WHERE participant_id=?", (participant_id,))
    if guest and guest["role"] in ("guest_pending", "guest_revoked", "retired"):
        raise ValueError("本事项参与尚未认可或已撤回")
    row = db.row(
        "SELECT participant_id, owner_id, label FROM plan_participant"
        " WHERE participant_id=? AND plan_id=?", (participant_id, plan_id))
    if row is None:
        raise ValueError("参与方不存在，或不属于这个事项")
    latest = db.row(
        "SELECT MAX(version) AS v FROM plan_proposal WHERE plan_id=?", (plan_id,))
    current_version = latest["v"] if latest and latest["v"] else 0
    if current_version == 0:
        raise ValueError("这个事项还没有方案版本，不能表态")
    if version != current_version:
        raise ValueError("只能对当前版本（第 %d 版）表态，旧版本接受不能算数" % current_version)
    if not actor_id:
        raise ValueError("缺少表态身份")
    if actor_id != row["owner_id"]:
        raise ValueError("只能由%s本人表态，不能替别家决定" % row["label"])
    # 有家庭改过条件、方案还没重排时，不接受任何表态：
    # 各家原来接受的是旧条件排出来的方案，条件已经变了，
    # 此时点「接受」只是对一份作废内容的确认。
    plan = db.row("SELECT needs_replan FROM plan WHERE plan_id=?", (plan_id,))
    if plan is not None and plan["needs_replan"]:
        # 原写「方案要重排后才能表态」——把重排说成唯一出路。参与者改的不一定是
        # 影响事情本身的条件。这里放行，但在决定里记下「这一版不含刚更新的条件」，
        # 让用户在知情下选；要不要重排由界面给路径，不由这句错误导。
        raise ValueError("有条件在排完这一版之后更新过；可以按新条件重排后再表态，"
                         "也可以仍对当前版本表态，但那一版里没有刚更新的条件")
    if guest and guest["role"] == "guest" and decided_by != "person" and db.row("SELECT 1 FROM plan_decision WHERE plan_id=? AND participant_id=? AND version=? AND decided_by='person' AND decision<>'stale'", (plan_id, participant_id, version)):
        raise ValueError("本事项本人已明确表态，代表建议不能覆盖本人决定")
    # 同一参与方对同一版本只保留最后一次本人／代表表态。
    # 代决策行另存：一个随行者一条，不在这条删除里，否则代决策会被自己人清掉。
    db.execute("DELETE FROM plan_decision WHERE plan_id=? AND participant_id=? AND version=?"
               " AND decided_by IN ('person','representative')",
               (plan_id, participant_id, version))
    did = dbm.new_id("pdec")
    # 任何一方表态都可能改变是否已约定：接受满各方就成，拒绝就退回。
    # 每次都刷新阶段，不让界面停留在上一个状态。
    db.execute(
        "INSERT INTO plan_decision(plan_decision_id, plan_id, participant_id, version,"
        " decision, reason, changes_json, decided_by, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (did, plan_id, participant_id, version, decision, reason,
         json.dumps(changes or {}, ensure_ascii=False),
         decided_by if decided_by in ("person", "representative", "delegate") else "person",
         dbm.now_iso()))
    db.audit(plan_id, actor_id, "plan_decision",
             {"participant_id": participant_id, "version": version, "decision": decision})
    if decision == "accept":
        _record_delegated_decisions(db, plan_id, participant_id, version, decision)
    refresh_plan_stage(db, plan_id)
    return did


def _record_delegated_decisions(db, plan_id, participant_id, version, decision):
    """为「没有配置自己助手」的随行者补代决策记录。

    代决策是单独一类：它说明这一票是声明人代随行者做的，不是随行者本人同意。
    已约定判据只数本人接受，代决策行永远不计入。
    """
    row = db.row("SELECT companions_json FROM plan_participant WHERE participant_id=?",
                 (participant_id,))
    if not row:
        return
    try:
        comps = (json.loads(row["companions_json"] or "{}") or {}).get("companions") or []
    except Exception:
        return
    for c in comps:
        if c.get("has_agent") or c.get("decision_mode") != "delegated":
            continue
        db.execute("DELETE FROM plan_decision WHERE plan_id=? AND participant_id=? AND version=?"
                   " AND decided_by='delegate' AND json_extract(changes_json,'$.companion')=?",
                   (plan_id, participant_id, version, c["name"]))
        db.execute(
            "INSERT INTO plan_decision(plan_decision_id, plan_id, participant_id, version,"
            " decision, reason, changes_json, decided_by, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (dbm.new_id("pdec"), plan_id, participant_id, version, decision,
             "代%s决策（其未配置自己的助手）" % c["name"],
             json.dumps({"companion": c["name"]}, ensure_ascii=False),
             "delegate", dbm.now_iso()))


def plan_status(db, plan_id):
    """事项现状：各方状态、当前版本、是否已约定。

    已约定的判据是硬的：所有必要参与方都对**当前版本**明确接受。
    只要有一个必要方还没回复、或接受的是旧版本，就仍是待确认，
    不能显示成已经约好。
    """
    plan = db.row("SELECT * FROM plan WHERE plan_id=?", (plan_id,))
    if plan is None:
        return None
    participants = _plan_rows(db, plan_id, "plan_participant")
    # 待重排是比阶段更硬的事实：有家庭刚改过条件、新方案还没排出来，
    # 这时哪怕库里还留着上一版的 accept，也不能算约定。
    needs_replan = bool(plan.get("needs_replan"))
    latest = db.row("SELECT MAX(version) AS v FROM plan_proposal WHERE plan_id=?", (plan_id,))
    version = latest["v"] if latest and latest["v"] else 0
    proposals = db.rows(
        "SELECT * FROM plan_proposal WHERE plan_id=? ORDER BY version DESC", (plan_id,))
    current = proposals[0] if proposals else None
    sides = []
    agreed = 0
    required_total = 0
    required_agreed = 0
    for p in participants:
        # 本方状态只认本人／代表表态：代决策行是代随行者投的，
        # 混进来会把「有人代投了」显示成「这一方本人同意了」。
        dec = db.row(
            "SELECT * FROM plan_decision WHERE plan_id=? AND participant_id=?"
            " AND version=? AND decided_by IN ('person','representative')"
            " ORDER BY created_at DESC LIMIT 1",
            (plan_id, p["participant_id"], version))
        state = dec["decision"] if dec else "pending"
        if p.get("role") == "guest":
            import time
            live_guest = db.row("SELECT 1 FROM plan_guest_session s JOIN plan_invitation i ON i.invitation_id=s.invitation_id WHERE s.guest_id=? AND s.plan_id=? AND s.revoked=0 AND i.state='claimed' AND i.approved=1 AND s.expires_at>?", (p["owner_id"], plan_id, time.time()))
            if not live_guest:
                state = "expired"
        if p.get("role") == "guest" and state != "expired" and dec and dec.get("decided_by") != "person":
            state = "reviewed"  # A representative recommendation is not the guest's consent.
        if p["required"]:
            required_total += 1
            if state == "accept":
                required_agreed += 1
        sides.append({
            "participant_id": p["participant_id"], "owner_id": p["owner_id"],
            "label": p["label"], "role": p.get("role", "family"),
            "conditions": json.loads(p["conditions_json"] or "{}"),
            # 朋友旅行：这一方声明的人数与出席，以及自己决定要不要说出来的随行。
            # 家庭场景这两个字段都是空对象，行为不变。
            "headcount": json.loads(p.get("headcount_json") or "{}"),
            "companions": (json.loads(p.get("companions_json") or "{}") or {}).get("companions") or [],
            "delegated": [c["name"] for c in (json.loads(p.get("companions_json") or "{}") or {}).get("companions") or []
                          if c.get("decision_mode") == "delegated"],
            "required": bool(p["required"]),
            "state": state,
            "reason": (dec or {}).get("reason", ""),
        })
        if state == "accept":
            agreed += 1
    all_required = required_total > 0 and required_agreed == required_total
    meta = _plan_meta(db, plan_id)
    gaps = _activity_gaps(db, plan_id, meta)
    return {
        "plan_id": plan_id, "title": plan["title"], "utterance": plan["utterance"],
        "owner_id": plan["owner_id"],
        "stage": plan["stage"], "outcome": plan["outcome"],
        "needs_replan": needs_replan,
        "current_version": version,
        "proposal": json.loads(current["content_json"]) if current else None,
        "proposal_reason": current["reason"] if current else "",
        "proposer": current["proposer"] if current else "",
        "participants": sides,
        "agreed_count": agreed,
        "required_total": required_total,
        "required_agreed": required_agreed,
        # 只有全部必要参与方接受当前版本，且没有待重排的条件，才算约定。
        "agreed": all_required and version > 0 and not needs_replan and not gaps,
        "confirmation_gaps": gaps,
        "candidate_only": bool(gaps),
        "waiting_on": [s["label"] for s in sides
                       if s["required"] and s["state"] != "accept"] + gaps,
    }


def _plan_rows(db, plan_id, table):
    extra = " AND role<>'retired'" if table == "plan_participant" else ""
    return db.rows("SELECT * FROM %s WHERE plan_id=?" % table + extra + " ORDER BY created_at", (plan_id,))


def refresh_plan_stage(db, plan_id):
    """按当前表态刷新事项阶段。谈成记 agreed，否则仍待确认。"""
    st = plan_status(db, plan_id)
    if st is None:
        return None
    if st["agreed"]:
        db.execute("UPDATE plan SET stage='agreed', outcome=? WHERE plan_id=?",
                   ("各方已接受第 %d 版方案" % st["current_version"], plan_id))
    else:
        waiting = "、".join(st["waiting_on"]) or "有参与方未确认"
        db.execute("UPDATE plan SET stage='awaiting', outcome=? WHERE plan_id=?",
                   ("还在等：%s" % waiting, plan_id))
    return plan_status(db, plan_id)


def update_plan_condition(db, plan_id, actor_id, conditions, reason=""):
    """参与方本人更新自己这一方的条件。

    这是用户改条件的唯一入口。条件更新和「我对这版方案表态」是两件事：
    改了条件不等于接受当前方案，这一方要重新表态，别家也要重新核对。

    这里只把新条件写进库，并把事项标成「需要重排」：
    机械地把「时间」一个字段覆盖到旧方案上，会留下「周六上午」配着
    「14:00 开始」这种自相矛盾的内容——那是在改字，不是在改方案。
    新版本由 plan_replan 依据各家当前授权条件重新拟定，一致了再落版本。

    actor_id 是演示身份。只能改自己那一方的条件，不能替别家改。
    """
    rows = db.rows(
        "SELECT participant_id, owner_id, label, conditions_json FROM plan_participant"
        " WHERE plan_id=? AND owner_id=?", (plan_id, actor_id))
    if not rows:
        raise ValueError("你在这个事项里没有代表的一方，不能改别人的条件")
    if len(rows) > 1:
        raise ValueError("这个身份在这个事项里代表多方，请指明是哪一方")
    side = rows[0]
    # 条件必须是结构化字段：模型和界面都按同一组键读写，不接受任意键。
    # 按事项场景取字段集：家庭活动用原 8 键，朋友旅行用旅行字段集。
    _plan_kind = (db.row("SELECT kind FROM plan WHERE plan_id=?", (plan_id,)) or {}).get("kind", "group_activity")
    cleaned, unknown, confirm = normalize_conditions(conditions, scenario=_plan_kind)
    if unknown:
        raise ValueError("条件只能包含这些字段：%s" % "、".join(condition_keys(_plan_kind)))
    if confirm:
        # 口径不明就停下来问，不默认成任何一个：一家的上限和整场的总预算
        # 不是一个数，猜错就是替他把条件定了。
        raise ValueError(
            "「%s」这句预算说不清是「本人出资上限」还是「活动总预算」，"
            "请说清是「我家最多出多少」还是「活动一共多少」"
            % "、".join(confirm))
    before = json.loads(side["conditions_json"] or "{}")
    # 只认这一方本次真正改动的字段，别把没提过的旧条件也算成变更。
    changed = {k: v for k, v in cleaned.items() if before.get(k) != v}
    if not changed:
        raise ValueError("这些条件和你原来的一样，没有需要更新的")
    merged = dict(before)
    merged.update(cleaned)
    db.execute("UPDATE plan_participant SET conditions_json=? WHERE participant_id=?",
               (json.dumps(merged, ensure_ascii=False), side["participant_id"]))
    note = reason or ("%s 更新了自己的条件：%s" % (
        side["label"], "；".join("%s 改为 %s" % (k, v) for k, v in changed.items())))
    # 不是所有改动都要求重排。参与者改的常常是不影响事情本身的小变量
    # （备注、或者只是自己这家的出资上限），一律打回重排既徒增一轮协调，
    # 也等于替用户判断「这点改动不要紧」。这里按改动落在哪类键上分开处理：
    #   plan_driving：这一版方案的内容变了 → 旧接受失效，要重排；
    #   self_only  ：只影响这一家自己能不能接受 → 方案内容没变，不必重排，
    #                 但这一家要重新对当前版本表态（他之前的 accept 作废）；
    #   context    ：补充说明 → 什么都不影响，原计划继续。
    sc = PLAN_SCENARIOS.get(_plan_kind, PLAN_SCENARIOS["group_activity"])
    driving = sorted(k for k in changed if k in sc["plan_driving_keys"])
    selfonly = sorted(k for k in changed if k in sc["self_only_keys"])
    ctxonly = sorted(k for k in changed if k in sc["context_keys"])
    other = sorted(k for k in changed
                   if k not in sc["plan_driving_keys"] and k not in sc["self_only_keys"]
                   and k not in sc["context_keys"])
    material = bool(driving or other)
    if material:
        # 旧接受必须当场失效：哪怕上一版刚刚被全票接受，条件一变，
        # 那份约定依据的就是旧条件，不能再算约定。标 stale 而不是删掉，
        # 历史还在，能查清当时各自接受了什么。
        db.execute("UPDATE plan_decision SET decision='stale', reason=? WHERE plan_id=?",
                   ("条件已更新，%s，旧接受不再有效" % note, plan_id))
        db.execute("UPDATE plan SET stage='awaiting', outcome=? WHERE plan_id=?",
                   ("%s，这一版是按更新前的条件排的" % note, plan_id))
        db.execute("UPDATE plan SET needs_replan=1 WHERE plan_id=?", (plan_id,))
    elif selfonly:
        # 只改了自己这家的上限：方案内容没变，别家不必重新表态；
        # 但这一家之前的 accept 作废——他是在旧的上限下接受的。
        db.execute("UPDATE plan_decision SET decision='stale', reason=? WHERE plan_id=?",
                   ("%s 改了自己这家的%s，他之前的接受不再有效" % (side["label"], "、".join(selfonly)),
                    plan_id), )
        db.execute("UPDATE plan_participant SET conditions_json=? WHERE participant_id=?",
                   (json.dumps(merged, ensure_ascii=False), side["participant_id"]))
        db.execute("UPDATE plan SET outcome=? WHERE plan_id=?",
                   ("%s；方案内容没变，这一家按新上限重新表态即可" % note, plan_id))
    else:
        # 只改了备注一类：原计划继续，不做任何状态回退。
        db.execute("UPDATE plan SET outcome=? WHERE plan_id=?",
                   ("%s；不影响方案，按原计划继续" % note, plan_id))
    db.audit(plan_id, actor_id, "plan_condition_updated",
             {"participant_id": side["participant_id"], "label": side["label"],
              "conditions": merged, "changed_fields": sorted(changed),
              "reason": note, "needs_replan": material,
              "materiality": {"driving": driving, "self_only": selfonly,
                              "context": ctxonly, "unclassified": other}})
    st = plan_status(db, plan_id)
    return {
        "plan_id": plan_id, "version": st["current_version"],
        "label": side["label"], "participant_id": side["participant_id"],
        "conditions": merged, "changed_fields": sorted(changed),
        "reason": note, "stage": st["stage"], "outcome": st["outcome"],
        "agreed": st["agreed"], "waiting_on": st["waiting_on"],
        "stale_decisions": db.row(
            "SELECT COUNT(*) c FROM plan_decision WHERE plan_id=? AND decision='stale'",
            (plan_id,))["c"],
        # 条件已更新，方案还没重排：调用方要接着跑 plan_replan，
        # 各家再按重排后的新版本表态，不能拿旧版本继续算约定。
        "needs_replan": True,
        "status": st,
    }


# 明确的纠正措辞。这些词出现时基本可以确定用户在改记录，不是交办。
CORRECTION_MARKERS = ("记错", "搞错", "说错", "写错", "更正", "纠正", "改为", "改成",
                      "不对，", "错了")

# 用户追问「要补什么」这类纯元提问。只在整句就是在问缺什么时命中，
# 不能靠子串抢答：「小李送我玩具，我怎么回礼？」是在交办新事情，
# 「给小周准备礼物需要什么？」也带着新目标，都要交给模型判断。
META_ASK_MARKERS = ("补充什么", "补充一句", "补一句", "补什么", "要补什么", "缺什么",
                    "要填什么", "填什么", "我该做什么", "该做什么", "怎么说", "说什么",
                    "怎么填", "怎么补", "什么意思", "然后呢", "接下来呢", "接下来干嘛")

# 这句话里出现这些内容时，说明它带着新的目标或条件，不是纯追问。
NEW_GOAL_MARKERS = ("准备", "买", "送", "回礼", "还礼", "回送", "预算", "以内",
                    "生日", "节日", "礼物", "玩具", "鞋", "书", "拼图", "蜡笔")


def is_meta_ask(text):
    """这句话是否只是在问「我要补什么/说什么」，没有别的目标。

    纯追问才走快速通道；带新目标的句子一律交给模型判断，
    否则会把新交办错误导回此前不相关的阻塞任务。
    """
    t = (text or "").strip().strip("？?。！!，,~～ ")
    if not t or len(t) > 20:
        return False
    if any(m in t for m in NEW_GOAL_MARKERS):
        return False
    return any(m in t for m in META_ASK_MARKERS)


# 问家人近况的措辞。命中这些词、又提到了某个记录里的人时，
# 走授权近况查询，不让用户重填人物关系和背景。
FAMILY_UPDATE_MARKERS = ("近况", "最近怎么样", "最近如何", "怎么样了孩子", "学习怎么样",
                         "睡得好", "睡眠", "进度", "最近在做", "近来")


def _wants_family_update(text):
    """这句话是否在问家人的近况，而不是问事情办到哪一步。"""
    t = (text or "").strip()
    if not t:
        return False
    return any(m in t for m in FAMILY_UPDATE_MARKERS)


def family_update_summary(db, owner_id, utterance):
    """按授权范围汇总某位家人的近况，回答「孩子最近怎么样」。

    和 status_summary 分开：那个回答的是「我的事办到哪一步」，
    这个回答的是「家人这边有什么事」。只汇总这个查看者被授权的字段，
    未授权的、已撤回的都不出现。没有可说的近况时如实说没有，
    不把「没上报」讲成「没发生」。
    """
    text = (utterance or "").strip()
    # 找出这句话说的是谁：按记录里的人名匹配，匹配不到就看唯一的孩子。
    # relation 要一起取出来：下面「没指名时找唯一的孩子」按关系认人，
    # 只取 name 会 KeyError——那是把「问近况」整条路打死的错。
    persons = [r for r in db.rows(
        "SELECT person_id, name, relation FROM person WHERE owner_id=? AND name!='我'",
        (owner_id,))]
    named = [p for p in persons if p["name"] in text]
    target = named[0] if len(named) == 1 else None
    if target is None and not named:
        # 没指名：只有一个孩子时才用那一个，否则要说清在问谁。
        # 孩子按关系认，不按名字认——写死「小雨」「小宇」换一套演示数据
        # 就认不出孩子，问近况会退化成「你想问谁的近况」。
        children = [p for p in persons if p["relation"] in ("friend_child", "child",
                                                            "own_child", "son", "daughter")]
        if len(children) == 1:
            target = children[0]
    if target is None:
        names = "、".join(p["name"] for p in persons[:5])
        return {"reply": ("你想问谁的近况？你记录里的有：" + names
                          + "。说个名字，我只看授权给我看的部分。"),
                "person_id": "", "events": [], "resolved": False}
    events = _life_events_both_sides(db, owner_id, target["person_id"])
    if not events:
        return {"reply": ("关于%s，我这边没有家人授权分享的近况。"
                          "没有记录不代表没有发生，只是还没有人分享给我。"
                          % target["name"]),
                "person_id": target["person_id"], "events": [], "resolved": True}
    lines = []
    for e in events[:5]:
        fields = "；".join("%s：%s" % (k, v) for k, v in e["fields"].items())
        src = e["source_label"]
        if e["is_device"]:
            src += "（设备数据，不是本人自述）"
        lines.append("%s %s，%s" % (e["happened_on"], src, fields))
    reply = ("关于%s，家人授权分享的近况有这些：\n%s\n"
             "只显示允许我看的部分，没授权的我不会看到。"
             % (target["name"], "\n".join(lines)))
    return {"reply": reply, "person_id": target["person_id"],
            "events": events, "resolved": True}


CHILD_RECORD_MARKERS = ("记一下", "记录一下", "记一笔", "记下", "记录")
CHILD_NOUNS = ("孩子", "小孩", "儿子", "女儿", "宝宝")
RECENT_MARKERS = ("这周", "本周", "今天", "昨天", "这几天", "最近", "情况", "进度",
                  "表现", "怎么样", "如何")
GIFT_MARKERS = ("礼物", "送礼", "买", "购", "预算", "生日", "节日", "红包", "元以内",
                "块钱", "下单", "订单")


def _is_child_record(text):
    """这句话是在记孩子的情况，不是在交办送礼或约家庭活动。

    四条同时成立才算，缺一条就交给后面的既有判据：
      1. 带记录动词（记一下/记录一下/记一笔/记下/记录）；
      2. 提到孩子（或句子里有具体人名——人名由调用方已知列表补，这里只做粗判）；
      3. 带近况/进度措辞（这周/今天/最近/情况/进度/表现…）；
      4. **不帶**任何送礼或采购措辞。
    第 4 条是关键：「给三家一起出游的孩子们准备礼物」带「礼物」，
    「周末三家一起带孩子出去玩」带「一起出去玩」且没有记录动词，
    都不会被这条抢走。
    """
    t = (text or "").strip()
    if not t:
        return False
    if not any(m in t for m in CHILD_RECORD_MARKERS):
        return False
    if not any(k in t for k in CHILD_NOUNS):
        return False
    if not any(m in t for m in RECENT_MARKERS):
        return False
    if any(g in t for g in GIFT_MARKERS):
        return False
    return True


def classify_intent(db, client, owner_id, utterance):
    """判断这句话是交办一件事、补充/纠正记录、问进展，还是在追问缺什么。

    返回 task / record / status / clarify。先看明确的纠正措辞和元提问做快速判断，
    再用模型判断，避免把「记错了，喜欢拼图的是哥哥」当成送礼需求，
    也避免把「补充什么？」当成新的交办、又开出一件重复的事。
    """
    text = (utterance or "").strip()
    if not text:
        return "task"
    for marker in CORRECTION_MARKERS:
        if marker in text:
            return "record"
    # 元提问只在确实有卡住的事时才算；没有待补充事项时按普通交办处理。
    if is_meta_ask(text) and _pending_clarify(db, owner_id) is not None:
        return "clarify"
    # 有已登记订单、且这句话只是在说那件事后来的情况，才算事后反馈。
    # 带新目标（要买什么、多少预算、送给谁）的一律不算，交给模型按交办判断。
    if _recent_purchased(db, owner_id) is not None and _is_pure_followup(text):
        return "followup"
    # 记录句快路：`记一下/记录/记一笔 + 孩子 + 这周/今天/情况/进度/表现`
    # 这类是在给资料，不是在交办一件送礼的事。必须走在模型判断之前：
    # 交给模型，「孩子」会被当成送礼目标词，整句被误判成 task，
    # 接着被要求「点一个送礼对象」——用户明确不要这个。
    # 判据收紧到三条同时成立，避免把「给三家一起出游的孩子们准备礼物」
    # 这类真送礼句、以及「周末三家一起带孩子出去玩」这类多家庭句抢走：
    #   ① 带记录动词；② 提到孩子（或具体人名）；③ 带近况/进度措辞；
    #   ④ 不带任何送礼或采购措辞。
    if _is_child_record(text):
        return "record"
    # 问家人近况。要在 followup 之后、模型判断之前：这句话同样可能提到孩子，
    # 交给模型容易被当成交办或问进展。只有确实带近况措辞、又没带新目标才算。
    if _wants_family_update(text) and not any(m in text for m in NEW_GOAL_MARKERS):
        return "family_update"
    # 多家庭活动：一句话直接进家庭活动草稿，不走送礼链。
    # 放在模型判断之前，用关键词而不是模型：这一条判错，用户原话会被当成
    # 送礼交办，接着被要求「点一个孩子」——那正是用户明确不要的。
    # 放在 family_update 之后：问近况的句子也可能提到孩子，
    # 但不会同时说「几家 + 一起做什么」。
    if group_activity_intent(text):
        return "group_activity"
    # 约朋友一起旅行：复用同一套多方协调引擎，条件字段用朋友旅行那套
    # （目的地、假期长度、人均预算……），不是家庭活动那套。原先这个函数
    # 定义了却没接进分派，主对话里说「约朋友去大理」只会落进家庭活动或送礼，
    # 朋友旅行只能从受邀页或测试种子进——文档和视频却说它支持。
    # 放在 group_activity 之后：多家庭句不会被它抢走。
    if friend_trip_intent(text):
        return "friend_trip"
    persons = db.rows("SELECT name, relation FROM person WHERE owner_id=? ORDER BY name",
                      (owner_id,))
    person_lines = "\n".join("- %s（%s）" % (p["name"], p["relation"]) for p in persons) or "（暂无）"
    system = (
        "判断用户这句话属于哪一类，只输出一个 JSON 对象，不要解释。\n"
        "字段 intent: 字符串，只能是 task / record / status / clarify / followup /"
        " family_update / group_activity / friend_trip 八者之一。\n"
        "  task: 用户要你去做一件事，例如准备礼物、买东西、安排事情。\n"
        "  record: 用户在补充或更正你记下的资料，例如「记错了，喜欢拼图的是哥哥」、\n"
        "          「小雨现在二年级了」「她喜欢紫色」。\n"
        "  status: 用户在问事情办到哪一步了，例如「现在办到哪一步了」「怎么样了」。\n"
        "  family_update: 用户在问某位家人的近况，例如「小雨最近怎么样」\n"
        "           「孩子学习进度如何」「她睡得好吗」。只问情况，没要求办事。\n"
        "  followup: 用户在说已经登记的那件事后来怎么样了，例如「送到了，孩子很喜欢」、\n"
        "            「孩子不太感兴趣」。只是补充结果，没有提出新的要求。\n"
        "  clarify: 用户在追问卡住的那件事缺什么，例如「补充什么？」「要补什么」\n"
        "           「我该说什么」「接下来干嘛」。这类话没有给出新信息，也没要求办新的事。\n"
        "  group_activity: 用户在约一场多个家庭一起做的事，例如「周末三家一起\n"
        "          带孩子出去玩」「两家人聚个餐」。这不是给某一个人准备礼物，\n"
        "          不要选成一个送礼对象；各家自己给条件。\n"        "  friend_trip: 用户在约朋友一起旅行或出游，例如「五一约朋友去云南」\n"
        "          「想和闺蜜去大理几天」。这不是多家庭活动，也不是送礼；\n"
        "          各人自己给条件，随行说不说由本人定。\n"
        "判断要点：用户是在要求你去办事、给你信息、问进展、问缺什么，\n"
        "还是在说一件已经办完的事后来怎么样了？\n"
        "给办事提条件（例如「300元以内」「别和上次重复」）仍然算 task。\n"
        "已知人物：\n" + person_lines
    )
    try:
        data = client.chat_json([
            {"role": "system", "content": system},
            {"role": "user", "content": utterance},
        ])
    except Exception:
        # 分类失败时按交办处理，让后面的抽取和硬条件检查给出人能看懂的原因。
        return "task"
    intent = data.get("intent") if isinstance(data.get("intent"), str) else ""
    # 白名单必须和提示词里列的类型一一对上。少了 family_update，
    # 模型明明判对了「问近况」，到这里也会被退回成 task，
    # 于是一句「小雨数学练得怎么样」被当成新的送礼交办去协商。
    # 白名单必须和提示词里列的类型一一对上。少了 group_activity，
    # 模型明明判对了「多家庭活动」，到这里也会被退回成 task，
    # 于是一句「周末三家一起带孩子出去玩」被当成送礼交办去协商。
    return intent if intent in ("task", "record", "status", "clarify", "followup",
                               "family_update", "group_activity",
                               "friend_trip") else "task"


def _pending_clarify(db, owner_id):
    """找出当前卡住、需要用户补信息的那件事。

    只认 stage=blocked：那才是真的缺信息停在半路。没有就返回 None，
    这样「要补什么」在没有待办时会按普通交办处理，不会误判。
    """
    rows = db.rows(
        "SELECT task_id, utterance, outcome, extracted_json FROM task"
        " WHERE owner_id=? AND stage='blocked' ORDER BY created_at DESC LIMIT 1", (owner_id,))
    if not rows:
        return None
    t = rows[0]
    try:
        extracted = json.loads(t["extracted_json"] or "{}")
    except (ValueError, TypeError):
        extracted = {}
    return {"task_id": t["task_id"], "utterance": t["utterance"],
            "outcome": t["outcome"] or "", "extracted": extracted}


def _pending_size_question_for_parent(db, owner_id):
    """家长一侧：找到正卡在尺码上、等家长本人确认的那件事。

    这件事记在送礼者名下（owner_id='A'），但缺的是只有家长能给的信息。
    家长从自己的身份进来时，必须能接到同一件事上补一句，否则家长说的话
    会被当成新的交办，那件事就一直卡着。

    只认自己的孩子：按名字对应到家长一侧的同一个人，不能替别人家的孩子答。
    """
    if owner_id != "B":
        return None
    for t in db.rows(
            "SELECT task_id, owner_id, utterance, outcome, extracted_json FROM task"
            " WHERE stage='blocked' ORDER BY created_at DESC LIMIT 20"):
        if t["owner_id"] == "B":
            continue
        if not any(k in (t["outcome"] or "") for k in ("鞋码", "尺码")):
            continue
        try:
            extracted = json.loads(t["extracted_json"] or "{}")
        except (ValueError, TypeError):
            extracted = {}
        subject_id = extracted.get("subject_id") or ""
        if not subject_id:
            continue
        a_person = db.row("SELECT name FROM person WHERE person_id=?", (subject_id,))
        if a_person is None:
            continue
        b_person = db.row("SELECT person_id FROM person WHERE owner_id='B' AND name=?",
                          (a_person["name"],))
        if b_person is None:
            continue
        return {"task_id": t["task_id"], "utterance": t["utterance"],
                "outcome": t["outcome"] or "", "extracted": extracted,
                "b_person_id": b_person["person_id"],
                "owner_id": t["owner_id"]}
    return None


def _looks_like_name_only(text):
    """这句话是不是只给了一个人名（或人名加一点称呼）。

    用来把「小李」这类补充接回卡住的那件事，而不是当成新的交办或普通记录。
    追问「补充什么？」「要补什么」这类问句不是名字，必须先挡掉，
    否则用户问一句缺什么，会被当成新的人名记下来。
    """
    t = (text or "").strip().strip("，。！？!?,~～ ")
    if not t or len(t) > 12:
        return False
    if is_meta_ask(t):
        return False
    # 出现这些字说明是在说事或在问事，不是只报名字。
    if any(w in t for w in ("买", "送", "准备", "要", "想", "帮", "记错", "改",
                            "是", "的", "了", "预算", "生日", "礼物", "回礼",
                            "补", "缺", "什么", "怎么", "哪", "谁", "吗", "呢")):
        return False
    return True


def answer_blocked_with_size(db, client, owner_id, utterance):
    """补上当前鞋码：只有家长本人说的才算数，接着办同一件事。

    这条记录必须带明确来源和当天日期，程序才认它是当前鞋码；
    只写「31 码」两个字、没有说明是谁确认的，不能当成可执行依据。

    身份来源是硬要求：尺码会影响下单，只有家长本人从自己的身份说出来，
    才写成家长一侧的确认记录。送礼者转述的同样内容只能标成送礼者的说法，
    事项继续卡着等家长本人确认——否则送礼者一句话就被伪装成家长确认。
    """
    pending = _pending_clarify(db, owner_id)
    if pending is None:
        # 送礼者名下那件事可能正卡在尺码上等家长。家长从自己的身份进来时，
        # 接的是同一件事，不是另开一件。
        pending = _pending_size_question_for_parent(db, owner_id)
    if pending is None:
        return None
    text = (utterance or "").strip()
    if not text:
        return None
    extracted = pending["extracted"]
    subject_id = extracted.get("subject_id") or ""
    if not subject_id:
        return None
    a_person = db.row("SELECT name FROM person WHERE person_id=?", (subject_id,))
    if a_person is None:
        return None
    b_person = db.row("SELECT person_id FROM person WHERE owner_id='B' AND name=?",
                      (a_person["name"],))
    if b_person is None:
        return {"reply": "找不到家长一侧的记录，没法记尺码。", "task_id": pending["task_id"],
                "candidates": [], "resolved": False}
    # 家长这一侧补的当前鞋码，属性和值都写成结构化记录。
    size_value = ""
    for m in _SIZE_IN_TEXT.finditer(text):
        size_value = m.group(0)
        break
    if not size_value:
        return {"reply": "我没听清是多少码。你跟我说一个数就行，比如「32 码」。",
                "task_id": pending["task_id"], "candidates": [], "resolved": False}
    task_id = pending["task_id"]
    # 送礼者转述的尺码：记在自己的名下、标明未经家长确认，不能当成执行依据。
    # 事项保持 blocked，等家长本人用自己的身份补一句。
    if owner_id != "B":
        relayed_fid = dbm.new_id("fact")
        db.execute(
            "INSERT INTO fact(fact_id, owner_id, subject_id, attribute, value,"
            " source_utterance, provider, observed_on, status) VALUES(?,?,?,?,?,?,?,?,?)",
            (relayed_fid, "A", subject_id, "shoe_size", size_value,
             text, "我转述的，家长本人还没确认", dbm.now_iso()[:10], "active"))
        db.audit(task_id, "A", "size_relayed_by_giver",
                 {"value": size_value, "fact_id": relayed_fid, "utterance": text})
        return {"reply": ("这句我先按你的说法记下了：孩子现在穿%s。"
                          "但尺码会写进订单，得家长本人确认才行。"
                          "请家长用自己的身份说一句现在的鞋码，这件事才能往下走。"
                          % size_value),
                "task_id": task_id, "candidates": [], "resolved": False,
                "stage": "blocked", "need_parent": True}
    new_fid = dbm.new_id("fact")
    db.execute(
        "INSERT INTO fact(fact_id, owner_id, subject_id, attribute, value,"
        " source_utterance, provider, observed_on, status) VALUES(?,?,?,?,?,?,?,?,?)",
        (new_fid, "B", b_person["person_id"], "shoe_size", size_value,
         text, "家长本人确认", dbm.now_iso()[:10], "active"))
    # 同一属性下的旧记录标记为已被替代，避免旧值继续被当成当前值。
    for r in db.rows(
            "SELECT fact_id FROM fact WHERE owner_id='B' AND subject_id=?"
            " AND attribute='shoe_size' AND status='active' AND fact_id!=?",
            (b_person["person_id"], new_fid)):
        db.execute("UPDATE fact SET status='superseded', superseded_by=? WHERE fact_id=?",
                   (new_fid, r["fact_id"]))
    db.audit(task_id, "B", "size_confirmed_by_parent",
             {"value": size_value, "fact_id": new_fid, "utterance": text})
    # 家长刚确认的当前鞋码要直接带进方案，否则重新协商时模型还可能
    # 拿着上一轮那个旧尺码来问，用户会看到同一句被反复卡住。
    latest = db.row(
        "SELECT proposal_id, asked_size FROM proposal WHERE task_id=?"
        " ORDER BY version DESC LIMIT 1", (task_id,))
    if latest is not None and (latest["asked_size"] or "").strip() != size_value:
        db.execute("UPDATE proposal SET asked_size=? WHERE proposal_id=?",
                   (size_value, latest["proposal_id"]))
        db.audit(task_id, "B", "size_updated_by_parent",
                 {"from": latest["asked_size"], "to": size_value})
    result = advance_task(db, client, task_id)
    reply = "好，我记下孩子现在穿%s。" % size_value
    if result.get("stage") == "blocked":
        reply += "还缺别的：" + (result.get("outcome") or "")
    elif result.get("stage") == "agreed":
        reply += "对方接受了这个方案，等你确认。"
    elif result.get("stage") == "counter_pending":
        reply += "对方提了个新方案：" + (result.get("reason") or "") + "你看看行不行。"
    elif result.get("stage") == "failed":
        reply += "这次没谈成：" + (result.get("outcome") or "")
    return {"reply": reply, "task_id": task_id, "candidates": [],
            "resolved": result.get("stage") not in ("blocked", "failed"),
            "stage": result.get("stage")}


def answer_blocked_with_name(db, client, owner_id, utterance):
    """用户只报了一个名字：把它接回卡住的那件事，能认出来就继续办。

    名字能对上记录里的人，直接补成对象并接着协商；
    对不上就如实说没这个人，请用户确认是不是哪一位，不擅自猜一个。
    """
    pending = _pending_clarify(db, owner_id)
    if pending is None:
        return None
    text = (utterance or "").strip().strip("，。！？!?,~～ ")
    if not text:
        return None
    person = _person_by_name(db, owner_id, text)
    if person is None:
        # 记录里没有这个人：说清楚，并把可能的人列出来让用户点。
        candidates = _blocked_candidates(db, pending["task_id"], pending["extracted"])
        names = [c.get("name") for c in candidates if c.get("name")]
        reply = "我记录里还没有「%s」这个人，不敢替你猜。" % text
        if names:
            reply += "你说的是不是下面这位：" + "、".join(names[:6]) + "？点一个就行。"
        else:
            reply += "你认识的人我这边还没有记录，先告诉我他是谁，我记下再办。"
        return {"reply": reply, "task_id": pending["task_id"], "candidates": candidates,
                "resolved": False}
    task_id = pending["task_id"]
    resolve_subject(db, client, task_id, person["person_id"])
    result = advance_task(db, client, task_id)
    reply = "好，这次是给%s。" % person["name"]
    if result.get("stage") == "blocked":
        reply += "不过还缺别的：" + (result.get("outcome") or "")
    elif result.get("stage") == "agreed":
        reply += "对方接受了这个方案，等你确认。"
    elif result.get("stage") == "counter_pending":
        reply += "对方提了个新方案：" + (result.get("reason") or "") + "你看看行不行。"
    return {"reply": reply, "task_id": task_id, "candidates": [],
            "resolved": True, "stage": result.get("stage")}


def clarify_reply(db, owner_id):
    """回答「要补什么」：说清缺哪一条、可以怎么补，并给出可点的人名。

    这里不调用模型：缺口是程序自己判出来的，必须如实说，不能让模型补一句含糊话。
    """
    pending = _pending_clarify(db, owner_id)
    if pending is None:
        return {"reply": "你现在没有卡住的事，不用补什么。要办新的事直接跟我说。",
                "task_id": "", "candidates": []}
    extracted = pending["extracted"]
    hint = (extracted.get("subject_hint") or "").strip()
    task_id = pending["task_id"]
    candidates = _blocked_candidates(db, task_id, extracted)
    names = [c.get("name") for c in candidates if c.get("name")]
    outcome = pending["outcome"] or ""

    # 尺码缺口：要说清要家长确认什么、为什么过去的记录不能用。
    if "鞋码" in outcome or "尺码" in outcome:
        reply = ("这件卡在尺码上。%s\n"
                 "你直接跟我说孩子现在穿多少码就行，比如「她现在穿 32 码」，"
                 "我记下来接着办。" % outcome)
        return {"reply": reply, "task_id": task_id, "candidates": [],
                "need": "size"}

    lines = ["你说的是「%s」，这件还差一条：这次要送给谁。" % pending["utterance"][:24]]
    if hint:
        lines.append("你叫他「%s」，我记录里没这个人，不敢替你猜。" % hint)
    if names:
        lines.append("下面这几个是你记过的，点一个就行：" + "、".join(names[:6]))
        lines.append("要是都不对，就在输入框里直接打他的名字，我第一次听说的人我会记下来。")
    else:
        lines.append("你认识的人我这边还没有记录。直接在输入框里打对方的名字，"
                     "比如「送我玩具的是我同事小王」，我记下后接着办。")
    return {"reply": "".join(lines), "task_id": task_id, "candidates": candidates}


# 事项状态的中文说法。界面和接口都用这一份，不各写一套：
# 两边不一致时，用户会看到同一个状态两种说法。
STAGE_TEXT = {
    "created": "已经记下，还没开始核实",
    "negotiating": "正在和对方核实",
    "agreed": "方案已约定，等你确认",
    "counter_pending": "对方提了新方案，需要你定",
    "purchased": "模拟订单已登记",
    "blocked": "还缺一点信息，需要你补充",
    "needs_renegotiation": "记录刚更正过，需要重新协商一次",
    "conflict": "这次没谈成",
    "failed": "这次没办成",
    "model_failed": "刚才没连上模型，可以重试",
}


def status_summary(db, owner_id):
    """用普通语言总结当前事项，回答「办到哪一步了」。"""
    matters_list = matters(db, owner_id)
    if not matters_list:
        return "你现在还没有交代我的事。跟我说一句就行。"
    lines = []
    for m in matters_list[:5]:
        stage = m["stage"]
        who = m.get("subject_name") or m.get("subject_hint") or "还没确定的人"
        text = STAGE_TEXT.get(stage, stage)
        line = "%s：%s（给%s）" % (m["utterance"][:16], text, who)
        if m.get("outcome") and stage in ("conflict", "failed", "model_failed"):
            line += "，%s" % m["outcome"][:40]
        lines.append(line)
    return "现在有 %d 件事。%s" % (len(matters_list), "；".join(lines))


# 这些阶段表示事情还没办完，同一句原话再来一次时接着办，不新开任务。
# purchased / conflict / failed / model_failed 是已经收尾或明确失败的，
# 之后用户再说同样的话，是合理的新一次交办，不能被永久复用挡住。
# 这些阶段表示事情还没走完，同一句原话再来一次时接着办，不新开任务。
REUSABLE_STAGES = ("created", "negotiating", "agreed", "counter_pending",
                   "blocked", "needs_renegotiation")


def _merge_followup(prev, new):
    """把跟进句抽出的条件并进原有事项，不覆盖已有主体，也不erase既有值。

    合并规则：新值非空才覆盖；原句、原主体、原 occasion 一律保留——
    跟进句本来就抽不出这些，覆盖只会把事项改坏。candidates 只在双方都没有时保留。
    """
    out = dict(prev or {})
    for k in ("category", "budget_max", "avoid_repeat", "hard_constraints",
              "subject_hint", "subject_name", "subject_id",
              "subject_resolved_name", "occasion"):
        v = (new or {}).get(k)
        if v in (None, "", [], False) and k not in ("avoid_repeat",):
            continue
        if k == "avoid_repeat" and not v:
            continue
        if out.get(k) in (None, "", [], False):
            out[k] = v
    # 主体仍然不明时，候选还是要能给出来
    if out.get("ambiguous_subject") and not out.get("candidates"):
        out["candidates"] = (new or {}).get("candidates") or []
    out["ambiguous_subject"] = bool(out.get("ambiguous_subject")) and not out.get("subject_id")
    out["note"] = (new or {}).get("note") or out.get("note") or ""
    return out


def create_task(db, client, owner_id, utterance):
    # 同一句原话重复交办时，接着原来那件没办完的事办，不再开一件重复的。
    # 用户重打一遍往往是没看到回应，不是想办两件一样的事；已经办完或明确失败的
    # 不在此列，那样会挡住用户以后合理再次交办。
    text = (utterance or "").strip()
    if text:
        dup = db.row(
            "SELECT task_id FROM task WHERE owner_id=? AND utterance=?"
            " AND stage IN ('created','negotiating','agreed','counter_pending',"
            "'blocked','needs_renegotiation')"
            " ORDER BY created_at DESC LIMIT 1", (owner_id, text))
        if dup is not None:
            row = db.row("SELECT extracted_json FROM task WHERE task_id=?",
                         (dup["task_id"],))
            try:
                extracted = json.loads(row["extracted_json"] or "{}")
            except (ValueError, TypeError):
                extracted = {}
            return {"task_id": dup["task_id"], "extracted": extracted, "reused": True}
    extracted = agents._extract_requirements(db, client, utterance, owner_id)
    # 跟进句接着原来那件办，不新开孤儿事项。
    # 「300 以内」「要软底的」这类话抽得出预算和条件，抽不出收礼人；
    # 原先只按原话一字不差复用，于是每次跟进都新建一条没有主体的事项，
    # 预算孤零零挂在那件上，原来那件的预算还是空的。
    # 判据三条同时成立才并：抽不出主体、只有一件未完结事项、那句话本身不是新交办
    # （新交办会说「再买一双」这类，extracted 里会带上新的 subject 或 occasion）。
    if extracted.get("ambiguous_subject") and not extracted.get("occasion"):
        open_rows = db.rows(
            "SELECT task_id, extracted_json FROM task WHERE owner_id=?"
            " AND stage IN ('created','negotiating','agreed','counter_pending',"
            "'blocked','needs_renegotiation')"
            " ORDER BY created_at DESC", (owner_id,))
        if len(open_rows) == 1:
            prev_task, prev_raw = open_rows[0]["task_id"], open_rows[0]["extracted_json"]
            try:
                prev = json.loads(prev_raw or "{}")
            except (ValueError, TypeError):
                prev = {}
            merged = _merge_followup(prev, extracted)
            db.execute("UPDATE task SET extracted_json=? WHERE task_id=?",
                       (json.dumps(merged, ensure_ascii=False), prev_task))
            db.audit(prev_task, owner_id, "task_followup_merged",
                     {"utterance": utterance, "merged_keys": sorted(
                         k for k in merged if merged.get(k) != prev.get(k))},
                     owner_id=owner_id)
            return {"task_id": prev_task, "extracted": merged, "reused": True}
    task_id = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage, outcome, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (task_id, owner_id, utterance, json.dumps(extracted, ensure_ascii=False),
         "created", "", dbm.now_iso()))
    db.audit(task_id, owner_id, "task_created",
             {"utterance": utterance, "extracted": extracted}, owner_id=owner_id)
    return {"task_id": task_id, "extracted": extracted}


def advance_task(db, client, task_id):
    return agents.run_negotiation(db, client, task_id)


# 协商里「在等谁」的几种情况。区分清楚很重要：等对方代表、等对方本人、
# 等模型调通、还是等我自己拍板，是不同的事，用户能做的下一步也不一样。
# 特别要分清「等别人」和「等我自己」：对方已经回了反提案，卡的是送礼者本人，
# 这时不能显示成「在等对方助理回复」——那和已经收到回复的事实矛盾。
WAITING_KINDS = ("awaiting_counterparty", "awaiting_person", "awaiting_my_decision",
                 "awaiting_model")


def _waiting_text(kind, question, since):
    """把「在等谁、等什么、等了多久」说成人话。

    未回应不是同意，也不能显示成已经约好。这里只陈述事实：缺什么、从什么时候
    开始等的、现在能做什么。等待时长按演示场景说明，不写成服务承诺。
    """
    who = {
        "awaiting_counterparty": "在等对方助理回复",
        "awaiting_person": "在等对方本人确认",
        "awaiting_model": "在等模型调通",
        "awaiting_my_decision": "在等你定",
    }.get(kind, "在等回复")
    text = who + "。" + (question or "")
    if since:
        text += "（从 %s 起在等）" % since[:16].replace("T", " ")
    return text


def set_waiting(db, task_id, owner_id, kind, question, payload=None):
    """记下这件事现在在等谁、待答什么。

    同一件事只留一条待答记录：重新协商或再次等待时覆盖旧的，
    不然用户会看到同一个问题挂了两条。

    conn 可传当前事务里的连接。确认购买时清理等待必须在同一个事务内完成，
    另开写连接会被自己持有的锁挡住，等待超时后整个确认跟着回滚。
    """
    conn = payload.pop("_conn", None) if isinstance(payload, dict) else None
    runner = conn if conn is not None else db
    runner.execute("DELETE FROM pending_action WHERE task_id=? AND kind=?", (task_id, kind))
    runner.execute(
        "INSERT INTO pending_action(action_id, task_id, kind, prompt, owner_id,"
        " payload_json, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (dbm.new_id("act"), task_id, kind, question, owner_id,
         json.dumps(payload or {}, ensure_ascii=False), "open", dbm.now_iso()))
    db.audit(task_id, owner_id, "waiting_recorded", {"kind": kind, "question": question})


def clear_waiting(db, task_id, kind=""):
    """事情往下走了：把对应等待标记清掉，不让人看到已经过了期的等待。

    conn 可传当前事务里的连接，语义同 set_waiting：确认购买时清理等待
    必须在同一个事务内完成，另开写连接会被自己持有的锁挡住。
    """
    conn = None
    if isinstance(kind, dict):
        conn = kind.pop("_conn", None)
        kind = kind.get("kind", "")
    runner = conn if conn is not None else db
    if kind:
        runner.execute("DELETE FROM pending_action WHERE task_id=? AND kind=?", (task_id, kind))
    else:
        runner.execute("DELETE FROM pending_action WHERE task_id=?", (task_id,))


def waiting_for(db, owner_id):
    """当前在等回复的事，按开始等的时间倒序。

    只列还没结束的。已经谈成、已经登记、已经收尾的不算在等。
    """
    rows = db.rows(
        "SELECT p.*, t.utterance, t.stage FROM pending_action p"
        " JOIN task t ON t.task_id=p.task_id"
        " WHERE p.owner_id=? AND p.status='open'"
        " ORDER BY p.created_at DESC", (owner_id,))
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        out.append({
            "action_id": r["action_id"], "task_id": r["task_id"],
            "kind": r["kind"], "question": r["prompt"],
            "since": r["created_at"], "stage": r["stage"],
            "utterance": r["utterance"], "payload": payload,
            "text": _waiting_text(r["kind"], r["prompt"], r["created_at"]),
        })
    return out


def _counterparty_accept(db, task_id, proposal, counterparty):
    """取对方对同一版本、同一提案人的接受记录。

    必须同时限定版本和提案人，避免把别的版本或自己那一侧的 accept 当成对方同意。
    """
    return db.row(
        "SELECT * FROM proposal WHERE task_id=? AND version=? AND proposer=?"
        " AND decided_by=? AND decision='accept'",
        (task_id, proposal["version"], proposal["proposer"], counterparty))


# 从一句话里找「32 码」「31码」这样的当前鞋码写法。
_SIZE_IN_TEXT = re.compile(r"\d+(?:\.\d+)?\s*码")

# 明确标记为「待核实」的来源措辞。带这些词的记录只是过去的印象，不是当前情况。
STALE_SOURCE_MARKERS = ("待核实", "未核实", "待确认", "存疑", "不确定", "待核对",
                        "上学期", "去年", "之前", "当时", "以前")

# 只有「鞋码」这一量纲的记录能支撑鞋码执行。
# 脚长（foot_length）是另一个量纲：20.5 cm 不等于 20 码，不能拿来当尺码证据，
# 更不能靠数字拼串做包含匹配——那会让「20.5」拼成 205 后误配到 20 码。
SIZE_EVIDENCE_ATTRS = ("shoe_size", "当前鞋码", "现穿鞋码", "鞋码")
# 记录里出现这些，说明是脚长/试穿印象，不是当前鞋码。
NON_SIZE_ATTRS = ("foot_length", "脚长", "试穿记录", "鞋内长")


def _is_stale_source(value, source):
    """这条记录是否只是过去的印象、需要再核实。"""
    text = "%s %s" % (value or "", source or "")
    return any(m in text for m in STALE_SOURCE_MARKERS)


def _size_digits(text):
    """取出记录里的尺码数字，只认紧跟「码」的数，避免把 20.5 cm 当成 205。

    「31 码」「31码」都算；「20.5 cm」「身高 128 cm」不算——那不是鞋码。
    返回归一化后的数字（"31.0" -> "31"），便于和商品选项比对。
    """
    import re
    t = text or ""
    out = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*码", t):
        num = m.group(1)
        if num.endswith(".0"):
            num = num[:-2]
        out.append(num)
    return out


def _size_evidence(db, b_subject_id, asked_size, product=None):
    """找出支撑某个尺码的当前证据。

    返回 (ok, evidence, problem)。规则：
    - 尺码必须能追溯到 B 侧一条当前有效的**鞋码**记录，量纲必须一致；
    - 脚长、试穿印象不是当前鞋码，不能作为执行依据；
    - 来源写明「待核实」「上学期」之类的旧印象不算证据；
    - 尺码还必须落在商品的有效选项里；
    - 没有当前证据时不猜，直接说需要家长确认当前尺码。
    """
    size = (asked_size or "").strip()
    if not size:
        return False, {}, "这件商品需要尺码，对方还没有给出当前尺码"
    want = _size_digits(size)
    if not want:
        return False, {}, "尺码「%s」无法核对" % size
    if product is not None and product.get("has_size"):
        try:
            options = json.loads(product.get("size_options") or "[]")
        except (ValueError, TypeError):
            options = []
        if options and size not in [str(o) for o in options]:
            return False, {}, ("尺码「%s」不在这件商品的有效选项（%s）里"
                               % (size, "、".join(str(o) for o in options)))
    if not b_subject_id:
        return False, {}, "找不到家长一侧的记录，无法核对尺码"
    rows = db.rows(
        "SELECT * FROM fact WHERE owner_id='B' AND subject_id=? AND status='active'"
        " ORDER BY observed_on DESC", (b_subject_id,))
    stale_hits = []
    for r in rows:
        if r["attribute"] not in SIZE_EVIDENCE_ATTRS:
            continue
        if any(k in (r["attribute"] or "") for k in NON_SIZE_ATTRS):
            continue
        value = r["value"] or ""
        if not any(d in want for d in _size_digits(value)):
            continue
        if _is_stale_source(value, r["source_utterance"] or ""):
            stale_hits.append(r)
            continue
        return True, {"fact_id": r["fact_id"], "attribute": r["attribute"],
                      "value": value, "provider": r["provider"],
                      "observed_on": r["observed_on"]}, ""
    if stale_hits:
        r = stale_hits[0]
        return False, {"fact_id": r["fact_id"], "value": r["value"],
                       "observed_on": r["observed_on"]}, \
            ("尺码只能追溯到「%s」（%s），这条记录自己写着现在是否合脚待核实，"
             "不能当成当前尺码。请家长确认孩子现在的鞋码后再下单。"
             % (r["value"], r["observed_on"]))
    return False, {}, ("家长这边没有能支撑「%s」的当前鞋码记录（脚长和试穿印象不算）。"
                       "请家长确认孩子现在的鞋码后再下单。" % size)


def _purchase_auth_problems(db, task, proposal):
    """本次执行是否有一条适用、未过期的购买授权。

    检查授权是否存在、动作是否是购买（不是只允许准备）、是否未过期、
    对象是否适用于本次送礼的孩子。任一不满足都拒绝执行。
    """
    problems = []
    extracted = json.loads(task["extracted_json"] or "{}")
    subject_id = extracted.get("subject_id") or ""
    today = dbm.now_iso()[:10]
    auths = db.rows(
        "SELECT * FROM authorization WHERE owner_id='A' AND scope_kind='purchase'")
    if not auths:
        problems.append("没有购买授权：记录里找不到允许购买的一条，只有授权后才能登记订单")
        return problems
    applicable = []
    for a in auths:
        if a["valid_until"] and a["valid_until"] < today:
            problems.append("购买授权已过期：%s" % a["valid_until"])
            continue
        # 授权限定了对象时，必须覆盖本次送礼的孩子。
        if a["subject_id"] and subject_id and a["subject_id"] != subject_id:
            continue
        applicable.append(a)
    if not applicable and not problems:
        problems.append("现有购买授权不适用于这次送礼的对象")
    # B 的分享授权过期时，协商依据可能已经失效。
    for a in db.rows("SELECT * FROM authorization WHERE owner_id='B' AND scope_kind='share'"):
        if a["valid_until"] and a["valid_until"] < today:
            problems.append("家长一方的分享授权已过期：%s，协商依据可能已失效" % a["valid_until"])
    return problems


def _pre_execution_check(db, task_id, proposal_id, confirmed_by, accept_counter):
    """执行前检查。任何一条不满足都拒绝执行，并给出人能看懂的原因。

    检查项：确认人是否为送礼者本人、提案是否属于当前任务、是否为最新提案、
    双方是否已同版本接受（对方反提案时本次确认是否已记录接受）、
    授权是否有效、必要规格是否已补齐。
    """
    problems = []
    task = db.row("SELECT * FROM task WHERE task_id=?", (task_id,))
    proposal = db.row("SELECT * FROM proposal WHERE proposal_id=?", (proposal_id,))
    if task is None or proposal is None:
        return None, None, ["任务或提案不存在"]
    if proposal["task_id"] != task_id:
        problems.append("提案不属于当前任务，不能用于本次确认")
        return task, proposal, problems
    if confirmed_by != "A":
        problems.append("只有送礼者本人能确认购买")
    # 已因事实纠正作废的提案，无论版本号是否最大都不能执行。
    if (proposal["decision"] or "") == "stale":
        # 作废有两种原因，必须说清是哪一种，用户才知道下一步干什么：
        # 换了商品要说「换了商品，要对方重新确认」；记录被纠正要说「记录已更正」。
        # 只写一句笼统的「方案已作废」，用户刚换的是商品，看到记录被更正会以为系统出错。
        why = proposal["decision_reason"] or ""
        if "已换为" in why:
            problems.append("换了商品等于改了方案，对方接受的是原来那件，"
                            "要先让对方确认这一件才能买")
        else:
            problems.append("这个方案依据的记录已被更正，不能执行，请重新协商")
    if task["stage"] not in ("agreed", "counter_pending"):
        problems.append("当前状态不能确认购买：%s" % task["stage"])
    # 必须是最新提案，旧版本确认不能推动执行
    latest = db.row(
        "SELECT MAX(version) AS v FROM proposal WHERE task_id=?", (task_id,))["v"]
    if latest is not None and proposal["version"] < latest:
        problems.append("这不是最新方案（当前最新为第 %s 版），变更后旧版本不能执行" % latest)

    # 对方是否已接受同一版本
    b_accepted = _counterparty_accept(db, task_id, proposal, "B")
    counterparty_ok = False
    if proposal["proposer"] == "B":
        # B 的反提案：需要 A 本次明确接受，才视为双方同版本
        counterparty_ok = bool(accept_counter)
        if not accept_counter:
            problems.append("这是对方的反提案，需要你先接受该方案再确认购买")
    else:
        # 必须由对方本人接受，且接受记录属于同一版本；
        # 不能只看存在一行 decision='accept'，那样会把别人的接受算成同版本。
        counterparty_ok = b_accepted is not None
        if not counterparty_ok:
            problems.append("对方还没有接受这一版方案")

    # 授权检查：必须存在一条适用于本次执行的购买授权。
    # 只遍历已有授权看是否过期是不够的：没有购买授权时循环根本不会报错，
    # prepare（只允许准备方案）也不能当成可以购买。
    problems.extend(_purchase_auth_problems(db, task, proposal))

    # 规格检查：需要尺码的商品不能只看条件里填了没有，
    # 还要能追溯到家长一侧一条当前的鞋码记录。B 口头说了尺码、但依据只是
    # 过去的试穿印象或脚长时，照样不能执行。
    product = db.row("SELECT * FROM product WHERE product_id=?", (proposal["product_id"],))
    if product is not None and product["has_size"]:
        spec = _order_spec(proposal, product)
        if not spec:
            problems.append("这件商品需要确定尺码，当前方案里还没有明确的尺码")
        else:
            extracted = json.loads(task["extracted_json"] or "{}")
            b_subject = _b_side_person(db, extracted.get("subject_id") or "")
            ok, evidence, problem = _size_evidence(db, b_subject, spec, product)
            if not ok:
                problems.append(problem)
    return task, proposal, problems


def _b_side_person(db, a_subject_id):
    """按 A 侧人物找到家长一侧同一个人的记录。"""
    if not a_subject_id:
        return ""
    a_person = db.row("SELECT name FROM person WHERE person_id=?", (a_subject_id,))
    if a_person is None:
        return ""
    b_person = db.row("SELECT person_id FROM person WHERE owner_id='B' AND name=?",
                      (a_person["name"],))
    return b_person["person_id"] if b_person else ""


def confirm_and_purchase(db, client, task_id, proposal_id, confirmed_by, accept_counter=False):
    """A 确认后登记本地模拟订单。

    accept_counter=True 表示本次确认同时接受对方的反提案。
    所有条件先检查，全部通过后才写入；检查不通过时不改动接受人、决定和阶段，
    也不产生订单。重复确认返回同一订单。
    """
    version_row = db.row("SELECT version FROM proposal WHERE proposal_id=?", (proposal_id,))
    dedup_key = "order:%s:v%s" % (task_id, (version_row or {}).get("version"))
    # 已下过的单直接返回同一订单，不被后续状态检查挡掉。
    existing = db.row("SELECT * FROM sim_order WHERE dedup_key=?", (dedup_key,))
    if existing is not None:
        db.audit(task_id, confirmed_by, "purchase_duplicate_confirmed",
                 {"order_id": existing["order_id"]})
        return dict(existing) | {"duplicate": True}

    # accept_counter 只作为「本次确认附带接受反提案」的意图参与检查，不提前落库。
    # 否则被拒绝的错误确认会留下 proposal 已接受、task 已约定的痕迹。
    task, proposal, problems = _pre_execution_check(db, task_id, proposal_id, confirmed_by,
                                                    accept_counter)
    if problems:
        db.audit(task_id, confirmed_by, "purchase_rejected",
                 {"proposal_id": proposal_id, "problems": problems,
                  "accept_counter": bool(accept_counter)})
        raise ValueError("；".join(problems))

    product = db.row("SELECT * FROM product WHERE product_id=?", (proposal["product_id"],))
    spec = _order_spec(proposal, product)
    order_id = dbm.new_id("simorder")
    recipient = json.loads(task["extracted_json"]).get("subject_id") or ""
    occasion = json.loads(task["extracted_json"]).get("occasion") or ""

    # 接受、订单、阶段、审计放在同一个事务里，中途失败不会留下半完成状态。
    with db.connect() as conn:
        # 登记的同时写一条送礼记录，关联这次订单。
        # 没有它，用户事后说「送到了，孩子很喜欢」就接不上任何东西，
        # 那句话会被当成新的交办或记录，送礼这件事就没有下文。
        gift_id = dbm.new_id("gift")
        conn.execute(
            "INSERT INTO gift_record(gift_id, order_id, owner_id, subject_id, product_id,"
            " product_name, spec, occasion, status, feedback, happened_on, simulated)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
            (gift_id, order_id, confirmed_by, recipient, product["product_id"],
             product["name"], spec, occasion, "purchased", "", dbm.now_iso()[:10]))
        if accept_counter:
            conn.execute(
                "UPDATE proposal SET decision='accept', decided_by=?, decision_reason=?"
                " WHERE proposal_id=?",
                (confirmed_by, "送礼者接受对方反提案并确认购买", proposal_id))
            conn.execute("UPDATE task SET stage='agreed' WHERE task_id=?", (task_id,))
        conn.execute(
            "INSERT INTO sim_order(order_id, task_id, proposal_version, confirmed_by, product_id,"
            " product_name, spec, recipient_subject_id, amount_cny, state, delivery_state,"
            " dedup_key, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, task_id, proposal["version"], confirmed_by, product["product_id"],
             product["name"], spec, recipient, product["price_cny"], "purchased", "simulated",
             dedup_key, dbm.now_iso()))
        conn.execute("UPDATE task SET stage='purchased', outcome=? WHERE task_id=?",
                     ("已登记模拟订单 %s" % order_id, task_id))
        # 已经登记，这件事不再是「在等谁」。
        # 必须用同一个连接：另开写连接会被这个事务自己持有的锁挡住，
        # 等到超时后整个确认购买会跟着回滚。
        clear_waiting(db, task_id, {"_conn": conn})
        conn.execute(
            "INSERT INTO audit_log(audit_id, task_id, actor, action, detail_json, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (dbm.new_id("aud"), task_id, confirmed_by, "purchase_confirmed",
             json.dumps({"order_id": order_id, "product_id": product["product_id"],
                         "amount_cny": product["price_cny"],
                         "proposal_version": proposal["version"], "spec": spec,
                         "accept_counter": bool(accept_counter)},
                        ensure_ascii=False),
             dbm.now_iso()))
    row = db.row("SELECT * FROM sim_order WHERE order_id=?", (order_id,))
    return dict(row) | {"duplicate": False}


def save_candidates(db, task_id, version, a_prop):
    """把这一轮模型给出的首选和备选都记下来。

    首选也要占一行，否则用户只看到备选、看不到自己正在买的这件。
    同一事项同一版本同一商品只留一条，重复协商不会越堆越多。
    """
    rows = [{
        "product_id": a_prop.get("product_id"),
        "product_name": "",  # 下面按商品表补，模型给的名字不作为依据
        "reason": a_prop.get("reason") or "",
        "primary": True,
    }]
    for c in a_prop.get("candidates") or []:
        rows.append({"product_id": c.get("product_id"), "product_name": "",
                     "reason": c.get("reason") or "", "primary": False})
    index = {p["product_id"]: p for p in db.rows("SELECT * FROM product")}
    for r in rows:
        pid = r["product_id"]
        if not pid or pid not in index:
            continue
        p = index[pid]
        db.execute(
            "INSERT OR IGNORE INTO order_candidate(candidate_id, task_id, proposal_id,"
            " version, product_id, product_name, price_cny, reason, checked, chosen,"
            " created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (dbm.new_id("cand"), task_id, "", version, pid, p["name"],
             p["price_cny"], r["reason"], 1 if r["primary"] else 0, 0, dbm.now_iso()))


def task_candidates(db, task_id, version=None):
    """这个事项的候选商品，按首选在前、价格在后排。"""
    sql = ("SELECT * FROM order_candidate WHERE task_id=?"
           + ("" if version is None else " AND version<=?"))
    params = [task_id] if version is None else [task_id, version]
    rows = db.rows(sql + " ORDER BY checked DESC, price_cny, created_at", params)
    seen = set()
    out = []
    # 同一件商品可能在不同版本都出现过：只保留最近一次，
    # 但保留它被选过的痕迹，否则换版本后看不出用户选过什么。
    for r in reversed(rows):
        if r["product_id"] in seen:
            continue
        seen.add(r["product_id"])
        out.append(r)
    return list(reversed(out))


def choose_candidate(db, task_id, product_id, owner_id="A"):
    """用户从候选里换一件商品。

    换商品等于改方案，必须重走一遍硬条件检查：预算、重复、品类、
    尺码一项都不能少。检查不通过就不改任何东西，也不产生订单。
    """
    task = db.row("SELECT * FROM task WHERE task_id=?", (task_id,))
    if task is None:
        raise ValueError("任务不存在")
    # 已经下过单的事不再换商品。换商品会改方案、把阶段退回重新协商，
    # 对一件已经买完、甚至已经送到的事做这些，界面上会重新出现「确认购买」，
    # 用户能对同一件事再下第二个单。要换就另开一件事来说。
    ordered = db.row("SELECT order_id, state FROM sim_order WHERE task_id=?"
                     " ORDER BY created_at LIMIT 1", (task_id,))
    if ordered is not None:
        db.audit(task_id, owner_id, "candidate_rejected",
                 {"product_id": product_id,
                  "reason": "事项已有订单，不能再换商品",
                  "order_id": ordered["order_id"]}, owner_id=owner_id)
        raise ValueError("这件事已经登记过订单（%s，%s），不能再换商品。"
                         "要送别的可以另外跟我说一件。"
                         % (ordered["order_id"],
                            {"purchased": "待支付", "paid": "已支付",
                             "shipped": "已发货", "delivered": "已送达"}
                            .get(ordered["state"], ordered["state"])))
    extracted = json.loads(task["extracted_json"] or "{}")
    product = db.row("SELECT * FROM product WHERE product_id=?", (product_id,))
    if product is None:
        raise ValueError("这件商品不在测试商品清单中")
    latest = db.row(
        "SELECT * FROM proposal WHERE task_id=? ORDER BY version DESC LIMIT 1", (task_id,))
    proposal = {
        "asked_size": (latest or {}).get("asked_size") or "",
        "shared_conditions_json": (latest or {}).get("shared_conditions_json") or "[]",
    }
    ok, problems, _notes = agents._check_hard_conditions(db, extracted, proposal, product)
    if not ok:
        db.audit(task_id, owner_id, "candidate_rejected",
                 {"product_id": product_id, "problems": problems},
                 owner_id=owner_id)
        raise ValueError("；".join(problems))
    # 通过检查才记下选择，并让这一版方案指向新商品。
    # 只把候选行标成 chosen 是不够的：确认购买用的是 proposal.product_id，
    # 不改它的话用户换了商品，买下来的还是原来那件，看着就像「点了没反应」。
    if latest is not None:
        # 换商品等于改方案。对方原来接受的是另一件商品，那条接受对
        # 这件新商品无效；若沿用，用户会买到一件对方从未同意过的东西。
        # 所以这一版的接受一并作废，执行时会被挡住并要求重新确认，
        # 而不是偷偷改掉商品编号后拿旧接受成交。
        db.execute(
            "UPDATE proposal SET decision='stale', decided_by='', decision_reason=?"
            " WHERE proposal_id=? AND decision IN ('accept','pending')",
            ("已换为%s，对方接受的是原来那件，需要重新确认" % product["name"],
             latest["proposal_id"]))
        # 事项阶段要跟着退回「需要重新协商」。
        # 只作废提案、不改阶段的话，界面还挂着「方案已约定」和「确认购买」，
        # 用户照着一路点下去只会被服务端拒绝——按钮看得见、点了办不成，
        # 这正是「让用户伺候系统」。阶段一改，卡片给出的是重新协商入口。
        stage_row = db.row("SELECT stage FROM task WHERE task_id=?", (task_id,))
        if stage_row is not None and stage_row["stage"] in ("agreed", "counter_pending"):
            db.execute(
                "UPDATE task SET stage='needs_renegotiation', outcome=? WHERE task_id=?",
                ("已换成%s，对方接受的是原来那件，要重新协商让对方确认这一件"
                 % product["name"], task_id))
            # 在等谁要跟着改：这件现在等的是对方重新确认，
            # 不能再显示成「在等你定」。
            clear_waiting(db, task_id)
            set_waiting(db, task_id, owner_id, "awaiting_counterparty",
                        "对方要重新确认「%s」这一件" % product["name"])
        db.execute(
            "UPDATE proposal SET product_id=?, reason=? WHERE proposal_id=?",
            (product_id,
             (latest["reason"] or "").split("；换为：")[0] + "；换为：" + product["name"],
             latest["proposal_id"]))
    db.execute(
        "UPDATE order_candidate SET chosen=CASE WHEN product_id=? THEN 1 ELSE 0 END"
        " WHERE task_id=?", (product_id, task_id))
    db.audit(task_id, owner_id, "candidate_chosen",
             {"product_id": product_id, "product_name": product["name"]})
    return {"task_id": task_id, "product_id": product_id,
            "product_name": product["name"], "price_cny": product["price_cny"]}


PRODUCT_CATEGORIES = {
    "puzzle": "拼图与益智", "art": "绘画与手工", "sport": "运动",
    "book": "图书", "daily": "日常用品", "toy": "玩具",
    "digital": "数码", "shoes": "鞋", "clothes": "衣物", "other": "其他",
}


def product_detail(db, product_id, task_id="", owner_id="A"):
    """商品详情：把这个事项用得到的属性摊开给用户看。

    演示数据里没有商品图片，也不接入真实商品库，所以不提供「主图」。
    真正会改变决策的属性是：价格、适龄、要不要尺码、是否发声、是否便于收纳，
    以及这一件在当前事项下过不过得了硬条件。把这些摊开比放一张占位图有用。
    """
    product = db.row("SELECT * FROM product WHERE product_id=?", (product_id,))
    if product is None:
        return None
    out = {
        "product_id": product["product_id"],
        "name": product["name"],
        "category": product["category"],
        "category_label": PRODUCT_CATEGORIES.get(product["category"], product["category"]),
        "price_cny": product["price_cny"],
        "age_hint": product["age_hint"],
        "note": product["note"],
        "has_size": bool(product["has_size"]),
        "size_options": json.loads(product["size_options"] or "[]"),
        "makes_noise": bool(product["makes_noise"]),
        "easy_to_store": bool(product["easy_to_store"]),
        # 演示数据里没有商品图片，界面不摆占位图、不谎称有图。
        "image": "",
        "image_note": "演示数据没有商品图片，这里只列会影响决策的属性。",
        "checks": [],
        "current": False,
    }
    if task_id:
        task = db.row("SELECT * FROM task WHERE task_id=?", (task_id,))
        if task is not None:
            extracted = json.loads(task["extracted_json"] or "{}")
            latest = db.row(
                "SELECT * FROM proposal WHERE task_id=? ORDER BY version DESC LIMIT 1",
                (task_id,))
            proposal = {
                "asked_size": (latest or {}).get("asked_size") or "",
                "shared_conditions_json": (latest or {}).get("shared_conditions_json") or "[]",
            }
            ok, problems, _notes = agents._check_hard_conditions(
                db, extracted, proposal, product)
            # 逐项给出通过与否：只报「不合格」的话，用户看不出是查过了还是没查。
            checks = []
            budget = extracted.get("budget_max")
            checks.append({"item": "预算", "ok": True,
                           "text": ("%.0f 元，预算 %.0f 元内" % (product["price_cny"], budget))
                           if budget is not None else "本次没有设预算上限"})
            if budget is not None and product["price_cny"] > budget:
                checks[-1] = {"item": "预算", "ok": False,
                              "text": "%.0f 元超出预算 %.0f 元" % (
                                  product["price_cny"], budget)}
            wanted = extracted.get("category")
            checks.append({"item": "品类", "ok": True,
                           "text": ("属于本次要求的「%s」" % wanted) if wanted else "本次不限品类"})
            if wanted and product["category"] != wanted:
                checks[-1] = {"item": "品类", "ok": False,
                              "text": "不属于本次要求的「%s」" % wanted}
            checks.append({"item": "尺码", "ok": True,
                           "text": "这件不需要选尺码" if not product["has_size"]
                           else "需要选尺码，要家长确认当前尺码"})
            # 重复检查要说实话：查过送礼记录就给结论，没有对象可查就说没查。
            subject_id = extracted.get("subject_id")
            if subject_id and extracted.get("avoid_repeat"):
                past = db.rows(
                    "SELECT product_id, product_name FROM gift_record"
                    " WHERE owner_id=? AND subject_id=? ORDER BY happened_on DESC",
                    (task["owner_id"], subject_id))
                same = [g for g in past if g["product_id"] == product_id
                        or g["product_name"] == product["name"]]
                if same:
                    checks.append({"item": "重复", "ok": False,
                                   "text": "和 %s 送过的「%s」重复" % (
                                       same[0]["happened_on"], same[0]["product_name"])})
                else:
                    checks.append({"item": "重复", "ok": True,
                                   "text": "和以往的送礼记录不重复"})
            else:
                checks.append({"item": "重复", "ok": True,
                               "text": "本次没有要求避开重复"})
            out["checks"] = checks
            out["checks_ok"] = ok
            out["problems"] = problems
            # 「当前这件」要同时认两种情况：模型的首选（checked），
            # 和用户手动换过的（chosen）。只认 chosen 会把模型首选漏掉。
            out["current"] = any(
                c["product_id"] == product_id and (c["chosen"] or c["checked"])
                for c in task_candidates(db, task_id))
    return out
def _simulate_delivery_chain(db, task_id, order_id, owner_id):
    """支付之后由系统自己把发货和物流走完，不要让用户点。

    用户从一开始就说了「少操作、少交代」。让用户去点「发货」「确认送达」，
    是在让用户伺候系统：真实电商里这两步是平台和物流自己更新的，
    用户既不需要、也没有信息去填物流单号。
    所以这里在支付记录落库的同一次调用里，接着写入发货与物流信息，
    界面只展示来源和状态，不给按钮。

    必须说清楚的两点：
    - delivery_state 全程是 simulated。演示数据没有真实电商或支付连接，
      抓不到真实物流，所以不谎称已连接真实平台。
    - 发货时间写成支付时间之后的确定偏移，不写真实承运商的承诺时效。
    """
    from datetime import timedelta
    order = db.row("SELECT * FROM sim_order WHERE order_id=?", (order_id,))
    if order is None:
        return None
    now = dbm.now_iso()
    ship_no = "simship_" + uuid.uuid4().hex[:12]
    # 时间按演示场景写：支付后系统登记发货，再往后一个演示间隔算送达。
    # 这里给的是本演示里的编排时间，不是任何真实平台的配送承诺。
    shipped_at = (dbm.parse_iso(now) + timedelta(hours=2)).isoformat()
    delivered_at = (dbm.parse_iso(now) + timedelta(hours=30)).isoformat()
    db.execute(
        "UPDATE sim_order SET state='shipped', delivery_state='simulated',"
        " ship_no=?, shipped_at=? WHERE order_id=?", (ship_no, shipped_at, order_id))
    db.audit(task_id, owner_id, "order_ship_auto",
             {"order_id": order_id, "note": "支付后系统自动登记发货", "ship_no": ship_no,
              "source": "本地模拟物流登记，未连接真实电商平台"})
    db.execute(
        "UPDATE sim_order SET state='delivered', delivery_state='simulated',"
        " delivered_at=? WHERE order_id=?", (delivered_at, order_id))
    db.audit(task_id, owner_id, "order_deliver_auto",
             {"order_id": order_id, "note": "系统按模拟物流更新送达",
              "source": "本地模拟物流登记，未连接真实电商平台"})
    return db.row("SELECT * FROM sim_order WHERE order_id=?", (order_id,))


def advance_order(db, task_id, order_id, action, owner_id="A"):
    """推进订单：只保留「支付」这一个用户动作。

    发货、物流、送达不再由用户点：支付记录一落库，系统接着把这条链走完
    （见 _simulate_delivery_chain）。原来那三个按钮是在让用户伺候系统，
    违背了这个产品「少操作、少交代」的出发点。
    仍然保留顺序保护：没支付就不能发货；重复支付被挡。
    """
    order = db.row("SELECT * FROM sim_order WHERE order_id=? AND task_id=?",
                   (order_id, task_id))
    if order is None:
        raise ValueError("订单不存在")
    now = dbm.now_iso()
    if action == "pay":
        if order["state"] != "purchased":
            raise ValueError("这一单已经付过了")
        db.execute(
            "UPDATE sim_order SET state='paid', paid_at=?, paid_amount_cny=amount_cny"
            " WHERE order_id=?", (now, order_id))
        # 支付之后系统自己走完后续，用户不需要再点任何一步。
        final = _simulate_delivery_chain(db, task_id, order_id, owner_id)
        db.audit(task_id, owner_id, "order_pay",
                 {"order_id": order_id, "note": "模拟支付完成 %.0f 元" % order["amount_cny"],
                  "at": now})
        if final is None:
            raise ValueError("订单不存在")
        return dict(final) | {
            "note": "支付完成 %.0f 元，物流状态由系统接着更新" % order["amount_cny"],
            "auto_advanced": True}
    else:
        # ship / deliver 不再是用户动作：系统在支付时已经接着走完。
        # 仍然显式拒绝，避免旧调用方以为这三个动作还能分开点。
        raise ValueError("发货和送达由系统在支付后自动更新，不需要手动操作")


def task_card(db, task_id):
    task = db.row("SELECT * FROM task WHERE task_id=?", (task_id,))
    if task is None:
        return None
    messages = db.rows(
        "SELECT * FROM message_log WHERE task_id=? ORDER BY created_at, msg_id", (task_id,))
    proposals = db.rows(
        "SELECT * FROM proposal WHERE task_id=? ORDER BY version, created_at", (task_id,))
    orders = db.rows("SELECT * FROM sim_order WHERE task_id=? ORDER BY created_at", (task_id,))
    audits = db.rows(
        "SELECT * FROM audit_log WHERE task_id=? ORDER BY created_at", (task_id,))
    extracted = json.loads(task["extracted_json"] or "{}")
    subject = None
    if extracted.get("subject_id"):
        subject = db.row("SELECT * FROM person WHERE person_id=?", (extracted["subject_id"],))
    facts = []
    if subject is not None:
        facts = db.rows(
            "SELECT * FROM fact WHERE owner_id=? AND subject_id=? ORDER BY observed_on DESC, fact_id",
            (task["owner_id"], subject["person_id"]))
    history = []
    if subject is not None:
        history = db.rows(
            "SELECT * FROM gift_record WHERE owner_id=? AND subject_id=?"
            " ORDER BY happened_on DESC", (task["owner_id"], subject["person_id"]))
    candidates = []
    if task["stage"] == "blocked":
        audit = db.row(
            "SELECT detail_json FROM audit_log WHERE task_id=? AND action='blocked_need_subject'"
            " ORDER BY created_at DESC LIMIT 1", (task_id,))
        if audit is not None:
            try:
                candidates = json.loads(audit["detail_json"]).get("candidates") or []
            except (ValueError, TypeError):
                candidates = []
        if not candidates:
            candidates = extracted.get("candidates") or []
    return {
        "task": task,
        "extracted": extracted,
        "subject": subject,
        "facts": facts,
        "history": history,
        "messages": [{**m, "payload": json.loads(m["payload_json"])} for m in messages],
        "proposals": proposals,
        "orders": orders,
        "audits": audits,
        "candidates": candidates,
        # 商品候选：模型给的备选，加上当前正在买的这一件。
        # 和上面的人物候选（candidates）不是一回事，界面要分开显示。
        "product_candidates": task_candidates(db, task_id),
        # 代填回执摘要：只给「有没有、状态是什么」，字段明细走
        # /api/task/{id}/receipts。这里带上它，卡片才知道要不要显示
        # 「查看代填回执」这个入口——没代填过的事项不该摆一个空入口。
        "receipts": _receipt_digest(db, task_id),
        # 这件事用到过哪些资料。本人明确点过「用于这件事」才会出现，
        # 保存资料本身不自动挂上去——不替用户决定这条资料跟哪件事有关。
        "linked_notes": linked_events(db, task_id, task["owner_id"]),
        "question": extracted.get("note") or task["outcome"],
    }


# 人物浏览分组。分组是用户组织自己联系人的方法，可以重叠，
# 不是互斥身份，也不授予任何权限。同一个人可以同时出现在多个组里。
PERSON_GROUPS = ("家人与亲属", "朋友与同学", "工作与业务", "社区与兴趣", "生活服务")


def person_groups(db, owner_id):
    """按分组列出我记录的人物。一个人可以同时属于多个组。

    分组只从关系和人物属性里归纳出来，不新建一套社交档案：
    「小周是朋友」归朋友与同学，「小周是前同事」也让他出现在工作与业务。
    没有归入任何组的人放「未分组」，不代表不能办事。
    """
    rows = db.rows("SELECT * FROM person WHERE owner_id=? ORDER BY name", (owner_id,))
    out = {g: [] for g in PERSON_GROUPS}
    out["未分组"] = []
    for row in rows:
        if row["name"] == "我":
            continue
        item = _person_brief(row)
        groups = _groups_for_person(db, row)
        item["groups"] = groups
        if not groups:
            out["未分组"].append(item)
        else:
            for g in groups:
                out[g].append(item)
    return {g: v for g, v in out.items()}


def _person_brief(row):
    try:
        attributes = json.loads(row.get("attributes_json") or "{}")
    except (ValueError, TypeError):
        attributes = {}
    try:
        aliases = json.loads(row.get("aliases") or "[]")
    except (ValueError, TypeError):
        aliases = []
    return {"person_id": row["person_id"], "name": row["name"],
            "relation": row["relation"], "note": row.get("note", ""),
            "aliases": aliases, "attributes": attributes}


# 关系到分组的对应。朋友的孩子算朋友与同学：这段交往是朋友带来的，
# 但不因此变成我的亲属。
_RELATION_GROUP = {
    "spouse": ["家人与亲属"], "own_child": ["家人与亲属"], "family": ["家人与亲属"],
    "friend": ["朋友与同学"], "friend_child": ["朋友与同学"],
    "friend_spouse": ["朋友与同学"],
    "colleague": ["工作与业务"], "former_colleague": ["工作与业务"],
    "classmate": ["朋友与同学"],
}


# 送礼管家里的关系分类 → 侧栏分组。和 _RELATION_GROUP 分开：
# 前者是送礼场景内的私有档案，后者是家庭结构与社交关系，两层不混。
_GIFT_RELATION_GROUP = {
    "长辈": ["家人与亲属"], "晚辈": ["家人与亲属"], "同龄人": ["朋友与同学"],
    "朋友": ["朋友与同学"], "同事": ["工作与业务"], "亲人": ["家人与亲属"],
}


def _groups_for_person(db, row):
    """这个人在哪些组里。组可以重叠，去重后按固定顺序返回。

    分组来源有两个，都只做明确判断，不做推断：
    - relation 表里这个人和我的每一条关系。同一个人可以既是朋友、
      又是前同事，两条关系各自成立，他就同时出现在两个组里；
    - 职业属性。班主任、教练这类具体事务联系人归生活服务。

    备注里的背景（「通过小周认识」）不参与分组：那是相识过程，
    不是这个人和我的关系，也不该因此多出一个组。
    """
    # 送礼管家里的关系分类也参与分组，但读的是 attributes_json.gift_target，
    # 不写回 person.relation——那一列驱动家庭结构判断，混入送礼关系会让孩子数算错。
    found = set(_RELATION_GROUP.get(row["relation"], []))
    try:
        _attrs = json.loads(row.get("attributes_json") or "{}")
    except (ValueError, TypeError):
        _attrs = {}
    gift = _attrs.get("gift_target") if isinstance(_attrs, dict) else None
    if isinstance(gift, dict):
        for g in _GIFT_RELATION_GROUP.get(gift.get("relationship") or "", []):
            found.add(g)
    # 我和这个人之间的每条关系都算：一条记录只写一个关系角色，
    # 一个人可以同时是朋友和前同事，relation 表里就是两条。
    for r in db.rows(
            "SELECT relation_type FROM relation WHERE owner_id=?"
            " AND (from_person_id=? OR to_person_id=?)",
            (row["owner_id"], row["person_id"], row["person_id"])):
        for g in _RELATION_GROUP.get(r["relation_type"], []):
            found.add(g)
    try:
        attributes = json.loads(row.get("attributes_json") or "{}")
    except (ValueError, TypeError):
        attributes = {}
    job = str(attributes.get("职业") or "")
    if job:
        found.add("生活服务")
    return [g for g in PERSON_GROUPS if g in found]


def search_persons(db, owner_id, query):
    """按姓名、别名、关系或备注找我记录的人物。

    空 query 时返回全部。匹配是子串匹配，不做拼音和模糊纠错：
    搜不到就说搜不到，不猜一个相似的人。
    """
    rows = db.rows("SELECT * FROM person WHERE owner_id=? ORDER BY name", (owner_id,))
    q = (query or "").strip()
    out = []
    for row in rows:
        if row["name"] == "我":
            continue
        item = _person_brief(row)
        item["groups"] = _groups_for_person(db, row)
        if not q:
            out.append(item)
            continue
        hay = " ".join([row["name"], row["relation"], row.get("note", "")]
                       + item["aliases"]
                       + [str(k) + str(v) for k, v in item["attributes"].items()])
        if q in hay:
            out.append(item)
    return out


def overview(db, owner_id="A"):
    """首页侧栏用的本人视图。

    只返回这个身份自己的人物、关系和记录。之前这里是全量视图，
    普通接口一调就把三个演示家庭的人物、事实、授权全带出去，
    等于把别人的资料也读给了当前会话。全量视图另放 /api/dev/overview。
    """
    users = db.rows("SELECT * FROM demo_user WHERE owner_id=?", (owner_id,))
    persons = db.rows("SELECT * FROM person WHERE owner_id=? ORDER BY name", (owner_id,))
    for person in persons:
        try:
            person["attributes"] = json.loads(person.get("attributes_json") or "{}")
        except (ValueError, TypeError):
            person["attributes"] = {}
        try:
            person["aliases"] = json.loads(person.get("aliases") or "[]")
        except (ValueError, TypeError):
            person["aliases"] = []
        # 分组一起给前端：侧栏要按组列出，也支持同一个人在多个组里。
        person["groups"] = _groups_for_person(db, person)
    relations = db.rows(
        "SELECT r.*, f.name AS from_name, t.name AS to_name FROM relation r"
        " JOIN person f ON f.person_id=r.from_person_id"
        " JOIN person t ON t.person_id=r.to_person_id"
        " WHERE r.owner_id=? ORDER BY r.relation_id", (owner_id,))
    facts = db.rows(
        "SELECT f.*, po.name AS subject_name FROM fact f JOIN person po ON po.person_id=f.subject_id"
        " WHERE f.owner_id=? ORDER BY f.subject_id, f.observed_on DESC", (owner_id,))
    gifts = db.rows(
        "SELECT g.*, po.name AS subject_name FROM gift_record g JOIN person po ON po.person_id=g.subject_id"
        " WHERE g.owner_id=? ORDER BY g.happened_on DESC", (owner_id,))
    tasks = db.rows("SELECT * FROM task WHERE owner_id=? ORDER BY created_at DESC",
                    (owner_id,))
    orders = db.rows(
        "SELECT o.* FROM sim_order o JOIN task t ON t.task_id=o.task_id"
        " WHERE t.owner_id=? ORDER BY o.created_at DESC", (owner_id,))
    auths = db.rows("SELECT * FROM authorization WHERE owner_id=? ORDER BY scope_kind",
                    (owner_id,))
    conversations = db.rows(
        "SELECT * FROM conversation WHERE owner_id=? ORDER BY created_at DESC LIMIT 50",
        (owner_id,))
    return {
        "users": users, "persons": persons, "relations": relations, "facts": facts,
        "gifts": gifts, "tasks": tasks, "orders": orders, "authorizations": auths,
        "conversations": conversations,
    }


# 用户对已登记事项的后续反馈措辞。这些词说明在说「后来怎么了」，
# 不是在交办新的事，也不是在纠正记录。
FOLLOWUP_MARKERS = ("送到了", "送出去了", "收到了", "孩子很", "他很喜欢", "她很喜欢",
                    "很喜欢", "挺喜欢", "不喜欢", "没兴趣", "玩了一会", "玩了一会儿",
                    "反馈", "回来说", "跟我说", "后续", "后来")


def _recent_purchased(db, owner_id):
    """最近一条已登记模拟订单的事项，用来接用户的后续反馈。"""
    row = db.row(
        "SELECT task_id, utterance, outcome FROM task WHERE owner_id=?"
        " AND stage='purchased' ORDER BY created_at DESC LIMIT 1", (owner_id,))
    return row


def _is_pure_followup(text):
    """这句话只是在说已办事情后来的情况，没有提出新的要求。

    区分很重要：「送到了，孩子很喜欢」是反馈，要接回那条送礼记录；
    「小周说孩子很喜欢画画，给她买一套颜料，100元以内」带着新的购买目标，
    是新交办。用关键词单独判断会把后者误记成上一单的反馈，新任务就被吞掉了。

    判定规则：出现新的目标、预算、对象或明确的办事要求时，一律不算纯反馈。

    注意不能用单字判新目标：「送到了」里就有「送」字，拿它当交办标志会把
    最典型的反馈也挡掉。这里只认明确的办事措辞。
    """
    t = (text or "").strip()
    if not t:
        return False
    # 带新目标/新条件：这是交办，不是反馈。
    if any(m in t for m in NEW_TASK_MARKERS):
        return False
    # 至少要有反馈类措辞，否则空泛的一句不该被当成事后反馈。
    return any(m in t for m in FOLLOWUP_MARKERS)


# 明确的办事措辞。只用多字组合，避免「送到了」里的「送」被当成新交办。
NEW_TASK_MARKERS = ("再买", "再送", "再给", "再准备", "还要", "顺便", "另外",
                    "改买", "换成", "重新买", "补一个", "补一件",
                    "元以内", "元以下", "预算",
                    "帮我买", "给她买", "给他买", "给我买", "去买",
                    "准备个", "准备一个", "买一个", "买一套", "买套",
                    "生日礼物", "回礼", "还礼", "回送")


def _followup_candidates(db, owner_id):
    """所有已登记订单、且送礼记录还没有写反馈的那几件。

    只列还没记过反馈的：已经记过的再写会覆盖原来的话。
    """
    rows = db.rows(
        "SELECT t.task_id, t.utterance, o.order_id, o.product_name, g.gift_id,"
        " g.feedback, g.subject_id FROM task t"
        " JOIN sim_order o ON o.task_id=t.task_id"
        " JOIN gift_record g ON g.order_id=o.order_id"
        " WHERE t.owner_id=? AND t.stage='purchased'"
        " ORDER BY t.created_at DESC", (owner_id,))
    out = []
    for r in rows:
        if (r["feedback"] or "").strip():
            continue
        person = db.row("SELECT name FROM person WHERE person_id=?", (r["subject_id"],))
        out.append({
            "task_id": r["task_id"], "utterance": r["utterance"],
            "order_id": r["order_id"], "product_name": r["product_name"],
            "gift_id": r["gift_id"],
            "person_name": person["name"] if person else "",
        })
    return out


def _followup_recorded(db, owner_id):
    """已经写过反馈的那几件，按时间倒序。

    反馈全部记完时，用户再说「后来玩了一会就放一边了」是接着补充同一条记录，
    这时不能因为没有待记订单就把它当成新交办——那会让一句事后反馈
    凭空长出一件新事项。列出来是为了让用户点一下就说清是哪一件。
    """
    rows = db.rows(
        "SELECT t.task_id, t.utterance, o.order_id, o.product_name, g.gift_id,"
        " g.feedback, g.subject_id FROM task t"
        " JOIN sim_order o ON o.task_id=t.task_id"
        " JOIN gift_record g ON g.order_id=o.order_id"
        " WHERE t.owner_id=? AND t.stage='purchased'"
        " ORDER BY t.created_at DESC", (owner_id,))
    out = []
    for r in rows:
        if not (r["feedback"] or "").strip():
            continue
        person = db.row("SELECT name FROM person WHERE person_id=?", (r["subject_id"],))
        out.append({
            "task_id": r["task_id"], "utterance": r["utterance"],
            "order_id": r["order_id"], "product_name": r["product_name"],
            "gift_id": r["gift_id"], "feedback": r["feedback"],
            "person_name": person["name"] if person else "",
        })
    return out


def _match_followup_target(db, owner_id, text, candidates):
    """从候选里找出用户这句话说的是哪一件。

    返回 (matched, ambiguous)。
    用户提到具体的人或礼物时按它对；什么都没提且只有一件待记，就用那一件；
    提了但对不上、或同时命中多件，返回 None。

    「只有一件待记就直接用」有个前提：用户确实没有指名道姓。
    只剩小雨一单时，说「小宇收到了，他很喜欢」是在讲另一个人，
    这时必须问清楚，不能顺手写到小雨名下——那等于把反馈安到错误的人头上。
    """
    if not candidates:
        return None, False
    t = (text or "").strip()
    # 按人名匹配：家庭里两个孩子都可能收到礼物，说错人就是把反馈记到另一个人名下。
    by_person = [c for c in candidates if c["person_name"] and c["person_name"] in t]
    if len(by_person) == 1:
        return by_person[0], False
    if len(by_person) > 1:
        return None, True
    # 按商品名匹配。
    by_product = [c for c in candidates
                  if c["product_name"] and c["product_name"] in t]
    if len(by_product) == 1:
        return by_product[0], False
    if len(by_product) > 1:
        return None, True
    # 用户明确提到了我记录里的某个人，但那个人名下没有待记的订单：
    # 这是在说另一个人，不能因为只剩一单就顺势写过去。
    known_names = [r["name"] for r in db.rows(
        "SELECT name FROM person WHERE owner_id=? AND name!='我'", (owner_id,))]
    if any(name in t for name in known_names):
        return None, True
    # 用户没有指名：只有一件待记时才用，否则问清楚。
    if len(candidates) == 1:
        return candidates[0], False
    return None, True


def record_followup(db, owner_id, utterance):
    """把用户对已送礼物的事后反馈接到那条记录上。

    只做一件事：把这句话写进送礼记录的 feedback，并说清记到了哪一件事上。
    不新建任务、不改事实、不重新协商——那是另一类话该走的路。

    接哪一条要按用户说的内容判断，不能一律取最近一单。用户明确提到某个孩子、
    某件礼物时，必须对上那一件；对不上就问清楚，不往最近一单上硬写——
    否则给小宇的反馈会被记到小雨名下。
    """
    text = (utterance or "").strip()
    if not text:
        return None
    candidates = _followup_candidates(db, owner_id)
    if not candidates:
        # 反馈都记完了。用户接着说同一件事后来的情况（例如先「送到了，孩子很喜欢」，
        # 再「后来玩了一会就放一边了」），这是接着补充同一条记录，
        # 不是新的交办。这时要能指名补记，而不是落进新建任务、也不能覆盖原来的话。
        recorded = _followup_recorded(db, owner_id)
        if not recorded:
            return None
        matched_rec = None
        by_person = [c for c in recorded if c["person_name"] and c["person_name"] in text]
        if len(by_person) == 1:
            matched_rec = by_person[0]
        elif not by_person and len(recorded) == 1:
            # 用户没有指名，而记过的反馈只有一件：顺着接上就行。
            # 多数人不会每次都重说一次「小雨那件」，这里反问会把人卡住。
            # 前提还是用户没有提到别人：提了就对不上，按澄清走。
            known_other = [r["name"] for r in db.rows(
                "SELECT name FROM person WHERE owner_id=? AND name!='我'", (owner_id,))]
            if not any(n in text for n in known_other):
                matched_rec = recorded[0]
        else:
            by_product = [c for c in recorded
                          if c["product_name"] and c["product_name"] in text]
            if len(by_product) == 1:
                matched_rec = by_product[0]
        if matched_rec is None:
            # 没有唯一对应：说清有哪些可以补，让用户点一下，不猜也不另开事项。
            lines = "；".join(
                "%s（%s，已记：%s）" % (c["person_name"], c["product_name"], c["feedback"])
                for c in recorded[:5] if c["person_name"])
            return {"reply": ("这条是接着补充已登记的那件事吧？我不确定你说的是哪一件，"
                              "先不另开新的事项。你记过的有：" + lines
                              + "。说清是哪一件，或者直接点下面的事项卡打开详情。"),
                    "task_id": "", "order_id": "", "gift_id": "",
                    "candidates": recorded, "resolved": False,
                    "ambiguous": True, "appendable": True}
        db.execute(
            "UPDATE gift_record SET feedback=? WHERE gift_id=?",
            (matched_rec["feedback"].rstrip() + "\n" + text, matched_rec["gift_id"]))
        db.audit(matched_rec["task_id"], owner_id, "gift_feedback_appended",
                 {"order_id": matched_rec["order_id"], "gift_id": matched_rec["gift_id"],
                  "appended": text})
        reply = ("记下了，接着记到「%s」（%s）那件上：\n%s\n"
                 "下次再给同一个人准备礼物时，我会把它一起考虑进去。"
                 % (matched_rec["utterance"][:20], matched_rec["product_name"], text))
        return {"reply": reply, "task_id": matched_rec["task_id"],
                "order_id": matched_rec["order_id"], "gift_id": matched_rec["gift_id"],
                "product_name": matched_rec["product_name"]}
    matched, ambiguous = _match_followup_target(db, owner_id, text, candidates)
    if matched is None:
        # 说到了具体的人或礼物，但对不上任何一条已登记的订单：问清楚，不猜。
        names = "、".join(c["person_name"] for c in candidates[:5] if c.get("person_name"))
        return {"reply": ("我不敢把这条反馈随便记到某一单上。"
                          "你已经登记的有：" + names + "。"
                          "说清是给谁买的那件，我再记。"),
                "task_id": "", "order_id": "", "gift_id": "",
                "candidates": candidates, "resolved": False,
                "ambiguous": True}
    task = matched
    if task is None:
        return None
    order = db.row(
        "SELECT order_id, product_name FROM sim_order WHERE task_id=?"
        " ORDER BY created_at DESC LIMIT 1", (task["task_id"],))
    if order is None:
        return None
    gift = db.row(
        "SELECT gift_id FROM gift_record WHERE order_id=?", (order["order_id"],))
    if gift is None:
        return None
    db.execute("UPDATE gift_record SET feedback=? WHERE gift_id=?", (text, gift["gift_id"]))
    db.audit(task["task_id"], owner_id, "gift_feedback_recorded",
             {"order_id": order["order_id"], "gift_id": gift["gift_id"], "feedback": text})
    reply = ("记下了。这条说的是「%s」（%s）的事后情况：%s\n"
             "下次再给同一个人准备礼物时，我会把它一起考虑进去。"
             % (task["utterance"][:20], order["product_name"], text))
    return {"reply": reply, "task_id": task["task_id"], "order_id": order["order_id"],
            "gift_id": gift["gift_id"], "product_name": order["product_name"]}


def model_status(client):
    # 探活也要给够 token：本地代理上的 step 系列在很小的预算下会把 token
    # 用满后返回空 content（finish_reason 仍是 stop），只留 8 个 token
    # 会让明明可用的模型被报成连不上。给空回复不算连上，如实报失败。
    try:
        raw = client.chat([{"role": "user", "content": "回复：ok"}],
                          max_tokens=256, temperature=0)
        if not raw:
            return {"ok": False, "model": client.model, "base_url": client.base_url,
                    "error": "模型返回了空内容"}
        return {"ok": True, "model": client.model, "base_url": client.base_url,
                "sample": raw[:40]}
    except ModelUnavailable as exc:
        return {"ok": False, "model": client.model, "base_url": client.base_url, "error": str(exc)}


QUESTION_MARKERS = ("请确认", "是否", "能否", "能不能", "仍穿", "还穿", "还是",
                   "待确认", "未确认", "待核实", "吗")


def _is_question(text):
    """判断一句话是在提问还是在确认。提问里的取值不能当执行值。"""
    return any(marker in str(text) for marker in QUESTION_MARKERS)


def _order_spec(proposal, product):
    """从提案里取出最终尺码写进订单。

    优先用结构化字段 asked_size（双方明确给出的值）；它为空时才回退到解析条件，
    并且跳过「请确认目前仍穿31码」这类提问，避免把待核实的旧尺码写进订单。
    """
    if not product.get("has_size"):
        return ""
    structured = (proposal.get("asked_size") or "").strip()
    if structured:
        return structured
    raw = proposal.get("shared_conditions_json") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = [raw]
    try:
        options = json.loads(product["size_options"])
    except (ValueError, TypeError, KeyError):
        options = []

    def digits(text):
        return "".join(ch for ch in str(text) if ch.isdigit())

    found = []
    for condition in raw:
        # 提问里的尺码不是确认值：「请确认目前仍穿31码」不能当成选定 31 码。
        if _is_question(condition):
            continue
        cd = digits(condition)
        if not cd:
            continue
        for opt in options:
            od = digits(opt)
            if od and od in cd:
                found.append((condition, opt))
    if not found:
        return ""
    # 后出现的条件是更正，覆盖前面的旧值。
    return found[-1][1]

def save_turn(db, owner_id, role, text, task_id="", plan_id=""):
    """保存一轮对话，刷新后可接着看。

    plan_id 单独一列，不塞进 task_id：家庭活动是 plan 链上的事，
    混在一起会让「打开事项详情」拿 task_id 去查 task 表查不到，
    用户点开的是一条不存在的记录。旧库没有这一列时由 _init_schema 补。
    """
    db.execute(
        "INSERT INTO conversation(turn_id, owner_id, role, text, task_id, plan_id,"
        " created_at) VALUES(?,?,?,?,?,?,?)",
        (dbm.new_id("turn"), owner_id, role, text, task_id, plan_id, dbm.now_iso()))


def conversation(db, owner_id, limit=80):
    # 按插入顺序返回，最早在前；刷新后接着看时不会乱序。
    rows = db.rows(
        "SELECT * FROM conversation WHERE owner_id=? ORDER BY seq DESC LIMIT ?",
        (owner_id, limit))
    return list(reversed(rows))


def person_detail(db, owner_id, person_id):
    """人物详情：关系、有效事实、往来、待核实项。按 owner 视角过滤。"""
    person = db.row("SELECT * FROM person WHERE person_id=? AND owner_id=?", (person_id, owner_id))
    if person is None:
        return None
    try:
        person["attributes"] = json.loads(person.get("attributes_json") or "{}")
    except (ValueError, TypeError):
        person["attributes"] = {}
    try:
        person["aliases"] = json.loads(person.get("aliases") or "[]")
    except (ValueError, TypeError):
        person["aliases"] = []
    relations_out = db.rows(
        "SELECT r.*, f.name AS from_name, t.name AS to_name FROM relation r"
        " JOIN person f ON f.person_id=r.from_person_id"
        " JOIN person t ON t.person_id=r.to_person_id"
        " WHERE r.owner_id=? AND (r.from_person_id=? OR r.to_person_id=?)"
        " ORDER BY r.relation_id", (owner_id, person_id, person_id))
    related = []
    for r in relations_out:
        other_id = r["to_person_id"] if r["from_person_id"] == person_id else r["from_person_id"]
        other = db.row("SELECT * FROM person WHERE person_id=?", (other_id,))
        if other is not None:
            outgoing = r["from_person_id"] == person_id
            other_row = db.row("SELECT * FROM person WHERE person_id=?", (other_id,))
            try:
                other_attrs = json.loads((other_row or {}).get("attributes_json") or "{}")
            except (ValueError, TypeError):
                other_attrs = {}
            try:
                self_attrs = json.loads(person.get("attributes_json") or "{}")
            except (ValueError, TypeError):
                self_attrs = {}
            if outgoing:
                # 记录读作「我是对方的<关系>」，展示时反过来：对方是我的<逆关系>。
                shown = _inverse_relation(r["relation_type"], other_attrs, self_attrs)
            else:
                # 记录读作「对方是我的<关系>」，直接用。
                shown = r["relation_type"]
            # 对方就是「我」本人时，不要再拼「我是我的朋友」这种绕口说法。
            # 关系链上点开的是别人，这里说清这一环指向自己就行。
            if other["name"] == "我":
                spoken = "就是我本人"
            else:
                spoken = other["name"] + "是我的" + shown
            related.append({
                "person_id": other_id,
                "name": other["name"],
                "relation": shown,
                "direction": "out" if outgoing else "in",
                "spoken": spoken,
                "label": r["label"],
            })
    facts = db.rows(
        "SELECT * FROM fact WHERE owner_id=? AND subject_id=?"
        " ORDER BY observed_on DESC, fact_id", (owner_id, person_id))
    gifts = db.rows(
        "SELECT * FROM gift_record WHERE owner_id=? AND subject_id=?"
        " ORDER BY happened_on DESC", (owner_id, person_id))
    # 待核实：事实和属性里凡带「待核实/待确认」的都要提示，来源一并保留。
    pending = [f for f in facts
               if f["status"] == "active"
               and any(k in (f["value"] + f["source_utterance"])
                       for k in ("待核实", "未确认", "不确定", "待确认"))]
    for key, value in person["attributes"].items():
        if isinstance(value, str):
            text = value
        elif isinstance(value, list):
            text = "、".join(str(v) for v in value)
        else:
            text = str(value)
        if any(k in text for k in ("待核实", "未确认", "不确定", "待确认")):
            pending.append({
                "fact_id": "", "attribute": key, "value": text,
                "provider": "我记录的人物资料", "observed_on": "",
                "status": "active", "source_utterance": "",
                "is_attribute": True,
            })
    return {
        "person": person,
        "related": related,
        "facts": facts,
        "gifts": gifts,
        "needs_check": pending,
        # 授权近况：来自标注测试来源、这个查看者被授权看到的事件。
        # 同一个人可能从学习 App 和日常记录 App 各报一件事，按人聚合在一起看，
        # 但每条都保留自己的来源、时间和授权字段，不合并成一份说不清谁说的。
        #
        # 事件记在实际持有的那一侧：小雨的事件由家长名下的 App 上报，
        # 所以存在 B 侧的人物实体上。A 侧看同一个人时也要能取到家长授权分享的近况。
        # 因此两侧都查，按 event_id 合并；能不能看见仍由每条事件的授权决定，
        # 合并只是让人名对上，不放大可见范围。
        "life_events": _life_events_both_sides(db, owner_id, person_id),
    }


def _life_events_both_sides(db, owner_id, person_id):
    """同一现实人物在两侧的授权近况，按 event_id 合并去重。

    家庭里两边各存一份关于同一个人的记录。事件、事实都记在实际持有的一方，
    查询时两侧都要看，否则授权分享过去的内容在对方那边取不到。

    但两侧记录要能对应上，靠的是显式建立的人物关联（person_link），
    不是按名字匹配。两个用户记录里同名的人不一定是同一个人，
    按名字合并会把事件挂到另一个人头上。
    """
    row = db.row("SELECT name FROM person WHERE person_id=?", (person_id,))
    if row is None:
        return []
    ids = [person_id] + linked_person_ids(db, person_id)
    seen = {}
    for pid in ids:
        for e in life_events_for(db, pid, owner_id):
            if e['source_kind'] == 'connector':
                ev = db.row('SELECT * FROM life_event WHERE event_id=?', (e['event_id'],))
                e['fields'] = _connector_model_fields(db, ev, e['fields'])
                if not e['fields']:
                    continue
            seen[e["event_id"]] = e
    # 每条记录带上它的配图清单（不含 base64）。界面按 event_id 单独取图，
    # 服务端在那里校验身份。不在列表里直接塞图片数据，否则刷一次人物详情
    # 就把这个人名下所有配图全拉一遍，手机上又慢又费流量。
    for e in seen.values():
        e["attachments"] = event_attachment_index(db, e["event_id"], owner_id)
    return sorted(seen.values(),
                  key=lambda e: (e.get("happened_on") or "", e.get("event_id") or ""),
                  reverse=True)


def event_attachment_index(db, event_id, viewer_id):
    """这条记录的配图索引：只有 ID 和媒体类型，不带图片内容。"""
    return [{"attachment_id": r["attachment_id"], "media": r["media"],
             "created_at": r["created_at"]}
            for r in event_attachments(db, event_id, viewer_id)]


def link_persons(db, person_id_a, person_id_b, confirmed_by="", note=""):
    """显式建立「这两条记录是同一个人」。由本人确认，不按名字自动合并。"""
    if person_id_a == person_id_b:
        return ""
    a = db.row("SELECT owner_id FROM person WHERE person_id=?", (person_id_a,))
    b = db.row("SELECT owner_id FROM person WHERE person_id=?", (person_id_b,))
    if a is None or b is None:
        raise ValueError("关联的人物不存在")
    have = db.row(
        "SELECT link_id FROM person_link WHERE status='active' AND"
        " ((person_id_a=? AND person_id_b=?) OR (person_id_a=? AND person_id_b=?))",
        (person_id_a, person_id_b, person_id_b, person_id_a))
    if have is not None:
        return have["link_id"]
    link_id = dbm.new_id("plink")
    db.execute(
        "INSERT INTO person_link(link_id, person_id_a, person_id_b, confirmed_by, note,"
        " status, created_at) VALUES(?,?,?,?,?,?,?)",
        (link_id, person_id_a, person_id_b, confirmed_by, note, "active", dbm.now_iso()))
    db.audit("", confirmed_by, "person_linked",
             {"link_id": link_id, "person_id_a": person_id_a, "person_id_b": person_id_b})
    return link_id


def linked_person_ids(db, person_id):
    """这条记录显式关联到的另一侧人物 id。没有关联就返回空。"""
    out = []
    for r in db.rows(
            "SELECT person_id_a, person_id_b FROM person_link"
            " WHERE status='active' AND (person_id_a=? OR person_id_b=?)",
            (person_id, person_id)):
        other = r["person_id_b"] if r["person_id_a"] == person_id else r["person_id_a"]
        if other != person_id:
            out.append(other)
    return out


def _inverse_relation(relation_type, other_attrs, self_attrs):
    """把「我是对方的 X」翻成「对方是我的 Y」。

    亲属关系要按对方的情况定：小雨是小宇的妹妹，反过来小宇是哥哥还是姐姐，
    看小宇的性别与年龄。
    """
    table = {
        "父亲": "孩子", "母亲": "孩子", "儿子": "父母", "女儿": "父母",
        "哥哥": "弟弟或妹妹", "姐姐": "弟弟或妹妹",
        "弟弟": "哥哥或姐姐", "妹妹": "哥哥或姐姐",
        "配偶": "配偶", "朋友": "朋友", "同事": "同事", "同学": "同学",
        "亲戚": "亲戚",
    }
    base = table.get(relation_type, relation_type)
    if base == "孩子":
        return "女儿" if other_attrs.get("性别") == "女" else "儿子"
    if base == "父母":
        return "母亲" if other_attrs.get("性别") == "女" else "父亲"
    if base in ("哥哥或姐姐", "弟弟或妹妹"):
        elder = None
        try:
            elder = float(other_attrs.get("年龄", 0)) > float(self_attrs.get("年龄", 0))
        except (TypeError, ValueError):
            elder = None
        if base == "哥哥或姐姐":
            return ("哥哥" if elder else "姐姐") if elder is not None else "哥哥或姐姐"
        return ("弟弟" if not elder else "妹妹") if elder is not None else "弟弟或妹妹"
    if base == "弟弟或妹妹" or base == "哥哥或姐姐":
        return base
    return base


def _receipt_digest(db, task_id):
    """事项的代填回执摘要。只回答「有没有、现在算不算成功」，不带字段明细。

    明细走 task_receipts：它带页面实际读回值，是给本人核对用的。
    这里只给一个薄摘要，让卡片决定要不要显示「查看代填回执」入口。
    """
    rows = db.rows(
        "SELECT receipt_id, status, created_at FROM ext_receipt WHERE task_id=?"
        " ORDER BY created_at DESC", (task_id,))
    if not rows:
        return []
    return [{"receipt_id": r["receipt_id"], "status": r["status"],
             "created_at": r["created_at"]} for r in rows]


def _blocked_candidates(db, task_id, extracted):
    """对象不明时，取出可点选的候选人物。

    协商入口会把候选连同追问写进审计日志，那里是最可靠的来源；
    日志缺失（例如刚建任务还没跑协商）时回退到抽取结果里的候选。
    """
    audit = db.row(
        "SELECT detail_json FROM audit_log WHERE task_id=? AND action='blocked_need_subject'"
        " ORDER BY created_at DESC LIMIT 1", (task_id,))
    candidates = []
    if audit is not None:
        try:
            candidates = json.loads(audit["detail_json"]).get("candidates") or []
        except (ValueError, TypeError):
            candidates = []
    if not candidates:
        candidates = extracted.get("candidates") or []
    return candidates


def plans(db, owner_id):
    """多人事项列表：三家庭这类共同事项。

    和送礼事项分开列。这里只说清现在卡在谁、等什么，
    不把「还没回复」显示成已约定。
    """
    rows = db.rows("SELECT p.* FROM plan p WHERE p.owner_id=? OR EXISTS(SELECT 1 FROM plan_participant x WHERE x.plan_id=p.plan_id AND x.owner_id=? AND x.role<>'retired') ORDER BY p.created_at DESC LIMIT 20", (owner_id,owner_id))
    out = []
    for p in rows:
        st = plan_status(db, p["plan_id"])
        if st is None:
            continue
        out.append({
            "plan_id": st["plan_id"],
            "title": st["title"],
            "utterance": st["utterance"],
            "stage": st["stage"],
            "outcome": st["outcome"],
            "needs_replan": st["needs_replan"],
            "agreed": st["agreed"],
            "current_version": st["current_version"],
            "participants": [{"label": s["label"], "state": s["state"],
                              "required": s["required"], "reason": s["reason"]}
                             for s in st["participants"]],
            "waiting_on": st["waiting_on"],
        })
    return out


def plan_detail(db, plan_id, viewer=None):
    """事项详情：各方自己的条件、每一版方案、各家对每版的表态。

    viewer 是当前会话身份。给了 viewer 就按「这一事项的参与方」过滤：
    - 非参与方返回 None（接口层回 404，不暴露这件事存在不存在）；
    - 参与方只看得到自己这一方与其他各方的**本事项条件与表态**，
      看不到发起人那一侧没有授权给这件事的人物与家庭资料。
    「被拉进一件事」不等于「把整本人物档案授权给对方看」。
    """
    st = plan_status(db, plan_id)
    if st is None:
        return None
    if viewer is not None:
        mine = db.row("SELECT participant_id FROM plan_participant"
                      " WHERE plan_id=? AND owner_id=? AND role<>'retired'", (plan_id, viewer))
        if mine is None:
            return None
    proposals = db.rows(
        "SELECT * FROM plan_proposal WHERE plan_id=? ORDER BY version", (plan_id,))
    decisions = db.rows(
        "SELECT d.*, p.label FROM plan_decision d"
        " JOIN plan_participant p ON p.participant_id=d.participant_id"
        " WHERE d.plan_id=? ORDER BY d.created_at", (plan_id,))
    meta = _plan_meta(db, plan_id)
    fams = family_overview(db, viewer or st.get("owner_id") or "")
    selected = {p["owner_id"] for p in st["participants"]}
    fams["families"] = [f for f in fams["families"] if f["is_me"] or
                        (viewer in (None, st["owner_id"]) and _family_owner(db, f) in selected)]
    if viewer not in (None, st["owner_id"]):
        visible = {c["person_id"] for c in _own_children(db, viewer)}
        meta["child_attendance"] = {k: v for k, v in meta.get("child_attendance", {}).items() if k in visible}
    return {
        **st,
        # 家庭与孩子按已知关系推导的结果，界面照着显示，不自己编。
        # 参与方只看自己那一方与别家在本事项里的条件，人物档案不摊开。
        "families": fams["families"],
        "family_note": "演示里的 A/B/C 是三个身份、两个家庭：小周和配偶小林"
                       "是同一个家庭。真要更多家庭，哪一家没有记录就说哪一家未知，"
                       "不拿认识的人凑数。",
        # 目标家庭数、已选（候选）家庭、未知方缺口、孩子出席，刷新后都还在。
        "requested_families": meta.get("requested_families", 0),
        "invited": meta.get("invited", []),
        "unknown_families": meta.get("unknown_families", 0),
        "child_attendance": meta.get("child_attendance", {}),
        "versions": [{
            "version": r["version"], "proposer": r["proposer"],
            "content": json.loads(r["content_json"] or "{}"),
            "reason": r["reason"], "created_at": r["created_at"],
        } for r in proposals],
        "decisions": [{
            "label": r["label"], "version": r["version"],
            "decision": r["decision"], "reason": r["reason"],
            "changes": json.loads(r["changes_json"] or "{}"),
            # 来源单独给前端：本人确认和代表审阅要分开显示。
            "decided_by": r["decided_by"],
            "created_at": r["created_at"],
        } for r in decisions],
    }


def recover_group_activity(db, owner_id, task_id):
    """把一条被误当成送礼的事项，改走家庭活动。

    旧事项**不删**：它是用户真实说过的话和当时系统的错误回应，
    删了就没有历史，也说不清这件事为什么会转成活动。这里只把它标成
    已转移到哪一件活动上，用户点开旧的仍看得到原话。
    这件事本来就不该按礼物办，所以不给恢复入口以外的东西——
    尤其不再提示「点一个送礼对象」。
    """
    task = db.row("SELECT * FROM task WHERE task_id=? AND owner_id=?",
                  (task_id, owner_id))
    if task is None:
        return None
    started = start_group_activity(db, None, owner_id, task["utterance"])
    db.execute("UPDATE task SET stage=?, outcome=?, plan_id=? WHERE task_id=?",
               ("redirected",
                "这件事按家庭活动重新记下；原话保留，未删除。",
                started["plan_id"], task_id))
    save_turn(db, owner_id, "system",
              "已经按家庭活动重开了，原来的记录还在，没有删。",
              task_id=task_id, plan_id=started["plan_id"])
    return {"task_id": task_id, "plan_id": started["plan_id"],
            "reused": started["reused"], "reply": started["reply"]}


def matters(db, owner_id, limit=20):
    """事项列表：当前状态用普通语言描述，不把 pending 说成已约定。"""
    tasks = db.rows(
        "SELECT * FROM task WHERE owner_id=? ORDER BY created_at DESC LIMIT ?", (owner_id, limit))
    out = []
    for t in tasks:
        extracted = json.loads(t["extracted_json"] or "{}")
        subject = None
        if extracted.get("subject_id"):
            subject = db.row("SELECT * FROM person WHERE person_id=?",
                             (extracted["subject_id"],))
        orders = db.rows("SELECT * FROM sim_order WHERE task_id=? ORDER BY created_at", (t["task_id"],))
        proposals = db.rows(
            "SELECT * FROM proposal WHERE task_id=? ORDER BY version DESC", (t["task_id"],))
        # 对象不明时要把「缺什么、能点谁」一起带给前端，否则卡片只会说一句要补充。
        candidates = _blocked_candidates(db, t["task_id"], extracted) if t["stage"] == "blocked" else []
        out.append({
            "task_id": t["task_id"],
            "utterance": t["utterance"],
            "stage": t["stage"],
            "outcome": t["outcome"],
            "created_at": t["created_at"],
            "subject_name": subject["name"] if subject else extracted.get("subject_resolved_name"),
            "subject_hint": extracted.get("subject_hint") or "",
            "budget_max": extracted.get("budget_max"),
            "orders": orders,
            "latest_proposal": proposals[0] if proposals else None,
            "candidates": candidates,
            # 商品候选要和人物候选分开：前者是「还有哪些礼物可选」，
            # 后者是「送礼对象是谁」。卡片上两处都要能点，混在一起就说不清。
            "product_candidates": task_candidates(db, t["task_id"]),
            # 代填回执摘要也要跟着列表走：卡片在消息流里渲染，
            # 只给 /api/task/{id} 带的话，列表页看不到回执入口。
            "receipts": _receipt_digest(db, t["task_id"]),
            # 这句话本身是一场多家庭活动，不是给某一个人准备礼物。
            # 卡片照这一行决定给「改成家庭活动」还是给「点一个送礼对象」——
            # 后者对这句话是错的提示，正是用户明确不要的。
            "group_activity": group_activity_intent(t["utterance"]),
            "plan_id": t.get("plan_id") or "",
        })
    return out

def resolve_subject(db, client, task_id, person_id, extra_utterance=""):
    """用户补上对象后，重新抽取并继续协商。

    person_id 必须属于该任务的送礼者，避免用他人视角选人。
    """
    task = db.row("SELECT * FROM task WHERE task_id=?", (task_id,))
    if task is None:
        raise ValueError("任务不存在")
    person = db.row("SELECT * FROM person WHERE person_id=? AND owner_id=?",
                    (person_id, task["owner_id"]))
    if person is None:
        raise ValueError("这个人不在你的记录里")
    extracted = json.loads(task["extracted_json"] or "{}")
    if extra_utterance:
        merged = agents._extract_requirements(db, client,
                                              task["utterance"] + "。" + extra_utterance,
                                              task["owner_id"])
        extracted.update({k: v for k, v in merged.items() if v not in (None, [], "")})
    extracted["subject_id"] = person["person_id"]
    extracted["subject_name"] = person["name"]
    extracted["subject_resolved_name"] = person["name"]
    extracted["subject_hint"] = ""
    extracted["ambiguous_subject"] = False
    extracted["candidates"] = []
    db.execute("UPDATE task SET extracted_json=?, outcome='' WHERE task_id=?",
               (json.dumps(extracted, ensure_ascii=False), task_id))
    db.audit(task_id, task["owner_id"], "subject_resolved",
             {"person_id": person["person_id"], "name": person["name"],
              "extra": extra_utterance})
    save_turn(db, task["owner_id"], "system",
              "好，这次是给%s。" % person["name"], task_id=task_id)
    return {"task_id": task_id, "extracted": extracted}



# ---------------------------------------------------------------------------
# 浏览器插件：身份、配对、参数包与代填回执。
#
# 设计约束（见 _codex/artifacts/personal-ai-hackathon/app-extension-integration-spec.md）：
# 1. 普通应用请求由服务端会话决定用户，插件由设备凭证决定用户。
#    两者都不接受 URL、表单、模型输出或插件配置里的 owner_id/viewer_id。
# 2. 配对码随机、短期（默认 5 分钟）、一次兑换；设备凭证默认 24 小时，
#    可显式撤销。撤销后该凭证立即失效。
# 3. 「准备」冻结一份参数包快照；「校验」在写页面之前实时读最新约定版本核对；
#    「回执」由服务端复核实际读回值后才给 verified，客户端自己报的不算。
# 4. 只返回本次网页需要的、已授权的字段，不返回完整画像。
# ---------------------------------------------------------------------------

PAIR_CODE_TTL_MIN = 5
# 设备凭证长期有效，取消固定到期日。
#
# 用户明确要求「连接一次就一直连着」：关掉浏览器、隔天再用、
# 正常轮换都不该要求重新手输配对码。固定 24 小时到期会把内部期限
# 转嫁给用户，变成每天配对一次——那不是安全要求，是登录摩擦。
#
# 安全上没有放松：撤销仍然是即时的，清浏览器数据仍然结束连接。
# 取消到期只是让「没出过事的一台设备」不再因为时间到点被踢掉。
DEVICE_TOKEN_TTL_HOURS = 0          # 0 表示不设固定到期日
DEVICE_RENEW_AFTER_HOURS = 24 * 30  # 30 天在后台静默换发一次
SNAPSHOT_TTL_MIN = 5

# 参数包字段白名单：只有这些键能进参数包，且必须本次页面真的需要。
EXT_FIELD_WHITELIST = ("sku", "size", "color", "quantity",
                       "recipient_name", "recipient_phone", "delivery_address")
# 本期只交付购物适配器；买票、预订以后各自新增 action_type 和字段清单，
# 不通过任意模型脚本扩展权限。
EXT_ACTION_TYPES = ("shopping_fill",)

# 已批准的代填目标。本期只交付一个购物页面适配器，origin 逐个列出来，
# 不用通配：任意站点都能拿参数包，等于把收货资料送给没确认过的页面。
# 插件模拟商店默认跑在 9000 端口；主服务自己只在开发期列出。
EXT_ALLOWED_ORIGINS = {
    "http://127.0.0.1:9000",
    "http://localhost:9000",
}

_EXT_LABELS = {
    "sku": "商品", "size": "尺码", "color": "颜色", "quantity": "数量",
    "recipient_name": "收货人", "recipient_phone": "收货电话",
    "delivery_address": "收货地址",
}


class ExtError(Exception):
    """插件接口错误。code 用于插件判断怎么提示，message 是给人看的中文。"""

    def __init__(self, code, message, status=400, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable


def _origin_of(url):
    """取 origin：协议 + 主机 + 端口。解析不出来时返回原串，交给白名单判否。"""
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            return "%s://%s" % (parts.scheme, parts.netloc)
    except (ValueError, TypeError):
        pass
    return url.rstrip("/")


def _ext_expiry(minutes):
    return (datetime.now(timezone.utc).astimezone()
            + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _device_expiry():
    """设备凭证的有效期。

    正常情况不设到期日（返回空字符串），由撤销和清理浏览器数据来结束连接。
    想恢复固定期限时把 DEVICE_TOKEN_TTL_HOURS 设成正数即可，
    不必改这里的判断逻辑。
    """
    if DEVICE_TOKEN_TTL_HOURS <= 0:
        return ""
    return _ext_expiry(DEVICE_TOKEN_TTL_HOURS * 60)


def _ext_stale(value):
    return not value or value < dbm.now_iso()


def me(db, owner_id):
    """本人身份。应用每次打开都问一次，不把身份存在前端。

    demo_mode 明确告诉调用方这是本地演示账号，不是生产级认证。
    """
    row = db.row("SELECT owner_id, name, role, blurb FROM demo_user WHERE owner_id=?",
                 (owner_id,))
    if row is None:
        raise ExtError("no_such_user", "找不到这个演示账号", status=404)
    return {"user_id": row["owner_id"], "display_name": row["name"],
            "role": row["role"], "demo_mode": True}


def create_pair_code(db, owner_id, client_name=""):
    """本人点击「连接浏览器插件」时生成一次性配对码。"""
    code = secrets.token_hex(4).upper()
    db.execute(
        "INSERT INTO ext_pair_code(code, owner_id, client_name, issued_at, expires_at)"
        " VALUES(?,?,?,?,?)",
        (code, owner_id, (client_name or "")[:60], dbm.now_iso(), _ext_expiry(PAIR_CODE_TTL_MIN)))
    db.audit("", owner_id, "ext_pair_code_issued", {"client_name": client_name},
             owner_id=owner_id)
    return {"code": code, "expires_at": _ext_expiry(PAIR_CODE_TTL_MIN),
            "expires_in_sec": PAIR_CODE_TTL_MIN * 60}


def pair_device(db, owner_id, code, client_name=""):
    """兑换配对码，发出设备凭证。一次兑换，重复兑换失败。

    设备归属只由配对码记录推导：码是谁生成的，设备就归谁。
    调用方传的 owner_id 不参与推导——插件兑换时它的会话未必是本人会话，
    拿会话身份去比码会把「码有效但会话是另一个人」误判成无效码。
    """
    code = (code or "").strip().upper()
    client_id = dbm.new_id("ext")
    token = secrets.token_urlsafe(32)
    issued = dbm.now_iso()
    expires = _device_expiry()
    with db.connect() as conn:
        # 一次性兑换靠这条条件 UPDATE 保证。在事务外先查再写时，两个并发请求
        # 可能都读到「未消费」，于是换出两个凭证——一次兑换就成了两次。
        # SQLite 写事务串行，第二个请求这条 UPDATE 影响行数为 0，直接判失败。
        row = conn.execute(
            "SELECT owner_id, expires_at, consumed_at FROM ext_pair_code WHERE code=?",
            (code,)).fetchone()
        # 兑换失败不说「码不存在」和「码属于别人」的区别，避免用来试探有效码。
        if row is None:
            raise ExtError("pair_code_invalid", "配对码无效，请在应用里重新生成",
                           status=401)
        # 归属从码记录取，不比较调用方身份。
        owner_id = row["owner_id"]
        if row["consumed_at"]:
            raise ExtError("pair_code_used", "这个配对码已经用过了，请重新生成一个",
                           status=409)
        if _ext_stale(row["expires_at"]):
            raise ExtError("pair_code_expired", "配对码已过期，请重新生成",
                           status=409)
        cur = conn.execute(
            "UPDATE ext_pair_code SET consumed_at=? WHERE code=? AND consumed_at=''",
            (issued, code))
        if cur.rowcount != 1:
            raise ExtError("pair_code_used", "这个配对码已经用过了，请重新生成一个",
                           status=409)
        conn.execute(
            "INSERT INTO ext_device(client_id, owner_id, client_name, access_token,"
            " issued_at, expires_at) VALUES(?,?,?,?,?,?)",
            (client_id, owner_id, (client_name or "浏览器插件")[:60], token, issued, expires))
    db.audit("", owner_id, "ext_device_paired",
             {"client_id": client_id, "client_name": client_name, "expires_at": expires})
    return {"access_token": token, "client_id": client_id, "expires_at": expires,
            "token_type": "Bearer",
            "user": {"user_id": owner_id, "display_name": me(db, owner_id)["display_name"]}}


def _device_by_token(db, token):
    row = db.row("SELECT * FROM ext_device WHERE access_token=?", (token or "",))
    if row is None:
        raise ExtError("bad_credentials", "设备凭证无效，请重新连接浏览器插件",
                       status=401)
    if row["revoked_at"]:
        raise ExtError("device_revoked", "这个设备已经被撤销，请重新连接",
                       status=403)
    # 到期判断只看非空值：没设到期日的凭证不会因为「空字符串小于当前时间」
    # 被判成过期。_ext_stale 对空值的语义是「已过期」，这里必须自己判，
    # 否则长期连接一上线就被判死。
    if row["expires_at"] and row["expires_at"] < dbm.now_iso():
        raise ExtError("device_expired", "设备凭证已过期，请重新连接", status=403)
    return row


def _maybe_renew_device(db, row):
    """用得够久的设备在后台换发一次凭证，用户不感知。

    这是「一次连接持续使用」的另一半：即使将来为了轮换设了内部期限，
    续期也必须由服务端在后台完成，不能让用户隔一段时间就重新配对一次。
    换发只改 access_token，client_id 和 owner_id 不变，
    插件存的那一套连接关系继续有效。

    返回新 token；不需要续期时返回空字符串。失败不影响本次请求——
    续期是优化，不是前置条件。
    """
    if DEVICE_TOKEN_TTL_HOURS <= 0:
        return ""      # 长期凭证不需要轮换
    issued = row["issued_at"] or ""
    if not issued:
        return ""
    try:
        age_h = (datetime.now(timezone.utc).astimezone()
                 - datetime.fromisoformat(issued)).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return ""
    if age_h < DEVICE_RENEW_AFTER_HOURS:
        return ""
    new_token = secrets.token_urlsafe(32)
    now = dbm.now_iso()
    expires = _device_expiry()
    try:
        with db.connect() as conn:
            cur = conn.execute(
                "UPDATE ext_device SET access_token=?, issued_at=?, expires_at=?"
                " WHERE client_id=? AND revoked_at=''",
                (new_token, now, expires, row["client_id"]))
            if cur.rowcount != 1:
                return ""
        db.audit("", row["owner_id"], "ext_device_renewed",
                  {"client_id": row["client_id"], "expires_at": expires})
    except Exception:
        # 续期失败不阻塞本次请求：旧凭证还能用，下次再试。
        return ""
    return new_token


def device_from_request(db, authorization):
    """从 Authorization: Bearer 解析当前设备。凭证只从这里来。"""
    if not authorization:
        raise ExtError("bad_credentials", "缺少设备凭证", status=401)
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise ExtError("bad_credentials", "设备凭证格式不对", status=401)
    row = _device_by_token(db, parts[1].strip())
    # 续期后把新 token 挂在这行上，由调用方写进响应头。
    # 插件看到这个头就换掉本地存的那一个，用户全程无感。
    renewed = _maybe_renew_device(db, row)
    if renewed:
        row = dict(row)
        row["access_token"] = renewed
        row["renewed_token"] = renewed
    return row


def revoke_device(db, owner_id, client_id, device=None):
    """撤销设备。本人会话可撤销本人的任意设备；设备只能撤销自己。"""
    target = db.row("SELECT * FROM ext_device WHERE client_id=?", (client_id or "",))
    if target is None:
        raise ExtError("no_such_device", "找不到这个设备", status=404)
    if target["owner_id"] != owner_id:
        # 不是本人的设备：报不存在而不是「存在但你不许撤销」，
        # 否则 client_id 变成可枚举的探测面。
        raise ExtError("no_such_device", "找不到这个设备", status=404)
    if device is not None and device["client_id"] != client_id:
        # 设备凭证只能撤销自己：不能让一个设备把别的设备踢掉。
        raise ExtError("cannot_revoke_other_device",
                       "设备只能撤销自己，其他设备请在应用里撤销", status=403)
    if target["revoked_at"]:
        return {"revoked": True, "client_id": client_id, "already": True}
    db.execute("UPDATE ext_device SET revoked_at=?, revoked_by=? WHERE client_id=?",
               (dbm.now_iso(), owner_id, client_id))
    db.audit("", owner_id, "ext_device_revoked",
             {"client_id": client_id, "by": device["client_id"] if device else owner_id})
    return {"revoked": True, "client_id": client_id, "already": False}


def list_devices(db, owner_id, only_active=True):
    """本人的已配对设备。只给撤销要用的字段，不给凭证本身。

    返回凭证会让人把 token 抄走复用；这里只说明哪台设备、叫什么、
    有没有被撤销、什么时候过期，足够判断要不要撤销。

    only_active 默认只列当前还能用的：联调和验收会留下大量已撤销、
    已过期的设备，全部摊出来等于让人自己找哪一台还在生效。
    已处理的设备数量另外给出来，界面上说明「另有 N 台已撤销或已过期」。
    """
    rows = db.rows(
        "SELECT client_id, client_name, issued_at, expires_at, revoked_at"
        " FROM ext_device WHERE owner_id=? ORDER BY issued_at DESC", (owner_id,))
    today = dbm.now_iso()
    active, inactive = [], 0
    for r in rows:
        expired = bool(r["expires_at"]) and r["expires_at"] < today
        # 状态由服务端算，不让前端自己判断「过没过期」：
        # 两处算法不一致时，界面上会显示一个还能用其实已经不能用的设备。
        state = "revoked" if r["revoked_at"] else ("expired" if expired else "active")
        if state != "active":
            inactive += 1
            if only_active:
                continue
        active.append({
            "client_id": r["client_id"],
            "client_name": r["client_name"] or "浏览器插件",
            "issued_at": r["issued_at"],
            "expires_at": r["expires_at"],
            "revoked_at": r["revoked_at"],
            "state": state,
        })
    return {"devices": active, "inactive_count": inactive}


def extension_tasks(db, device):
    """本人可办事项摘要。只给判断能不能准备的字段，不含地址和档案。"""
    rows = db.rows(
        "SELECT * FROM task WHERE owner_id=? ORDER BY created_at DESC LIMIT 30",
        (device["owner_id"],))
    out = []
    for t in rows:
        stage = t["stage"]
        # 只有双方同版本接受之后才谈得上代填：未回应、反提案未接受、
        # 缺参数、模型失败的事项都还是待处理。
        can_prepare = stage in ("agreed", "purchased")
        blocked = ""
        if not can_prepare:
            blocked = _blocked_reason(db, t)
        out.append({"task_id": t["task_id"], "title": t["utterance"],
                    "status": stage, "can_prepare": can_prepare,
                    "blocked_reason": blocked})
    return {"tasks": out}


# ---------------------------------------------------------------------------
# 跨页面需求卡（POST /api/extension/context）。
#
# 和 prepare 的分工：prepare 是「要填进这个页面」，字段按白名单收窄到
# 收货资料和规格；context 是「这次要买什么」，摊开预算、用途、偏好、
# 排除项和待确认项，让用户换到另一个页面时不用把这件事重讲一遍。
#
# 两条硬边界：
#   1. 查看已知需求不以双方已约定为前提，但执行资格仍由 can_prepare/validate
#      决定——能复制需求不等于能代填；
#   2. 导出默认匿名：真实姓名、电话、地址、关系网、私密原因都不进 export_text。
#      只授权查看、没授权用于本次选购的字段，同样不外发。
# ---------------------------------------------------------------------------

_CONTEXT_PURPOSES = ("shopping_help",)

# 可进入需求卡的字段。按本次选购用途收窄，和 prepare 的白名单是两套：
# prepare 只管填进页面，这里还包括「买给谁、为什么买」这类用户能直接
# 拿去问人的内容。两者不共用一张清单，才不会出现「给代填用的电话
# 被复制到外部需求说明里」。
_CONTEXT_REQ_LABELS = {
    "occasion": "用途", "budget_max": "预算", "avoid_repeat": "避开重复",
    "category": "品类", "hard_constraints": "明确要求",
    "subject_label": "送给谁", "likes": "对方兴趣", "已有物品": "已有物品",
}

# 关系称呼。导出文本里不出现人名，只用关系词——
# 「朋友的孩子」能说清买给谁，又不会把姓名带给外部页面。
_CONTEXT_RELATION_TEXT = {
    "self": "我", "spouse": "配偶", "own_child": "我的孩子",
    "family": "亲属", "friend": "朋友", "friend_child": "朋友的孩子",
    "friend_spouse": "朋友的配偶", "colleague": "同事",
    "former_colleague": "前同事", "classmate": "同学", "neighbor": "邻居",
    "teacher": "老师", "other": "认识的人",
}


def _context_subject_label(db, extracted):
    """事项对象的称呼。没有解析出对象时返回空串，不猜。"""
    subject_id = extracted.get("subject_id") or ""
    if subject_id:
        row = db.row("SELECT name FROM person WHERE person_id=?", (subject_id,))
        if row is not None:
            return row["name"] or ""
    # 解析失败时退回用户原话里的称呼，界面另标出来源不确定。
    return extracted.get("subject_resolved_name") or extracted.get("subject_name") or ""


def _context_requirements(db, task, extracted, owner_id):
    """按 known/pending 摊开本次需求。返回 (requirements, missing_fields, sources)。

    state 的判定只看「有没有一个当前有效的值」：对方还没回应、没有来源、
    只有一句待核实的旧印象，都算 pending。把没确认的值写成 known，
    用户会拿它去下单，这正是这套机制要避免的事。
    """
    requirements = []
    missing = []
    sources = []
    subject_id = extracted.get("subject_id") or ""

    def add(key, value, state, source_label=""):
        requirements.append({
            "key": key, "label": _CONTEXT_REQ_LABELS.get(key, key),
            "value": "" if value is None else str(value),
            "state": state,
        })
        if state == "pending":
            missing.append(_CONTEXT_REQ_LABELS.get(key, key))
        elif source_label:
            sources.append({"field": key, "source_label": source_label})

    # 送礼对象：没定下来，其他需求都无从谈起。
    label = _context_subject_label(db, extracted)
    add("subject_label", label, "known" if label else "pending",
        "你这次说的人" if label else "")

    occasion = (extracted.get("occasion") or "").strip()
    add("occasion", occasion, "known" if occasion else "pending",
        "你这次说的场合" if occasion else "")

    budget = extracted.get("budget_max")
    if budget is None or budget == "":
        add("budget_max", "", "pending")
    else:
        add("budget_max", "%g 元" % float(budget), "known", "你这次说的预算")

    if extracted.get("avoid_repeat"):
        # 值写成一句明确要求；具体避开哪些由「已有物品」那一项说，
        # 不在这里把整份送礼历史摊开。
        add("avoid_repeat", "避开已经送过的东西", "known", "你这次说的要求")
    else:
        add("avoid_repeat", "没有要求避开重复", "known")

    category = (extracted.get("category") or "").strip()
    # pending 的项不留占位值。写「不限品类」会让插件把这个当确定规格展示，
    # 用户以为已经确认过——而 pending 的语义是「还不知道」。
    add("category", category, "known" if category else "pending",
        "你这次说的品类" if category else "")

    constraints = extracted.get("hard_constraints") or []
    if isinstance(constraints, str):
        try:
            constraints = json.loads(constraints)
        except (ValueError, TypeError):
            constraints = [constraints]
    constraints = [str(c).strip() for c in constraints if str(c).strip()]
    add("hard_constraints", "；".join(constraints),
        "known" if constraints else "pending",
        "你这次明确提的" if constraints else "")

    # 对方兴趣和已有物品来自对方授权给本人看的字段。
    # 没授权的一个字都不出现，不是「给了再藏」。
    b_subject = _context_b_side_person(db, subject_id)
    for attr, key in (("likes", "likes"), ("已有物品", "已有物品")):
        value, source_label = _context_shared_value(
            db, owner_id, b_subject, attr, subject_id)
        add(key, value, "known" if value else "pending", source_label)

    return requirements, missing, sources


def _context_shared_value(db, owner_id, b_subject_id, attr, a_subject_id):
    """取授权给本人看的当前有效值。返回 (值, 来源说明)。

    授权必须逐字段、逐个对象、按接收方、按用途、按时效全部对上，缺一样就不给：
    - 对象对不上，说明这条授权是写给别的孩子的；
    - 字段没列进去，说明对方只愿意分享别的字段；
    - 过期了，说明对方已经收回；
    - 备注没有写清可以用于本次选购或转述时，不能进 export_text——
      那是要把内容发给外部页面的，比本人看一眼走得更远。
      禁止字样优先，含糊用途一律不放行。

    先读家长一侧：那是对方自己维护的记录，来源清楚。读不到时才回退到本人
    这边同一个人的记录，并标明是本人记的。两侧都没有就返回空，
    由调用方标成 pending——不猜，也不拿别人的值顶上。
    """
    if b_subject_id and _context_share_allows(db, owner_id, b_subject_id, attr):
        # B 侧事实的持有者就是 B，不是凭证本人。用 owner_id 查这一侧
        # 永远读不到，于是一条授权直接落空、退回 A 自己的旧记录，
        # 界面还标着「家长记录」——那是把来源也写错了。
        row = db.row(
            "SELECT value, provider FROM fact"
            " WHERE owner_id='B' AND subject_id=? AND attribute=? AND status='active'"
            " ORDER BY observed_on DESC LIMIT 1", (b_subject_id, attr))
        if row is not None and (row["value"] or "").strip():
            return (row["value"] or "").strip(), "%s（家长记录）" % (
                row["provider"] or "家长记录")
    if a_subject_id:
        # 本人自己记的内容不需要额外授权：这是他自己的记录。
        # 但 owner 必须真的是凭证本人，不能拿固定值——否则 A 的会话
        # 会读到 B 名下同名人物的事实。
        row = db.row(
            "SELECT value, provider FROM fact"
            " WHERE owner_id=? AND subject_id=? AND attribute=? AND status='active'"
            " ORDER BY observed_on DESC LIMIT 1", (owner_id, a_subject_id, attr))
        if row is not None and (row["value"] or "").strip():
            return (row["value"] or "").strip(), "%s（我记的）" % (
                row["provider"] or "我的记录")
    return "", ""


def _context_exportable_preferences(db, owner_id, a_subject_id, b_subject_id):
    """可以安全外发的对方偏好。返回 (列表, 来源说明)。

    只取短值：兴趣、喜欢颜色、喜欢题材这类一两个词的，
    对方也写明了可以用于本次选购时才给。长句子（性格描述、家长本次要求）
    不带——那些里常有「因为父母刚离婚」这类私密原因，正则会漏。

    一条都没有时返回空，调用方如实标成缺项，不拿别的内容顶上。
    """
    prefs = []
    labels = []
    for owner, subject, tag in (("B", b_subject_id, "对方授权"),
                                ("A", a_subject_id, "本人记录")):
        if not subject:
            continue
        for attr, label in (("likes", "兴趣"), ("喜欢颜色", "喜欢的颜色"),
                            ("喜欢题材", "喜欢的题材")):
            if owner == "B":
                if not _context_share_allows(db, owner_id, subject, attr):
                    continue
            row = db.row(
                "SELECT value FROM fact WHERE owner_id=? AND subject_id=?"
                " AND attribute=? AND status='active' ORDER BY observed_on DESC LIMIT 1",
                (owner, subject, attr))
            value = (row["value"] or "").strip() if row else ""
            if not value or len(value) > 20:
                continue
            prefs.append("%s：%s" % (label, value))
            labels.append(tag)
    # 去重并保持顺序：同一句话不要出现两遍。
    seen = set()
    out = []
    for item in prefs:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out, ("、".join(sorted(set(labels))) if out else "")


def _context_excluded_items(db, owner_id, a_subject_id):
    """已经送过的东西，用于「避开重复」。返回 (列表, 来源说明)。

    只取商品名：那是结构化短值，能安全外发。
    不带 feedback——「母亲说她经常翻」是家里人说的话，不该出现在
    要贴到外部页面的文本里。
    """
    if not a_subject_id:
        return [], ""
    rows = db.rows(
        "SELECT product_name FROM gift_record WHERE owner_id=? AND subject_id=?"
        " AND status='delivered' ORDER BY happened_on DESC LIMIT 5",
        (owner_id, a_subject_id))
    items = []
    for r in rows:
        name = (r["product_name"] or "").strip()
        if name and name not in items:
            items.append(name)
    return items, ("我记的送礼记录" if items else "")


def _context_b_side_person(db, a_subject_id):
    """按显式人物关联找家长一侧同一个人。没有关联就返回空，不按名字兜底。

    为什么不能只按名字：三个演示家庭里都有叫「小雨」的孩子，
    按名字取 B 侧第一条就可能取到别人家的。人物关联（person_link）是
    两侧都确认过的对应关系，拿它才不会把 A 家的孩子和 B 家的孩子混成一个。

    没有关联就返回空：同名不同人是真实情况，按名字兜底会把 B 家孩子的
    记录当成 A 家孩子的需求发出去，正是「别混入别人的资料」要挡的事。
    退回空只是少一项需求、标成待确认，代价比串人大得多。
    """
    if not a_subject_id:
        return ""
    for other in linked_person_ids(db, a_subject_id):
        row = db.row("SELECT person_id FROM person WHERE person_id=? AND owner_id='B'",
                     (other,))
        if row is not None:
            return row["person_id"]
    return ""


def _context_share_allows(db, owner_id, b_subject_id, attr):
    """对方是否把 attr 这个字段授权给本人用于本次购物协助。

    授权记录记在对方名下（owner_id='B'），所以查的是 B 的 authorization。
    逐个字段判断：只授权了 liken 不等于也授权了已有物品。

    还要看授权写明了什么用途。备注里出现「仅供查看」「只能看」「不得外传」
    这类字样时，这个字段不能进 export_text 和 search_terms——那是要把内容
    发给外部页面的，比本人看一眼走得更远。宁可退回待确认，也不把查看权
    当成转述权。
    """
    today = dbm.now_iso()[:10]
    for a in db.rows(
            "SELECT * FROM authorization WHERE owner_id='B' AND scope_kind='share'",
            ()):
        if a["counterparty"] and a["counterparty"] != owner_id:
            continue
        if a["subject_id"] and a["subject_id"] != b_subject_id:
            continue
        if a["valid_until"] and a["valid_until"] < today:
            continue
        # 只有写明了可以用于本次选购或转述，才允许外发。
        # 缺这一条就不给：备注只写「允许查看」「用于活动安排」时，
        # 那是让对方知道，不是让对方把内容贴到外部页面。
        if not _context_allows_external_use(a["note"]):
            continue
        try:
            attrs = json.loads(a["attributes_json"] or "[]")
        except (ValueError, TypeError):
            continue
        if attr in attrs:
            return True
    return False


# 备注里出现这些字样，说明对方同意把这个字段用于本次选购或对外说明。
# 判断依据是备注文本：授权表没有单独的用途字段，备注是唯一写明范围的地方。
_CONTEXT_EXTERNAL_USE_MARKS = ("用于准备礼物", "用于选购", "用于购买", "用于这次选购",
                               "用于购物", "挑礼物", "准备礼物", "买礼物")

# 禁止优先于许可。备注写成「不得用于购物，仅供查看」时，
# 里面同样含「用于购物」这几个字，只做肯定式匹配会把它判成已许可——
# 而那正是最该被拒的一种写法。所以先查禁止，命中就直接拒绝。
_CONTEXT_DENY_MARKS = ("不得用于", "不可用于", "不能用于", "禁止用于", "仅本人可见",
                       "仅供查看", "仅查看", "只能看", "不得外传", "不可转述",
                       "不要转告", "别转告", "不得转述")


def _context_allows_external_use(note):
    """这条授权是否写明了可以把内容用于本次选购或对外转述。

    两步判断，禁止优先：
    1. 命中任何禁止字样，直接拒绝——哪怕同一句里也有许可字样；
    2. 必须命中许可字样才放行。没写用途、只写「允许查看」「用于活动安排」
       这类含糊说法的，一律不放行。

    不用单纯的否认清单：那列不全，漏一种就把隐私送出去了。
    这里是「禁止优先 + 必须显式许可」，两个方向都收。
    """
    text = (note or "")
    if any(mark in text for mark in _CONTEXT_DENY_MARKS):
        return False
    return any(mark in text for mark in _CONTEXT_EXTERNAL_USE_MARKS)


def _context_relation_word(db, extracted):
    """关系称呼，不返回人名，也不回退到原话提示。

    subject_hint 是用户原话里截出来的，可能直接带姓名（「给小周的女儿」）。
    把它当兜底写进导出文本，匿名就漏了。解析不出关系就用中性词。
    """
    subject_id = extracted.get("subject_id") or ""
    if subject_id:
        row = db.row("SELECT relation FROM person WHERE person_id=?", (subject_id,))
        if row is not None and row["relation"]:
            return _CONTEXT_RELATION_TEXT.get(row["relation"], "认识的人")
    return "认识的人"


def _context_export_text(db, owner_id, extracted, requirements):
    """可复制的对外需求说明。默认匿名。

    不出现真实姓名、电话、地址、关系网和私密原因：这段文字是要被用户
    粘到别的平台或问别人的，带上这些等于把家里的事一起送出去。
    「送给谁」用关系称呼，不用名字。

    外发的内容按「这个值本身安不安全」逐项判，不按字段名一刀切：
    - 预算、品类、避开重复：结构化，直接给；
    - 对方偏好：只取一两个词的短值，且对方写明了可用于本次选购；
    - 排除项：只取已送过的商品名，不带家里人说的反馈原话；
    - occasion / hard_constraints / 性格描述这类自由文本：不带。
      它们从用户原话截出来，可能夹带姓名、门牌、私密原因，正则会漏。

    这样「换页面不用重讲需求」才成立：预算、偏好、排除项都在，
    而不是只剩一句「预算 300 元」。
    """
    by_key = {r["key"]: r for r in requirements}
    who = _context_relation_word(db, extracted)
    subject_id = extracted.get("subject_id") or ""
    b_subject = _context_b_side_person(db, subject_id)
    lines = ["想给%s挑一件礼物。" % who]

    budget = (by_key.get("budget_max", {}).get("value") or "").strip()
    if budget:
        lines.append("预算：%s。" % budget)
    category = (by_key.get("category", {}).get("value") or "").strip()
    if category and category != "不限品类":
        lines.append("品类：%s。" % category)

    prefs, _ = _context_exportable_preferences(db, owner_id, subject_id, b_subject)
    if prefs:
        lines.append("对方偏好：%s。" % "；".join(prefs))

    if (by_key.get("avoid_repeat", {}).get("value") or "").startswith("避开"):
        excluded, _ = _context_excluded_items(db, owner_id, subject_id)
        if excluded:
            lines.append("请避开已经送过的：%s。" % "、".join(excluded))
        else:
            lines.append("已经送过一些东西，请避开重复。")
    pending = [r["label"] for r in requirements if r["state"] == "pending"]
    if pending:
        lines.append("还没定：%s。" % "、".join(pending))
    return "\n".join(lines)


# 可外发的只有结构化、可核对的购物条件。
#
# occasion / hard_constraints / likes / 已有物品都是用户原话或模型从原话里
# 截出来的自由文本，可能夹带姓名、电话、门牌、地名，或者「因为父母刚
# 离婚」这类不该外传的私密原因。用正则拦不住：列得再全也只是枚举已知
# 模式，漏一种就是把隐私送出去。
#
# 所以默认整段不进 export_text 和 search_terms，只保留下面这些字段。
# 用户确实想把某句带出去时，由他在本人界面上显式选择，不是系统替他决定。
_CONTEXT_STRUCTURED_EXPORT_KEYS = ("budget_max", "avoid_repeat", "category")


def _context_search_terms(db, owner_id, extracted, requirements):
    """可复制的搜索词。只用本次选购用得上的内容，不带人名和地址。

    和 export_text 同样保守：occasion / likes 是自由文本，默认不进。
    只有品类这种结构化取值直接可用。
    """
    by_key = {r["key"]: r for r in requirements}
    terms = []
    category = (by_key.get("category", {}).get("value") or "").strip()
    if category and category != "不限品类":
        terms.append(category)
    return terms


def _context_summary(task, extracted, subject_label):
    """给本人看的一句话：这件事现在到哪了、要买什么。"""
    stage = task["stage"]
    occasion = (extracted.get("occasion") or "").strip()
    what = ("给%s挑%s" % (subject_label, occasion)
            if subject_label and occasion else "这件事")
    text = {
        "purchased": "已经登记了订单，可以查看物流和回执",
        "agreed": "方案双方已经谈定，等你确认",
        "awaiting": "还在等对方回复",
        "counter_pending": "对方提了新方案，需要你先看",
        "conflict": "这次没谈成，需要重新协商",
        "blocked": "还缺一点信息",
        "model_failed": "刚才没连上模型",
    }.get(stage, "还在准备中")
    return "%s%s。" % (what, text)


def _context_revision(task, proposal, requirements):
    """内容版本号。同样的状态算出同样的号，状态一变号就变。

    用阶段、提案版本和每项需求的取值/状态拼摘要。
    不掺 generated_at：那个每次请求都变，掺进去版本号就失去
    「变没变」的含义，插件复制前的核对也就没意义了。
    """
    parts = [task["stage"], str((proposal or {}).get("version") or 0),
             str((proposal or {}).get("decision") or "")]
    for r in requirements:
        parts.append("%s=%s:%s" % (r["key"], r["state"], r["value"]))
    return "ctx_" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def extension_context(db, device, task_id, purpose):
    """跨页面需求卡：按当前事项、用途和授权，摊开这次要买什么。

    和 prepare 的关键区别：这里不要求双方已约定。未协商完的事项也能
    看已知需求和待确认项——用户常常是在还没谈成时就需要去别的页面
    问问「这个预算能买到什么」。执行资格另由 can_prepare / validate 管。
    """
    if purpose not in _CONTEXT_PURPOSES:
        raise ExtError("unsupported_purpose", "本期只支持购物协助这一种用途",
                       status=422)
    task = db.row("SELECT * FROM task WHERE task_id=?", (task_id or "",))
    # 归属校验：凭证是谁，就只能看谁的事项。
    if task is None or task["owner_id"] != device["owner_id"]:
        raise ExtError("no_such_task", "找不到这个事项，或它不属于你", status=404)

    # 本人身份只从凭证来。后面所有取值、人名替换都以它为准，
    # 不用固定值——用固定值会把 B 名下的记录读给 A。
    owner_id = device["owner_id"]
    extracted = json.loads(task["extracted_json"] or "{}")
    requirements, missing, sources = _context_requirements(
        db, task, extracted, owner_id)
    subject_label = _context_subject_label(db, extracted)
    proposal = db.row(
        "SELECT version, decision FROM proposal WHERE task_id=?"
        " ORDER BY version DESC LIMIT 1", (task_id,))
    can_prepare = task["stage"] in ("agreed", "purchased")
    return {
        "task_id": task_id,
        "purpose": purpose,
        "context_revision": _context_revision(task, proposal, requirements),
        "subject_label": subject_label,
        "task_status": task["stage"],
        "summary": _context_summary(task, extracted, subject_label),
        "requirements": requirements,
        "missing_fields": missing,
        "search_terms": _context_search_terms(
            db, owner_id, extracted, requirements),
        "export_text": _context_export_text(
            db, owner_id, extracted, requirements),
        "source_summary": sources,
        "generated_at": dbm.now_iso(),
        # 执行资格单独给：能看到需求不等于能代填。
        "can_prepare": can_prepare,
        "blocked_reason": "" if can_prepare else _blocked_reason(db, task),
    }


def _blocked_reason(db, task):
    stage = task["stage"]
    if stage == "conflict":
        return "这次没谈成，需要重新协商"
    if stage == "failed":
        return "这件事没办成"
    if stage == "model_failed":
        return "刚才没连上模型，等一会儿再试"
    if stage == "awaiting":
        return "还在等对方回复"
    if stage == "counter_pending":
        return "对方提了新方案，需要你先看"
    if stage == "blocked":
        return task["outcome"] or "还缺一点信息，需要你补充"
    if stage == "purchased":
        return ""
    return "这件事还在准备中"


def _snapshot_fields(db, task, proposal):
    """按字段白名单和页面需要挑出本次要填的值。

    只返回真正用得上的字段：收货资料必须有可用来源和本次使用授权，
    尺码必须属于正确的人和这件商品的规格，不能按年龄猜。
    """
    extracted = json.loads(task["extracted_json"] or "{}")
    subject_id = extracted.get("subject_id") or ""
    product = db.row("SELECT * FROM product WHERE product_id=?", (proposal["product_id"],))
    fields = []
    sources = []
    missing = []

    if product is None:
        missing.append("商品")
        return fields, sources, missing, subject_id, product

    fields.append({"key": "sku", "value": product["product_id"],
                   "label": _EXT_LABELS["sku"]})
    sources.append({"field": "sku", "source_label": "本次协商选定的商品",
                    "confirmed_at": proposal["created_at"]})

    if product["has_size"]:
        spec = _order_spec(proposal, product)
        if not spec:
            missing.append("尺码")
        else:
            fields.append({"key": "size", "value": spec, "label": _EXT_LABELS["size"]})
            # 尺码的来源要能追到家长一侧当前记录，不能写「按年龄估的」。
            b_subject = _b_side_person(db, subject_id)
            ok, evidence, _ = _size_evidence(db, b_subject, spec, product)
            sources.append({
                "field": "size",
                "source_label": evidence or "协商中明确给出的尺码" if ok else "",
                "confirmed_at": proposal["created_at"]})
            if not ok:
                missing.append("尺码依据")

    # 数量的来源：本期没有「用户明确说过要几件」的字段，页面默认一件，
    # 所以如实写「本次默认一件，待本人核对」，不拿协商时间冒充确认时间，
    # 也不写成已经核对过的既成事实。用户在预览里能改。
    fields.append({"key": "quantity", "value": "1", "label": _EXT_LABELS["quantity"]})
    sources.append({"field": "quantity",
                    "source_label": "本次默认一件，待你在预览里核对",
                    "confirmed_at": ""})

    # 收货资料：演示用虚构地址，且必须本次被授权使用。
    addr = _delivery_source(db, task)
    if addr is None:
        missing.append("收货地址")
    else:
        for key, attr in (("recipient_name", "name"), ("recipient_phone", "phone"),
                          ("delivery_address", "address")):
            value = addr.get(attr) or ""
            if not value:
                missing.append(_EXT_LABELS[key])
                continue
            fields.append({"key": key, "value": value, "label": _EXT_LABELS[key]})
            sources.append({"field": key, "source_label": addr.get("label") or "本人资料",
                            "confirmed_at": addr.get("confirmed_at") or ""})
    return fields, sources, missing, subject_id, product


# 收货三个字段在授权表里用的键。逐个字段判断：只授权了电话
# 不等于也授权了地址，不能一放全放。
_DELIVERY_ATTRS = (("name", "收货人"), ("phone", "收货电话"),
                   ("address", "收货地址"))
# 参数包字段名 -> 授权表里的键。两处必须一致，否则授权和放值对不上。
_EXT_LABEL_FOR = {"name": "收货人", "phone": "收货电话", "address": "收货地址"}
_DELIVERY_PURPOSE = "delivery"
# 本期收货授权不限具体对象；接收方留空表示给已批准网页的代填使用。
_DELIVERY_SUBJECT_ANY = ""
_DELIVERY_GRANTEE = ""


def _delivery_grants(db, owner_id):
    """本次代填用得上的收货授权，按字段给出。

    只认明确授权：scope_kind 必须是 delivery，未过期，且授权里写明了
    收货字段。空属性列表一律不放行——那不是「整体授权」，
    是一条没写清范围的记录，按它放出地址就是把资料送给没确认过的用途。
    """
    today = dbm.now_iso()[:10]
    allowed: set[str] = set()
    for a in db.rows("SELECT * FROM authorization WHERE owner_id=?", (owner_id,)):
        if a["scope_kind"] != _DELIVERY_PURPOSE:
            continue
        if a["valid_until"] and a["valid_until"] < today:
            continue
        # 授权限定了对象或接收方时，必须和本次一致。
        # 空表示不限；非空却不符，是写给别的用途或别的对象的，不能拿来用。
        if a["subject_id"] and a["subject_id"] != _DELIVERY_SUBJECT_ANY:
            continue
        if a["counterparty"] and a["counterparty"] not in ("", _DELIVERY_GRANTEE):
            continue
        try:
            scope = json.loads(a["attributes_json"] or "[]")
        except (ValueError, TypeError):
            continue
        keys = set(scope)
        if not keys:
            continue
        allowed |= {field for field, _ in _DELIVERY_ATTRS
                    if _EXT_LABEL_FOR[field] in keys}
    return allowed


def _delivery_source(db, task):
    """收货资料必须来自本人可核对的来源，并且逐字段有本次授权。

    演示资料是虚构的，明确标注出来。返回 {name, phone, address, label,
    confirmed_at}，其中没有授权的键值为空字符串，由调用方决定是缺字段
    还是跳过这个字段——不是这里替他决定。
    """
    person = db.row("SELECT * FROM person WHERE owner_id=? AND relation='self'",
                    (task["owner_id"],))
    if person is None:
        return None
    allowed = _delivery_grants(db, task["owner_id"])
    if not allowed:
        return None
    try:
        attrs = json.loads(person["attributes_json"] or "{}")
    except (ValueError, TypeError):
        attrs = {}
    out = {"label": "本人资料（演示用虚构地址）", "confirmed_at": ""}
    # 演示资料的确认时间如实标注：种子数据没有记录本人什么时候核对的，
    # 就不写一个时间。空字符串由界面显示「时间未知」，不显示成已核对。
    for field, attr in _DELIVERY_ATTRS:
        # 没有这个字段的授权就留空：宁可缺一项，也不能把没授权的值发出去。
        out[field] = (attrs.get(attr) or "") if field in allowed else ""
    # 三个字段一个都没授权到，等于没有可用来源。
    if not any(out.get(f) for f, _ in _DELIVERY_ATTRS):
        return None
    return out


def prepare_snapshot(db, device, task_id, action_type, target_origin):
    """准备参数包：冻结事项、约定版本、字段和来源，给用户预览。"""
    if action_type not in EXT_ACTION_TYPES:
        raise ExtError("unsupported_action",
                       "本期只支持购物页代填，这个操作类型还没实现", status=422)
    # 目标网页必须在批准的清单里。只检查是不是 http 开头不够：
    # 任意站点都能准备参数包，等于把收货资料送给一个没确认过的页面。
    # 契约 v1：target_origin 就是协议+主机+端口，客户端一直按这个传。
    # 这里不做单边协议变更：不因为后台想绑定整页就要求插件改传 URL。
    # 完整页面/tab/商品绑定由插件侧负责，后台只认 origin。
    raw = (target_origin or "").strip()
    origin = _origin_of(raw)
    # target_url 只作内部可选记录：调用方显式传了完整 URL 才存，
    # 不参与校验，也不允许因为没传就拒绝。
    target_url = raw if raw.rstrip("/") != origin else ""
    if origin not in EXT_ALLOWED_ORIGINS:
        raise ExtError("origin_not_allowed",
                       "这个网页还不能代填，本期只支持已批准的模拟商店页面",
                       status=422)
    task = db.row("SELECT * FROM task WHERE task_id=?", (task_id or "",))
    # 事项归属校验：凭证是谁，就只能准备谁的事项。不靠前端传 owner_id。
    if task is None or task["owner_id"] != device["owner_id"]:
        raise ExtError("no_such_task", "找不到这个事项，或它不属于你", status=404)

    proposal = db.row(
        "SELECT * FROM proposal WHERE task_id=? AND decision<>'stale'"
        " ORDER BY version DESC LIMIT 1", (task_id,))
    if proposal is None:
        raise ExtError("no_proposal", "这件事还没有可执行的方案，需要先协商", status=409)
    # 执行权：双方必须同版本接受。未回应的不能提前准备代填。
    if task["stage"] not in ("agreed", "purchased"):
        raise ExtError("not_agreed", _blocked_reason(db, task), status=409)
    if proposal["proposer"] == "B" and (proposal["decision"] or "") != "accept":
        raise ExtError("not_agreed", "这是对方的方案，还需要你先接受", status=409)
    if proposal["proposer"] == "A" and _counterparty_accept(db, task_id, proposal, "B") is None:
        raise ExtError("not_agreed", "对方还没有接受这一版方案", status=409)

    fields, sources, missing, subject_id, product = _snapshot_fields(db, task, proposal)
    extracted = json.loads(task["extracted_json"] or "{}")
    subject = db.row("SELECT name FROM person WHERE person_id=?", (subject_id,))
    snapshot_id = dbm.new_id("snap")
    created = dbm.now_iso()
    db.execute(
        "INSERT INTO ext_snapshot(snapshot_id, owner_id, client_id, task_id, action_type,"
        " target_origin, target_url, contract_version, proposal_version, subject_person_id,"
        " subject_label, product_id, fields_json, source_summary_json, missing_fields_json,"
        " can_fill, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (snapshot_id, device["owner_id"], device["client_id"], task_id, action_type,
         origin, raw, "1", proposal["version"], subject_id,
         (subject or {}).get("name") or "", proposal["product_id"],
         json.dumps(fields, ensure_ascii=False),
         json.dumps([s for s in sources if s.get("source_label")], ensure_ascii=False),
         json.dumps(missing, ensure_ascii=False), 0 if missing else 1, created,
         _ext_expiry(SNAPSHOT_TTL_MIN)))
    db.audit(task_id, device["owner_id"], "ext_snapshot_prepared",
             {"snapshot_id": snapshot_id, "client_id": device["client_id"],
              "proposal_version": proposal["version"], "missing": missing,
              "fields": [f["key"] for f in fields], "target_origin": origin})
    return {"contract_version": "1", "snapshot_id": snapshot_id, "task_id": task_id,
            "proposal_version": proposal["version"], "action_type": action_type,
            "target_origin": origin,
            "subject_label": (subject or {}).get("name") or "",
            "product": {"id": proposal["product_id"],
                        "title": (product or {}).get("name") or ""},
            "fields": fields, "source_summary": [s for s in sources if s.get("source_label")],
            "missing_fields": missing, "can_fill": not missing,
            "expires_at": _ext_expiry(SNAPSHOT_TTL_MIN)}


def _delivery_drift_reasons(db, task, snap):
    """收货字段相对快照漂移了没有。

    对快照里实际包含的每个收货字段，检查它现在仍然合法、且值没变。
    撤掉其中一项授权，或改了某个值，旧参数包都要失效：用户看到的是
    「参数过期，请重新准备」，而不是按已经撤回的资料填一单。

    注意不能只比「新的非空值」：撤掉电话授权时 name/address 仍有效，
    若跳过为空的 phone，旧包里带着电话照样放行——那正是要拦的情况。
    """
    now = _delivery_source(db, task)
    if now is None:
        return ["收货资料或它的授权已经不可用，请重新准备"]
    snap_fields = {f["key"]: str(f["value"])
                   for f in json.loads(snap["fields_json"] or "[]")}
    current = {"recipient_name": now.get("name") or "",
               "recipient_phone": now.get("phone") or "",
               "delivery_address": now.get("address") or ""}
    reasons = []
    for key, snap_value in snap_fields.items():
        if key not in current:
            continue
        value = current[key]
        if not value:
            # 快照里有这个字段，现在却没有合法值：授权被撤了或资料被清空。
            reasons.append("%s的授权或资料已经不可用，请重新准备"
                           % _EXT_LABELS.get(key, key))
        elif value != snap_value:
            reasons.append("%s已经变了，请重新准备" % _EXT_LABELS.get(key, key))
    return reasons


def _snapshot_stale_reasons(db, snap, target_origin, observed_product_id=""):
    """参数包在当前时刻还成立吗。

    validate（写页面之前）和 save_receipt（写回执）必须按同一套判断：
    两处各写一份，改了一处漏一处，就会出现「校验时有效、回执时失效」
    或者相反的漏洞。这个函数只读、不改任何业务状态。
    """
    reasons = []
    if _ext_stale(snap["expires_at"]):
        reasons.append("参数包已过期（默认 5 分钟），请重新准备")
    # v1 只比 origin：插件一直按协议传 origin，换站要拦，换页不拦
    # （页面和商品由插件侧绑定，后台不为此扩大协议）。
    if _origin_of(snap["target_origin"]) != _origin_of(target_origin):
        reasons.append("目标网页和准备时不一致，请重新准备")
    if snap["can_fill"] == 0:
        reasons.append("这个参数包还缺必填项：%s" % "、".join(
            json.loads(snap["missing_fields_json"] or "[]")))
    task = db.row("SELECT * FROM task WHERE task_id=?", (snap["task_id"],))
    # 取最新提案，不能先按 decision<>'stale' 过滤：最新一版如果刚被标记 stale
    # （依据的资料被纠正），过滤后就找不到它，stale 判断永远不成立，
    # 于是按已作废的方案照样能校验通过。
    proposal = db.row(
        "SELECT * FROM proposal WHERE task_id=? ORDER BY version DESC LIMIT 1",
        (snap["task_id"],))
    if task is None or proposal is None:
        reasons.append("这件事或方案已经不在了")
    else:
        # 约定版本必须还是准备时那一版：协商变了，旧包不能继续用。
        if proposal["version"] != snap["proposal_version"]:
            reasons.append("约定已经有新版本（当前第 %s 版，准备时是第 %s 版），"
                           "请重新准备" % (proposal["version"], snap["proposal_version"]))
        if (proposal["decision"] or "") == "stale":
            reasons.append("这个方案依据的资料已被更正，需要重新协商")
        if task["stage"] not in ("agreed", "purchased"):
            reasons.append("这件事当前状态不能代填：%s" % _blocked_reason(db, task))
        # 对方的反提案要本人先接受；本人方案要对方先接受同一版本。
        if proposal["proposer"] == "B" and (proposal["decision"] or "") != "accept":
            reasons.append("这是对方的方案，还需要你先接受")
        if proposal["proposer"] == "A" and \
                _counterparty_accept(db, snap["task_id"], proposal, "B") is None:
            reasons.append("对方还没有接受这一版方案")
        # 收货授权在准备之后可能被撤回，地址也可能被改：按当前授权重新算一遍，
        # 再和快照逐项比。只判断「还有没有来源」不够——撤掉地址授权、
        # 只留电话时旧包里仍带着地址，会照样放行。
        reasons.extend(_delivery_drift_reasons(db, task, snap))
    # 商品必须对得上：换了商品就不能按旧包填。空值不能当「跳过」——
    # 不读商品 ID 就放行，等于把这一步校验变成可选项。
    if observed_product_id is not None:
        if not (observed_product_id or "").strip():
            reasons.append("读不到页面上的商品，请重新打开商品页再试")
        elif observed_product_id != snap["product_id"]:
            reasons.append("页面上的商品和准备时不是同一件")
    return reasons


def _live_device(db, device):
    """回库复核这台设备当前还有效。

    validate 和 save_receipt 拿到的 device 可能是调用方早先解析出来的字典。
    那之后设备可能已被撤销或过期：不重新读库，一把已撤销的凭证仍能校验、
    仍能记回执。这里按凭证重新取一次，撤销和过期都在 _device_by_token 里判。
    """
    return _device_by_token(db, device.get("access_token") or "")


def validate_snapshot(db, device, snapshot_id, target_origin, observed_product_id,
                      user_confirmed):
    """写页面之前的校验。实时读最新约定版本，不信任旧包。"""
    device = _live_device(db, device)
    snap = db.row("SELECT * FROM ext_snapshot WHERE snapshot_id=?", (snapshot_id or "",))
    if snap is None or snap["owner_id"] != device["owner_id"]:
        raise ExtError("no_such_snapshot", "找不到这个参数包，请重新准备", status=404)
    if snap["client_id"] != device["client_id"]:
        raise ExtError("no_such_snapshot", "这个参数包不是你的设备生成的，请重新准备",
                       status=404)
    reasons = _snapshot_stale_reasons(db, snap, target_origin, observed_product_id)
    if not user_confirmed:
        reasons.append("需要你先看过对象、网页和参数并确认，才能代填")

    if reasons:
        db.audit(snap["task_id"], device["owner_id"], "ext_validate_rejected",
                 {"snapshot_id": snapshot_id, "reasons": reasons,
                  "client_id": device["client_id"]})
        return {"valid": False, "reasons": reasons, "validated_at": dbm.now_iso()}
    db.execute(
        "UPDATE ext_snapshot SET validated_at=?, validated_by=?, observed_product_id=?"
        " WHERE snapshot_id=?",
        (dbm.now_iso(), device["client_id"], observed_product_id or "", snapshot_id))
    db.audit(snap["task_id"], device["owner_id"], "ext_validated",
             {"snapshot_id": snapshot_id, "client_id": device["client_id"],
              "observed_product_id": observed_product_id or ""})
    return {"valid": True, "reasons": [], "validated_at": dbm.now_iso()}


def save_receipt(db, device, snapshot_id, target_origin, observed_fields,
                 client_status, idempotency_key):
    """保存代填回执。verified 由服务端复核后给出，客户端报的不算。"""
    device = _live_device(db, device)
    if client_status not in ("filled", "partial", "failed"):
        raise ExtError("bad_client_status",
                       "插件状态只能是 filled / partial / failed", status=422)
    key = (idempotency_key or "").strip()
    if not key:
        raise ExtError("missing_idempotency_key", "缺少幂等键，无法安全重试", status=422)
    snap = db.row("SELECT * FROM ext_snapshot WHERE snapshot_id=?", (snapshot_id or "",))
    if snap is None or snap["owner_id"] != device["owner_id"]:
        raise ExtError("no_such_snapshot", "找不到这个参数包，请重新准备", status=404)
    if snap["client_id"] != device["client_id"]:
        raise ExtError("no_such_snapshot", "这个参数包不是你的设备生成的，请重新准备",
                       status=404)
    if not snap["validated_at"]:
        raise ExtError("not_validated", "这个参数包还没有通过校验，不能记回执",
                       status=409)
    # 幂等只在「同一个设备、同一个参数包」内成立。先按全局 key 查重的话，
    # 别的设备猜到同一个 key 就能读到别人的回执；先校验主体和包再查重，
    # 既挡住越权，也让网络重试仍拿到同一份回执。
    have = db.row(
        "SELECT * FROM ext_receipt WHERE idempotency_key=? AND client_id=?"
        " AND snapshot_id=?", (key, device["client_id"], snapshot_id))
    if have is not None:
        # 重发的内容和第一次不一致时不能悄悄覆盖：说明这不是重试，
        # 是拿旧 key 报新结果。
        if json.loads(have["observed_fields_json"] or "{}") != {
                str(k): str(v) for k, v in (observed_fields or {}).items()
                if isinstance(k, str)} or have["client_status"] != client_status:
            raise ExtError("idempotency_conflict",
                           "这个幂等键已经用过了，且内容和上次不同，请换个键重试",
                           status=409)
        return _receipt_out(have)
    # 回执也要对着当前状态复核：validate 之后资料被纠正、授权被撤回、
    # 约定换了版本、参数包过期或填错页，都不能再记成 verified。
    # 和 validate 共用同一套判断，避免两处规则漂移。
    # 商品按 validate 时记下的那一件复核：回执本身不带商品 ID，
    # 但页面必须还是校验过的那一页那一件。
    stale = _snapshot_stale_reasons(db, snap, target_origin,
                                    snap["observed_product_id"])
    if stale:
        db.audit(snap["task_id"], device["owner_id"], "ext_receipt_rejected",
                 {"snapshot_id": snapshot_id, "reasons": stale,
                  "client_id": device["client_id"]})
        return {"receipt_id": "", "snapshot_id": snapshot_id, "task_id": snap["task_id"],
                "status": "stale", "client_status": client_status,
                "mismatches": [{"field": "", "label": r, "expected": "", "observed": ""}
                               for r in stale],
                "target_origin": target_origin or snap["target_origin"],
                "created_at": dbm.now_iso()}

    expected = {f["key"]: str(f["value"]) for f in
                json.loads(snap["fields_json"] or "[]")}
    observed = {str(k): str(v) for k, v in (observed_fields or {}).items()
                if isinstance(k, str)}
    # 只核对该包涉及的全部预期字段；插件没读回的字段算缺失，不算相符。
    mismatches = []
    for key_, want in expected.items():
        got = observed.get(key_, "")
        if got != want:
            mismatches.append({"field": key_, "label": _EXT_LABELS.get(key_, key_),
                               "expected": want, "observed": got})
    extra = [k for k in observed if k not in expected]

    if client_status == "failed":
        status = "failed"
    elif mismatches or extra or client_status != "filled":
        status = "partial"
    else:
        status = "verified"

    receipt_id = dbm.new_id("rcpt")
    db.execute(
        "INSERT INTO ext_receipt(receipt_id, snapshot_id, owner_id, client_id, task_id,"
        " target_origin, observed_fields_json, client_status, status, mismatches_json,"
        " idempotency_key, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (receipt_id, snapshot_id, snap["owner_id"], device["client_id"], snap["task_id"],
         target_origin or snap["target_origin"],
         json.dumps(observed, ensure_ascii=False), client_status, status,
         json.dumps(mismatches, ensure_ascii=False), key, dbm.now_iso()))
    db.audit(snap["task_id"], device["owner_id"], "ext_receipt_saved",
             {"receipt_id": receipt_id, "snapshot_id": snapshot_id, "status": status,
              "client_status": client_status, "mismatches": mismatches,
              "missing": [m["field"] for m in mismatches], "extra": extra,
              "client_id": device["client_id"]})
    return _receipt_out(db.row("SELECT * FROM ext_receipt WHERE receipt_id=?", (receipt_id,)))


def _receipt_out(row):
    """把一条回执摊给调用方看。

    observed_fields 必须带上：本人界面要逐字段核对页面实际读回值，
    只给 status 的话，verified 只是服务端结论，本人看不到依据。
    这些键就是参数包白名单里的字段，插件没有别的字段可漏出。
    """
    return {"receipt_id": row["receipt_id"], "snapshot_id": row["snapshot_id"],
            "task_id": row["task_id"], "status": row["status"],
            "client_status": row["client_status"],
            "mismatches": json.loads(row["mismatches_json"] or "[]"),
            "observed_fields": json.loads(row["observed_fields_json"] or "{}"),
            "target_origin": row["target_origin"],
            "created_at": row["created_at"]}


def task_receipts(db, owner_id, task_id):
    """应用侧读某个事项的回执。只能读本人的事项的回执。"""
    task = db.row("SELECT owner_id FROM task WHERE task_id=?", (task_id or "",))
    if task is None or task["owner_id"] != owner_id:
        raise ExtError("no_such_task", "找不到这个事项，或它不属于你", status=404)
    rows = db.rows(
        "SELECT * FROM ext_receipt WHERE task_id=? ORDER BY created_at DESC", (task_id,))
    return {"task_id": task_id, "receipts": [_receipt_out(r) for r in rows]}


# Local matter capabilities: not an account or proof of a named contact identity.
PARTICIPATION_NOTE = "邀请参与者本人自称，未核验实名；仅限本事项，不继承联系人资料"


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _unfinished_plan(db, plan_id):
    p = db.row("SELECT * FROM plan WHERE plan_id=?", (plan_id,))
    if not p or p['stage'] in ('closed', 'completed', 'cancelled'):
        raise ValueError('事项不存在或已完成')
    return p


def _member(db, plan_id, actor):
    p = _unfinished_plan(db, plan_id)
    side = db.row("SELECT * FROM plan_participant WHERE plan_id=? AND owner_id=? AND role<>'retired'", (plan_id, actor))
    if not side:
        raise ValueError('不是本事项参与方')
    return p, side


def invalidate_plan_dependencies(db, plan_id, reason):
    p = db.row('SELECT stage FROM plan WHERE plan_id=?', (plan_id,))
    if not p or p['stage'] in ('completed', 'closed', 'cancelled'):
        return
    db.execute("UPDATE plan SET needs_replan=1,stage='awaiting',outcome=? WHERE plan_id=?", (reason + '，请核对后重新排方案', plan_id))
    db.execute("UPDATE plan_decision SET decision='stale',reason=? WHERE plan_id=?", (reason + '，旧接受不再有效', plan_id))


def invitation_create(db, actor, plan_id, label):
    import time
    if _unfinished_plan(db, plan_id)['owner_id'] != actor:
        raise ValueError('只有发起人能邀请')
    label = str(label).strip()
    if not label or len(label) > 40:
        raise ValueError('邀请称呼须1–40字')
    token, iid = secrets.token_urlsafe(32), dbm.new_id('inv')
    guest = 'guest_' + secrets.token_hex(16)
    expires = time.time() + 7200
    db.execute('INSERT INTO plan_invitation(invitation_id,plan_id,issuer,token_hash,label,guest_id,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)', (iid, plan_id, actor, _digest(token), label, guest, expires, dbm.now_iso()))
    return {'invitation_id': iid, 'token': token, 'expires_at': expires,
            'participate_url': '/static/participate.html#token=' + token,
            'identity_note': PARTICIPATION_NOTE}


def invitations_for(db, actor, plan_id):
    if _unfinished_plan(db, plan_id)['owner_id'] != actor:
        raise ValueError('只有发起人能查看邀请')
    return db.rows('SELECT invitation_id,label,state,guest_id,participant_id,claimed_name,approved,expires_at FROM plan_invitation WHERE plan_id=?', (plan_id,))


def invitation_accept(db, token, display_name):
    import time
    name = str(display_name).strip()
    if not name or len(name) > 40 or len(token) > 200:
        raise ValueError('请填写本人自称（1–40字）')
    with db.atomic():
        row = db.row('SELECT * FROM plan_invitation WHERE token_hash=?', (_digest(token),))
        if not row or row['state'] != 'pending' or row['expires_at'] <= time.time():
            raise ValueError('邀请无效、已用、已撤回或过期')
        _unfinished_plan(db, row['plan_id'])
        # BEGIN IMMEDIATE serializes claims across threads/processes.
        db.execute("UPDATE plan_invitation SET state='claimed',claimed_name=? WHERE invitation_id=? AND state='pending'", (name, row['invitation_id']))
        pid = add_plan_participant(db, row['plan_id'], row['guest_id'], '自称：' + name + '（未核验）', {}, True, role='guest_pending')
        db.execute('UPDATE plan_invitation SET participant_id=? WHERE invitation_id=?', (pid, row['invitation_id']))
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute('INSERT INTO plan_guest_session(session_hash,invitation_id,plan_id,guest_id,csrf_token,expires_at) VALUES(?,?,?,?,?,?)', (_digest(session), row['invitation_id'], row['plan_id'], row['guest_id'], csrf, time.time() + 86400))
        invalidate_plan_dependencies(db, row['plan_id'], '有自称参与者接受邀请，待发起人认可')
    return {'session_token': session, 'csrf_token': csrf, 'plan_id': row['plan_id'], 'guest_id': row['guest_id'], 'participant_id': pid, 'identity_note': PARTICIPATION_NOTE}


def matter_session(db, token):
    import time
    row = db.row('SELECT s.*,i.state,i.approved,i.participant_id FROM plan_guest_session s JOIN plan_invitation i ON i.invitation_id=s.invitation_id WHERE s.session_hash=?', (_digest(token),))
    if not row or row['revoked'] or row['state'] == 'revoked' or row['expires_at'] <= time.time():
        raise ValueError('事项会话失效，请联系发起人')
    _member(db, row['plan_id'], row['guest_id'])
    return row


def invitation_approve(db, actor, plan_id, iid, replace=''):
    with db.atomic():
        if _unfinished_plan(db, plan_id)['owner_id'] != actor:
            raise ValueError('只有发起人能认可本人自称')
        inv = db.row('SELECT * FROM plan_invitation WHERE invitation_id=? AND plan_id=?', (iid, plan_id))
        if not inv or inv['state'] != 'claimed':
            raise ValueError('请先由接收者明确接受邀请')
        if inv['approved']:
            return {'ok': True}
        if replace:
            old = db.row('SELECT * FROM plan_participant WHERE participant_id=? AND plan_id=?', (replace, plan_id))
            if (not old or old['owner_id'] == actor or old['role'] != 'family' or old['status'] not in ('pending','candidate') or json.loads(old['conditions_json']) or json.loads(old['attendance_json']) or db.row('SELECT 1 FROM plan_decision WHERE participant_id=?', (replace,))):
                raise ValueError('仅可明确替换没有条件、孩子出席和表态的候选槽')
            db.execute("UPDATE plan_participant SET role='retired',required=0,status='retired' WHERE participant_id=?", (replace,))
        db.execute('UPDATE plan_invitation SET approved=1 WHERE invitation_id=?', (iid,))
        db.execute("UPDATE plan_participant SET role='guest',status='confirmed' WHERE participant_id=?", (inv['participant_id'],))
        invalidate_plan_dependencies(db, plan_id, '发起人认可本事项自称参与方（非实名核验）')
    return {'ok': True}


def invitation_revoke(db, actor, plan_id, iid):
    with db.atomic():
        if _unfinished_plan(db, plan_id)['owner_id'] != actor:
            raise ValueError('只有发起人能撤回邀请')
        inv = db.row('SELECT * FROM plan_invitation WHERE invitation_id=? AND plan_id=?', (iid, plan_id))
        if not inv:
            raise ValueError('邀请不存在')
        db.execute("UPDATE plan_invitation SET state='revoked',approved=0 WHERE invitation_id=?", (iid,))
        db.execute('UPDATE plan_guest_session SET revoked=1 WHERE invitation_id=?', (iid,))
        if inv['participant_id']:
            db.execute("UPDATE plan_participant SET role='guest_revoked',conditions_json='{}',attendance_json='{}' WHERE participant_id=?", (inv['participant_id'],))
        invalidate_plan_dependencies(db, plan_id, '参与邀请已撤回')
    return {'ok': True}


def guest_condition(db, session, conditions, children, children_count):
    if isinstance(children_count, bool) or (children_count is not None and (not isinstance(children_count, int) or not 0 <= children_count <= 20)) or not isinstance(children, list) or len(children)>20 or (children_count is not None and len(children) != children_count):
        raise ValueError('请本人确认0–20个孩子的名单与人数')
    kids, names = [], set()
    for i, c in enumerate(children):
        if not isinstance(c, dict) or set(c) != {'name', 'attendance'} or not isinstance(c['name'], str) or not c['name'].strip() or len(c['name']) > 40 or c['name'].strip() in names or c['attendance'] not in ('coming','not_coming','unknown'):
            raise ValueError('孩子仅包含本人自称姓名与出席，不能指定他人person_id')
        names.add(c['name'].strip())
        kids.append({'name': c['name'].strip(), 'attendance': c['attendance']})
    cleaned, unknown, confirm = normalize_conditions(conditions)
    if unknown or confirm:
        raise ValueError('条件字段或预算口径不明确')
    with db.atomic():
        live = matter_session(db, session['raw_token'])
        _, side = _member(db, live['plan_id'], live['guest_id'])
        db.execute('UPDATE plan_participant SET conditions_json=?,attendance_json=? WHERE participant_id=?', (json.dumps(cleaned, ensure_ascii=False), json.dumps({'guest_children': kids, 'children_count': children_count, 'source': PARTICIPATION_NOTE}, ensure_ascii=False), side['participant_id']))
        invalidate_plan_dependencies(db, live['plan_id'], '本人自称参与方更新自己的条件或孩子出席')
    return {'ok': True}


def participation_plan(db, session):
    st = plan_status(db, session['plan_id'])
    mine = next(s for s in st['participants'] if s['owner_id'] == session['guest_id'])
    side = db.row('SELECT attendance_json FROM plan_participant WHERE participant_id=?', (mine['participant_id'],))
    att = json.loads(side['attendance_json'])
    # No utterance, contacts, raw meta, owner source references or attachments.
    out = {k: st[k] for k in ('plan_id','title','proposal','proposal_reason','current_version','participants','waiting_on','agreed','needs_replan','confirmation_gaps')}
    out['kind'] = db.row('SELECT kind FROM plan WHERE plan_id=?', (session['plan_id'],))['kind']
    out['versions'] = [{'version': r['version'], 'content': json.loads(r['content_json']), 'reason': r['reason']} for r in db.rows('SELECT * FROM plan_proposal WHERE plan_id=? ORDER BY version', (session['plan_id'],))]
    out['decisions'] = db.rows('SELECT participant_id,version,decision,reason,decided_by FROM plan_decision WHERE plan_id=?', (session['plan_id'],))
    comp = (json.loads(db.row('SELECT companions_json FROM plan_participant WHERE participant_id=?', (mine['participant_id'],))['companions_json'] or '{}') or {}).get('companions') or []
    out.update(me={**mine, 'children': att.get('guest_children', []), 'children_count': att.get('children_count'), 'companions': comp}, approved=bool(session['approved']), csrf_token=session['csrf_token'], identity_note=PARTICIPATION_NOTE)
    return out


def _note_fields(ev):
    payload = json.loads(ev['payload_json'] or '{}')
    excluded = {'model_transcribed','image_b64','attachments','voice_text','input_kinds'}
    return {k: v for k, v in payload.items() if isinstance(k,str) and 0 < len(k) <= 60 and k not in excluded and isinstance(v,(str,int,float)) and len(str(v)) <= 4000}


def note_fields(db, actor, event_id):
    ev = db.row("SELECT * FROM life_event WHERE event_id=? AND owner_id=? AND status='active'", (event_id, actor))
    if not ev:
        raise ValueError('资料不存在、非本人或已失效')
    return {'event_id': event_id, 'fields': _note_fields(ev), 'provider': ev['provider'], 'happened_on': ev['happened_on']}


def linkable_plans(db, actor):
    return [p for p in plans(db, actor) if p['stage'] not in ('closed','completed','cancelled')]


def _source_fingerprint(ev):
    return _digest(json.dumps({k: ev[k] for k in ('event_id','payload_json','status','superseded_by')}, ensure_ascii=False, sort_keys=True))


def _remove_published(db, link):
    side = db.row('SELECT * FROM plan_participant WHERE plan_id=? AND owner_id=?', (link['plan_id'], link['owner_id']))
    if not side:
        return
    cond = json.loads(side['conditions_json'])
    for k,v in json.loads(link['public_conditions_json']).items():
        if cond.get(k) == v:
            cond.pop(k, None)
    db.execute('UPDATE plan_participant SET conditions_json=? WHERE participant_id=?', (json.dumps(cond, ensure_ascii=False), side['participant_id']))


def link_event_to_plan(db, actor, event_id, plan_id, selected_fields, public_conditions=None, publish_confirmed=False, replace_link_id=''):
    with db.atomic():
        _member(db, plan_id, actor)
        if actor.startswith('guest_'):
            raise ValueError('受邀事项会话无个人资料库权限')
        data = note_fields(db, actor, event_id)
        fields = data['fields']
        source_event = db.row('SELECT * FROM life_event WHERE event_id=?', (event_id,))
        if source_event['source_kind'] == 'connector' and selected_fields:
            approved = _connector_model_fields(db, source_event, fields)
            if any(k not in approved for k in selected_fields):
                raise ValueError('连接器字段尚未单独授权用于建议；请在数据接入页逐项授权')
        if not isinstance(selected_fields, list) or len(selected_fields)>20 or any(not isinstance(k,str) or k not in fields for k in selected_fields) or len(set(selected_fields)) != len(selected_fields):
            raise ValueError('只能选择记录中已有的具体字段')
        public_conditions = public_conditions or {}
        if not isinstance(public_conditions, dict):
            raise ValueError('公开条件必须是对象')
        cleaned, unknown, confirm = normalize_conditions(public_conditions)
        if not isinstance(public_conditions,dict) or unknown or confirm or (cleaned and not publish_confirmed):
            raise ValueError('公开条件须本人再次明确核对，且字段和预算口径合法')
        # Published values must be selected source strings (not a fabricated body payload).
        values = {str(fields[k]).strip() for k in selected_fields if k != '正文'}
        if any(str(v).strip() not in values for v in cleaned.values()):
            raise ValueError('公开条件只能来自本次明确选中的资料字段；正文需先更正为具体字段')
        ev = db.row('SELECT * FROM life_event WHERE event_id=?', (event_id,))
        existing = db.row('SELECT * FROM event_plan_link WHERE event_id=? AND plan_id=? AND owner_id=?', (event_id, plan_id, actor))
        if replace_link_id:
            old = db.row('SELECT * FROM event_plan_link WHERE link_id=? AND owner_id=? AND plan_id=?', (replace_link_id,actor,plan_id))
            oldev = db.row('SELECT * FROM life_event WHERE event_id=?', ((old or {}).get('event_id',''),))
            if not old or not oldev or oldev['superseded_by'] != event_id:
                raise ValueError('新来源不是这条旧引用的更正记录')
            _remove_published(db, old)
            db.execute("UPDATE event_plan_link SET status='superseded' WHERE link_id=?", (replace_link_id,))
        for other in db.rows("SELECT * FROM event_plan_link WHERE plan_id=? AND owner_id=? AND status='active'", (plan_id,actor)):
            if not existing or other['link_id'] != existing['link_id']:
                if set(json.loads(other['public_conditions_json'])) & set(cleaned):
                    raise ValueError('同一公开条件不能同时由两条来源覆盖')
        selection = json.dumps(sorted(selected_fields),ensure_ascii=False)
        pub = json.dumps(cleaned,ensure_ascii=False,sort_keys=True)
        fingerprint = _source_fingerprint(ev)
        if existing and existing['status']=='active' and existing['selected_fields_json']==selection and existing['public_conditions_json']==pub and existing['source_fingerprint']==fingerprint:
            return {'link_id': existing['link_id'], 'created': False}
        if existing:
            _remove_published(db,existing)
            lid = existing['link_id']
            db.execute("UPDATE event_plan_link SET selected_fields_json=?,public_conditions_json=?,source_fingerprint=?,status='active' WHERE link_id=?", (selection,pub,fingerprint,lid))
        else:
            lid = dbm.new_id('plnk')
            db.execute('INSERT INTO event_plan_link(link_id,event_id,plan_id,owner_id,selected_fields_json,public_conditions_json,source_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?)', (lid,event_id,plan_id,actor,selection,pub,fingerprint,dbm.now_iso()))
        if cleaned:
            side = db.row('SELECT * FROM plan_participant WHERE plan_id=? AND owner_id=?', (plan_id,actor))
            cond = json.loads(side['conditions_json']); cond.update(cleaned)
            db.execute('UPDATE plan_participant SET conditions_json=? WHERE participant_id=?', (json.dumps(cond,ensure_ascii=False),side['participant_id']))
        invalidate_plan_dependencies(db,plan_id,'本人更新所选资料引用或明确公开的条件')
    return {'link_id':lid,'created':not bool(existing)}


def unlink_event_from_plan(db,actor,event_id,plan_id):
    with db.atomic():
        _member(db,plan_id,actor)
        link=db.row('SELECT * FROM event_plan_link WHERE event_id=? AND plan_id=? AND owner_id=?',(event_id,plan_id,actor))
        if not link:
            raise ValueError('没有本人的这条引用')
        _remove_published(db,link)
        db.execute("UPDATE event_plan_link SET status='unlinked' WHERE link_id=?",(link['link_id'],))
        invalidate_plan_dependencies(db,plan_id,'本人撤回资料用于事项')
    return {'ok':True}


def invalidate_event_plans(db,event_id):
    for link in db.rows('SELECT * FROM event_plan_link WHERE event_id=? AND status=\'active\'',(event_id,)):
        plan=db.row('SELECT stage FROM plan WHERE plan_id=?',(link['plan_id'],))
        if plan and plan['stage'] not in ('completed','closed','cancelled'):
            _remove_published(db,link)
            db.execute("UPDATE event_plan_link SET status='stale' WHERE link_id=?",(link['link_id'],))
            invalidate_plan_dependencies(db,link['plan_id'],'所引用资料更正或撤回，待本人重新核对')


def linked_plan_events(db,plan_id,actor):
    if not db.row("SELECT 1 FROM plan_participant WHERE plan_id=? AND owner_id=? AND role<>'retired'",(plan_id,actor)):
        raise ValueError('不是本事项参与方')
    latest=db.row('SELECT provenance_json FROM plan_proposal WHERE plan_id=? ORDER BY version DESC LIMIT 1',(plan_id,))
    used=json.loads((latest or {}).get('provenance_json') or '{}').get(actor,[])
    out=[]
    for link in db.rows('SELECT * FROM event_plan_link WHERE plan_id=? AND owner_id=? ORDER BY created_at',(plan_id,actor)):
        ev=db.row('SELECT * FROM life_event WHERE event_id=?',(link['event_id'],))
        active=ev and ev['status']=='active' and link['status']=='active'
        payload=_note_fields(ev) if active else {}
        selection=json.loads(link['selected_fields_json'])
        out.append({'link_id':link['link_id'],'event_id':link['event_id'],'source_label':ev['source_id'] if ev else '', 'provider':ev['provider'] if ev else '', 'happened_on':ev['happened_on'] if ev else '', 'fields':{k:payload[k] for k in selection if k in payload},'status':link['status'] if link['status']!='active' else (ev or {}).get('status','missing'), 'replacement_event_id':(ev or {}).get('superseded_by') or '', 'used':active and not (db.row('SELECT needs_replan FROM plan WHERE plan_id=?',(plan_id,)) or {}).get('needs_replan') and any(x['link_id']==link['link_id'] and x['fingerprint']==link['source_fingerprint'] for x in used), 'public_conditions':json.loads(link['public_conditions_json']) if active else {}})
    return out


def create_plan_with_note(db,actor,title,event_id,selected_fields,public_conditions=None,publish_confirmed=False):
    with db.atomic():
        pid=create_plan(db,actor,title,'本人从所选资料创办家庭活动')
        add_plan_participant(db,pid,actor,'我家',{},True)
        result=link_event_to_plan(db,actor,event_id,pid,selected_fields,public_conditions,publish_confirmed)
    return {**result,'plan_id':pid}


def plan_dependency_snapshot(db,plan_id):
    # Full source/selection/conditions/membership/versions snapshot prevents late writes.
    pieces={t:db.rows('SELECT * FROM '+t+' WHERE plan_id=?',(plan_id,)) for t in ('plan','plan_participant','plan_proposal','plan_decision','plan_invitation','plan_guest_session','event_plan_link')}
    pieces['sources']=[db.row('SELECT * FROM life_event WHERE event_id=?',(l['event_id'],)) for l in pieces['event_plan_link']]
    return _digest(json.dumps(pieces,sort_keys=True,ensure_ascii=False))


def plan_provenance(db,plan_id):
    out={}
    for link in db.rows("SELECT l.* FROM event_plan_link l JOIN life_event e ON e.event_id=l.event_id WHERE l.plan_id=? AND l.status='active' AND e.status='active'",(plan_id,)):
        public=json.loads(link['public_conditions_json'])
        side=db.row('SELECT conditions_json FROM plan_participant WHERE plan_id=? AND owner_id=?',(plan_id,link['owner_id']))
        cond=json.loads((side or {}).get('conditions_json') or '{}')
        if public and all(cond.get(k)==v for k,v in public.items()):
            out.setdefault(link['owner_id'],[]).append({'link_id':link['link_id'],'event_id':link['event_id'],'fingerprint':link['source_fingerprint'],'field_keys':[k for k in json.loads(link['selected_fields_json']) if str(_note_fields(db.row('SELECT * FROM life_event WHERE event_id=?',(link['event_id'],))).get(k,'')).strip() in {str(v).strip() for v in public.values()}]})
    return out


def plan_model_guard(fn):
    """Call model without a DB lock; acquire one and recheck before any result writes."""
    import functools
    from contextlib import ExitStack
    @functools.wraps(fn)
    def wrapped(db,client,plan_id,*args,**kwargs):
        snapshot=plan_dependency_snapshot(db,plan_id)
        with ExitStack() as stack:
            class GuardClient:
                def chat_json(self,*a,**kw):
                    result=client.chat_json(*a,**kw)
                    stack.enter_context(db.atomic())
                    if snapshot!=plan_dependency_snapshot(db,plan_id):
                        raise ValueError('等待期间事项条件、资料或参与授权已变化，请核对后重试')
                    import time
                    for g in db.rows("SELECT s.expires_at,s.revoked,i.state FROM plan_guest_session s JOIN plan_invitation i ON i.invitation_id=s.invitation_id JOIN plan_participant p ON p.participant_id=i.participant_id WHERE s.plan_id=? AND p.role='guest'",(plan_id,)):
                        if g['expires_at']<=time.time() or g['revoked'] or g['state']=='revoked':
                            # Revoked guests are left as confirmation gaps, not active model actors.
                            raise ValueError('参与授权已过期或撤回，不能落迟到方案')
                    return result
            return fn(db,GuardClient(),plan_id,*args,**kwargs)
    return wrapped


def _atomic_lifecycle(fn):
    import functools
    @functools.wraps(fn)
    def wrapped(db,*args,**kwargs):
        with db.atomic():
            return fn(db,*args,**kwargs)
    return wrapped

revoke_life_event = _atomic_lifecycle(revoke_life_event)
correct_life_event = _atomic_lifecycle(correct_life_event)
update_plan_condition = _atomic_lifecycle(update_plan_condition)
decide_plan = _atomic_lifecycle(decide_plan)


# Connectors import privately; model use requires explicit per-record field consent.
CONNECTOR_FIELDS = ('进度', '已完成项目', '发生日期', '对象')
CONNECTOR_MAX_BYTES = 512 * 1024
CONNECTOR_CATALOG = {
    'school': {'name': '学校通知', 'description': '带来通知事项与日期；不带来成绩或孩子评价。', 'item': '周六图书角整理通知'},
    'course': {'name': '课程表', 'description': '带来课程项目与日期；不带来实际出席或学习效果。', 'item': '周六美术课'},
    'todo': {'name': '待办清单', 'description': '带来待办与进度；不代表已执行或他人承诺。', 'item': '整理绘本：完成一半'},
    'wishlist': {'name': '购物愿望单', 'description': '带来愿望项目与日期；不带来库存、价格或购买授权。', 'item': '希望借阅拼图绘本'},
}


def _connector(db, owner, cid, active=True):
    row = db.row('SELECT * FROM app_connector WHERE connector_id=? AND owner_id=?', (cid, owner))
    if not row or (active and row['revoked']):
        raise ValueError('连接器不存在、不是本人的或已撤回')
    return row


def _connector_url(base_url, path):
    base, tail = urlsplit(base_url), urlsplit(path)
    if base.scheme not in ('http', 'https') or not base.hostname:
        raise ValueError('接口地址只允许 http 或 https')
    if base.username or base.password or base.query or base.fragment or tail.scheme or tail.netloc or tail.query or tail.fragment or not path.startswith('/'):
        raise ValueError('地址不能含凭证、查询或片段；路径须以 / 开头且不能切换主机')
    try:
        host = ipaddress.ip_address(base.hostname)
    except ValueError:
        host = None
    if host and not host.is_global and not host.is_loopback:
        raise ValueError('不能访问内网、链路本地或云元数据地址；本地演示仅允许回环地址')
    return base_url.rstrip('/') + path


def connector_create(db, owner, kind, name, base_url='', path='', field_map=None, auth_token=''):
    if kind not in ('self_app', 'third_party') or not isinstance(name, str) or not 0 < len(name.strip()) <= 60:
        raise ValueError('请选择连接器类型并填写 1–60 字名称')
    if auth_token:
        raise ValueError('本演示不接收或保存凭证；请提供无需凭证的演示接口')
    if kind == 'self_app':
        _connector_url(base_url, path)
        if not isinstance(field_map, dict) or not field_map or len(field_map) > 4 or any(not isinstance(k, str) or not 0 < len(k) <= 80 or not isinstance(v, str) or v not in CONNECTOR_FIELDS for k, v in field_map.items()) or len(set(field_map.values())) != len(field_map):
            raise ValueError('请提供不重复的外部字段 → 进度、已完成项目、发生日期、对象映射')
    else:
        if path not in CONNECTOR_CATALOG:
            raise ValueError('请选择内置模拟连接器')
        base_url, field_map = '', {'progress': '进度', 'completed': '已完成项目', 'date': '发生日期', 'subject': '对象'}
    cid = dbm.new_id('connector')
    db.execute('INSERT INTO app_connector(connector_id,owner_id,kind,name,base_url,path,field_map_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
               (cid, owner, kind, name.strip(), base_url, path, json.dumps(field_map, ensure_ascii=False), dbm.now_iso()))
    return cid


def connector_demo_items(db, owner):
    subject = db.row("SELECT person_id FROM person WHERE owner_id=? ORDER BY CASE WHEN name='我' THEN 0 ELSE 1 END,person_id LIMIT 1", (owner,))
    return [{'id': 'progress-demo-1', 'progress': '读完两本绘本', 'completed': '数学练习 20 题', 'date': '2026-10-17', 'subject': '我' if subject else ''}]


def _connector_fetch(db, row):
    if row['kind'] == 'third_party':
        item = connector_demo_items(db, row['owner_id'])[0]
        item.update(id=row['path']+'-demo-1', progress=CONNECTOR_CATALOG[row['path']]['item'], completed='示例项目（模拟）')
        return [item]
    url = _connector_url(row['base_url'], row['path'])
    try:
        # Resolve once and reject non-public network targets; loopback is explicit local-demo allowance.
        import socket
        for info in socket.getaddrinfo(urlsplit(url).hostname, urlsplit(url).port or (443 if url.startswith('https:') else 80), type=socket.SOCK_STREAM):
            address = ipaddress.ip_address(info[4][0])
            if not address.is_global and not address.is_loopback:
                raise ValueError('域名指向内网或链路本地地址，不能接入')
        with httpx.Client(timeout=httpx.Timeout(5.0), follow_redirects=False, trust_env=False) as client:
            import time
            deadline = time.monotonic() + 10
            with client.stream('GET', url, headers={'Accept': 'application/json'}) as response:
                if response.status_code != 200:
                    raise ValueError('接口返回 HTTP %d（不跟随重定向）' % response.status_code)
                media = response.headers.get('content-type', '').split(';')[0].strip().lower()
                if media != 'application/json' and not media.endswith('+json'):
                    raise ValueError('接口必须返回 JSON Content-Type')
                chunks, length = [], 0
                for chunk in response.iter_bytes():
                    length += len(chunk)
                    if length > CONNECTOR_MAX_BYTES:
                        raise ValueError('接口响应超过 512 KiB 上限')
                    if time.monotonic() > deadline:
                        raise ValueError('接口响应总读取超过 10 秒')
                    chunks.append(chunk)
                data = json.loads(b''.join(chunks))
    except httpx.TimeoutException as exc:
        raise ValueError('接口请求超时（每段等待最多 5 秒）') from exc
    except (httpx.RequestError, OSError) as exc:
        raise ValueError('接口连接失败：%s；请检查服务是否启动及地址' % type(exc).__name__) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError('接口响应不是可解析的 JSON') from exc
    if isinstance(data, dict):
        data = data.get('items')
    if not isinstance(data, list) or len(data) > 100 or any(not isinstance(x, dict) for x in data):
        raise ValueError('JSON 须为最多 100 条对象数组，或包含 items 数组')
    return data


def _connector_preview(db, row):
    data = _connector_fetch(db, row)
    mapping, items, identities = json.loads(row['field_map_json']), [], set()
    for index, raw in enumerate(data):
        fields, warnings = {}, []
        for external, local in mapping.items():
            value = raw.get(external)
            if value is None or value == '':
                warnings.append('缺少外部字段「%s」，无法映射到「%s」' % (external, local))
            elif not isinstance(value, (str, int, float)) or isinstance(value, bool) or len(str(value)) > 2000:
                warnings.append('外部字段「%s」不是可用的短文本或数字' % external)
            else:
                fields[local] = str(value)
        external_id = raw.get('id')
        if not isinstance(external_id, (str, int)) or isinstance(external_id, bool) or not 0 < len(str(external_id)) <= 200:
            warnings.append('缺少稳定的外部 id，不能确认接入')
        elif str(external_id) in identities:
            warnings.append('外部 id 重复，不能确认接入')
        else:
            identities.add(str(external_id))
        subject = db.row('SELECT person_id FROM person WHERE owner_id=? AND person_id=?', (row['owner_id'], fields.get('对象', '')))
        if not subject:
            named = db.rows('SELECT person_id FROM person WHERE owner_id=? AND name=?', (row['owner_id'], fields.get('对象', '')))
            subject = named[0] if len(named) == 1 else None
        if not subject:
            warnings.append('对象未匹配到本人已有记录；不按相似姓名猜测')
        try:
            datetime.strptime(fields.get('发生日期', ''), '%Y-%m-%d')
        except ValueError:
            warnings.append('发生日期须为有效 YYYY-MM-DD；不会补今天')
        if not any(k in fields for k in ('进度', '已完成项目')):
            warnings.append('没有映射到进度或已完成项目')
        items.append({'index': index+1, 'raw_keys': sorted(str(k) for k in raw), 'mapped': fields, 'warnings': warnings,
                      'external_id': str(external_id) if external_id is not None else '', 'subject_id': subject['person_id'] if subject else '', 'importable': not warnings})
    return {'simulated': row['kind']=='third_party', 'network_request': row['kind']=='self_app', 'total': len(items), 'preview': items[:5], '_items': items}


def connector_test(db, owner, cid):
    row = _connector(db, owner, cid)
    try:
        preview = _connector_preview(db, row)
    except ValueError as exc:
        db.execute("UPDATE app_connector SET status='failed',last_error=? WHERE connector_id=?", (str(exc), cid))
        raise
    preview.pop('_items')
    return preview


def connector_sync(db, owner, cid):
    row = _connector(db, owner, cid)
    try:
        preview = _connector_preview(db, row)
        items = preview.pop('_items')
        if any(not x['importable'] for x in items):
            raise ValueError('映射未完成，未导入：'+'；'.join(next(x['warnings'] for x in items if x['warnings'])))
        created = 0
        with db.atomic():
            _connector(db, owner, cid)
            for item in items:
                existing = db.row('SELECT * FROM connector_record WHERE connector_id=? AND external_id=?', (cid, item['external_id']))
                if existing:
                    continue  # Never silently overwrite corrected/revoked history.
                sid = cid+':'+hashlib.sha256(item['external_id'].encode()).hexdigest()[:24]
                payload = {k: v for k, v in item['mapped'].items() if k not in ('对象', '发生日期')}
                eid, made = import_life_event(db, sid, item['subject_id'], 'connector_record', payload, item['mapped']['发生日期'], row['name']+('（模拟数据）' if preview['simulated'] else '（接口数据）'), owner, source_kind='connector')
                db.execute('INSERT INTO connector_record(connector_id,external_id,source_id,event_id) VALUES(?,?,?,?)', (cid, item['external_id'], sid, eid))
                created += int(made)
            db.execute("UPDATE app_connector SET status='connected',last_sync_at=?,last_error='' WHERE connector_id=?", (dbm.now_iso(), cid))
        return {**preview, 'created': created}
    except ValueError as exc:
        db.execute("UPDATE app_connector SET status='failed',last_error=? WHERE connector_id=?", (str(exc), cid))
        raise


def connector_list(db, owner):
    out = []
    for row in db.rows('SELECT * FROM app_connector WHERE owner_id=? ORDER BY created_at DESC', (owner,)):
        events = []
        for record in db.rows('SELECT * FROM connector_record WHERE connector_id=?', (row['connector_id'],)):
            ev = db.row('SELECT * FROM life_event WHERE event_id=?', (record['event_id'],))
            if ev:
                events.append({'event_id': ev['event_id'], 'status': ev['status'], 'source_id': ev['source_id'], 'provider': ev['provider'], 'happened_on': ev['happened_on'], 'fields': json.loads(ev['payload_json']), 'authorized_fields': json.loads(record['authorized_fields_json'])})
        out.append({k: row[k] for k in ('connector_id','kind','name','base_url','path','auth_ref','status','last_sync_at','last_error','created_at','revoked')})
        out[-1].update(field_map=json.loads(row['field_map_json']), simulated=row['kind']=='third_party', imported_count=len(events), events=events)
    return out


def connector_authorize(db, owner, cid, event_id, fields):
    with db.atomic():
        _connector(db, owner, cid)
        ev = db.row("SELECT * FROM life_event WHERE event_id=? AND owner_id=? AND status='active'", (event_id, owner))
        record = db.row('SELECT * FROM connector_record WHERE connector_id=? AND event_id=?', (cid, event_id))
        if not ev or not record or not isinstance(fields, list) or any(not isinstance(k,str) or k not in json.loads(ev['payload_json']) for k in fields) or len(fields)!=len(set(fields)):
            raise ValueError('只能授权本人当前记录中逐项选择的字段')
        db.execute('UPDATE connector_record SET authorized_fields_json=? WHERE connector_id=? AND event_id=?', (json.dumps(fields, ensure_ascii=False), cid, event_id))
        # Removing even one published field invalidates prior plan dependencies.
        if not set(json.loads(record['authorized_fields_json'])).issubset(fields):
            invalidate_event_plans(db, event_id)
    return {'authorized_fields': fields}


def connector_revoke(db, owner, cid):
    with db.atomic():
        _connector(db, owner, cid, active=False)
        for r in db.rows("SELECT e.* FROM connector_record c JOIN life_event e ON e.source_id=c.source_id WHERE c.connector_id=? AND e.status='active'", (cid,)):
            revoke_life_event(db, r['event_id'], owner)
        db.execute("UPDATE connector_record SET authorized_fields_json='[]' WHERE connector_id=?", (cid,))
        db.execute("UPDATE app_connector SET revoked=1,status='disconnected' WHERE connector_id=?", (cid,))
    return {'revoked': True, 'history_preserved': True}


def _connector_model_fields(db, ev, fields):
    if ev['source_kind'] != 'connector':
        return fields
    row = db.row('SELECT r.authorized_fields_json,c.revoked FROM connector_record r JOIN app_connector c ON c.connector_id=r.connector_id WHERE r.source_id=? AND r.event_id=?', (ev['source_id'], ev['event_id']))
    allowed = json.loads(row['authorized_fields_json']) if row and not row['revoked'] and ev['status']=='active' else []
    return {k: v for k, v in fields.items() if k in allowed}


# ======================== 送礼对象档案（t49） ========================
# 复用 life_event 与 person，不建平行表。键名契约见
# docs/evidence/gift-target-profile-20261017.md：
#   person.attributes_json = {"gift_target": {"relationship": <五类之一>, "marked_at", "note"}}
#   gift_target 键存在即把该 person 标记为送礼对象；relationship 是唯一关系来源。
#   与 t53 的「通用人际关系分类」是两层：本层命名空间隔离在 gift_target 下，
#   不预设、不占用通用位、不与其混成一个字段。
# 档案条目 = life_event(event_type='gift_profile', payload={category, value})：
#   更正走 correct_life_event(supersede，旧值仅作来源可查)、撤回走 revoke_life_event
#   (tombstone)、授权走 event_grant、来源/日期/提供者齐备、provenance 走 audit。
GIFT_RELATIONSHIP_CATEGORIES = ("长辈", "晚辈", "同龄人", "朋友", "同事")
GIFT_PROFILE_CATEGORIES = ("preference", "size", "avoid", "last_gift")
GIFT_CATEGORY_LABELS = {"preference": "偏好", "size": "尺寸",
                        "avoid": "忌讳", "last_gift": "上次送过什么"}
GIFT_PROFILE_EVENT_TYPE = "gift_profile"
_GIFT_SOURCE_PREFIX = "gift_profile"


def _gift_attrs(db, person_id):
    row = db.row("SELECT attributes_json FROM person WHERE person_id=?", (person_id,))
    if row is None:
        return {}
    try:
        attrs = json.loads(row["attributes_json"] or "{}")
    except (ValueError, TypeError):
        attrs = {}
    return attrs if isinstance(attrs, dict) else {}


def _write_gift_attrs(db, person_id, gift_block):
    attrs = _gift_attrs(db, person_id)
    attrs["gift_target"] = gift_block
    db.execute("UPDATE person SET attributes_json=? WHERE person_id=?",
               (json.dumps(attrs, ensure_ascii=False), person_id))


def is_gift_target(db, person_id):
    return isinstance(_gift_attrs(db, person_id).get("gift_target"), dict)


def create_gift_target(db, owner_id, name="", relationship="", person_id="", note=""):
    """把一个人标记为送礼对象并设定关系分类。可关联已有 person，也可新建。

    **不写 person.relation**：那一列驱动家庭结构（配偶/同户）判断，也驱动侧栏分组。
    送礼管家里的关系分类是送礼场景内的私有档案层，写进 relation 会把
    「给爸爸送礼」变成「我爸是我家户主」，家庭推导和孩子数跟着错。
    侧栏分组改读 attributes_json.gift_target，见 _groups_for_person。
    """
    if relationship and relationship not in GIFT_RELATIONSHIP_CATEGORIES:
        raise ValueError("关系分类须为：%s" % "/".join(GIFT_RELATIONSHIP_CATEGORIES))
    if person_id:
        person = db.row("SELECT * FROM person WHERE person_id=? AND owner_id=?",
                        (person_id, owner_id))
        if person is None:
            raise ValueError("这个人物不在你的记录里")
        pid = person_id
        created = False
    else:
        name = (name or "").strip()
        if not name:
            raise ValueError("新建送礼对象需要姓名")
        if _person_by_name(db, owner_id, name) is not None:
            raise ValueError("已存在同名人物，请关联这个人物而不是重复新建")
        pid = dbm.new_id("p")
        # 新建的人默认 "other"：没有任何关系证据时不猜。
        # 侧栏分组另走 attributes_json.gift_target，不会掉进「未分组」。
        db.execute("INSERT INTO person(person_id,owner_id,name,relation,note) VALUES(?,?,?,?,?)",
                   (pid, owner_id, name, "other", note or "送礼对象"))
        created = True
    _write_gift_attrs(db, pid, {"relationship": relationship or "",
                                "marked_at": dbm.now_iso()[:10], "note": note or ""})
    row = db.row("SELECT name FROM person WHERE person_id=?", (pid,)) or {}
    db.audit("", owner_id, "gift_target_created",
             {"person_id": pid, "relationship": relationship, "created": created}, owner_id=owner_id)
    return {"person_id": pid, "name": row.get("name", ""),
            "relationship": relationship, "created": created}


def set_gift_relationship(db, owner_id, person_id, relationship):
    if relationship not in GIFT_RELATIONSHIP_CATEGORIES:
        raise ValueError("关系分类须为：%s" % "/".join(GIFT_RELATIONSHIP_CATEGORIES))
    person = db.row("SELECT * FROM person WHERE person_id=? AND owner_id=?", (person_id, owner_id))
    if person is None:
        raise ValueError("这个人物不在你的记录里")
    block = _gift_attrs(db, person_id).get("gift_target")
    if not isinstance(block, dict):
        block = {"marked_at": dbm.now_iso()[:10], "note": ""}
    block["relationship"] = relationship
    _write_gift_attrs(db, person_id, block)
    db.audit("", owner_id, "gift_target_relationship",
             {"person_id": person_id, "relationship": relationship}, owner_id=owner_id)
    return block


def _gift_source_id(person_id, category, value):
    digest = hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]
    return "%s:%s:%s:%s" % (_GIFT_SOURCE_PREFIX, person_id, category, digest)


def _gift_entry_view(db, event_id):
    row = db.row("SELECT * FROM life_event WHERE event_id=?", (event_id,))
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (ValueError, TypeError):
        payload = {}
    return {"event_id": row["event_id"], "category": payload.get("category", ""),
            "value": payload.get("value", ""), "provider": row["provider"],
            "happened_on": row["happened_on"], "source": row["source_id"],
            "status": row["status"], "superseded_by": row["superseded_by"] or ""}


def _active_gift_entry(db, owner_id, person_id, category, value):
    for r in db.rows(
            "SELECT event_id FROM life_event WHERE owner_id=? AND subject_id=?"
            " AND event_type=? AND status='active'",
            (owner_id, person_id, GIFT_PROFILE_EVENT_TYPE)):
        view = _gift_entry_view(db, r["event_id"])
        if view and view["category"] == category and view["value"] == value:
            return view
    return None


def add_gift_profile_entry(db, owner_id, person_id, category, value, happened_on="",
                           provider="我本人", source_utterance=""):
    """新增一条送礼对象档案（偏好/尺寸/忌讳/上次送过什么）。同一值重复提交幂等。"""
    if category not in GIFT_PROFILE_CATEGORIES:
        raise ValueError("档案类别须为：%s" % "/".join(GIFT_PROFILE_CATEGORIES))
    value = (value or "").strip()
    if not value:
        raise ValueError("档案内容不能为空")
    person = db.row("SELECT * FROM person WHERE person_id=? AND owner_id=?", (person_id, owner_id))
    if person is None:
        raise ValueError("送礼对象不在你的记录里")
    existing = _active_gift_entry(db, owner_id, person_id, category, value)
    if existing is not None:
        return existing
    event_id, _created = import_life_event(
        db, source_id=_gift_source_id(person_id, category, value),
        subject_id=person_id, event_type=GIFT_PROFILE_EVENT_TYPE,
        payload={"category": category, "value": value},
        happened_on=happened_on or dbm.now_iso()[:10],
        provider=provider or "我本人", owner_id=owner_id,
        source_kind=GIFT_PROFILE_EVENT_TYPE)
    db.audit("", owner_id, "gift_profile_added",
             {"event_id": event_id, "person_id": person_id, "category": category}, owner_id=owner_id)
    return _gift_entry_view(db, event_id)


def correct_gift_profile_entry(db, owner_id, event_id, value):
    """更正一条档案：旧值 superseded（仅作来源可查），写入新值。仅所有者。"""
    value = (value or "").strip()
    if not value:
        raise ValueError("更正内容不能为空")
    row = db.row("SELECT * FROM life_event WHERE event_id=?", (event_id,))
    if row is None or row["event_type"] != GIFT_PROFILE_EVENT_TYPE:
        raise ValueError("档案条目不存在")
    if row["owner_id"] != owner_id:
        raise ValueError("只能更正自己记的档案")
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (ValueError, TypeError):
        payload = {}
    category = payload.get("category", "")
    new_id = correct_life_event(db, event_id, {"category": category, "value": value}, owner_id)
    db.audit("", owner_id, "gift_profile_corrected",
             {"old_event_id": event_id, "new_event_id": new_id, "category": category}, owner_id=owner_id)
    return _gift_entry_view(db, new_id)


def revoke_gift_profile_entry(db, owner_id, event_id):
    """撤回一条档案：tombstone，当前查询不可见，历史仍可查。仅所有者。"""
    row = db.row("SELECT * FROM life_event WHERE event_id=?", (event_id,))
    if row is None or row["event_type"] != GIFT_PROFILE_EVENT_TYPE:
        raise ValueError("档案条目不存在")
    revoke_life_event(db, event_id, owner_id)
    return True


def grant_gift_profile_field(db, owner_id, event_id, viewer_id, fields):
    """显式授权某条档案的哪些字段能给谁看。默认不授权（私有）。"""
    row = db.row("SELECT * FROM life_event WHERE event_id=?", (event_id,))
    if row is None or row["owner_id"] != owner_id:
        raise ValueError("只能授权自己记的档案")
    if not isinstance(fields, list):
        fields = []
    db.execute(
        "INSERT INTO event_grant(grant_id, event_id, viewer_id, fields_json,"
        " valid_until, note, created_at) VALUES(?,?,?,?,?,?,?)",
        (dbm.new_id("grant"), event_id, viewer_id, json.dumps(fields, ensure_ascii=False),
         "", "送礼对象档案授权", dbm.now_iso()))
    db.audit("", owner_id, "gift_profile_granted",
             {"event_id": event_id, "viewer_id": viewer_id, "fields": fields}, owner_id=owner_id)
    return True


def gift_target_entries(db, owner_id, person_id, category=None, include_history=False):
    """某送礼对象的档案条目。默认只回当前有效；include_history 带回被更正/撤回的旧值。"""
    sql = ("SELECT * FROM life_event WHERE owner_id=? AND subject_id=? AND event_type=?")
    params = [owner_id, person_id, GIFT_PROFILE_EVENT_TYPE]
    if not include_history:
        sql += " AND status='active'"
    sql += " ORDER BY happened_on DESC, created_at DESC"
    out = []
    for r in db.rows(sql, tuple(params)):
        view = _gift_entry_view(db, r["event_id"])
        if view is None:
            continue
        if category and view["category"] != category:
            continue
        out.append(view)
    return out


def list_gift_targets(db, owner_id):
    """按关系分类分组的送礼对象列表，带条目计数与上次送过什么。"""
    groups: dict[str, list] = {}
    targets = []
    for p in db.rows("SELECT * FROM person WHERE owner_id=? ORDER BY name", (owner_id,)):
        block = _gift_attrs(db, p["person_id"]).get("gift_target")
        if not isinstance(block, dict):
            continue
        rel = block.get("relationship") or "未分类"
        entries = gift_target_entries(db, owner_id, p["person_id"])
        counts = {c: sum(1 for e in entries if e["category"] == c) for c in GIFT_PROFILE_CATEGORIES}
        item = {"person_id": p["person_id"], "name": p["name"], "relationship": rel,
                "entry_counts": counts, "entry_total": len(entries),
                "last_gift": next((e for e in entries if e["category"] == "last_gift"), None)}
        groups.setdefault(rel, []).append(item)
        targets.append(item)
    return {"groups": groups, "targets": targets, "relationships": list(GIFT_RELATIONSHIP_CATEGORIES)}


def gift_target_context(db, owner_id, subject_id, viewer_id=""):
    """授权门后的送礼对象档案块，供模型上下文。

    viewer 默认为 owner_id。授权规则复用 _event_visible_fields：记忆所有者看自己
    免授权；他人仅见被显式授权（未过期）的字段。value 是敏感内容，以它是否对
    viewer 可见为准；category 只是标签，不单独作为敏感字段。未授权条目的 value
    绝不会出现在返回结果里，因而也不会进提示词。
    """
    viewer = (viewer_id or owner_id or "").strip() or owner_id
    result = {"is_gift_target": False, "relationship": "", "entries": [],
              "last_gift": None, "missing": list(GIFT_PROFILE_CATEGORIES), "basis_count": 0}
    if not subject_id:
        return result
    block = _gift_attrs(db, subject_id).get("gift_target")
    if not isinstance(block, dict):
        return result
    result["is_gift_target"] = True
    result["relationship"] = block.get("relationship") or "未分类"
    visible = []
    for r in db.rows(
            "SELECT * FROM life_event WHERE subject_id=? AND event_type=? AND status='active'"
            " ORDER BY happened_on DESC, created_at DESC",
            (subject_id, GIFT_PROFILE_EVENT_TYPE)):
        allowed = _event_visible_fields(db, r["event_id"], viewer)
        if "value" not in allowed:
            continue
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        cat, val = payload.get("category") or "", payload.get("value") or ""
        if not cat or not val:
            continue
        visible.append({"event_id": r["event_id"], "category": cat, "value": val,
                        "provider": r["provider"], "happened_on": r["happened_on"]})
    result["entries"] = visible
    result["basis_count"] = len(visible)
    result["last_gift"] = next((e for e in visible if e["category"] == "last_gift"), None)
    have = {e["category"] for e in visible}
    result["missing"] = [c for c in GIFT_PROFILE_CATEGORIES if c not in have]
    return result


# ==================== 通用人际关系分类（t53，壳体） ===================
# 用户原话（thread_history 01a12033，10-13 16:46）：「人际关系是否应该分个类，
# 比如亲人/长辈晚辈 朋友 工作伙伴，如果是一个社牛，所有人都并列，会比较难办吧」。
# 本轮只做壳体：能存、能改、界面标注建设中。不按分类检索、不推荐、
# 不进任何模型提示词、不影响家庭结构（配偶/同户）与授权判断。
#
# 存储取舍：不新表、不迁移——person.attributes_json 本就是人物实体自己的
# 扩展字段（t49 的送礼对象关系分类同址存放），在其下开 social_class 命名空间。
# 键名与 t49 的 gift_target 分层隔离，互不占用；不占 person.relation 列。
# 契约（tests/test_relationship_class_shell.py 锁定）：
#   person.attributes_json = {"social_class": {"classes": [...], "marked_at": "...", "note": ""}}
SOCIAL_CLASSES = ("亲人", "长辈", "晚辈", "同龄人", "朋友", "工作伙伴", "其他")
SOCIAL_CLASS_SHELL_NOTE = ("建设中：这只是人际关系分类本身，能存能改；"
                           "它还不驱动检索、推荐或任何协商，也不要据此推断家庭结构。")
_SOCIAL_CLASS_KEY = "social_class"


def _social_class_attrs(db, person_id):
    row = db.row("SELECT attributes_json FROM person WHERE person_id=?", (person_id,))
    try:
        attrs = json.loads((row or {}).get("attributes_json") or "{}")
    except (ValueError, TypeError):
        attrs = {}
    return attrs if isinstance(attrs, dict) else {}


def get_person_social_classes(db, owner_id, person_id):
    """读一个人身上的通用人际关系分类。坏数据按未分类，不抛。"""
    person = db.row("SELECT * FROM person WHERE person_id=? AND owner_id=?",
                    (person_id, owner_id))
    if person is None:
        raise ValueError("这个人物不在你的记录里")
    block = _social_class_attrs(db, person_id).get(_SOCIAL_CLASS_KEY)
    if not isinstance(block, dict):
        return {"classes": []}
    classes = block.get("classes")
    return {"classes": [c for c in classes if isinstance(c, str)] if isinstance(classes, list) else [],
            "marked_at": block.get("marked_at", ""), "note": block.get("note", "")}


def set_person_social_classes(db, owner_id, person_id, classes):
    """设置一个人的通用人际关系分类（可多选、可清空=不分类）。

    先校验后落库：枚举外的取值抛 ValueError，且不改动原值——静默吞掉或
    先写后判，都会让用户以为设上了。只写 attributes_json 下的 social_class
    命名空间：person.relation、relation 表、person_link 一律不碰，
    家庭结构判断与授权不受影响；其他命名空间（如 t49 的 gift_target）原样保留。
    """
    if classes is None:
        classes = []
    if not isinstance(classes, list):
        raise ValueError("关系分类须为列表")
    cleaned = []
    for c in classes:
        c = str(c).strip()
        if c not in SOCIAL_CLASSES:
            raise ValueError("关系分类须为：%s" % "、".join(SOCIAL_CLASSES))
        if c not in cleaned:
            cleaned.append(c)
    person = db.row("SELECT * FROM person WHERE person_id=? AND owner_id=?",
                    (person_id, owner_id))
    if person is None:
        raise ValueError("这个人物不在你的记录里")
    attrs = _social_class_attrs(db, person_id)
    old = attrs.get(_SOCIAL_CLASS_KEY)
    old = old if isinstance(old, dict) else {}
    note = old.get("note", "")
    block = {"classes": cleaned, "marked_at": dbm.now_iso()[:10],
             "note": note if isinstance(note, str) else ""}
    attrs[_SOCIAL_CLASS_KEY] = block
    db.execute("UPDATE person SET attributes_json=? WHERE person_id=?",
               (json.dumps(attrs, ensure_ascii=False), person_id))
    db.audit("", owner_id, "social_class_set",
             {"person_id": person_id, "classes": cleaned}, owner_id=owner_id)
    return block


def list_person_social_classes(db, owner_id):
    """给界面的壳体数据：可选值、建设中说明、每个人当前的分类。

    不过滤、不推荐、不排序暗示——壳体现在只是「分类本身」。未分类的人
    也列出来，让用户看得到谁还没有分类。
    """
    rows = db.rows("SELECT * FROM person WHERE owner_id=? ORDER BY name", (owner_id,))
    persons = []
    for row in rows:
        if row["name"] == "我":
            continue
        block = _social_class_attrs(db, row["person_id"]).get(_SOCIAL_CLASS_KEY)
        classes = block.get("classes") if isinstance(block, dict) else None
        persons.append({"person_id": row["person_id"], "name": row["name"],
                        "relation": row["relation"],
                        "classes": [c for c in classes if isinstance(c, str)]
                        if isinstance(classes, list) else []})
    return {"status": "shell", "note": SOCIAL_CLASS_SHELL_NOTE,
            "values": list(SOCIAL_CLASSES), "persons": persons}


# ==================== 朋友旅行场景（t56） ====================
# 约朋友一起旅行：复用同一套多方协调引擎，条件字段用 friend_trip 字段集。
# 与家庭活动、送礼都区分开：送礼句、多家庭活动句都不能误判成朋友旅行。
FRIEND_TRIP_MARKERS = ("朋友", "旅行", "旅游", "度假", "出游", "想去", "自驾游",
                       "一起去玩", "约朋友", "闺蜜", "哥们", "老同学", "驴友")
FAMILY_TRIP_MARKERS = ("几家", "多少个家庭", "多少户", "家庭", "带孩子", "带娃", "遛娃")
GIFTING_MARKERS = ("礼物", "送礼", "买给", "生日礼", "买什么", "回礼", "还礼")


def start_friend_trip(db, client, owner_id, utterance):
    """从一句话直接开一场朋友旅行草稿，复用多方协调引擎。

    和 start_group_activity 的分工：那一个建 kind=group_activity，条件字段用
    家庭活动那套（时间/地点/活动总预算/孩子安排…）；这一个建 kind=friend_trip，
    用朋友旅行那套（时间/目的地/假期长度/人均预算/住宿标准/能否带伴侣…）。
    两家共用的规则一条不减：各家只补自己的条件、代表审阅不算本人接受、
    同一版本才计数、条件变了旧接受失效、未授权数据不进提示词。

    不做的事：不替朋友决定来不来，不把「人均预算」当成某人自己的上限，
    也不因为说了「朋友」就假设有几个家庭。
    """
    text = (utterance or "").strip()
    title = text[:120] or "朋友旅行"
    reused = find_open_plan(db, owner_id, title) is not None
    plan_id = create_plan(db, owner_id, title, text, kind="friend_trip")
    cap = extract_own_cap(text)
    conditions = {}
    if cap:
        # 朋友旅行场景里，本人这一方的上限键是「本人预算上限」，不是家庭活动的
        # 「本人出资上限」；两者不是一个数，也不能互相顶替。
        conditions["本人预算上限"] = cap
    mine = db.row("SELECT participant_id FROM plan_participant WHERE plan_id=? AND owner_id=?",
                  (plan_id, owner_id))
    if mine is None:
        add_plan_participant(db, plan_id, owner_id, "我家", conditions, True,
                             role="self")
    elif cap:
        cur = db.row("SELECT conditions_json FROM plan_participant"
                     " WHERE plan_id=? AND owner_id=?", (plan_id, owner_id))
        have = json.loads(cur["conditions_json"] or "{}")
        if "本人预算上限" not in have:
            have["本人预算上限"] = cap
            db.execute("UPDATE plan_participant SET conditions_json=? WHERE plan_id=?"
                       " AND owner_id=?",
                       (json.dumps(have, ensure_ascii=False), plan_id, owner_id))
    # 随行由本人自己决定说不说：这里只把「可以说」这件事讲清，
    # 不替他填任何人。真正声明随行走 /api/participation/trip-party。
    reply = (
        "这件事按朋友旅行记下了。各家自己补条件：时间、目的地、假期长度、"
        "人均预算、住宿标准、能否带伴侣、忌口或体力，都能各说各的。"
        + ("你说的「%s」记在**本人预算上限**里，不是你替大家定的人均预算。" % cap
           if cap else "")
        + "想说自己这边几个人、随行是谁，在参与页声明；没说就只记你这一方。"
        + "带伴侣、不带伴侣都行，由各人自己讲。"
    )
    return {"plan_id": plan_id, "reused": reused, "title": title,
            "own_cap": cap, "reply": reply, "companions": []}


def friend_trip_intent(text):
    """这句话是约朋友一起旅行，不是家庭活动、也不是送礼。

    判据：有朋友/旅行措辞，且不在说给某个人准备东西（送礼），
    也不是多家庭一起的活动。「给三家一起出游的孩子们准备礼物」是送礼，
    「周末三家一起带孩子出去玩」是家庭活动，两者都不算朋友旅行。
    """
    t = (text or "").strip()
    if not t:
        return False
    if any(g in t for g in GIFTING_MARKERS):
        return False
    if any(f in t for f in FAMILY_TRIP_MARKERS):
        return False
    if group_activity_intent(t):
        return False
    return any(k in t for k in FRIEND_TRIP_MARKERS)


def _clean_companions(companions):
    """清洗随行声明：姓名可选，填了就必须是人名；每人标注有没有自己的助手。

    返回 {"companions":[{name,has_agent,attendance}], "source": "participation"}。
    没有配置自己助手的随行者，decision_mode=delegated，由声明人代决策。
    """
    if companions is None:
        companions = []
    if not isinstance(companions, list):
        raise ValueError("随行声明须为数组")
    out, seen = [], set()
    for c in companions:
        if isinstance(c, str):
            c = {"name": c}
        if not isinstance(c, dict):
            raise ValueError("随行须为对象")
        name = str(c.get("name", "")).strip()
        if not name:
            continue
        if len(name) > 20:
            raise ValueError("随行姓名最长 20 字")
        if name in seen:
            continue
        seen.add(name)
        att = c.get("attendance", "coming")
        if att not in ("coming", "not_coming", "tbd"):
            att = "tbd"
        out.append({"name": name, "has_agent": bool(c.get("has_agent")),
                    "attendance": att,
                    "decision_mode": "self" if c.get("has_agent") else "delegated"})
    if len(out) > 20:
        raise ValueError("随行最多 20 人")
    return {"companions": out, "source": PARTICIPATION_NOTE}


def declare_trip_party(db, session, headcount, conditions, companions=None):
    """朋友旅行：本人声明自己这一方几个人、分别什么类别、出席状态，
    并**可以自己决定**要不要说出随行是谁。

    随行规则（用户 2026-10-17 定）：
    - 填不填随行姓名，由声明的人自己决定；不填就只报人数；
    - 填了姓名的随行者**进共同事项**，成为这一方的成员；
    - 随行者没有配置自己的 agent/服务时，默认由声明的参与者**代他决策**；
      代决策是单独一类记录，不算本人接受，也不和「代表审阅只是建议」混为一谈。
    """
    if not isinstance(headcount, dict):
        raise ValueError("人数声明须为对象")
    if [k for k in headcount if k not in ("adults", "kids", "tbd")]:
        raise ValueError("人数声明只收 成人/孩子/待定 的计数")
    cleaned_companions = _clean_companions(companions)
    counts = {}
    for k in ("adults", "kids", "tbd"):
        v = headcount.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 50:
            raise ValueError("人数须为 0–50 的整数")
        counts[k] = v
    if sum(counts.values()) <= 0:
        raise ValueError("请声明这一方的人数，或明确标为待定")
    cleaned, unknown, confirm = normalize_conditions(conditions, scenario="friend_trip")
    if unknown or confirm:
        raise ValueError("条件字段或预算口径不明确")
    with db.atomic():
        live = matter_session(db, session["raw_token"])
        plan = db.row("SELECT kind FROM plan WHERE plan_id=?", (live["plan_id"],))
        if (plan or {}).get("kind") != "friend_trip":
            raise ValueError("人数声明只用于朋友旅行场景")
        _, side = _member(db, live["plan_id"], live["guest_id"])
        db.execute(
            "UPDATE plan_participant SET headcount_json=?,conditions_json=?,companions_json=?"
            " WHERE participant_id=?",
            (json.dumps(dict(counts, source=PARTICIPATION_NOTE), ensure_ascii=False),
             json.dumps(cleaned, ensure_ascii=False),
             json.dumps(cleaned_companions, ensure_ascii=False), side["participant_id"]))
        invalidate_plan_dependencies(db, live["plan_id"], "朋友旅行参与方更新自己一方的人数与条件")
    return {"ok": True, "headcount": counts, "companions": cleaned_companions}


# 三个虚构朋友的演示资料：明确虚构、不含真实个人信息，独立于 A/B/C 家庭演示数据。
FRIEND_DEMO_PERSONS = (
    ("阿远", "虚构演示朋友，喜欢徒步和摄影"),
    ("小奈", "虚构演示朋友，爱吃辣、想海边度假"),
    ("老K", "虚构演示朋友，预算敏感、可能带家人"),
)


def seed_friend_trip_demo(db, owner_id="A"):
    """建三个虚构朋友人物，跑通朋友旅行场景用。幂等，不改动既有家庭演示数据。"""
    created = []
    for name, note in FRIEND_DEMO_PERSONS:
        if _person_by_name(db, owner_id, name) is not None:
            continue
        pid = dbm.new_id("p")
        db.execute("INSERT INTO person(person_id,owner_id,name,relation,note) VALUES(?,?,?,?,?)",
                   (pid, owner_id, name, "朋友", note))
        created.append({"person_id": pid, "name": name})
    return {"created": created, "note": "虚构演示人物，非真实个人信息"}
