"""两个独立代表：A（送礼者）与 B（家长）。

边界：
- A 的私有记忆不会直接传给 B；B 只按授权范围返回有限属性。
- 程序负责检查硬条件（预算、重复、对象、授权、提案版本），模型负责提出方案与理由。
- 双方必须接受同一个版本，变更后旧确认失效。
"""

from __future__ import annotations

import json
from typing import Any

from . import db as dbm
from . import service as _service
from .model_client import ModelUnavailable, normalize_decision

MAX_ROUNDS = 2

# A 侧允许在协商中公开的条件，不含 A 的私有备注。
SHARABLE_A_CONDITIONS = ("budget_max", "occasion", "recipient", "avoid_repeat", "hard_constraints")


def _person_rows(db, owner_id):
    return db.rows("SELECT * FROM person WHERE owner_id=? ORDER BY name", (owner_id,))


def _person_by_name(db, owner_id, name):
    if not name:
        return None
    rows = db.rows("SELECT * FROM person WHERE owner_id=? AND name=?", (owner_id, name))
    if rows:
        return rows[0]
    rows = db.rows("SELECT * FROM person WHERE owner_id=? AND name LIKE ?", (owner_id, "%" + name + "%"))
    if len(rows) == 1:
        return rows[0]
    return None


def _active_facts(db, owner_id, subject_id=None, attribute=None):
    sql = "SELECT * FROM fact WHERE owner_id=? AND status='active'"
    params: list[Any] = [owner_id]
    if subject_id:
        sql += " AND subject_id=?"
        params.append(subject_id)
    if attribute:
        sql += " AND attribute=?"
        params.append(attribute)
    sql += " ORDER BY observed_on DESC, fact_id"
    return db.rows(sql, tuple(params))


def _shared_attrs_for(db, subject_id, counterparty):
    """B 侧：该主体**当前有效**授权给对方的属性列表。

    过期授权要在这里就挡掉，不能只靠执行前检查。过期字段一旦进入模型输入，
    信息已经离开授权范围，之后拒绝下单也补不回来；撤回（attributes_json 变空）
    同理。
    """
    today = dbm.now_iso()[:10]
    auths = db.rows(
        "SELECT * FROM authorization WHERE owner_id='B' AND scope_kind='share'"
        " AND subject_id=? AND counterparty=?", (subject_id, counterparty))
    attrs: list[str] = []
    for a in auths:
        if a["valid_until"] and a["valid_until"] < today:
            continue
        try:
            attrs.extend(json.loads(a["attributes_json"]))
        except (ValueError, TypeError):
            continue
    return sorted(set(attrs))


def _a_context(db, owner_id, subject_id, extracted, task_id=""):
    """构造 A 的上下文：A 自己的有效事实 + 送礼历史 + 授权范围。

    task_id 用来带上「本人明确用于这件事的资料」。只有点过「用于这件事」的
    才进这里——保存资料本身不自动进入协商，那是系统替用户决定这条资料
    跟哪件事有关。带进来的内容是本人自己写的或核对后采用的，
    仍然只用于这一件事项，不因此获得对外分享或执行权。
    """
    facts = _active_facts(db, owner_id, subject_id)
    linked_notes = []
    if task_id:
        try:
            linked_notes = _service.linked_events(db, task_id, owner_id)
        except Exception:
            # 取不到关联资料不能让整场协商失败：资料是补充依据，
            # 不是必要条件。这里如实略过，界面仍会显示已保存的记录。
            linked_notes = []
    # 家人授权分享的近况也进 A 的上下文：问「孩子最近学习怎么样」时，
    # 不应该要求用户重填人物关系和背景。只取授权字段，规则同 facts。
    life_events = _service._life_events_both_sides(db, owner_id, subject_id)
    history = db.rows(
        "SELECT * FROM gift_record WHERE owner_id=? AND subject_id=? ORDER BY happened_on DESC",
        (owner_id, subject_id))
    auths = db.rows("SELECT * FROM authorization WHERE owner_id=? ORDER BY auth_id", (owner_id,))
    subject_row = db.row("SELECT * FROM person WHERE person_id=?", (subject_id,))
    return {
        "owner_id": owner_id,
        "subject_id": subject_id,
        "subject_name": subject_row["name"] if subject_row else "",
        "category": extracted.get("category"),
        "known_facts": [
            {"attribute": f["attribute"], "value": f["value"], "provider": f["provider"],
             "observed_on": f["observed_on"], "source": f["source_utterance"]}
            for f in facts
        ],
        "gift_history": [
            {"product_id": g["product_id"], "product_name": g["product_name"],
             "spec": g["spec"], "occasion": g["occasion"], "feedback": g["feedback"],
             "happened_on": g["happened_on"]}
            for g in history
        ],
        # 本人明确用于这件事的资料。和 family_updates 分开：
        # 那些是授权来的近况，这些是本人这次办事指定要用的内容，
        # 来源、时间、是否经过核对都标清楚，混在一起就说不清依据是什么。
        "linked_notes": [
            {"source": n["source_label"], "event_type": n["event_type"],
             "happened_on": n["happened_on"], "provider": n["provider"],
             "linked_at": n["linked_at"], "fields": n["fields"]}
            for n in linked_notes
        ],
        # 跨小 App 复用：同一人的授权近况按来源列出来，
        # 每条带来源、时间和提供者，不合并成一份说不清是谁说的。
        "family_updates": [
            {"source": e["source_label"], "event_type": e["event_type"],
             "happened_on": e["happened_on"], "provider": e["provider"],
             "is_device": e["is_device"], "fields": e["fields"]}
            for e in life_events
        ],
        "authorizations": [
            {"scope_kind": a["scope_kind"], "subject_id": a["subject_id"],
             "note": a["note"], "valid_until": a["valid_until"]}
            for a in auths
        ],
        "requirements": {k: extracted.get(k) for k in SHARABLE_A_CONDITIONS if extracted.get(k) is not None},
        # 送礼对象档案：授权可见的偏好/尺寸/忌讳/上次送过什么，供提案带依据、明示缺口。
        # additive：非送礼对象时 is_gift_target=False、entries 空，不影响既有上下文。
        "gift_target_profile": _service.gift_target_context(db, owner_id, subject_id, viewer_id=owner_id),
    }
    # 本次品类要求单独放在上下文里，供提案时约束商品选择。


def _gift_profile_prompt(ctx):
    """授权可见的送礼对象档案 → 注入模型提示词的正文。

    非送礼对象/无授权条目时返回空串。未授权条目的 value 已被 gift_target_context
    的授权门滤掉，绝不会出现在这里，因而也不会进提示词。明示依据来自哪几条、
    还缺什么、不编造。
    """
    if not ctx or not ctx.get("is_gift_target"):
        return ""
    labels = _service.GIFT_CATEGORY_LABELS
    rel = ctx.get("relationship") or "未分类"
    entries = ctx.get("entries") or []
    lines = ["【送礼对象档案·关系：%s】（私有档案，仅本人授权可见的部分）" % rel]
    if not entries:
        lines.append("（当前没有已授权的档案条目可作为依据）")
    else:
        for e in entries:
            lines.append("- %s：%s（提供者 %s，%s，依据条目 %s）" % (
                labels.get(e.get("category"), e.get("category")), e.get("value", ""),
                e.get("provider") or "未标注", e.get("happened_on") or "日期未标注",
                e.get("event_id", "")))
        lg = ctx.get("last_gift")
        if lg:
            lines.append("上次送过：%s（%s）" % (lg.get("value", ""), lg.get("happened_on") or ""))
    missing = ctx.get("missing") or []
    if missing:
        lines.append("还缺：%s。缺的要据实向对方问，不替对方编尺寸、不编偏好。" %
                     "、".join(labels.get(m, m) for m in missing))
    lines.append("回复与每个候选都要明示依据来自上面哪几条（可指依据条目号）；不要引用不存在的档案。")
    return "\n".join(lines)


def _gift_recommendation_prompt(ctx):
    """「送什么好」推荐指令：每个候选须指回具体依据；没有依据就说不知道，不用常见款凑。"""
    base = _gift_profile_prompt(ctx)
    rule = ("推荐规则：每个候选必须注明依据——指明是依据上面哪条档案或上次送过什么；"
            "没有依据就说「不知道，需要先了解对方的偏好/尺寸」，据实询问，"
            "不要用常见款凑数，不要编造对方的偏好或尺寸。")
    if not (ctx and ctx.get("basis_count")):
        rule += "当前没有任何已授权档案作为依据：直接说明还不了解对方，据实询问，不要推荐具体商品。"
    return (base + "\n" + rule) if base else rule


def _b_context(db, subject_id, a_proposal, extracted):
    """构造 B 的上下文。

    隔离规则：
    - B 只按授权属性读取自己这边关于孩子的记录。
    - A 的送礼历史不直接进 B 的上下文；只有 A 在提案里主动声明分享的部分才可见。
    - A 的自由文本提案原样传递，但那也是 A 主动发出的内容，不是 A 的私有记忆。
    """
    attrs = _shared_attrs_for(db, subject_id, "A")
    facts = _active_facts(db, "B", subject_id)
    b_side = []
    for f in facts:
        if f["attribute"] in attrs:
            b_side.append({"attribute": f["attribute"], "value": f["value"],
                           "provider": f["provider"], "observed_on": f["observed_on"]})
    known_gifts = db.rows(
        "SELECT * FROM gift_record WHERE owner_id='B' AND subject_id=? ORDER BY happened_on DESC",
        (subject_id,))
    # A 主动声明分享的送礼历史，取提案里的字段；没有则为空。
    shared_past = a_proposal.get("shared_past_gifts") or []
    return {
        "owner_id": "B",
        "subject_id": subject_id,
        "authorized_attributes": attrs,
        "child_status": b_side,
        "already_received": [
            {"product_id": g["product_id"], "product_name": g["product_name"],
             "spec": g["spec"], "happened_on": g["happened_on"]}
            for g in known_gifts
        ],
        "giver_history_visible_to_me": shared_past,
        "counterparty_proposal": a_proposal,
        "requirements_from_giver": extracted.get("hard_constraints") or [],
        "budget_from_giver": extracted.get("budget_max"),
    }


