"""模型服务商配置：库里存、接口不泄密钥、保存即生效、重置不清连接。

背景（这一轮加的真功能，此前一行测试都没有）：
模型接口原先只能靠启动命令里的环境变量 PA_MODEL_BASE_URL / PA_MODEL_NAME /
PA_MODEL_API_KEY 指定。界面上没有任何地方能改，用户看不到当前连的是谁，
演示时想换一个模型也没有入口——配置只存在于启动命令里。

现在 app/db.py 多了 model_config 表与 model_config_get/set/clear_key，
app/main.py 多了 GET/POST /api/model-config 与 POST /api/model-config/clear-key，
启动时 _apply_model_config() 把库里的配置套到运行中的 client 上。
app/static/index.html 的「本人」弹层里给了配置入口。

这套脚本用 TestClient 跑真实 HTTP 栈，模型探活全部打桩：
不碰演示数据、不消耗模型额度。验七件事：
1. 全新库里读配置：四项全空、has_api_key=False，不报错；
2. 读接口绝不把密钥内容回给前端（只有「有没有」）；
3. 接口地址或模型名缺失、地址不是 http(s) 时返回 422，且原因是人话；
4. 保存后立即生效：运行中的 client 真的换了 base_url / model / api_key；
5. 表单里密钥留空等于「保持不变」，不会被悄悄抹掉；
6. 「清空密钥」是显式动作，清完回落到匿名请求；
7. 重启后仍读库里的配置（_apply_model_config 可重复调用）；
8. 重置演示资料不清模型连接，但业务表真的被清空。

运行：/tmp/hack_venv/bin/python tests/test_model_config_api.py
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


class StubClient:
    """只实现探活要用的 chat，记录被套上的配置，不联网。"""

    def __init__(self):
        self.base_url = "http://stub.invalid/v1"
        self.model = "stub-model"
        self.api_key = ""

    def chat(self, messages, max_tokens=800, temperature=0.2):
        return "桩：连上了"


def main() -> int:
    # 隔离数据库与身份：必须在 import app 之前设好。
    tmp = tempfile.mkdtemp(prefix="pa_model_config_")
    os.environ["PA_DATA_DIR"] = tmp
    os.environ["PA_DEMO_OWNER"] = "A"
    # 环境变量是兜底，不是唯一入口：这里显式给一组，
    # 用来证明库里的配置能覆盖它，而不是被它盖掉。
    os.environ["PA_MODEL_BASE_URL"] = "http://127.0.0.1:10100/v1"
    os.environ["PA_MODEL_NAME"] = "step/step-5-preview"
    os.environ["PA_MODEL_API_KEY"] = "env-key"
    sys.path.insert(0, str(ROOT))

    from fastapi.testclient import TestClient
    from app import main as app_main
    from app import db as dbm

    db = app_main.db
    stub = StubClient()
    app_main.client = stub
    c = TestClient(app_main.app)

    # 1) 全新库读配置：不能崩，也不能凭空给出值。
    cfg = dbm.model_config_get(db)
    check("全新库里读模型配置：四项全空，不报错",
          cfg == {"base_url": "", "model_name": "", "api_key": "", "updated_at": ""},
          cfg)

    r = c.get("/api/model-config")
    check("GET /api/model-config 返回 200", r.status_code == 200,
          {"status": r.status_code, "body": r.text[:200]})
    body = r.json()
    check("没配过时读接口不带任何密钥内容",
          body.get("has_api_key") is False and "api_key" not in body
          and "env-key" not in r.text,
          {"body": body, "raw": r.text[:200]})

    # 2) 保存：缺字段、地址不对，都要被挡住，且原因是人话。
    for bad, why in (({"base_url": "", "model_name": "m"}, "接口地址为空"),
                     ({"base_url": "http://x/v1", "model_name": ""}, "模型名为空"),
                     ({"base_url": "127.0.0.1:10100/v1", "model_name": "m"}, "地址缺协议")):
        rr = c.post("/api/model-config", json=bad)
        detail = rr.json().get("detail", "") if rr.status_code != 200 else ""
        check("保存被拒（%s）：422 且原因是人话" % why,
              rr.status_code == 422 and isinstance(detail, str)
              and detail != "" and "[object Object]" not in detail,
              {"status": rr.status_code, "detail": detail})

    # 3) 正常保存：落库、立即生效、探活结果照实回。
    r = c.post("/api/model-config", json={
        "base_url": "http://127.0.0.1:10100/v1/", "model_name": "step/step-5-preview",
        "api_key": "user-key"})
    check("保存模型配置返回 200", r.status_code == 200,
          {"status": r.status_code, "body": r.text[:300]})
    check("保存后照实回探活结果", r.json().get("model", {}).get("ok") is True,
          r.json())
    check("保存后接口只回「有没有密钥」，不回密钥内容",
          r.json().get("has_api_key") is True and "user-key" not in r.text,
          r.text[:300])
    stored = dbm.model_config_get(db)
    check("配置真的落了库",
          stored["base_url"] == "http://127.0.0.1:10100/v1/"
          and stored["model_name"] == "step/step-5-preview"
          and stored["api_key"] == "user-key",
          stored)
    check("库里照用户填的存，生效时再规整尾斜杠",
          stub.base_url == "http://127.0.0.1:10100/v1", stub.base_url)
    check("保存立即生效：运行中的 client 换了 model",
          stub.model == "step/step-5-preview", stub.model)
    check("保存立即生效：运行中的 client 换了 api_key",
          stub.api_key == "user-key", stub.api_key)

    # 4) 只改模型名、密钥留空：密钥必须还在。
    r = c.post("/api/model-config", json={
        "base_url": "http://127.0.0.1:10100/v1", "model_name": "gpt-5.5"})
    stored = dbm.model_config_get(db)
    check("只改模型名时密钥不被抹掉",
          stored["api_key"] == "user-key" and stored["model_name"] == "gpt-5.5",
          stored)
    check("密钥留空保存后仍报「已设置」", r.json().get("has_api_key") is True,
          r.json())

    # 5) 显式清空密钥：清完回落到匿名请求。
    r = c.post("/api/model-config/clear-key", json={})
    check("清空密钥返回 200", r.status_code == 200,
          {"status": r.status_code, "body": r.text[:200]})
    check("清空后库里密钥为空、运行中 client 也空了",
          dbm.model_config_get(db)["api_key"] == "" and stub.api_key == "",
          {"stored": dbm.model_config_get(db), "client": stub.api_key})
    check("清空后读接口报「未设置」",
          c.get("/api/model-config").json().get("has_api_key") is False,
          c.get("/api/model-config").json())

    # 6) 重启：重新起一个 client 再套一次，配置要读得回来。
    #    只改内存不落库的话，重启就打回默认模型，用户看不出为什么换了。
    fresh = StubClient()
    app_main.client = fresh
    app_main._apply_model_config()
    check("重启后仍读库里的配置：base_url 与 model 都恢复",
          fresh.base_url == "http://127.0.0.1:10100/v1"
          and fresh.model == "gpt-5.5",
          {"base_url": fresh.base_url, "model": fresh.model})
    check("密钥已清空，重启后不会把旧密钥捡回来", fresh.api_key == "",
          fresh.api_key)
    app_main.client = stub

    # 7) 重置演示资料：业务表清空，模型连接不动。
    #    之前 reset_demo 漏了 plan 四张表、order_candidate、event_task_link、
    #    event_attachment 和插件五张表，用户点完重置，侧栏里的事项和共同事项
    #    照样是测试里造出来的那几条，看着就像重置没生效。
    plan_id = dbm.new_id("plan")
    task_id = dbm.new_id("task")
    db.execute(
        "INSERT INTO plan(plan_id,owner_id,title,utterance,kind,stage,outcome,"
        "needs_replan,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (plan_id, "A", "周末三家一起带孩子去公园", "周末三家一起带孩子出去玩",
         "group_activity", "awaiting", "还在等各方确认", 0, dbm.now_iso()))
    db.execute(
        "INSERT INTO plan_participant(participant_id,plan_id,owner_id,label,"
        "conditions_json,required,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (dbm.new_id("pp"), plan_id, "A", "我家", '{"预算":"100 元以内"}', 1,
         "pending", dbm.now_iso()))
    db.execute(
        "INSERT INTO plan_proposal(plan_proposal_id,plan_id,version,proposer,"
        "content_json,reason,created_at) VALUES(?,?,?,?,?,?,?)",
        (dbm.new_id("prop"), plan_id, 1, "system", "{}", "第一版", dbm.now_iso()))
    db.execute(
        "INSERT INTO plan_decision(plan_decision_id,plan_id,participant_id,"
        "version,decision,reason,changes_json,decided_by,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (dbm.new_id("pd"), plan_id, "pp_none", 1, "pending", "", "{}", "person",
         dbm.now_iso()))
    db.execute(
        "INSERT INTO task(task_id,owner_id,utterance,extracted_json,stage,"
        "outcome,created_at) VALUES(?,?,?,?,?,?,?)",
        (task_id, "A", "给小周的女儿准备生日礼物，300 元以内",
         json.dumps({"subject_id": "p_xiaoyu", "budget_max": 300},
                    ensure_ascii=False), "draft", "", dbm.now_iso()))
    db.execute(
        "INSERT INTO order_candidate(candidate_id,task_id,proposal_id,version,"
        "product_id,product_name,price_cny,reason,checked,chosen,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (dbm.new_id("cand"), task_id, "", 1, "pr_draw_set", "画笔套装", 128,
         "预算内", 1, 0, dbm.now_iso()))
    db.execute(
        "INSERT INTO event_task_link(link_id,event_id,task_id,owner_id,note,"
        "created_at) VALUES(?,?,?,?,?,?)",
        (dbm.new_id("link"), "ev_none", task_id, "A", "用于这件事", dbm.now_iso()))
    db.execute(
        "INSERT INTO event_attachment(attachment_id,event_id,owner_id,media,"
        "b64,note,created_at) VALUES(?,?,?,?,?,?,?)",
        (dbm.new_id("att"), "ev_none", "A", "image/png", "", "配图",
         dbm.now_iso()))
    # 插件链路四张表：之前整表漏清，配对过的设备在重置后仍然有效。
    db.execute(
        "INSERT INTO ext_pair_code(code,owner_id,client_name,issued_at,expires_at)"
        " VALUES(?,?,?,?,?)",
        ("123456", "A", "Chrome", dbm.now_iso(), dbm.now_iso()))
    db.execute(
        "INSERT INTO ext_device(client_id,owner_id,client_name,access_token,"
        "issued_at,expires_at) VALUES(?,?,?,?,?,?)",
        (dbm.new_id("dev"), "A", "Chrome", "tok", dbm.now_iso(), dbm.now_iso()))
    db.execute(
        "INSERT INTO ext_snapshot(snapshot_id,owner_id,client_id,task_id,"
        "action_type,target_origin,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
        (dbm.new_id("snap"), "A", "dev_none", task_id, "fill_form",
         "http://127.0.0.1:8000", dbm.now_iso(), dbm.now_iso()))
    db.execute(
        "INSERT INTO ext_receipt(receipt_id,snapshot_id,owner_id,client_id,"
        "task_id,target_origin,idempotency_key,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (dbm.new_id("rcpt"), "snap_none", "A", "dev_none", task_id,
         "http://127.0.0.1:8000", "key1", dbm.now_iso()))

    before = {t: db.rows("SELECT COUNT(*) AS n FROM " + t)[0]["n"]
              for t in ("plan", "plan_participant", "plan_proposal",
                        "plan_decision", "task", "order_candidate",
                        "event_task_link", "event_attachment", "ext_pair_code",
                        "ext_device", "ext_snapshot", "ext_receipt")}
    check("重置前这些表里确实有数据（不是空表假通过）",
          all(n > 0 for n in before.values()), before)

    rr = c.post("/api/reset")
    check("POST /api/reset 返回 200", rr.status_code == 200,
          {"status": rr.status_code, "body": rr.text[:200]})
    after = {t: db.rows("SELECT COUNT(*) AS n FROM " + t)[0]["n"]
             for t in before}
    check("重置后业务表与插件表都清了（含 plan 四张表、order_candidate、"
          "event_task_link、event_attachment、插件四张表）",
          all(n == 0 for n in after.values()), after)

    kept = dbm.model_config_get(db)
    check("重置不动模型连接：配置还在",
          kept["base_url"] == "http://127.0.0.1:10100/v1"
          and kept["model_name"] == "gpt-5.5",
          kept)
    check("重置后运行中的 client 也没被改回默认值",
          stub.base_url == "http://127.0.0.1:10100/v1"
          and stub.model == "gpt-5.5",
          {"base_url": stub.base_url, "model": stub.model})

    _report()
    return 0 if all(ok for _, ok, _ in results) else 1


def _report():
    passed = sum(1 for _, ok, _ in results if ok)
    print("=" * 66)
    for name, ok, detail in results:
        print("%s %s" % ("PASS" if ok else "FAIL", name))
        if not ok:
            print("      证据：%s" % json.dumps(detail, ensure_ascii=False,
                                            default=str)[:400])
    print("=" * 66)
    print("模型服务商配置：%d/%d 通过" % (passed, len(results)))
    print("边界：TestClient 跑真实 HTTP 栈，探活打桩；验的是配置存取、")
    print("      密钥不外泄、保存即生效与重置边界，不验真实模型连通性。")


if __name__ == "__main__":
    sys.exit(main())
