"""近况事件的归属：获准查看不等于获准编辑，也不能往别人名下写。

不调模型、不连网络、用临时库。覆盖三处：
1. 普通入口不能拿别人记录里的人物建自己来源的事件；
2. 获准查看的人不能撤回记录所有者的那条事件；
3. 获准查看的人不能更正记录所有者的那条事件。

被拒时对方的状态、内容和授权都必须原样不动——只抛错不算数，
真改了再抛也一样是越权。

运行：/tmp/hack_venv/bin/python tests/test_life_event_ownership.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db as dbm
from app import service

TMP = Path("/tmp/pa_life_ownership.db")
results: list[tuple[str, bool, object]] = []


def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))


def fresh():
    if TMP.exists():
        TMP.unlink()
    db = dbm.DB(TMP)
    dbm.seed(db)
    return db


def event_state(db, event_id):
    row = db.row("SELECT * FROM life_event WHERE event_id=?", (event_id,))
    if row is None:
        return None
    grants = [dict(g) for g in
              db.rows("SELECT * FROM event_grant WHERE event_id=? ORDER BY grant_id", (event_id,))]
    return {"status": row["status"], "payload": row["payload_json"],
            "provider": row["provider"], "superseded_by": row["superseded_by"],
            "grants": grants}


def main():
    db = fresh()
    # B 记录里的小雨，A 被授权看她的部分字段。
    b_event = db.row(
        "SELECT event_id FROM life_event WHERE subject_id='p_xiaoyu_b'"
        " AND source_id='study_app'")
    check("准备：B 侧有一条孩子近况", b_event is not None,
          [r["source_id"] for r in db.rows("SELECT source_id FROM life_event")])
    event_id = b_event["event_id"]
    before = event_state(db, event_id)
    # 确认 A 确实能读到（被授权），否则后面的拒绝可能是「根本看不到」造成的。
    visible = service.life_events_for(db, "p_xiaoyu_b", "A")
    has_study = any(e["event_id"] == event_id for e in visible)
    check("准备：A 获准查看这条事件", has_study,
          [e["source_id"] for e in visible])

    # ---------- 1. 不能往别人记录里的人物名下写自己的事件 ----------
    try:
        service.import_life_event(
            db, "a_forged_app", "p_xiaoyu_b", "study_progress",
            {"本人愿意分享": "A 编的内容"}, "2026-10-14", "A 的来源",
            "A", [{"viewer_id": "B", "fields": ["本人愿意分享"], "valid_until": ""}])
        check("用别人记录的人物建事件被拒", False, "竟然建成功了")
    except ValueError as exc:
        check("用别人记录的人物建事件被拒", True, str(exc))
    forged = db.row("SELECT * FROM life_event WHERE source_id='a_forged_app'")
    check("被拒后没有留下这条记录", forged is None, forged and forged["event_id"])

    # 本人记录里的人物可以正常导入：这条 guard 不是把所有导入都堵死。
    # 参数按签名逐个传：owner_id 在 provider 之后，漏传会落回默认值 B，
    # 于是「A 导入 A 的人物」被误判成越权。
    ok_id, created = service.import_life_event(
        db, "a_own_app", "p_me", "daily_note",
        {"备注": "A 自己的一条记录"}, "2026-10-14", "A 本人", "A")
    check("本人记录的人物可以正常导入", bool(created), (ok_id, created))
    own_row = db.row("SELECT owner_id FROM life_event WHERE event_id=?", (ok_id,))
    check("导入记录的归属是本人", own_row and own_row["owner_id"] == "A", own_row)

    # ---------- 2. 获准查看的人不能撤回 ----------
    try:
        service.revoke_life_event(db, event_id, "A")
        check("非所有者撤回被拒", False, "竟然撤回了")
    except ValueError as exc:
        check("非所有者撤回被拒", True, str(exc))
    after_revoke = event_state(db, event_id)
    check("撤回被拒后状态没变",
          after_revoke["status"] == before["status"],
          (before["status"], after_revoke["status"]))
    check("撤回被拒后内容没变",
          after_revoke["payload"] == before["payload"],
          (before["payload"], after_revoke["payload"]))
    check("撤回被拒后授权没变",
          after_revoke["grants"] == before["grants"],
          (len(before["grants"]), len(after_revoke["grants"])))
    still = service.life_events_for(db, "p_xiaoyu_b", "A")
    check("撤回被拒后 A 仍能正常读到", any(e["event_id"] == event_id for e in still),
          [e["source_id"] for e in still])

    # 所有者本人可以撤回：这条 guard 不是把撤回功能关掉。
    service.revoke_life_event(db, event_id, "B")
    gone = service.life_events_for(db, "p_xiaoyu_b", "A")
    check("所有者撤回后查看方看不到",
          not any(e["event_id"] == event_id for e in gone),
          [e["source_id"] for e in gone])

    # ---------- 3. 获准查看的人不能更正 ----------
    db2 = fresh()
    ev2 = db2.row(
        "SELECT event_id FROM life_event WHERE subject_id='p_xiaoyu_b'"
        " AND source_id='study_app'")["event_id"]
    before2 = event_state(db2, ev2)
    try:
        service.correct_life_event(db2, ev2, {"已完成项目": "A 改的内容"}, "A")
        check("非所有者更正被拒", False, "竟然更正了")
    except ValueError as exc:
        check("非所有者更正被拒", True, str(exc))
    after_correct = event_state(db2, ev2)
    check("更正被拒后内容没变",
          after_correct["payload"] == before2["payload"],
          (before2["payload"], after_correct["payload"]))
    check("更正被拒后没有被替代标记",
          not after_correct["superseded_by"], after_correct["superseded_by"])
    check("更正被拒后授权没变",
          after_correct["grants"] == before2["grants"],
          (len(before2["grants"]), len(after_correct["grants"])))
    # 没有多出一条「已更正」的新记录。
    dup = db2.rows(
        "SELECT event_id FROM life_event WHERE source_id='study_app'"
        " AND subject_id='p_xiaoyu_b'")
    check("更正被拒后没有新增记录", len(dup) == 1, len(dup))

    # 所有者本人可以更正，且查看方读到的是新内容。
    new_id = service.correct_life_event(db2, ev2, {"已完成项目": "数学练习 30 题"}, "B")
    after_own = service.life_events_for(db2, "p_xiaoyu_b", "A")
    row = [e for e in after_own if e["source_id"] == "study_app"]
    check("所有者更正后查看方读到新内容",
          bool(row) and "30 题" in json.dumps(row[0]["fields"], ensure_ascii=False),
          row and row[0]["fields"])
    check("更正后旧记录标为被替代",
          (db2.row("SELECT status FROM life_event WHERE event_id=?", (ev2,)) or {})
          .get("status") == "superseded", new_id)

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(("[PASS] " if ok else "[FAIL] ") + name)
        if not ok:
            print("       " + repr(detail)[:600])
    print("\n通过 %d/%d" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