def _extract_requirements(db, client, utterance, owner_id):
    """模型抽取交办：对象、预算、硬条件。输出按白名单校验。"""
    persons = _person_rows(db, owner_id)
    person_lines = "\n".join(
        "- %s（%s）%s" % (p["name"], p["relation"],
                         ("；" + p["note"]) if p["note"] else "")
        for p in persons) or "（暂无人物记录）"
    # 把人物列表提到最前，并要求模型给出与列表完全一致的姓名。
    # 用户说「小周的女儿」时，模型必须落到具体人物名（如 朵朵），不能自己造一个新主体。
    system = (
        "你是个人助理的参数抽取器。只输出一个 JSON 对象，不要解释、不要代码块。\n"
        "首先阅读下面的已知人物列表。\n"
        "字段：\n"
        "  subject_name: 字符串。如果用户说的对象能对应到列表中的某个人，\n"
        "    必须原样使用该人物的姓名。如果不能确定是哪一个人，填 null，\n"
        "    不要自己编一个名字，也不要用「我的孩子」这类称呼当姓名。\n"
        "  subject_hint: 字符串。用户原来怎么称呼这个对象的，原样保留，\n"
        "    例如「我的孩子」「小李」「朋友的女儿」。用于向用户确认。\n"
        "  occasion: 字符串，场合，如「生日」；没有填 null。\n"
        "  category: 字符串，用户想要的商品品类，只能是 art / puzzle / shoes / clothes /"
        " toy / book / sport / daily / digital 之一；用户没说品类时填 null。\n"
        "  例如「买双鞋」填 shoes，「买盒蜡笔」填 art，「买个拼图」填 puzzle。\n"
        "  判断对象时：用户说「A 送 B 一个东西，我怎么回礼/还礼/回送」时，\n"
        "    要送礼的对象是 A，不是 B。B 只是上次的收礼人。\n"
        "  budget_max: 数字或 null，单位元。\n"
        "  avoid_repeat: 布尔，是否要求不与上次重复。\n"
        "  hard_constraints: 字符串数组，其他硬性要求；没有给 []。\n"
        "  note: 字符串，一句话说明你的理解。\n"
        "人物列表：\n" + person_lines + "\n"
        "禁止猜测或编造人物。对象不明确时 subject_name 填 null，"
        "subject_hint 写用户的原话称呼，note 说明为什么不确定。"
    )
    data = client.chat_json([
        {"role": "system", "content": system},
        {"role": "user", "content": utterance},
    ])
    subject = _person_by_name(db, owner_id, data.get("subject_name"))
    budget = data.get("budget_max")
    try:
        budget = float(budget) if budget is not None else None
    except (TypeError, ValueError):
        budget = None
    constraints = data.get("hard_constraints")
    if not isinstance(constraints, list):
        constraints = []
    constraints = [str(c) for c in constraints if isinstance(c, (str, int, float))][:6]
    hint = data.get("subject_hint") if isinstance(data.get("subject_hint"), str) else ""
    extracted = {
        "subject_name": data.get("subject_name") if isinstance(data.get("subject_name"), str) else None,
        "subject_hint": hint,
        "subject_id": subject["person_id"] if subject else None,
        "subject_resolved_name": subject["name"] if subject else None,
        "occasion": data.get("occasion") if isinstance(data.get("occasion"), str) else None,
        "category": _normalize_category(data.get("category")),
        "budget_max": budget,
        "avoid_repeat": bool(data.get("avoid_repeat")),
        "hard_constraints": constraints,
        "note": data.get("note") if isinstance(data.get("note"), str) else "",
        "ambiguous_subject": subject is None,
    }
    if subject is None:
        # 给出可选人物，让用户点一下就能补上，不用自己想该怎么说。
        extracted["candidates"] = _subject_candidates(db, owner_id, hint)
    return extracted


ALLOWED_CATEGORIES = {"art", "puzzle", "shoes", "clothes", "toy", "book",
                       "sport", "daily", "digital"}


def _normalize_category(value):
    """只接受已知品类，避免模型自造品类进入查询条件。"""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in ALLOWED_CATEGORIES:
        return v
    aliases = {
        "画材": "art", "绘画": "art", "蜡笔": "art", "水彩": "art",
        "拼图": "puzzle", "鞋": "shoes", "鞋子": "shoes", "运动鞋": "shoes",
        "衣服": "clothes", "衣物": "clothes", "玩具": "toy", "积木": "toy",
        "书": "book", "绘本": "book", "图书": "book",
        "运动": "sport", "足球": "sport", "水壶": "daily", "数码": "digital",
        "耳机": "digital",
    }
    return aliases.get(v)


def _subject_candidates(db, owner_id, hint):
    """对象不确定时，挑出最可能的人选供用户点选。

    优先按称呼里的名字片段找人；称呼明确指向孩子时才只列孩子；
    没有线索时给出全部可送礼对象，让用户自己点。
    """
    rows = db.rows(
        "SELECT person_id, name, relation, note FROM person WHERE owner_id=? AND name<>'我'"
        " ORDER BY person_id", (owner_id,))

    def strip_possessive(text):
        for prefix in ("我的", "我"):
            if text.startswith(prefix):
                return text[len(prefix):].strip()
        return (text or "").strip()

    child_keywords = ("孩子", "女儿", "儿子", "小孩", "宝宝", "学生")

    # 1) 称呼里点名了姓氏或名字片段：只列对得上的人。
    token = strip_possessive(hint)
    if len(token) >= 2:
        named = [r for r in rows if token in r["name"] or r["name"] in token]
        if named:
            return [_as_candidate(r) for r in named][:6]

    # 2) 称呼明确指向孩子时，只列关系本身就是孩子的人。
    #    只看每个人自己的 relation，不把称呼拼进去——否则提示里带「孩子」
    #    会让所有带备注的大人也命中，候选就失去筛选意义。
    if any(k in (hint or "") for k in child_keywords):
        children = [r for r in rows
                    if any(k in (r["relation"] or "") for k in child_keywords)]
        if children:
            return [_as_candidate(r) for r in children][:6]

    # 3) 没有线索：给全部，让用户自己点。
    return [_as_candidate(r) for r in rows][:6]


RELATION_LABELS = {
    "self": "我",
    "friend": "朋友",
    "friend_child": "朋友的孩子",
    "friend_spouse": "朋友的配偶",
    "spouse": "配偶",
    "own_child": "我的孩子",
    "family": "亲属",
    "colleague": "同事",
    "former_colleague": "前同事",
    "classmate": "同学",
    "neighbor": "邻居",
    "teacher": "老师",
    "other": "其他",
}


def _relation_label(relation):
    """把关系代码翻成中文，界面不显示英文标识。"""
    if not relation:
        return ""
    return RELATION_LABELS.get(relation, relation)


def _as_candidate(row):
    return {"person_id": row["person_id"], "name": row["name"],
            "relation": _relation_label(row["relation"])}


def _a_propose(db, client, task_id, ctx, products, version, feedback=None):
    """A 的模型提案：给出商品编号与理由。"""
    # 用户自己换过的商品必须被尊重。重新协商时若不看这个，模型会按自己的
    # 判断再选一件，用户刚换的东西被悄悄换回去——看着像「换了没用」。
    picked = db.row(
        "SELECT product_id FROM order_candidate WHERE task_id=? AND chosen=1"
        " ORDER BY created_at DESC LIMIT 1", (task_id,))
    product_lines = "\n".join(
        "- %s | %s | %s元 | %s%s" % (
            p["product_id"], p["name"], int(p["price_cny"]), p["category"],
            (" | 需选尺码: " + p["size_options"]) if p["has_size"] else "")
        for p in products)
    system = (
        "你是送礼者的个人代表。根据已知信息和送礼历史，从商品列表里选一个最合适的礼物。\n"
        "只输出一个 JSON 对象，字段：\n"
        "  product_id: 字符串，必须从商品列表中选。\n"
        "  reason: 字符串，一句话说明为什么选它。\n"
        "  candidates: 数组，最多 3 个备选。每项是 {\"product_id\": 编号, \"reason\": 一句话}。\n"
        "    product_id 必须来自商品列表；没有合适备选就给空数组。\n"
        "  conditions: 字符串数组，你希望对方确认的条件。\n"
        "  needs_size: 布尔，该商品是否需要尺码。\n"
        "  asked_size: 字符串，你选的具体尺码；不需要尺码填空字符串。\n"
        "关于尺码：你手里的旧尺码记录可能已经过期。\n"
        "  你不确定当前尺码时，asked_size 填空字符串，并在 conditions 里请对方给当前尺码。\n"
        "  只有当你依据的就是最新记录时才填。\n"
        "不要选送礼历史里已送过的商品。预算超了必须换更便宜的。"
    )
    user = {
        "任务": "为送礼对象挑选一个礼物。",
        "送礼对象": ctx.get("subject_name") or "",
        "本次品类要求": ctx.get("category") or "不限品类",
        "本次要求": ctx["requirements"],
        "我已知的关于对象的信息": ctx["known_facts"],
        "我过去的送礼记录": ctx["gift_history"],
        # 家人授权分享的近况。只含授权字段，未授权的、已撤回的都不在这里，
        # 让「能不能进模型输入」有实际意义，而不是只在界面上藏。
        "家人授权分享的近况": ctx.get("family_updates") or [],
        # 本人这次指定用于这件事的资料。没有就是空列表，不编。
        "本人指定用于这件事的资料": ctx.get("linked_notes") or [],
        "商品列表": product_lines,
        "提案版本": version,
    }
    # 送礼对象档案 + 推荐依据：只带入授权可见部分，明示依据来自哪几条、还缺什么、
    # 不编造；没有依据就说不知道，不用常见款凑。非送礼对象时不加这个键。
    _gp = ctx.get("gift_target_profile") or {}
    if _gp.get("is_gift_target"):
        user["送礼对象档案与推荐依据"] = _gift_recommendation_prompt(_gp)
    if picked is not None and picked["product_id"]:
        # 明确告诉模型这一件是用户自己选的，不要另选一件。
        # 仍然要过一遍硬条件检查，所以只在指令层面约束，不绕过检查。
        user["送礼者本人已选定的商品"] = picked["product_id"]
        user["关于这件"] = ("送礼者本人已经从候选里换成了这一件。"
                            "这一轮必须仍用这件，不要另选一件，"
                            "也不要把这件列进备选。")
    if feedback:
        user["对方上一轮反馈"] = feedback
    data = client.chat_json([
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ])
    chosen_pid = data.get("product_id") if isinstance(data.get("product_id"), str) else None
    # 模型不听指令时按用户的选择兜底：他换过的那一件仍然有效。
    # 只在两件都确实存在时才兜底，否则让硬条件检查去报「商品不存在」，
    # 不用一个假商品把这条链走通。
    if picked is not None and picked["product_id"] and chosen_pid != picked["product_id"]:
        still_there = [p for p in products if p["product_id"] == picked["product_id"]]
        if still_there:
            chosen_pid = picked["product_id"]
            data["product_id"] = chosen_pid
            data["reason"] = (data.get("reason") or "") + "（沿用送礼者本人换的这一件）"
    return {
        "product_id": chosen_pid,
        "reason": data.get("reason") if isinstance(data.get("reason"), str) else "",
        "conditions": [str(c) for c in data.get("conditions", [])][:6] if isinstance(data.get("conditions"), list) else [],
        "needs_size": bool(data.get("needs_size")),
        "asked_size": data.get("asked_size") if isinstance(data.get("asked_size"), str) else "",
        # 候选：模型除首选之外列出的备选，按贴合度排序。
        # 只从商品列表里已有的编号取，模型编一个不存在的编号会被丢掉。
        "candidates": _clean_candidates(data.get("candidates"), products),
    }


