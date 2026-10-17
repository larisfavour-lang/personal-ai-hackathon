"""普通入口的 HTTP 契约：不带 owner_id 要能通，冒充别家要能被拒。

背景（这一轮修的真缺陷）：
普通接口的 schema 一直要求 body 必填 owner_id，而 handler 全都从
current_user(request) 取本人，两边矛盾。前端按「身份只在服务端会话」的
设计只发业务字段，于是每个普通入口都返回 422；detail 是 FastAPI 的
校验错误数组，页面拼出来就是「出了点问题：[object Object]」——
用户看到一句乱码，还以为自己那句话有问题。

现在 owner_id 改为可选：带了就必须和会话一致，没带就用会话本人。

本脚本用 TestClient 跑真实 HTTP 栈（路由、schema、会话 cookie、handler），
数据库隔离在临时目录，模型调用全部打桩：不碰演示数据，不消耗模型额度。
不重跑 tests/test_e2e_chain.py 那套 22 项——那套主要验服务层，
替代不了「无 owner_id 的真实 HTTP 入口」这一条。

运行：/tmp/hack_venv/bin/python tests/test_http_entry_contract.py
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
    # 隔离数据库与身份：必须在 import app 之前设好。
    tmp = tempfile.mkdtemp(prefix="pa_http_contract_")
    os.environ["PA_DATA_DIR"] = tmp
    os.environ["PA_DEMO_OWNER"] = "A"
    sys.path.insert(0, str(ROOT))

    from fastapi.testclient import TestClient
    from app import main as app_main
    from app import service
    from app import db as dbm

    # 模型打桩：只返回最小可解析结构，不联网、不耗额度。
    class StubClient:
        def chat_json(self, messages, max_tokens=1600, temperature=0.2):
            sys_msg = messages[0]["content"] if messages else ""
            if "意图" in sys_msg or "交办" in sys_msg:
                return {"intent": "family_update"}
            return {"intent": "family_update"}

        def chat(self, messages, max_tokens=800, temperature=0.2):
            return "这是桩回复，不是模型输出。"

        def chat_vision_json(self, prompt, image_b64, media_type="image/jpeg",
                             user_prompt=""):
            return {"transcribed": "", "fields": {}, "confidence": "low",
                    "unclear_parts": ["桩：不识别图片"], "happened_on": ""}

    app_main.client = StubClient()
    db = app_main.db          # 应用启动时连的那个库（已在隔离目录）
    service_client_backup = getattr(service, "client", None)
    if hasattr(service, "client"):
        service.client = StubClient()

    c = TestClient(app_main.app)

    # 1) 普通入口不带 owner_id：必须 200，不能 422。
    r = c.post("/api/say", json={"utterance": "她最近还想画画吗"})
    check("普通入口 /api/say 不带 owner_id 返回 200", r.status_code == 200,
          {"status": r.status_code, "body": r.text[:300]})

    # 2) 带错身份：必须被拒，且原因说得清，不能是 [object Object]。
    r_bad = c.post("/api/say", json={"owner_id": "B",
                                     "utterance": "她最近还想画画吗"})
    check("冒充别家（owner_id=B，会话为 A）被拒绝",
          r_bad.status_code == 403,
          {"status": r_bad.status_code, "body": r_bad.text[:300]})
    if r_bad.status_code == 403:
        body = r_bad.json()
        detail = body.get("detail", "")
        check("拒绝原因是人话，不含 [object Object]",
              isinstance(detail, str) and "[object Object]" not in detail
              and detail != "",
              {"detail": detail})

    # 3) 带对身份：放行（与会话一致）。
    r_ok = c.post("/api/say", json={"owner_id": "A",
                                    "utterance": "现在办到哪一步了？"})
    check("带一致身份（owner_id=A）返回 200", r_ok.status_code == 200,
          {"status": r_ok.status_code, "body": r_ok.text[:200]})

    # 4) 造两个真实对象：本人 A 的事项/计划，和别家 B 的事项/计划。
    #    用真实 id 而不是 task_not_exist：不存在的 id 会走到「任务不存在」，
    #    返回 404/409 也会让「!= 422」这类断言变绿，那是假通过。
    a_plan = service.create_plan(db, "A", "周末三家一起带孩子出去玩，我出 100 以内")
    service.add_plan_participant(db, a_plan, "A", "我", {"本人出资上限": "100 元以内"})
    service.add_plan_participant(db, a_plan, "B", "小周", {"时间": "周六上午"})
    b_plan = service.create_plan(db, "B", "别家自己的事项")
    service.add_plan_participant(db, b_plan, "B", "小周", {"时间": "周日"})
    #    A / B 各造一个真实事项：新的隔离库里没有现成 task，直接插入。
    #    用真实 id 而不是 task_not_exist：不存在的 id 会走到「任务不存在」，
    #    返回 404/409 也会让「!= 422」这类断言变绿，那是假通过。
    a_task_id = dbm.new_id("task")
    b_task_id = dbm.new_id("task")
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage,"
        " outcome, created_at) VALUES(?,?,?,?,?,?,?)",
        (a_task_id, "A", "给小周的女儿准备生日礼物，300元以内",
         json.dumps({"subject_id": "p_xiaoyu", "budget_max": 300},
                    ensure_ascii=False), "draft", "", dbm.now_iso()))
    db.execute(
        "INSERT INTO task(task_id, owner_id, utterance, extracted_json, stage,"
        " outcome, created_at) VALUES(?,?,?,?,?,?,?)",
        (b_task_id, "B", "别家的事项",
         json.dumps({"subject_id": "p_xiaoyu_b", "budget_max": 300},
                    ensure_ascii=False), "draft", "", dbm.now_iso()))
    a_task = {"task_id": a_task_id}
    b_task = {"task_id": b_task_id}

    # 5) 普通入口：不带 owner_id 要能到达业务层（不是 422，也不是 405/500）。
    #    期望状态写清楚：对象存在就是 200/409 业务拒绝，不存在才是 404。
    entry_probes = [
        ("/api/record", {"utterance": "记错了，喜欢拼图的是哥哥"}, (200,)),
        ("/api/task", {"utterance": "给小周的女儿准备生日礼物，300元以内"}, (200, 409)),
        ("/api/task/advance", {"task_id": a_task["task_id"]}, (200, 409)),
        ("/api/task/resolve-subject",
         {"task_id": a_task["task_id"], "person_id": "p_xiaoyu"}, (200, 409)),
        ("/api/task/choose-candidate",
         {"task_id": a_task["task_id"], "product_id": "pr_draw_set"}, (200, 409)),
        ("/api/order/advance",
         {"task_id": a_task["task_id"], "order_id": "simorder_none", "action": "pay"},
         (200, 409)),
        ("/api/confirm",
         {"task_id": a_task["task_id"], "proposal_id": "prop_none"}, (200, 409)),
    ]
    for path, body, expect in entry_probes:
        rr = c.post(path, json=body)
        check("普通入口 %s 不带 owner_id 返回 %s（不是 422/500/405）"
              % (path, "/".join(map(str, expect))),
              rr.status_code in expect,
              {"status": rr.status_code, "body": rr.text[:200]})

    # 6) 计划入口：本人事项要能到达业务层。
    plan_probes = [
        # 条件用口径清楚的键：旧键「预算」光一个数字说不清是本家上限还是
        # 活动总预算，服务端会 400 请本人确认——那正是 t17 要的行为，
        # 不能拿它当「入口坏了」。这里用「本人出资上限」探活入口本身。
        ("/api/plan/create",
         {"title": "周末三家一起带孩子出去玩，我出 100 以内",
          "participants": [{"owner_id": "A", "label": "我",
                            "conditions": {"本人出资上限": "100 元以内"}}]}, (200,)),
        ("/api/plan/%s/propose" % a_plan, {}, (200, 409)),
        ("/api/plan/%s/replan" % a_plan, {}, (200, 409)),
        ("/api/plan/%s/decide" % a_plan,
         {"participant_id": "pp_none", "version": 1, "decision": "accept"}, (200, 409)),
        ("/api/plan/%s/condition" % a_plan,
         {"conditions": {"本人出资上限": "80 元以内"}}, (200, 409)),
        # review 现在校验「这一方是不是本人」：participant_id 不存在时回 404，
        # 冒充别家时回 403，都是业务层的人话拒绝，不是入口坏了。
        ("/api/plan/%s/review" % a_plan,
         {"participant_id": "pp_none", "version": 1}, (404,)),
    ]
    for path, body, expect in plan_probes:
        rr = c.post(path, json=body)
        check("计划入口 %s 不带 owner_id 返回 %s（不是 422/500/405）"
              % (path, "/".join(map(str, expect))),
              rr.status_code in expect,
              {"status": rr.status_code, "body": rr.text[:200]})

    # 7) 越权：拿别家 B 的事项/计划来操作，必须被拒，且拒绝是人话。
    #    这一组才是「冒充别家」的真负例——id 是真实存在的，只是不属于本会话。
    cross = [
        ("/api/task/advance", {"task_id": b_task["task_id"]}, "task"),
        ("/api/task/resolve-subject",
         {"task_id": b_task["task_id"], "person_id": "p_xiaoyu"}, "task"),
        ("/api/task/choose-candidate",
         {"task_id": b_task["task_id"], "product_id": "pr_draw_set"}, "task"),
        ("/api/plan/%s/replan" % b_plan, {}, "plan"),
        ("/api/plan/%s/condition" % b_plan, {"conditions": {"预算": "1 元"}}, "plan"),
        ("/api/plan/%s/decide" % b_plan,
         {"participant_id": "pp_none", "version": 1, "decision": "accept"}, "plan"),
    ]
    for path, body, kind in cross:
        rr = c.post(path, json=body)
        check("别家%s id 操作被拒：%s" % (kind, path),
              rr.status_code in (403, 409),
              {"status": rr.status_code, "body": rr.text[:200]})
        if rr.status_code in (403, 409):
            detail = rr.json().get("detail", "")
            check("拒绝原因是人话，不含 [object Object]：%s" % path,
                  isinstance(detail, str) and "[object Object]" not in detail
                  and detail != "",
                  {"detail": detail})

    # Separate strict negatives: do not accept a widened 400/403 blanket.
    for cond, code in (({"预算":"100元"}, 400), ({"预算":"每家不超过100元"}, 200), ({"预算":"活动一共300元"}, 200)):
        rr = c.post("/api/plan/create", json={"title":"calibre-" + str(cond), "participants":[{"owner_id":"A","label":"我家","conditions":cond}]})
        check("明确与含糊预算独立contract " + str(cond), rr.status_code == code, rr.text)
    b_part = db.row("SELECT participant_id FROM plan_participant WHERE plan_id=?", (b_plan,))["participant_id"]
    for path, body, code in (("/api/plan/" + b_plan + "/propose", {}, 403),
                             ("/api/plan/" + b_plan + "/review", {"participant_id":b_part,"version":1}, 403)):
        before = db.row("SELECT COUNT(*) c FROM plan_proposal")["c"]
        rr = c.post(path, json=body)
        check("真实别家id严格权限 " + path, rr.status_code == code and db.row("SELECT COUNT(*) c FROM plan_proposal")["c"] == before, rr.text)
    check("非参与方GET严格404", c.get("/api/plan/" + b_plan).status_code == 404)
    before = db.row("SELECT COUNT(*) c FROM plan")["c"]
    rr = c.post("/api/plan/create", json={"title":"overlength", "participants":[{"owner_id":"A","label":"我家"}]*6 + [{"owner_id":"Z","label":"bad"}]})
    check("第7非法参与方整体拒绝且不落库", rr.status_code == 400 and db.row("SELECT COUNT(*) c FROM plan")["c"] == before, rr.text)

    # 8) 越权后状态不变：B 的条件不能被 A 改掉。
    b_cond = db.rows(
        "SELECT conditions_json FROM plan_participant WHERE plan_id=? AND owner_id='B'",
        (b_plan,))
    check("越权被拒后别家条件未被改写",
          bool(b_cond) and json.loads(b_cond[0]["conditions_json"]).get("时间") == "周日",
          {"b_cond": b_cond})

    # 9) resolve-subject 要核对 task 归属：拿 A 的 task 去选 B 的人也必须被拒。
    rr = c.post("/api/task/resolve-subject",
                json={"task_id": a_task["task_id"], "person_id": "p_azhou_b"})
    check("resolve-subject 拒绝把别家的人物安到本人的事项上",
          rr.status_code in (403, 409),
          {"status": rr.status_code, "body": rr.text[:200]})

    # 6) 会话隔离仍在：不带 owner_id 时读到的仍是会话本人的内容。
    me = c.get("/api/me").json()
    check("会话本人由服务端给出（/api/me 返回 user_id）",
          bool(me.get("user_id")), {"me": me})

    # 7) 测试专用路径仍要求身份：/api/demo/* 不放宽。
    rr = c.post("/api/demo/switch", json={})
    check("演示专用路径 /api/demo/switch 仍要求 owner_id（不放宽）",
          rr.status_code == 422,
          {"status": rr.status_code, "body": rr.text[:200]})

    if service_client_backup is not None:
        service.client = service_client_backup

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
    print("普通入口 HTTP 契约：%d/%d 通过" % (passed, len(results)))
    print("边界：本脚本跑真实 HTTP 栈但模型全部打桩，验的是请求契约与会话隔离；")
    print("      模型产出质量仍由 tests/test_e2e_chain.py 的 22 项覆盖。")


if __name__ == "__main__":
    sys.exit(main())
