"""配图的可见边界：字段授权不等于整张图授权。

不调模型、不连网络、用临时库。覆盖五处：
1. 记忆所有者本人能取到自己的配图；
2. 只有字段授权、没有配图授权的查看者取不到图；
3. 授权里明确写了配图标记才取得到；
4. 授权过期后重新取不到；
5. 从没授权的第三方取不到。

被拒时返回空列表，不是先返回图再报错——权限必须在查询时就拦住。

运行：/tmp/hack_venv/bin/python tests/test_event_attachment_guard.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db as dbm
from app import service

TMP = Path("/tmp/pa_event_attachment_guard.db")
results = []


def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))


def fresh():
    if TMP.exists():
        TMP.unlink()
    db = dbm.DB(TMP)
    dbm.seed(db)
    return db


def attach(db, event_id, owner_id, b64="ZHVtbXk=", media="image/png"):
    return service._save_event_attachments(
        db, event_id, owner_id, [{"b64": b64, "media": media}])["kept"]


def grant(db, event_id, viewer_id, fields):
    db.execute(
        "INSERT INTO event_grant(grant_id, event_id, viewer_id, fields_json,"
        " valid_until, note, created_at) VALUES(?,?,?,?,?,?,?)",
        (dbm.new_id("grant"), event_id, viewer_id, json.dumps(fields),
         "", "测试授权", dbm.now_iso()))


def main():
    db = fresh()
    # 找 A 名下的一个人即可：这条只验配图权限，不验识别。
    subject = db.row("SELECT * FROM person WHERE owner_id='A' LIMIT 1")
    sid = subject["person_id"]

    # 直接建一条事件，绕开模型调用。
    event_id, _ = service.import_life_event(
        db, "photo_guard_test", sid, "photo_record", {"科目": "数学"}, "",
        "拍照记录（A 拍摄，本人核对后采用）", owner_id="A", source_kind="photo")

    kept = attach(db, event_id, "A")
    check("本人配图已保存", len(kept) == 1, kept)

    mine = service.event_attachments(db, event_id, "A")
    check("记忆所有者能取到自己的配图", len(mine) == 1, len(mine))

    grant(db, event_id, "B", ["科目"])
    fields_only = service.event_attachments(db, event_id, "B")
    check("只授权字段取不到配图", fields_only == [], fields_only)

    grant(db, event_id, "B", ["科目", service.ATTACHMENT_GRANT_MARK])
    with_mark = service.event_attachments(db, event_id, "B")
    check("授权里写明配图标记才取得到", len(with_mark) == 1, len(with_mark))

    for g in db.rows("SELECT * FROM event_grant WHERE event_id=? AND viewer_id='B'",
                     (event_id,)):
        db.execute("UPDATE event_grant SET valid_until=? WHERE grant_id=?",
                   ("2020-01-01", g["grant_id"]))
    expired = service.event_attachments(db, event_id, "B")
    check("授权过期后取不到配图", expired == [], expired)

    other = service.event_attachments(db, event_id, "C")
    check("未授权的第三方取不到配图", other == [], other)

    TMP.unlink(missing_ok=True)
    ok = 0
    print("=" * 66)
    for name, passed, detail in results:
        print(("PASS " if passed else "FAIL ") + name)
        if not passed:
            print("       证据：" + repr(detail)[:300])
        ok += 1 if passed else 0
    print("=" * 66)
    print("配图授权边界：%d/%d 通过" % (ok, len(results)))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