def _clean_candidates(raw, products):
    """校验模型给的候选：编号必须在商品列表里，最多留 3 个，首选不算候选。"""
    if not isinstance(raw, list):
        return []
    known = {p["product_id"]: p for p in products}
    out = []
    seen = set()
    for item in raw[:6]:
        if not isinstance(item, dict):
            continue
        pid = item.get("product_id")
        if not isinstance(pid, str) or pid not in known or pid in seen:
            continue
        seen.add(pid)
        out.append({
            "product_id": pid,
            "product_name": known[pid]["name"],
            "price_cny": known[pid]["price_cny"],
            "reason": item.get("reason") if isinstance(item.get("reason"), str) else "",
        })
        if len(out) >= 3:
            break
    return out


def _b_review(db, client, task_id, ctx, a_proposal, products, version):
    """B 的模型核对：接受、拒绝或反提案。"""
    product_lines = "\n".join(
        "- %s | %s | %s元 | %s%s" % (
            p["product_id"], p["name"], int(p["price_cny"]), p["category"],
            (" | 尺码: " + p["size_options"]) if p["has_size"] else "")
        for p in products)
    system = (
        "你是孩子家长的个人代表。你只能依据「授权可见的孩子情况」和「已收到的礼物」核对对方提案。\n"
        "只输出一个 JSON 对象，字段：\n"
        "  decision: 字符串，只能是 accept / reject / counter / ask_clarify。\n"
        "  reason: 字符串，一句话说明理由。\n"
        "  counter_product_id: 字符串，反提案的商品编号；不是反提案填空字符串。\n"
        "  conditions: 字符串数组，你需要补充的条件。\n"
        "  asked_size: 字符串，你建议的具体尺码，必须从商品列表的尺码选项中原样选择；\n"
        "              不需要尺码填空字符串。\n"
        "规则：\n"
        "1. 只能依据「授权可见的孩子情况」和「孩子已经有的礼物」判断。\n"
        "2. 对方选的孩子已经有了，或信息已过期，必须 reject 或 counter。\n"
        "3. 提案符合授权信息、且没有重复或尺码冲突时，用 accept。\n"
        "4. ask_clarify 只用于对方能补充的信息，例如商品需要尺码而对方没给。\n"
        "   但尺码如果授权数据里已经有（脚长、试穿记录），你要自己据此判断，\n"
        "   不能反过来向送礼者索要他不可能知道的鞋内长、鞋宽。\n"
        "   不要追问只有你知道、而授权数据里没有的孩子情况（例如难度是否合适、\n"
        "   喜欢什么主题）——那会让协商无法收尾。这类不确定应在 accept 的 reason 里\n"
        "   如实说明，交给家长本人判断。\n"
        "不得透露授权范围之外的孩子信息。"
    )
    user = {
        "授权可见的孩子情况": ctx["child_status"],
        "孩子已经有的礼物": ctx["already_received"],
        "送礼者历史（我可见部分）": ctx["giver_history_visible_to_me"],
        "对方提案": a_proposal,
        "对方预算": ctx["budget_from_giver"],
        "对方硬性要求": ctx["requirements_from_giver"],
        "商品列表": product_lines,
        # 商品的客观属性（是否发声、便于收纳）由本系统提供，
        # 家长据此核对自己的要求，不需要向送礼者索要商品本身的信息。
        "商品属性": ctx.get("product_traits") or {},
        "提案版本": version,
    }
    data = client.chat_json([
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ])
    decision = normalize_decision(data.get("decision")) or "ask_clarify"
    return {
        "decision": decision,
        "reason": data.get("reason") if isinstance(data.get("reason"), str) else "",
        "counter_product_id": data.get("counter_product_id") if isinstance(data.get("counter_product_id"), str) else "",
        "conditions": [str(c) for c in data.get("conditions", [])][:6] if isinstance(data.get("conditions"), list) else [],
        "asked_size": data.get("asked_size") if isinstance(data.get("asked_size"), str) else "",
    }


def _check_hard_conditions(db, extracted, proposal, product):
    """程序校验硬条件。返回 (ok, problems, notes)。"""
    problems = []
    notes = []
    if product is None:
        problems.append("选中的商品不在测试商品清单中")
        return False, problems, notes
    budget = extracted.get("budget_max")
    if budget is not None and product["price_cny"] > budget:
        problems.append("价格 %s 元超出预算 %s 元" % (int(product["price_cny"]), int(budget)))
    if extracted.get("avoid_repeat"):
        history = db.rows(
            "SELECT product_id, product_name, happened_on FROM gift_record"
            " WHERE owner_id='A' AND subject_id=? ORDER BY happened_on DESC",
            (extracted.get("subject_id"),))
        for g in history:
            if g["product_id"] == product["product_id"]:
                problems.append("与 %s 送过的「%s」重复" % (g["happened_on"], g["product_name"]))
            elif g["product_name"] == product["name"]:
                problems.append("与已送过的「%s」是同名商品" % g["product_name"])
    if product["has_size"] and not proposal.get("asked_size"):
        notes.append("该商品需要选择尺码，等待补充")
    if not extracted.get("subject_id"):
        problems.append("送礼对象没有确定，不能继续")
    # 用户指定了品类时，选出的商品必须在该品类内。
    wanted = extracted.get("category")
    if wanted and product is not None and product["category"] != wanted:
        problems.append("这件商品不属于你要求的品类（%s）" % wanted)
    return (not problems), problems, notes


def _size_agreed(db, product, a_prop, b_dec, b_subject_id="", parent_size=""):
    """尺码是否已由双方确认，并且确认有当前依据。

    返回 (ok, problem, need_parent)。A 自己说的尺码只是 A 的记忆，可能过期；
    B 亲口说一个尺码也不够——还要看 B 这边有没有一条当前的鞋码记录支撑它。
    脚长、过去的试穿印象都不是当前鞋码，量纲也不同，不能拿来当执行依据。

    need_parent=True 表示缺的是家长才能给的当前鞋码，这时应当停下来问家长，
    而不是让协商反复重试直到判失败。
    """
    if product is None or not product["has_size"]:
        return True, "", False
    # 家长已经在本事项确认过当前鞋码：那就是既定值，双方都要照这个来，
    # 模型再报别的数值不算数。
    if parent_size:
        return True, "", False
    a_size = (a_prop.get("asked_size") or "").strip()
    b_size = (b_dec.get("asked_size") or "").strip()
    if not b_size:
        if a_size:
            return False, ("这件商品需要尺码。你记的是「%s」，但这只是你这边过去的记录，"
                           "需要家长确认当前尺码后才能下单。" % a_size), True
        return False, "这件商品需要确定尺码，家长还没有确认现在的鞋码。", True
    if b_subject_id:
        ok, _evidence, problem = _service._size_evidence(db, b_subject_id, b_size, product)
        if not ok:
            return False, problem, True
    return True, "", False


def _parent_confirmed_size(db, task_id):
    """家长在本事项里明确确认过的当前鞋码。

    家长补过一次「现在穿 32 码」后，这个值就是这件事的既定事实；
    之后重新协商仍然用它，不再由模型每次重新猜一个。
    """
    row = db.row(
        "SELECT detail_json FROM audit_log WHERE task_id=?"
        " AND action IN ('size_confirmed_by_parent','size_updated_by_parent')"
        " ORDER BY created_at DESC LIMIT 1", (task_id,))
    if row is None:
        return ""
    try:
        value = (json.loads(row["detail_json"]).get("value") or "").strip()
    except (ValueError, TypeError):
        return ""
    return value


