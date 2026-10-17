"""跨页面需求卡的 HTTP 层边界：无凭证、错凭证、别人的事项、用途不符。

tests/test_context_guards.py 验的是服务层取值与授权，这里补它证明不了的
那一层：真实 HTTP 栈上的 401/403/404/422 和 {error:{code,message}} 契约。

服务层单测不构造 None 凭证：那只会抛 TypeError，证明不了接口真的返回 401。

数据库隔离在临时目录，不碰演示数据，不调模型、不联网。

运行：/tmp/hack_venv/bin/python tests/test_context_http.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
results: list[tuple[str, bool, object]] = []


def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="pa_context_http_")
    os.environ["PA_DATA_DIR"] = tmp
    os.environ["PA_DEMO_OWNER"] = "A"
    sys.path.insert(0, str(ROOT))

    from fastapi.testclient import TestClient
    from app import main as app_main
    from app import db as dbm

    c = TestClient(app_main.app)
    db = app_main.db

    # 本人 A 的事项和别家 B 的事项，都用真实 id。
    a_task = dbm.new_id("task")
    b_task = dbm.new_id("task")
    for tid, owner in ((a_task, "A"), (b_task, "B")):
        db.execute(
            "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage,"
            " outcome, created_at) VALUES(?,?,?,?,?,?,?)",
            (tid, owner, "给小雨准备生日礼物，300元以内",
             json.dumps({"subject_id": "p_xiaoyu" if owner == "A" else "p_xiaoyu_b",
                         "budget_max": 300, "occasion": "生日"},
                        ensure_ascii=False),
             "created", "", dbm.now_iso()))

    # 1) 无凭证：必须 401，且错误体是冻结契约的 {error:{code,message,retryable}}。
    r = c.post("/api/extension/context",
               json={"task_id": a_task, "purpose": "shopping_help"})
    check("无凭证请求需求卡返回 401", r.status_code == 401,
          {"status": r.status_code, "body": r.text[:200]})
    err = r.json().get("error") or {}
    check("错误体按契约给 error.code/message",
          bool(err.get("code")) and bool(err.get("message")),
          {"error": err})
    check("错误体不含 [object Object]", "[object Object]" not in r.text, r.text[:200])

    # 2) 错凭证：必须 401，不能说成 500。
    r_bad = c.post("/api/extension/context",
                   headers={"Authorization": "Bearer not-a-real-token"},
                   json={"task_id": a_task, "purpose": "shopping_help"})
    check("无效凭证返回 401", r_bad.status_code == 401,
          {"status": r_bad.status_code, "body": r_bad.text[:200]})

    # 3) 正常路径：本人会话生成配对码 -> 兑换 -> 请求本人的事项。
    pc = c.post("/api/extension/pair-codes", json={"client_name": "契约验证"})
    code = pc.json()["code"]
    paired = c.post("/api/extension/pair",
                    json={"code": code, "client_name": "契约验证"})
    token = paired.json()["access_token"]
    hdr = {"Authorization": "Bearer " + token}
    r_ok = c.post("/api/extension/context", headers=hdr,
                  json={"task_id": a_task, "purpose": "shopping_help"})
    check("本人事项返回 200", r_ok.status_code == 200,
          {"status": r_ok.status_code, "body": r_ok.text[:300]})
    if r_ok.status_code == 200:
        card = r_ok.json()
        check("需求卡字段齐全",
              all(k in card for k in ("task_id", "purpose", "context_revision",
                                      "subject_label", "task_status", "summary",
                                      "requirements", "missing_fields",
                                      "search_terms", "export_text",
                                      "source_summary", "generated_at")),
              sorted(card.keys()))

    # 4) 别人的事项：必须 404，不能因为 task_id 存在就给出内容。
    r_other = c.post("/api/extension/context", headers=hdr,
                     json={"task_id": b_task, "purpose": "shopping_help"})
    check("别人的事项返回 404", r_other.status_code == 404,
          {"status": r_other.status_code, "body": r_other.text[:200]})

    # 5) 用途不符：422，且说明只支持购物协助。
    r_purpose = c.post("/api/extension/context", headers=hdr,
                       json={"task_id": a_task, "purpose": "buy_ticket"})
    check("不支持的用途返回 422", r_purpose.status_code == 422,
          {"status": r_purpose.status_code, "body": r_purpose.text[:200]})

    # 6) 撤销后立即失效。
    rev = c.post("/api/extension/revoke", headers=hdr,
                 json={"client_id": paired.json()["client_id"]})
    check("撤销设备成功", rev.status_code == 200, rev.text[:200])
    r_revoked = c.post("/api/extension/context", headers=hdr,
                       json={"task_id": a_task, "purpose": "shopping_help"})
    check("撤销后需求卡被拒", r_revoked.status_code in (401, 403),
          {"status": r_revoked.status_code, "body": r_revoked.text[:200]})

    # 7) 新签发凭证不带固定到期日（用户要求一次连接持续使用）。
    pc2 = c.post("/api/extension/pair-codes", json={"client_name": "期限验证"})
    paired2 = c.post("/api/extension/pair",
                     json={"code": pc2.json()["code"], "client_name": "期限验证"})
    check("新凭证不设固定到期日", paired2.json().get("expires_at") == "",
          paired2.json().get("expires_at"))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + name
              + ((" | " + str(detail)) if not ok else ""), flush=True)
    print("需求卡 HTTP 边界：%d/%d 通过" % (passed, len(results)), flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
