"""设备连接寿命：旧有效连接迁移、失效凭证不复活、长期有效、撤销即时。

用户要求「连接一次持续使用」：关掉浏览器、隔天再用都不该要求重新配对。
取消固定到期日要连已经连着的那批一起改，否则老连接到点照样被踢下线。

但「不设到期日」不能顺带把失效的凭证救活：已撤销的必须继续拒绝，
已经过期的也必须继续拒绝。少了这一条，迁移就成了悄悄延长寿命。

用临时库 + 固定构造时间，不碰演示数据，不调模型。

运行：/tmp/hack_venv/bin/python tests/test_device_lifetime.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db as dbm
from app import service

TMP = Path("/tmp/pa_device_lifetime.db")
results: list[tuple[str, bool, object]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def fresh():
    if TMP.exists():
        TMP.unlink()
    db = dbm.DB(TMP)
    dbm.seed(db)
    return db


def insert_device(db, client_id, expires_at, revoked_at=""):
    db.execute(
        "INSERT INTO ext_device(client_id, owner_id, client_name, access_token,"
        " issued_at, expires_at, revoked_at) VALUES(?,?,?,?,?,?,?)",
        (client_id, "A", "旧设备", "tok_" + client_id,
         (datetime.now(timezone.utc).astimezone()
          - timedelta(hours=30)).isoformat(timespec="seconds"),
         expires_at, revoked_at))


def usable(db, token):
    """这个凭证现在还能不能用。直接走真实的解析入口。

    传真正的 token，不按 client_id 拼：pair_device 签发的是随机 token，
    拼出来的串查不到行，会把「实现没问题」误判成失败。
    """
    try:
        service.device_from_request(db, "Bearer " + token)
        return True
    except service.ExtError:
        return False


def main():
    db = fresh()
    # 三类旧连接：仍有效、已过期、已撤销。
    insert_device(db, "d_valid", (datetime.now(timezone.utc).astimezone()
                                  + timedelta(hours=20)).isoformat(timespec="seconds"))
    insert_device(db, "d_expired", (datetime.now(timezone.utc).astimezone()
                                    - timedelta(hours=1)).isoformat(timespec="seconds"))
    insert_device(db, "d_revoked", (datetime.now(timezone.utc).astimezone()
                                    + timedelta(hours=20)).isoformat(timespec="seconds"),
                  now())

    # 触发启动迁移（真实路径：重新建一次 DB 对象会跑 connect 里的迁移）。
    db2 = dbm.DB(TMP)

    check("迁移后旧有效连接仍可用", usable(db2, "tok_d_valid"))
    check("已过期连接不被迁移复活", not usable(db2, "tok_d_expired"))
    check("已撤销连接仍被拒绝", not usable(db2, "tok_d_revoked"))
    row = db2.row("SELECT expires_at FROM ext_device WHERE client_id='d_valid'")
    check("旧有效连接的到期日已清空", row["expires_at"] == "", row["expires_at"])

    # 新签发的凭证不带固定到期日。
    code = service.create_pair_code(db2, "A", "新设备")["code"]
    paired = service.pair_device(db2, "A", code, "新设备")
    check("新签发凭证无固定到期日", paired["expires_at"] == "", paired["expires_at"])
    check("新签发凭证立即可用", usable(db2, paired["access_token"]))

    # 推进时间：把它的 issued_at 推到很久以前，仍应可用（无固定到期日）。
    db2.execute("UPDATE ext_device SET issued_at=? WHERE client_id=?",
                ((datetime.now(timezone.utc).astimezone()
                  - timedelta(days=365)).isoformat(timespec="seconds"),
                 paired["client_id"]))
    check("推进一年后仍可用（长期连接）", usable(db2, paired["access_token"]))

    # 撤销后即时失效，且不能因为「没有到期日」而绕过。
    service.revoke_device(db2, "A", paired["client_id"])
    check("撤销后立即失效", not usable(db2, paired["access_token"]))

    # 恢复固定期限的配置下，续期分支要能真的换 token。
    service.DEVICE_TOKEN_TTL_HOURS = 24
    service.DEVICE_RENEW_AFTER_HOURS = 1
    try:
        code2 = service.create_pair_code(db2, "A", "轮换设备")["code"]
        p2 = service.pair_device(db2, "A", code2, "轮换设备")
        old_token = p2["access_token"]
        check("固定期限模式下签出到期日", p2["expires_at"] != "", p2["expires_at"])
        # 把 issued_at 推到超过续期阈值。
        db2.execute("UPDATE ext_device SET issued_at=? WHERE client_id=?",
                    ((datetime.now(timezone.utc).astimezone()
                      - timedelta(hours=5)).isoformat(timespec="seconds"),
                     p2["client_id"]))
        row_after = service.device_from_request(db2, "Bearer " + old_token)
        renewed = row_after.get("renewed_token") or ""
        check("超过阈值后后台换发新凭证", bool(renewed) and renewed != old_token, renewed[:8])
        if renewed:
            check("新凭证立即可用", usable(db2, renewed))
            # 旧 token 换发后应失效：两个凭证同时有效等于撤销失效。
            try:
                service.device_from_request(db2, "Bearer " + old_token)
                old_ok = True
            except service.ExtError:
                old_ok = False
            check("换发后旧凭证失效", not old_ok)
        # 撤销仍然即时。
        service.revoke_device(db2, "A", p2["client_id"])
        check("固定期限模式下撤销仍即时", not usable(db2, renewed))
    finally:
        service.DEVICE_TOKEN_TTL_HOURS = 0
        service.DEVICE_RENEW_AFTER_HOURS = 24 * 30

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + name
              + ((" | " + str(detail)) if not ok else ""), flush=True)
    print("设备连接寿命：%d/%d 通过" % (passed, len(results)), flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