def _b_side_person_id(db, a_subject_id):
    """按 A 侧人物找到家长一侧同一个人的记录，用于核对尺码依据。"""
    if not a_subject_id:
        return ""
    a_person = db.row("SELECT name FROM person WHERE person_id=?", (a_subject_id,))
    if a_person is None:
        return ""
    b_person = db.row("SELECT person_id FROM person WHERE owner_id='B' AND name=?",
                      (a_person["name"],))
    return b_person["person_id"] if b_person else ""


def _agreed_size(product, a_prop, b_dec):
    """取双方确认后的尺码：以家长给出的为准，没有则留空。"""
    if product is None or not product["has_size"]:
        return ""
    return (b_dec.get("asked_size") or "").strip()


def _product_index(db):
    return {p["product_id"]: p for p in db.rows("SELECT * FROM product ORDER BY product_id")}


def _product_traits(db):
    """家长核对要用的商品属性：是否发声、是否便于收纳。

    家长只按自己授权范围内的记录和事实判断，这两项是商品的客观属性，
    由程序提供，避免家长代表反过来向送礼者索要只有商品本身才知道的信息。
    """
    out = {}
    for p in db.rows("SELECT product_id, name, makes_noise, easy_to_store FROM product"):
        out[p["product_id"]] = {
            "product_name": p["name"],
            "是否发声": bool(p["makes_noise"]),
            "便于收纳": bool(p["easy_to_store"]),
        }
    return out


def _log_message(db, task_id, sender, kind, payload):
    db.execute(
        "INSERT INTO message_log(msg_id, task_id, sender, kind, payload_json, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (dbm.new_id("msg"), task_id, sender, kind,
         json.dumps(payload, ensure_ascii=False), dbm.now_iso()))


def _new_proposal(db, task_id, version, proposer, product_id, reason, conditions,
                  facts_used, asked_size=""):
    pid = dbm.new_id("prop")
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id, reason,"
        "asked_size, shared_conditions_json, facts_used_json, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (pid, task_id, version, proposer, product_id, reason, asked_size,
         json.dumps(conditions, ensure_ascii=False),
         json.dumps(facts_used, ensure_ascii=False), dbm.now_iso()))
    return pid


def _decide_proposal(db, proposal_id, decided_by, decision, reason):
    db.execute(
        "UPDATE proposal SET decision=?, decided_by=?, decision_reason=? WHERE proposal_id=?",
        (decision, decided_by, reason, proposal_id))
    db.audit("", decided_by, "proposal_decision",
             {"proposal_id": proposal_id, "decision": decision, "reason": reason})


