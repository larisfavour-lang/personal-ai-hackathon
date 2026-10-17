"""插件接口的收货字段边界：字段级授权、撤权失效、跨设备不泄露。

不调模型、不连网络、不写业务库：用临时库 + 直接构造已约定事项来触发
prepare/validate/save_receipt，只验后端 guard。这样每次改动都能立刻重跑，
不必等一次完整模型回归。

覆盖：
1. 只授权收货电话时，参数包只出电话，不出姓名和地址；
2. 准备之后单独撤掉电话授权，旧参数包 validate 被拒，且不能再记成 verified；
3. 准备之后改掉地址，旧参数包 validate 被拒；
4. 换一个新版本方案后，旧参数包 validate 被拒；
5. 另一台设备拿同一个幂等键收不到别人的回执；
6. 撤销设备后，它的参数包不能再校验。

运行：/tmp/hack_venv/bin/python tests/test_ext_guards.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db as dbm
from app import service

TMP = Path("/tmp/pa_ext_guards.db")
results: list[tuple[str, bool, object]] = []


def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))


def fresh():
    if TMP.exists():
        TMP.unlink()
    db = dbm.DB(TMP)
    dbm.seed(db)
    return db


def make_agreed_task(db, owner_id="A"):
    """造一件已经双方同版本接受的事项，直接进入可代填状态。"""
    # 授权全部三个收货字段，逐条用例再按需撤掉。
    db.execute("UPDATE authorization SET attributes_json=? WHERE owner_id=?"
               " AND scope_kind='delivery'",
               (json.dumps(["收货人", "收货电话", "收货地址"]), owner_id))
    task_id = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage,"
        " outcome, created_at) VALUES(?,?,?,?,?,?,?)",
        (task_id, owner_id, "给小周的女儿准备生日礼物，300元以内",
         json.dumps({"subject_id": "p_xiaoyu", "budget_max": 300}, ensure_ascii=False),
         "agreed", "", dbm.now_iso()))
    proposal_id = dbm.new_id("prop")
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id,"
        " reason, decision, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (proposal_id, task_id, 1, "A", "pr_draw_set", "科普绘本合适", "accept",
         dbm.now_iso()))
    # 对方同版本接受，prepare 的执行权判断才放行。
    # 对方的接受必须落在同一版本、同一提案人，并写明是哪一方决定的：
    # _counterparty_accept 同时按 version/proposer/decided_by 取行，
    # 少写 decided_by 会被当成无效接受，prepare 一直报「对方还没有接受」。
    db.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id,"
        " reason, decision, decided_by, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (dbm.new_id("prop"), task_id, 1, "A", "pr_draw_set", "可以", "accept", "B",
         dbm.now_iso()))
    return task_id


def make_device(db, owner_id="A", name="测试设备"):
    code = service.create_pair_code(db, owner_id, name)["code"]
    paired = service.pair_device(db, owner_id, code, name)
    return service.device_from_request(db, "Bearer " + paired["access_token"])


def fields_of(pkg):
    return {f["key"]: f["value"] for f in pkg.get("fields", [])}


def main():
    ORIGIN = "http://127.0.0.1:9000"

    # ---------- 1. 只授权电话：姓名和地址不能进参数包 ----------
    db = fresh()
    task_id = make_agreed_task(db)
    db.execute("UPDATE authorization SET attributes_json=? WHERE owner_id='A'"
               " AND scope_kind='delivery'",
               (json.dumps(["收货电话"]),))
    dev = make_device(db)
    pkg = service.prepare_snapshot(db, dev, task_id, "shopping_fill", ORIGIN)
    got = fields_of(pkg)
    check("只授权电话时参数包含电话", got.get("recipient_phone") == "13800000000",
          got.get("recipient_phone"))
    check("只授权电话时不含姓名",
          "recipient_name" not in got or not got.get("recipient_name"),
          sorted(got))
    check("只授权电话时不含地址",
          "delivery_address" not in got or not got.get("delivery_address"),
          sorted(got))
    check("缺的字段标进 missing_fields",
          set(pkg.get("missing_fields", [])) >= {"收货人", "收货地址"},
          pkg.get("missing_fields"))
    check("字段不全时 can_fill 为假", pkg.get("can_fill") is False, pkg.get("can_fill"))

    # ---------- 2. 先校验通过，再撤掉电话授权 ----------
    # 顺序很重要：必须先真的 validate 成功并记下 validated_at，再撤权。
    # 反过来做的话，save_receipt 只要抛 not_validated 就「通过」，
    # 根本没走进「此前已校验 → 撤权 → 再次回执」这个真实窗口。
    db2 = fresh()
    task2 = make_agreed_task(db2)
    dev2 = make_device(db2, name="撤权测试")
    pkg2 = service.prepare_snapshot(db2, dev2, task2, "shopping_fill", ORIGIN)
    check("全授权时 can_fill 为真", pkg2.get("can_fill") is True, pkg2.get("missing_fields"))
    v0 = service.validate_snapshot(db2, dev2, pkg2["snapshot_id"], ORIGIN,
                                   pkg2["product"]["id"], True)
    check("撤权前校验通过", v0.get("valid") is True, v0.get("reasons"))
    snap_row = db2.row("SELECT validated_at FROM ext_snapshot WHERE snapshot_id=?",
                       (pkg2["snapshot_id"],))
    check("校验通过后记下 validated_at", bool(snap_row and snap_row["validated_at"]),
          snap_row)
    r0 = service.save_receipt(db2, dev2, pkg2["snapshot_id"], ORIGIN,
                              fields_of(pkg2), "filled", "k-before-revoke")
    check("撤权前回执 verified", r0.get("status") == "verified", r0.get("status"))
    verified_before = db2.row(
        "SELECT COUNT(*) c FROM ext_receipt WHERE snapshot_id=? AND status='verified'",
        (pkg2["snapshot_id"],))["c"]

    # 现在只撤电话一项：姓名和地址授权仍在，旧包里仍带着电话。
    db2.execute("UPDATE authorization SET attributes_json=? WHERE owner_id='A'"
                " AND scope_kind='delivery'", (json.dumps(["收货人", "收货地址"]),))
    v = service.validate_snapshot(db2, dev2, pkg2["snapshot_id"], ORIGIN,
                                 pkg2["product"]["id"], True)
    check("撤掉电话授权后旧包被拒", v.get("valid") is False, v.get("reasons"))
    check("拒绝原因说明是哪一项",
          any("电话" in r for r in v.get("reasons", [])), v.get("reasons"))
    # 已经校验过的包，撤权后再用旧读回值提交一份新回执：不能变成 verified，
    # 也不能在库里多出一条 verified。
    r1 = service.save_receipt(db2, dev2, pkg2["snapshot_id"], ORIGIN,
                              fields_of(pkg2), "filled", "k-after-revoke")
    check("撤权后新回执不是 verified", r1.get("status") != "verified", r1.get("status"))
    verified_after = db2.row(
        "SELECT COUNT(*) c FROM ext_receipt WHERE snapshot_id=? AND status='verified'",
        (pkg2["snapshot_id"],))["c"]
    check("撤权后 verified 回执数不增加",
          verified_after == verified_before, (verified_before, verified_after))

    # ---------- 3. 改掉地址：旧包被拒 ----------
    db3 = fresh()
    task3 = make_agreed_task(db3)
    dev3 = make_device(db3, name="改址测试")
    pkg3 = service.prepare_snapshot(db3, dev3, task3, "shopping_fill", ORIGIN)
    before_addr = fields_of(pkg3).get("delivery_address")
    # 同样先校验通过，再改地址：只验「改完被拒」不够，
    # 要证明这个包本来是可用的，是改地址让它失效。
    v3_0 = service.validate_snapshot(db3, dev3, pkg3["snapshot_id"], ORIGIN,
                                     pkg3["product"]["id"], True)
    check("改址前校验通过", v3_0.get("valid") is True, v3_0.get("reasons"))
    db3.execute("UPDATE person SET attributes_json=? WHERE person_id="
                "(SELECT person_id FROM person WHERE owner_id='A' AND relation='self')",
                (json.dumps({"收货人": "测试用户", "收货电话": "13800000000",
                             "收货地址": "上海市静安区新地址 9 号（虚构）"},
                            ensure_ascii=False),))
    v3 = service.validate_snapshot(db3, dev3, pkg3["snapshot_id"], ORIGIN,
                                  pkg3["product"]["id"], True)
    check("地址改掉后旧包被拒", v3.get("valid") is False, v3.get("reasons"))
    check("拒绝原因指向地址",
          any("地址" in r for r in v3.get("reasons", [])), v3.get("reasons"))
    check("改址前地址确实是旧值", bool(before_addr), before_addr)
    r3 = service.save_receipt(db3, dev3, pkg3["snapshot_id"], ORIGIN,
                              fields_of(pkg3), "filled", "k-after-addr-change")
    check("改址后回执不是 verified", r3.get("status") != "verified", r3.get("status"))

    # ---------- 4. 换新版本方案：旧包被拒 ----------
    db4 = fresh()
    task4 = make_agreed_task(db4)
    dev4 = make_device(db4, name="新版本测试")
    pkg4 = service.prepare_snapshot(db4, dev4, task4, "shopping_fill", ORIGIN)
    db4.execute(
        "INSERT INTO proposal(proposal_id, task_id, version, proposer, product_id,"
        " reason, decision, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (dbm.new_id("prop"), task4, 2, "A", "pr_sneakers", "改成运动鞋", "accept",
         dbm.now_iso()))
    v4 = service.validate_snapshot(db4, dev4, pkg4["snapshot_id"], ORIGIN,
                                  pkg4["product"]["id"], True)
    check("约定换新版本后旧包被拒", v4.get("valid") is False, v4.get("reasons"))
    check("拒绝原因提到新版本",
          any("版本" in r for r in v4.get("reasons", [])), v4.get("reasons"))

    # ---------- 4b. 最新提案被标 stale：旧包被拒 ----------
    # 和「换新版本」是两条路：stale 表示这版方案依据的资料被更正，
    # 版本号没变，但方案已经不能执行。只判版本号会漏掉这一种。
    db4b = fresh()
    task4b = make_agreed_task(db4b)
    dev4b = make_device(db4b, name="stale 测试")
    pkg4b = service.prepare_snapshot(db4b, dev4b, task4b, "shopping_fill", ORIGIN)
    # 先校验通过，再标 stale：否则 save_receipt 只会抛 not_validated，
    # 走不到「此前已校验 → 方案被标 stale → 再记回执」这个真实窗口。
    v4b0 = service.validate_snapshot(db4b, dev4b, pkg4b["snapshot_id"], ORIGIN,
                                     pkg4b["product"]["id"], True)
    check("stale 前校验通过", v4b0.get("valid") is True, v4b0.get("reasons"))
    db4b.execute("UPDATE proposal SET decision='stale' WHERE task_id=? AND version=1",
                 (task4b,))
    v4b = service.validate_snapshot(db4b, dev4b, pkg4b["snapshot_id"], ORIGIN,
                                   pkg4b["product"]["id"], True)
    check("最新提案标 stale 后旧包被拒", v4b.get("valid") is False, v4b.get("reasons"))
    check("拒绝原因说明资料被更正",
          any("更正" in r or "重新协商" in r for r in v4b.get("reasons", [])),
          v4b.get("reasons"))
    r4b = service.save_receipt(db4b, dev4b, pkg4b["snapshot_id"], ORIGIN,
                               fields_of(pkg4b), "filled", "k-stale-1")
    check("stale 后回执不是 verified", r4b.get("status") != "verified", r4b.get("status"))

    # ---------- 5. 跨设备同幂等键：读不到别人的回执 ----------
    db5 = fresh()
    task5 = make_agreed_task(db5)
    dev5a = make_device(db5, name="设备甲")
    dev5b = make_device(db5, name="设备乙")
    pkg5 = service.prepare_snapshot(db5, dev5a, task5, "shopping_fill", ORIGIN)
    service.validate_snapshot(db5, dev5a, pkg5["snapshot_id"], ORIGIN,
                              pkg5["product"]["id"], True)
    r5 = service.save_receipt(db5, dev5a, pkg5["snapshot_id"], ORIGIN,
                              fields_of(pkg5), "filled", "same-key-123")
    check("本设备回执 verified", r5.get("status") == "verified", r5.get("status"))
    # 设备乙拿同一个键去要回执：拿不到甲的。
    try:
        r5b = service.save_receipt(db5, dev5b, pkg5["snapshot_id"], ORIGIN,
                                   fields_of(pkg5), "filled", "same-key-123")
        check("别的设备同幂等键拿不到他人回执",
              r5b.get("receipt_id") != r5.get("receipt_id") or r5b.get("status") != "verified",
              (r5.get("receipt_id"), r5b.get("receipt_id"), r5b.get("status")))
    except service.ExtError as exc:
        check("别的设备同幂等键拿不到他人回执", True, (exc.code, exc.message))
    # 设备乙拿自己的包配同一个键：能存下自己的一份，不冲突。
    pkg5b = service.prepare_snapshot(db5, dev5b, task5, "shopping_fill", ORIGIN)
    service.validate_snapshot(db5, dev5b, pkg5b["snapshot_id"], ORIGIN,
                              pkg5b["product"]["id"], True)
    r5c = service.save_receipt(db5, dev5b, pkg5b["snapshot_id"], ORIGIN,
                               fields_of(pkg5b), "filled", "same-key-123")
    check("不同参数包同幂等键各自成立",
          r5c.get("status") == "verified" and r5c.get("receipt_id") != r5.get("receipt_id"),
          (r5.get("receipt_id"), r5c.get("receipt_id"), r5c.get("status")))

    # ---------- 6. 撤销设备后：它的包不能再校验 ----------
    db6 = fresh()
    task6 = make_agreed_task(db6)
    dev6 = make_device(db6, name="将撤销设备")
    pkg6 = service.prepare_snapshot(db6, dev6, task6, "shopping_fill", ORIGIN)
    service.revoke_device(db6, "A", dev6["client_id"])
    try:
        v6 = service.validate_snapshot(db6, dev6, pkg6["snapshot_id"], ORIGIN,
                                       pkg6["product"]["id"], True)
        check("撤销设备后旧包校验被拒", v6.get("valid") is False, v6.get("reasons"))
    except service.ExtError as exc:
        check("撤销设备后旧包校验被拒", exc.status in (401, 403), (exc.code, exc.status))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(("[PASS] " if ok else "[FAIL] ") + name)
        if not ok:
            print("       " + repr(detail)[:600])
    print("\n通过 %d/%d" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