def run_negotiation(db, client, task_id, max_rounds=MAX_ROUNDS):
    """执行协商。返回事项卡数据。任何模型失败都会中止并保留现场。"""
    task = db.row("SELECT * FROM task WHERE task_id=?", (task_id,))
    if task is None:
        raise ValueError("任务不存在")
    extracted = json.loads(task["extracted_json"])
    all_products = list(_product_index(db).values())
    # 用户指定了品类时，只在那个品类里选；没说才给全部。
    category = extracted.get("category")
    if category:
        narrowed = [p for p in all_products if p["category"] == category]
        if narrowed:
            all_products = narrowed
    products = all_products
    owner_id = task["owner_id"]
    subject_id = extracted.get("subject_id")
    if not subject_id:
        hint = extracted.get("subject_hint") or ""
        candidates = extracted.get("candidates") or []
        if hint:
            question = "你想送的是「%s」吗？我这边没对上号。" % hint
        else:
            question = "我没听清这次要送谁。"
        if candidates:
            question += "下面这些人里，你点一个就行："
        else:
            question += "你告诉我对方叫什么，我记下来。"
        db.execute("UPDATE task SET stage='blocked', outcome=? WHERE task_id=?",
                   (question, task_id))
        db.audit(task_id, owner_id, "blocked_need_subject",
                 {"hint": hint, "candidates": candidates, "note": extracted.get("note", "")})
        return {
            "stage": "blocked",
            "outcome": question,
            "question": question,
            "hint": hint,
            "candidates": candidates,
            "need": "subject",
        }

    a_ctx = _a_context(db, owner_id, subject_id, extracted, task_id=task_id)
    facts_used = [
        {"attribute": f["attribute"], "value": f["value"], "provider": f["provider"],
         "observed_on": f["observed_on"], "side": owner_id}
        for f in _active_facts(db, owner_id, subject_id)]
    # 家长授权给这次协商的事实也要记进依据。
    # 否则家长后来更正自己这边的记录时，匹配不到送礼者名下那件正按旧值办的事，
    # 旧方案不会被作废，用户会看到过时的方案还能确认。
    other = "B" if owner_id == "A" else "A"
    b_subject_for_share = _b_side_person_id(db, subject_id) if other == "B" else subject_id
    if b_subject_for_share:
        shared_attrs = _shared_attrs_for(db, b_subject_for_share, owner_id)
        for f in _active_facts(db, other, b_subject_for_share):
            if f["attribute"] in shared_attrs:
                facts_used.append({
                    "attribute": f["attribute"], "value": f["value"],
                    "provider": f["provider"], "observed_on": f["observed_on"],
                    "side": other,
                })
    # 送礼对象档案条目也记进依据：每条指回具体 event_id，让「依据来自哪几条」可追溯。
    for _ge in (a_ctx.get("gift_target_profile") or {}).get("entries") or []:
        facts_used.append({
            "attribute": "送礼档案:" + _ge["category"], "value": _ge["value"],
            "provider": _ge.get("provider", ""), "observed_on": _ge.get("happened_on", ""),
            "side": owner_id, "basis": "gift_profile", "event_id": _ge["event_id"]})

    db.execute("UPDATE task SET stage='negotiating' WHERE task_id=?", (task_id,))
    db.audit(task_id, owner_id, "extracted", extracted)

    b_ctx = None
    # 版本号从该事项已有最大版本之后接着排。事实被纠正后重新协商时，
    # 旧方案仍留在库里，若还从 1 开始就会和新方案同 version，
    # 之后「是不是最新方案」就分不清了。
    max_version_row = db.row(
        "SELECT MAX(version) AS v FROM proposal WHERE task_id=?", (task_id,))
    version = (max_version_row["v"] or 0) + 1
    feedback = None
    final = None
    trace = []

    for round_no in range(1, max_rounds + 1):
        try:
            a_prop = _a_propose(db, client, task_id, a_ctx, products, version, feedback)
        except ModelUnavailable as exc:
            db.execute("UPDATE task SET stage='model_failed', outcome=? WHERE task_id=?",
                       (str(exc), task_id))
            # 没连上模型不是「在等对方回复」：两者的下一步完全不同。
            # 前者要重试，后者要等或换路。
            _service.clear_waiting(db, task_id)
            return {"stage": "model_failed", "outcome": str(exc)}
        product = _product_index(db).get(a_prop.get("product_id") or "")
        ok, problems, notes = _check_hard_conditions(db, extracted, a_prop, product)
        # 候选在这一轮就落库：之后用户想换备选时，还能看到当时模型给了哪些、
        # 为什么给。换候选必须重走硬条件检查，不能绕过。
        _service.save_candidates(db, task_id, version, a_prop)
        trace.append({"round": round_no, "actor": "A", "action": "propose",
                      "product_id": a_prop.get("product_id"), "reason": a_prop.get("reason"),
                      "conditions": a_prop.get("conditions")})
        _log_message(db, task_id, "A", "propose",
                     {"version": version, "proposal": a_prop,
                      "program_check": {"ok": ok, "problems": problems, "notes": notes}})
        if not ok:
            feedback = {"type": "program_reject", "problems": problems, "notes": notes}
            trace.append({"round": round_no, "actor": "system", "action": "reject",
                          "problems": problems, "notes": notes})
            version += 1
            if round_no == max_rounds:
                db.execute("UPDATE task SET stage='failed', outcome=? WHERE task_id=?",
                           ("连续 %d 轮未通过硬条件检查：%s" % (max_rounds, "；".join(problems)), task_id))
                # 已经收尾：不再「在等谁」，否则用户会看到一条永远等不到回复的记录。
                _service.clear_waiting(db, task_id)
                return {"stage": "failed",
                        "outcome": "连续 %d 轮未通过硬条件检查" % max_rounds,
                        "problems": problems, "trace": trace}
            continue

        if b_ctx is None:
            b_subject = db.row(
                "SELECT person_id FROM person WHERE owner_id='B' AND name="
                " (SELECT name FROM person WHERE person_id=?)", (subject_id,))
            b_ctx = _b_context(db, b_subject["person_id"] if b_subject else subject_id,
                               a_prop, extracted)
            b_ctx["product_traits"] = _product_traits(db)

        shared_conditions = list(a_prop.get("conditions") or [])
        try:
            b_dec = _b_review(db, client, task_id, b_ctx, a_prop, products, version)
        except ModelUnavailable as exc:
            db.execute("UPDATE task SET stage='model_failed', outcome=? WHERE task_id=?",
                       (str(exc), task_id))
            _service.clear_waiting(db, task_id)
            return {"stage": "model_failed", "outcome": str(exc)}
        trace.append({"round": round_no, "actor": "B", "action": b_dec["decision"],
                      "reason": b_dec.get("reason"),
                      "counter_product_id": b_dec.get("counter_product_id")})
        _log_message(db, task_id, "B", b_dec["decision"], {"version": version, "review": b_dec})

        # 家长一侧同一个人的记录，和家长在本事项确认过的当前鞋码。
        # accept 和 counter 两个分支都要用，所以放在分支外面。
        b_subject_id = _b_side_person_id(db, subject_id)
        parent_size = _parent_confirmed_size(db, task_id)

        if b_dec["decision"] == "accept":
            # A 单方面给出的尺码不能直接执行：A 那条记录可能是旧的
            # （例：A 记着 31 码，B 现在脚长 20.5 cm、脚偏宽）。
            # 只有 B 在同一轮里明确给出尺码，才把它写进双方约定。
            size_ok, size_problem, need_parent = _size_agreed(
                db, product, a_prop, b_dec, b_subject_id, parent_size)
            if not size_ok:
                trace.append({"round": round_no, "actor": "system", "action": "reject",
                              "problems": [size_problem]})
                # 缺的是家长才能给的当前鞋码：这时候应该停下来问家长，
                # 让同一件事等家长补一句，而不是反复重试直到判失败。
                if need_parent:
                    question = ("这件要 %s。%s" % (product["name"], size_problem))
                    db.execute("UPDATE task SET stage='blocked', outcome=? WHERE task_id=?",
                               (question, task_id))
                    db.audit(task_id, "B", "blocked_need_size",
                             {"problem": size_problem, "product_id": product["product_id"],
                              "asked_size": (b_dec.get("asked_size") or "").strip()})
                    # 卡在只有家长能给的当前鞋码上：这件事现在在等对方本人。
                    # 未回应不是同意，也不能显示成已经约好，所以要把等待记下来，
                    # 让用户看到「在等谁、等什么、从什么时候开始等」。
                    _service.set_waiting(db, task_id, "B", "awaiting_person", question,
                                         {"need": "size", "product_id": product["product_id"]})
                    return {"stage": "blocked", "outcome": question,
                            "question": question, "need": "size",
                            "problems": [size_problem], "trace": trace}
                feedback = {"type": "program_reject", "problems": [size_problem],
                            "notes": ["尺码必须由家长一方确认后才能执行"]}
                version += 1
                if round_no == max_rounds:
                    db.execute("UPDATE task SET stage='failed', outcome=? WHERE task_id=?",
                               ("连续 %d 轮未通过硬条件检查：%s" % (max_rounds, size_problem),
                                task_id))
                    # 已经收尾：不再「在等谁」，否则用户会看到一条永远等不到回复的记录。
                    _service.clear_waiting(db, task_id)
                    return {"stage": "failed",
                            "outcome": "连续 %d 轮未通过硬条件检查" % max_rounds,
                            "problems": [size_problem], "trace": trace}
                continue
            # 家长已经在本事项明确确认过当前鞋码时，那就是这件事的既定尺码，
            # 重新协商不能让模型随口换个数值把它推翻。
            agreed_size = parent_size or _agreed_size(product, a_prop, b_dec)
            # 记下尺码是谁确认的：执行用的必须是家长这一侧给出的当前值。
            # 数值即使和 A 的旧记录相同，依据也是家长本次的确认，不是 A 的旧记忆。
            if product["has_size"] and agreed_size:
                db.audit(task_id, "B", "size_confirmed",
                         {"a_asserted_size": (a_prop.get("asked_size") or "").strip(),
                          "b_confirmed_size": agreed_size})
            pid = _new_proposal(db, task_id, version, "A", a_prop.get("product_id"),
                                a_prop.get("reason"), shared_conditions, facts_used,
                                asked_size=agreed_size)
            _decide_proposal(db, pid, "B", "accept", b_dec.get("reason"))
            final = {
                "stage": "agreed",
                "proposal_id": pid,
                "version": version,
                "product_id": a_prop.get("product_id"),
                "product_name": product["name"],
                "price_cny": product["price_cny"],
                "asked_size": a_prop.get("asked_size", ""),
                "needs_size": product["has_size"],
                "reason": a_prop.get("reason"),
                "b_reason": b_dec.get("reason"),
                "shared_conditions": shared_conditions,
                "facts_used": facts_used,
                "trace": trace,
            }
            db.execute("UPDATE task SET stage='agreed' WHERE task_id=?", (task_id,))
            # 谈成之后要把 outcome 一起更新。换过商品或更正过记录的事项，
            # outcome 里还留着上一轮「要重新协商」的原因；阶段已经走到约定，
            # 那句过期原因再挂在库里，换个界面读它就会自相矛盾。
            db.execute(
                "UPDATE task SET outcome='' WHERE task_id=? AND outcome LIKE '%%重新协商%%'",
                (task_id,))
            # 双方已经同版本接受：之前记的等待都过了期，清掉，
            # 不让用户在卡片上还看到「在等谁」。
            _service.clear_waiting(db, task_id)
            return final

        if b_dec["decision"] == "counter" and b_dec.get("counter_product_id"):
            counter_product = _product_index(db).get(b_dec["counter_product_id"])
            ok2, problems2, notes2 = _check_hard_conditions(db, extracted,
                {"asked_size": b_dec["counter_product_id"] and ""}, counter_product)
            if ok2:
                # 反提案的尺码同样要过证据校验：家长在本事项确认过当前鞋码时
                # 沿用那个值；否则必须能追溯到一条当前的鞋码记录，
                # 不能让模型随口说一个数值就进方案。
                counter_size = _parent_confirmed_size(db, task_id) \
                    or (b_dec.get("asked_size") or "").strip()
                if counter_product is not None and counter_product["has_size"] \
                        and counter_size:
                    c_ok, _c_evidence, c_problem = _service._size_evidence(
                        db, b_subject_id, counter_size, counter_product)
                    if not c_ok:
                        trace.append({"round": round_no, "actor": "system",
                                      "action": "reject_counter",
                                      "problems": [c_problem]})
                        question = "这件要 %s。%s" % (counter_product["name"], c_problem)
                        db.execute("UPDATE task SET stage='blocked', outcome=?"
                                   " WHERE task_id=?", (question, task_id))
                        db.audit(task_id, "B", "blocked_need_size",
                                 {"problem": c_problem, "via": "counter",
                                  "asked_size": counter_size})
                        _service.set_waiting(db, task_id, "B", "awaiting_person", question,
                                             {"need": "size", "via": "counter",
                                              "product_id": counter_product["product_id"]})
                        return {"stage": "blocked", "outcome": question,
                                "question": question, "need": "size",
                                "problems": [c_problem], "trace": trace}
                    db.audit(task_id, "B", "size_confirmed",
                             {"a_asserted_size": (a_prop.get("asked_size") or "").strip(),
                              "b_confirmed_size": counter_size,
                              "via": "counter"})
                pid = _new_proposal(db, task_id, version, "B", b_dec["counter_product_id"],
                                    b_dec.get("reason"),
                                    shared_conditions + list(b_dec.get("conditions") or []), facts_used,
                                    asked_size=counter_size)
                _decide_proposal(db, pid, "A", "pending", "等待 A 接受对方的反提案")
                final = {
                    "stage": "counter_pending",
                    "proposal_id": pid,
                    "version": version,
                    "product_id": b_dec["counter_product_id"],
                    "product_name": counter_product["name"],
                    "price_cny": counter_product["price_cny"],
                    "asked_size": b_dec.get("asked_size", ""),
                    "needs_size": counter_product["has_size"],
                    # 反提案的理由归 B，不能沿用 A 旧提案的理由，否则商品/理由不一致。
                    "reason": b_dec.get("reason"),
                    "b_reason": b_dec.get("reason"),
                    "shared_conditions": shared_conditions + list(b_dec.get("conditions") or []),
                    "facts_used": facts_used,
                    "trace": trace,
                }
                db.execute("UPDATE task SET stage='counter_pending' WHERE task_id=?", (task_id,))
                # 同上：过期的事故原因不能跟着新阶段走。
                db.execute(
                    "UPDATE task SET outcome='' WHERE task_id=? AND outcome LIKE '%%重新协商%%'",
                    (task_id,))
                # 对方提了新方案，现在等送礼者本人定：接受还是再想想。
                # 这是「等我自己」，不是「等对方」——对方已经回过话了。
                _service.set_waiting(
                    db, task_id, owner_id, "awaiting_my_decision",
                    "对方提了新方案（%s），等你定：接受这个方案，还是再想想。"
                    % (counter_product["name"] if counter_product else ""),
                    {"need": "decision", "version": version,
                     "proposal_id": pid, "counter_product_id": b_dec["counter_product_id"]})
                return final
            feedback = {"type": "program_reject", "problems": problems2, "notes": notes2}
            trace.append({"round": round_no, "actor": "system", "action": "reject_counter",
                          "problems": problems2, "notes": notes2})
        else:
            feedback = {"type": "b_review", "decision": b_dec["decision"],
                        "reason": b_dec.get("reason"), "conditions": b_dec.get("conditions")}
            trace.append({"round": round_no, "actor": "system", "action": "reject",
                          "reason": b_dec.get("reason")})
        version += 1
        if round_no == max_rounds:
            outcome = "协商未达成一致：%s" % (feedback.get("reason") or feedback.get("problems") or "超出协商轮次")
            db.execute("UPDATE task SET stage='conflict', outcome=? WHERE task_id=?",
                       (outcome, task_id))
            # 谈不成也是收尾：不能再显示成「在等对方回复」。
            _service.clear_waiting(db, task_id)
            return {"stage": "conflict", "outcome": outcome, "trace": trace}
    return {"stage": "failed", "outcome": "未达成一致", "trace": trace}


# ---------------------------------------------------------------------------
# 多人事项：多个家庭共同决定一次本地活动。
#
# 和送礼协商的分工一致：模型负责读各方授权条件、提候选和理由，
# 程序负责检查是否各方都接受了同一版本。未回复不是同意，
# 一方改时间，受影响的旧接受失效。


def _plan_allowed_keys(scenario="group_activity"):
    """方案字段白名单。键值和用户改条件的字段保持一致（按场景取）。

    在函数里取而不是模块顶层：service 正在初始化时 agents 就被导入，
    顶层取会踩到循环导入。
    """
    return _service.condition_keys(scenario)


def _plan_context(db, plan_id):
    """构造规划上下文：各方自己授权提供的条件。

    只取参与方自己填的那一份，不替别家补条件，也不把别家的条件
    说成是这一方提出的。缺的一方就是缺的。
    """
    st = _service.plan_status(db, plan_id)
    if st is None:
        return None
    kind = (db.row("SELECT kind FROM plan WHERE plan_id=?", (plan_id,)) or {}).get("kind", "group_activity")
    if kind == "friend_trip":
        # 朋友旅行走自己的上下文：不带家庭专属字段（孩子安排/孩子出席/家庭），
        # 只带各方声明的人数与条件；随行姓名不进共同事项。
        return _friend_trip_context(db, plan_id, st)
    sides = []
    for s in st["participants"]:
        if s.get("role") in ("guest_pending", "guest_revoked", "retired"):
            continue
        attendance = json.loads((db.row("SELECT attendance_json FROM plan_participant WHERE participant_id=?", (s["participant_id"],)) or {}).get("attendance_json") or "{}")
        # Attendance is voluntarily supplied for this matter. It does not grant
        # access to all stored age/health/preferences or the owner's contacts.
        children = [{"person_id": c["person_id"], "姓名": c["name"],
                     "出席": attendance[c["person_id"]], "年龄": "未知（未取得本事项字段授权）",
                     "依据": {"owner_id": s["owner_id"], "participant_id": s["participant_id"],
                              "source": "这一方本人在本事项确认的孩子出席"}}
                    for c in _service._own_children(db, s["owner_id"])
                    if attendance.get(c["person_id"]) in ("coming", "not_coming")]
        if s.get("role") == "guest":
            children = [{"姓名": c["name"], "出席": c["attendance"], "年龄": "未知", "依据": _service.PARTICIPATION_NOTE} for c in attendance.get("guest_children", []) if c["attendance"] in ("coming", "not_coming")]
            attendance = {c["name"]: c["attendance"] for c in attendance.get("guest_children", [])}
        sides.append({
            "owner_id": s["owner_id"],
            "家庭": s["label"],
            "本人确认的孩子出席": attendance,
            "本事项已授权孩子": children,
            "已确认出席人数": sum(c["出席"] == "coming" for c in children),
            "资料边界": "未确认出席的孩子不送入；年龄/偏好无本事项授权时未知，不读取整本档案",
            "这一方的条件": s["conditions"],
            "是否必须参加": "是" if s["required"] else "否",
            # 条件和表态是两件事：条件是这个家庭已经给出的限制，
            # 表态是它对某一版方案还没回复。混在一起会让模型把
            # 「还没回复」当成「还没给条件」，于一上来就产出
            # 一版全是「待确认」的方案。分开写清楚。
            "当前表态": {"accept": "已接受", "reject": "不接受",
                         "counter": "提了修改意见", "pending": "还没回复",
                         "stale": "接受的是旧版本，已失效"}.get(s["state"], s["state"]),
        })
    previous = dict(st["proposal"] or {})
    last = db.row("SELECT provenance_json FROM plan_proposal WHERE plan_id=? ORDER BY version DESC LIMIT 1", (plan_id,))
    if last and json.loads(last["provenance_json"] or "{}") != _service.plan_provenance(db, plan_id):
        # Partial value matching cannot erase paraphrases in notes/child arrangements.
        # Preserve the historical version in DB, but never reuse it as live authority.
        previous = {}
    for link in db.rows("SELECT * FROM event_plan_link WHERE plan_id=? AND status<>'active'", (plan_id,)):
        for key, value in json.loads(link["public_conditions_json"]).items():
            if previous.get(key) == value:
                previous.pop(key, None)
    return {
        "事项": st["title"],
        "用户原话": st["utterance"],
        "当前版本": st["current_version"],
        "上一版方案": previous,
        "各方条件": sides,
        "还没回复的家庭": st["waiting_on"],
        "是否已约定": st["agreed"],
        "目标家庭数": _service._plan_meta(db, plan_id).get("requested_families", 0),
        "确认缺口": st.get("confirmation_gaps", []),
    }


def _friend_trip_context(db, plan_id, st):
    """朋友旅行上下文：只带各方自己授权的人数(成人/孩子/待定)与条件。

    不带家庭专属字段(孩子安排/孩子出席/家庭)。随行姓名由声明人自己决定填不填：
    填了的进共同事项，模型据此安排名额与预算；没配自己助手的随行者标注代决策，
    代决策不是本人同意。顶层键与家庭上下文对齐，供 plan_propose/plan_replan 读取。
    """
    sides = []
    for s in st["participants"]:
        if s.get("role") in ("guest_pending", "guest_revoked", "retired"):
            continue
        hc = s.get("headcount") or {}
        comps = s.get("companions") or []
        sides.append({
            "owner_id": s["owner_id"],
            "参与方": s["label"],
            "一方人数与出席": {k: hc.get(k, 0) for k in ("adults", "kids", "tbd")},
            # 声明人自己决定说出来的随行：有名有姓，模型可按真实人数与需求安排。
            "随行": [{"称呼": c["name"],
                      "出席": {"coming": "来", "not_coming": "不来", "tbd": "待定"}.get(c.get("attendance"), "待定"),
                      "谁决策": "本人（其配有自己的助手）" if c.get("has_agent")
                               else "由本参与方代决策（其未配置自己的助手）"}
                     for c in comps],
            "这一方的条件": s["conditions"],
            "是否必须参加": "是" if s["required"] else "否",
            "当前表态": {"accept": "已接受", "reject": "不接受", "counter": "提了修改意见",
                         "pending": "还没回复", "stale": "接受的是旧版本，已失效"}.get(s["state"], s["state"]),
            "资料边界": "只带本人声明的人数、随行与条件；不读整本档案；代决策不算本人同意",
        })
    return {
        "场景": "朋友旅行",
        "事项": st["title"],
        "用户原话": st["utterance"],
        "当前版本": st["current_version"],
        "上一版方案": dict(st["proposal"] or {}),
        "各方条件": sides,
        "还没回复的家庭": st["waiting_on"],
        "是否已约定": st["agreed"],
        "目标家庭数": 0,
        "确认缺口": st.get("confirmation_gaps", []),
    }


def _friend_budget_violation(ctx, content):
    """朋友旅行预算校验：本人预算上限只能填提案方自己给出的金额；
    人均预算只能来自某方自己给出的，不合成、不相加、不冒认。"""
    sides = ctx.get("各方条件", [])
    caps = {s["owner_id"]: s.get("这一方的条件", {}).get("本人预算上限")
            for s in sides if s.get("这一方的条件", {}).get("本人预算上限")}
    bad = []
    cap = content.get("本人预算上限")
    if cap:
        proposer = ctx.get("提案方")
        mine = caps.get(proposer) if proposer else None
        if not mine or str(cap).strip() != str(mine).strip():
            bad.append("本人预算上限只能填提案方自己给出的金额，不能改数或替别人定")
    pc = content.get("人均预算")
    if pc:
        vals = {str(s.get("这一方的条件", {}).get("人均预算")).strip()
                for s in sides if s.get("这一方的条件", {}).get("人均预算")}
        if str(pc).strip() not in vals:
            bad.append("人均预算没有一致授权依据，不能合成或冒认")
    return bad


def _budget_violation(ctx, content, scenario="group_activity"):
    """Validate amounts semantically, but never invent or aggregate caps."""
    if scenario == "friend_trip":
        return _friend_budget_violation(ctx, content)
    import re
    from decimal import Decimal
    def amount(v):
        nums = re.findall(r"(?<![0-9.])[0-9]+(?:\.[0-9]+)?", str(v).replace(",", ""))
        return Decimal(nums[0]) if len(nums) == 1 else None
    def same(a, b):
        x, y = amount(a), amount(b)
        return (x is not None and y is not None and x == y) or str(a).strip() == str(b).strip()
    sides = ctx.get("各方条件", [])
    totals = [s["这一方的条件"]["活动总预算"] for s in sides if s.get("这一方的条件", {}).get("活动总预算")]
    caps = [s["这一方的条件"]["本人出资上限"] for s in sides if s.get("这一方的条件", {}).get("本人出资上限")]
    bad = []
    total = content.get("活动总预算")
    cap = content.get("本人出资上限")
    if total and (not totals or not all(same(total, t) for t in totals)):
        bad.append("活动总预算没有一致的授权金额依据，不能把上限相加")
    if cap:
        proposer = ctx.get("提案方")
        mine = [s.get("这一方的条件", {}).get("本人出资上限") for s in sides if s.get("owner_id") == proposer]
        basis = [v for v in mine if v] if proposer else caps if len(caps) == 1 else []
        valid = not isinstance(cap, dict) and len(basis) == 1 and same(cap, basis[0])
        if isinstance(cap, dict):
            valid = bool(cap)
            seen = set()
            for key, value in cap.items():
                matches = [s for s in sides if key in (s.get("owner_id"), s.get("家庭"))]
                if len(matches) != 1 or matches[0].get("owner_id") in seen or not matches[0].get("这一方的条件", {}).get("本人出资上限") or not same(value, matches[0]["这一方的条件"]["本人出资上限"]):
                    valid = False
                    break
                seen.add(matches[0].get("owner_id"))
        # A labeled per-side list is not an aggregate scalar. Every number must
        # retain an explicit authorized owner/label and match that side's cap.
        if not valid and isinstance(cap, str):
            entries = [e.strip() for e in re.split(r"[；;\n]", cap) if e.strip()]
            seen = set()
            valid = bool(entries)
            for entry in entries:
                pair = re.split(r"[:：]", entry, maxsplit=1)
                matches = [s for s in sides if len(pair) == 2 and pair[0].strip() in
                           (s.get("家庭"), s.get("owner_id"), str(s.get("owner_id")) + " 家庭")]
                if len(matches) != 1:
                    valid = False
                    break
                side = matches[0]
                owner = side.get("owner_id")
                expected = side.get("这一方的条件", {}).get("本人出资上限")
                if owner in seen or not expected or not same(pair[1], expected):
                    valid = False
                    break
                seen.add(owner)
        if not valid:
            bad.append("本人出资上限缺少对应本人依据，不能合成、冒认或改变金额")
    return bad


def _guard_plan_model(fn):
    import functools
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        return _service.plan_model_guard(fn)(*args, **kwargs)
    return wrapped


@_guard_plan_model
def plan_propose(db, client, plan_id, proposer="A", feedback=None):
    """模型读各方授权条件，提出一版候选方案。

    候选必须落在允许的字段里，不替任何一方决定他没给过的条件。
    """
    ctx = _plan_context(db, plan_id)
    if ctx is None:
        raise ValueError("事项不存在")
    if not any(s["owner_id"] == proposer for s in ctx["各方条件"]):
        raise ValueError("你在这个事项里没有代表的一方")
    ctx["提案方"] = proposer
    _kind = (db.row("SELECT kind FROM plan WHERE plan_id=?", (plan_id,)) or {}).get("kind", "group_activity")
    system = (
        "你是活动规划助手。多个家庭要一起办一次本地活动，"
        "你只能依据「各方条件」里每个家庭自己提供的条件来安排。\n"
        "「这一方的条件」就是这个家庭已经给出的限制，照着排就行。"
        "「当前表态」说的是某个家庭对上一版方案的态度，和给没给条件是两件事："
        "还没表态不等于没有条件，也不能把条件写成「待确认」这类占位。\n"
        "只输出一个 JSON 对象，字段：\n"
        "  content: 对象，只能包含这些键：%s。\n"
        "  reason: 字符串，一两句说明这版怎么兼顾各方条件的。\n"
        "  conflicts: 字符串数组，列出你没法同时满足的条件，没有就空数组。\n"
        "规则：\n"
        "1. 不能替任何一方决定他没给过的条件，也不能把别家的条件安到另一家头上。\n"
        "2. 第一版按已给的时间、地点、活动内容排候选；预算没依据必须留空。"
        "未授权人数与出席只列确认缺口，不编造安排。\n"
        "3. 有家庭还没回复时，同样照已给条件安排，只把「还没得到谁确认」"
        "写进 conflicts，不改写条件本身。\n"
        "4. 条件确实冲突时，选一个能满足最多必要家庭的安排，把冲突如实写进 conflicts，"
        "不要假装各方都满意。\n"
        "5. 不安排真实订票、付款或对外发消息。\n"
        "6. 「本人出资上限」只是这一家自己最多愿意出多少，不是他同意出这个数，"
        "更不是承诺。不许把各家的上限相加当成「活动总预算」——上限加起来"
        "没有任何一家同意过，拿它当总预算就是替所有家庭做了承诺。\n"
        "7. 「活动总预算」只能来自某一家自己给出的总预算；没人给就留空，"
        "并在 conflicts 里写清「还没有任何一家给出活动总预算」。\n"
        "8. 不给任何家庭自动安排出资、分工或代表权：谁出多少、谁负责什么，"
        "都由那一方自己说。\n9. content 的「本人出资上限」仅填提案方自己的原金额或空串；其他家庭逐家上限保留在备注，不给未知家庭编值。" % "、".join(_plan_allowed_keys(_kind))
    )
    if _kind == "friend_trip":
        # 朋友旅行用旅行口径的提示词：不提家庭/孩子/活动总预算那套，
        # 只依据各方自己给的条件与声明的人数（不含随行姓名）来排。
        system = (
            "你是出行规划助手。几个朋友要一起出去玩，"
            "你只能依据「各方条件」里每个参与方自己提供的条件来安排。\n"
            "「这一方的条件」就是这个参与方已经给出的限制，照着排就行。"
            "「一方人数与出席」是这一方自己声明的 成人/孩子/待定 人数，不含姓名，"
            "你不要追问或编造随行的是谁。\n"
            "「当前表态」说的是某个参与方对上一版方案的态度，和给没给条件是两件事："
            "还没表态不等于没有条件，也不能把条件写成「待确认」这类占位。\n"
            "只输出一个 JSON 对象，字段：\n"
            "  content: 对象，只能包含这些键：%s。\n"
            "  reason: 字符串，一两句说明这版怎么兼顾各方条件的。\n"
            "  conflicts: 字符串数组，列出你没法同时满足的条件，没有就空数组。\n"
            "规则：\n"
            "1. 不能替任何一方决定他没给过的条件，也不能把别家的条件安到另一方头上。\n"
            "2. 每个人想去的目的地、假期长度、住宿标准可能不一样，第一版按已给条件排，"
            "把调不齐的写进 conflicts，不强行统一；预算没依据必须留空。\n"
            "3. 有参与方还没回复时，同样照已给条件安排，只把「还没得到谁确认」写进 conflicts。\n"
            "4. 条件确实冲突时，选一个能满足最多必要参与方的安排，把冲突如实写进 conflicts，"
            "不要假装各方都满意。\n"
            "5. 不安排真实订票、付款或对外发消息。\n"
            "6. 「本人预算上限」只是这一方自己最多愿意出多少，不是他同意出这个数，更不是承诺；"
            "不许把各方上限相加，也不拿「人均预算」当成谁答应了的总价。\n"
            "7. 「人均预算」只能来自某一方自己给出的人均预算；没人给就留空。\n"
            "8. 能否带伴侣等由那一方自己说，不自动替他决定。\n"
            "9. content 的「本人预算上限」仅填提案方自己的原金额或空串，其他方上限保留在备注，不编值。"
            % "、".join(_plan_allowed_keys(_kind))
        )
    user = {
        "各方条件": ctx["各方条件"],
        "上一版方案": ctx["上一版方案"],
        "当前版本": ctx["当前版本"],
        "还没回复的家庭": ctx["还没回复的家庭"],
        "目标家庭数": ctx["目标家庭数"], "确认缺口": ctx["确认缺口"],
        "提案方": proposer,
    }
    if feedback:
        user["上一版的修改意见"] = feedback
    data = client.chat_json([
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ])
    content = data.get("content") if isinstance(data.get("content"), dict) else {}
    # 只保留允许的键，模型多给的字段不进方案。
    content = {k: v for k, v in content.items() if k in _plan_allowed_keys(_kind)}
    conflicts = [str(c) for c in data.get("conflicts", [])][:6] \
        if isinstance(data.get("conflicts"), list) else []
    # 预算/出资口径由代码判，不靠提示词自觉：判不出依据就不落版本。
    # 落一个把各家上限相加得到的总预算，等于替所有家庭做了承诺。
    bad = _budget_violation(ctx, content, scenario=_kind)
    if bad:
        raise ValueError("这一版的预算没有依据：" + "；".join(bad))
    ppid, version = _service.new_plan_proposal(
        db, plan_id, proposer, content,
        data.get("reason") if isinstance(data.get("reason"), str) else "")
    db.audit(plan_id, proposer, "plan_proposed",
             {"version": version, "content": content, "conflicts": conflicts})
    return {"plan_proposal_id": ppid, "version": version, "content": content,
            "reason": data.get("reason") or "", "conflicts": conflicts}


def _replan_changes(db, plan_id):
    """这次重排是因为谁改了什么条件。

    只从审计日志里取最近一次「更新条件」记录。模型需要知道
    改动本身：上一版方案是按照旧条件排的，只说「有家庭改了条件」
    而不说改了什么，模型很容易顺着上一版继续排，把已经作废的
    时间又排回来。缺记录时返回空，调用方照常重排。
    """
    audit = db.row(
        "SELECT detail_json FROM audit_log WHERE task_id=? AND"
        " action='plan_condition_updated' ORDER BY created_at DESC LIMIT 1",
        (plan_id,))
    if audit is None:
        return []
    try:
        detail = json.loads(audit["detail_json"] or "{}")
    except (ValueError, TypeError):
        return []
    return [{
        "家庭": detail.get("label") or "",
        "改了什么": detail.get("reason") or "",
        "改动字段": detail.get("changed_fields") or [],
    }]


@_guard_plan_model
def plan_replan(db, client, plan_id, feedback=None):
    """有家庭改了自己的条件后，依据各家当前条件重拟一版一致方案。

    和 plan_propose 的区别只在输入：这里明确告诉模型「有家庭刚改过条件」，
    要按各方当前条件重排，而不是接着上一版改字。上一版里和已改条件
    冲突的内容（例如时间是周六上午、活动内容还写着 14:00 开始）
    必须重写成互相一致的一版。

    重排后落成新版本，各家对旧版本的接受随之失效，要重新表态。
    """
    ctx = _plan_context(db, plan_id)
    if ctx is None:
        raise ValueError("事项不存在")
    ctx["提案方"] = db.row("SELECT owner_id FROM plan WHERE plan_id=?", (plan_id,))["owner_id"]
    _kind = (db.row("SELECT kind FROM plan WHERE plan_id=?", (plan_id,)) or {}).get("kind", "group_activity")
    system = (
        "你是活动规划助手。有家庭刚刚改了自己的条件，你要依据「各方条件」里"
        "每个家庭当前的条件，重排一版内部一致的方案。\n"
        "「这一方的条件」就是该家庭当前有效的限制，以它为准，不要沿用上一版。\n"
        "只输出一个 JSON 对象，字段：\n"
        "  content: 对象，只能包含这些键：%s。\n"
        "  reason: 字符串，一两句说明这版怎么兼顾各方当前条件。\n"
        "  conflicts: 字符串数组，列出你没法同时满足的条件，没有就空数组。\n"
        "规则：\n"
        "1. 重排后方案内部必须一致：时间改了，活动内容、孩子安排、交通里"
        "提到的时间也要跟着改，不能留着和它矛盾的内容。\n"
        "2. 不能替任何一方决定他没给过的条件，也不能把别家的条件安到另一家头上。\n"
        "3. 有家庭还没表态时，照各方当前条件安排，只把「还没得到谁确认」"
        "写进 conflicts。\n"
        "4. 条件确实冲突时，选一个能满足最多必要家庭的安排，把冲突如实写进 conflicts，"
        "不要假装各方都满意。\n"
        "5. 不安排真实订票、付款或对外发消息。\n"
        "6. 「本人出资上限」只是这一家自己最多愿意出多少，不是他同意出这个数，"
        "更不是承诺。不许把各家的上限相加当成「活动总预算」——上限加起来"
        "没有任何一家同意过，拿它当总预算就是替所有家庭做了承诺。\n"
        "7. 「活动总预算」只能来自某一家自己给出的总预算；没人给就留空，"
        "并在 conflicts 里写清「还没有任何一家给出活动总预算」。\n"
        "8. 不给任何家庭自动安排出资、分工或代表权：谁出多少、谁负责什么，"
        "都由那一方自己说。\n9. content 的「本人出资上限」仅填提案方自己的原金额或空串；其他家庭逐家上限保留在备注，不给未知家庭编值。" % "、".join(_plan_allowed_keys(_kind))
    )
    if _kind == "friend_trip":
        # 朋友旅行重排：按各方当前条件（旅行字段）重排，不提家庭/孩子/活动总预算。
        system = (
            "你是出行规划助手。有参与方刚改了自己的条件，你要依据「各方条件」里"
            "每个参与方当前的条件，重排一版内部一致的旅行安排。\n"
            "以各方当前有效的条件为准，不要沿用上一版；把调不齐的写进 conflicts，"
            "不强行统一，也不用常见行程凑数。\n"
            "只输出一个 JSON 对象，字段：\n"
            "  content: 对象，只能包含这些键：%s。\n"
            "  reason: 字符串，一两句说明这版怎么兼顾各方当前条件。\n"
            "  conflicts: 字符串数组，列出你没法同时满足的条件，没有就空数组。\n"
            "规则：不替任何一方决定他没给过的条件；「本人预算上限」只填提案方自己的金额，"
            "不把各方上限相加、不拿「人均预算」当总价；能否带伴侣等由那一方自己说；"
            "不安排真实订票、付款或对外发消息。"
            % "、".join(_plan_allowed_keys(_kind))
        )
    user = {
        "各方当前条件": ctx["各方条件"],
        "上一版方案": ctx["上一版方案"],
        "当前版本": ctx["当前版本"],
        "这次改了什么": _replan_changes(db, plan_id),
        "为什么重排": ctx["事项"],
        "目标家庭数": ctx["目标家庭数"], "确认缺口": ctx["确认缺口"],
    }
    if feedback:
        user["补充意见"] = feedback
    data = client.chat_json([
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ])
    content = data.get("content") if isinstance(data.get("content"), dict) else {}
    content = {k: v for k, v in content.items() if k in _plan_allowed_keys(_kind)}
    conflicts = [str(c) for c in data.get("conflicts", [])][:6] \
        if isinstance(data.get("conflicts"), list) else []
    # 和第一版同一道代码校验：重排同样不能落一个没有依据的总预算。
    bad = _budget_violation(ctx, content, scenario=_kind)
    if bad:
        raise ValueError("这一版的预算没有依据：" + "；".join(bad))
    # 重排失败（模型不可用、输出不可解析）会在这里抛出去，
    # 此时不能清 needs_replan：条件已改、方案还没重排，这个事实要留住，
    # 界面据此继续显示「要重排」，也不让各家对旧版本表态。
    ppid, version = _service.new_plan_proposal(
        db, plan_id, ctx["提案方"], content,
        data.get("reason") if isinstance(data.get("reason"), str) else "")
    # 新版本落库后才算重排完成：清掉待重排标记，
    # 各家可以按这一版重新表态，是否约定重新按当前版本判定。
    db.execute("UPDATE plan SET needs_replan=0 WHERE plan_id=?", (plan_id,))
    db.audit(plan_id, "A", "plan_replanned",
             {"version": version, "content": content, "conflicts": conflicts})
    return {"plan_proposal_id": ppid, "version": version, "content": content,
            "reason": data.get("reason") or "", "conflicts": conflicts}


@_guard_plan_model
def plan_review(db, client, plan_id, participant_id, version):
    """模型代替某一个家庭审阅这一版：接受、不接受，或提出修改。

    只看这一方自己的条件。审阅结果只代表这一方，不代表别家。
    """
    st = _service.plan_status(db, plan_id)
    if st is None:
        raise ValueError("事项不存在")
    side = None
    for s in st["participants"]:
        if s["participant_id"] == participant_id:
            side = s
            break
    if side is None:
        raise ValueError("参与方不存在")
    if side.get("role") == "guest" and db.row("SELECT 1 FROM plan_decision WHERE plan_id=? AND participant_id=? AND version=? AND decided_by='person' AND decision<>'stale'", (plan_id, participant_id, version)):
        raise ValueError("本人已明确表态，代表建议不能覆盖本人的决定")
    proposal = db.row(
        "SELECT * FROM plan_proposal WHERE plan_id=? AND version=?", (plan_id, version))
    if proposal is None:
        raise ValueError("这一版方案不存在")
    if json.loads(proposal.get("provenance_json") or "{}") != _service.plan_provenance(db, plan_id):
        raise ValueError("这一版引用的资料授权已变化，请先核对重排")
    # 校验必须在调用模型、写任何东西之前：
    # counter 会先落一个新版本再记录决定，如果等到那之后才校验，
    # 旧版本或待重排的候选已经被写成新版本，拒绝就留下了痕迹。
    if version != st["current_version"]:
        raise ValueError("只能审阅当前版本（第 %d 版），旧版本不能作为依据"
                         % st["current_version"])
    if st.get("needs_replan"):
        raise ValueError("有家庭刚更新了条件，方案要重排后才能审阅")
    content = json.loads(proposal["content_json"])
    _kind = (db.row("SELECT kind FROM plan WHERE plan_id=?", (plan_id,)) or {}).get("kind", "group_activity")
    system = (
        "你是%s这一方的个人代表。你只依据「我方的条件」判断这一版方案行不行。\n"
        "只输出一个 JSON 对象，字段：\n"
        "  decision: 字符串，只能是 accept / reject / counter 三者之一。\n"
        "  reason: 字符串，一句话说明为什么。\n"
        "  changes: 对象，你方要求的修改，只能包含这些键：%s；没有就空对象。\n"
        "规则：\n"
        "1. 方案满足我方条件时用 accept；不满足时用 reject 或 counter。\n"
        "2. 不要替别家表态，也不要把别家的条件当成我方的。\n"
        "3. 方案里没有提到我方在意的条件时，不要默认它没问题，要指出来。\n"
        % (side["label"], "、".join(_plan_allowed_keys(_kind)))
    )
    user = {
        "我方的条件": side["conditions"],
        "这一版方案": content,
        "方案说明": proposal["reason"],
        "版本": version,
    }
    data = client.chat_json([
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ])
    # 模型等待期间，别家可能已经改条件或落了新版本。
    # 这个迟到的回复不能先写进去：counter 会另起一个新版本，
    # 等到 decide_plan 才发现过期就晚了，旧候选已经留下了痕迹。
    # 所以在写任何东西之前，按当前状态再核一次。
    latest = _service.plan_status(db, plan_id)
    if latest is None:
        raise ValueError("事项不存在")
    if version != latest["current_version"]:
        raise ValueError("等待期间方案已更新到第 %d 版，这一版（第 %d 版）的回复不再适用"
                         % (latest["current_version"], version))
    if latest.get("needs_replan"):
        raise ValueError("等待期间有家庭刚更新了条件，方案要重排后才能审阅")
    decision = normalize_decision(data.get("decision")) or "ask_clarify"
    changes = data.get("changes") if isinstance(data.get("changes"), dict) else {}
    changes = {k: v for k, v in changes.items() if k in _plan_allowed_keys(_kind)}
    reason = data.get("reason") if isinstance(data.get("reason"), str) else ""
    # 反提案要落到新版本：这一方改了时间，别家对旧版本的接受随之失效。
    if decision == "counter" and changes:
        new_content = dict(content)
        new_content.update(changes)
        ctx = _plan_context(db, plan_id)
        ctx["提案方"] = side["owner_id"]
        bad = _budget_violation(ctx, new_content)
        if bad:
            raise ValueError("反提案预算没有依据：" + "；".join(bad))
        ppid, new_version = _service.new_plan_proposal(
            db, plan_id, side["owner_id"], new_content,
            "%s 提出修改：%s" % (side["label"], reason))
        _service.decide_plan(db, plan_id, participant_id, new_version, "counter",
                             reason, changes, actor_id=side["owner_id"],
                             decided_by="representative")
        db.audit(plan_id, side["owner_id"], "plan_countered",
                 {"participant_id": participant_id, "from_version": version,
                  "new_version": new_version, "changes": changes,
                  "decided_by": "representative"})
        return {"decision": "counter", "reason": reason, "changes": changes,
                "new_version": new_version, "plan_proposal_id": ppid,
                "decided_by": "representative"}
    # 代表审阅出来的结论：来源是 representative，不是这一方本人在手机上点的。
    # 界面上要分开显示，不能把「代表说可以」当成本人已确认。
    _service.decide_plan(db, plan_id, participant_id, version, decision, reason, changes,
                         actor_id=side["owner_id"], decided_by="representative")
    return {"decision": decision, "reason": reason, "changes": changes,
            "version": version, "decided_by": "representative"}
