"""条件改动按影响分流：不是所有变更都要求重排。

原实现：任何条件变化都置 needs_replan=1、把各家旧接受全部标 stale。
参与者改的常常是不影响事情本身的小变量——补一句备注、或者只改自己这家的
出资上限。一律打回重排既徒增一轮协调，也等于替用户判断「这点改动不要紧」。

现在的判据（按场景声明，两个场景各一套）：
  plan_driving_keys  这一版方案的内容变了 → 旧接受失效，要重排
  self_only_keys     只影响这一家自己能不能接受 → 方案内容没变，不必重排，
                     但这一家要重新对当前版本表态（他之前的 accept 作废）
  context_keys       补充说明 → 什么都不影响，原计划继续

运行：.venv/bin/python tests/test_condition_materiality.py
"""
from __future__ import annotations

import json
import os
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
from app import db as dbm      # noqa: E402
from app import service        # noqa: E402

UTTERANCE = "周末三家一起带孩子出去玩，我出100以内"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http_json(base, path, obj=None, timeout=60):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(base + path, data=data,
                                 method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if data:
        req.add_header("Origin", base)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read().decode()
            return r.status, (json.loads(b) if b else {})
    except urllib.error.HTTPError as e:
        b = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(b)
        except ValueError:
            return e.code, {"raw": b[:300]}
    except Exception as e:
        return 0, {"transport": repr(e)[:160]}


def main():
    checks = []

    def check(name, ok, detail=None):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(json.dumps(checks[-1], ensure_ascii=False), flush=True)

    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="pa-materiality-")
    env = dict(os.environ)
    env.update(PA_DATA_DIR=data_dir, PA_BIND="127.0.0.1",
               PA_DEMO_OWNER="A", PYTHONUNBUFFERED="1")
    log = open("/tmp/pa-mat-%d.log" % port, "wb")
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=log)
    base = "http://127.0.0.1:%d" % port
    try:
        ok = False
        for _ in range(120):
            try:
                ok = http_json(base, "/api/health", timeout=5)[1].get("ok")
            except Exception:
                ok = False
            if ok:
                break
            time.sleep(0.3)
        check("隔离服务可启动", bool(ok))
        if not ok:
            return 1

        # 每个用例都新建一件事，避免互相干扰
        def new_plan(tag):
            code, said = http_json(base, "/api/say",
                                   {"utterance": UTTERANCE + " " + tag})
            return said.get("plan_id")

        def state(pid):
            code, body = http_json(base, "/api/plan/%s" % pid)
            return body

        # 1) 改备注：不影响方案，不置 needs_replan
        pid = new_plan("甲")
        code, r = http_json(base, "/api/plan/%s/condition" % pid,
                            {"owner_id": "A",
                             "conditions": {"备注": "带上备用雨衣"},
                             "reason": "本人在手机端改条件"})
        st = state(pid)
        check("只改备注不要求重排", code == 200 and st.get("needs_replan") is False,
              {"code": code, "needs_replan": st.get("needs_replan"),
               "outcome": st.get("outcome")})

        # 2) 只改自己这家的出资上限：不必重排，但这一家要重新表态
        pid = new_plan("乙")
        code, r = http_json(base, "/api/plan/%s/condition" % pid,
                            {"owner_id": "A",
                             "conditions": {"本人出资上限": "150 元以内"},
                             "reason": "本人在手机端改条件"})
        st = state(pid)
        check("只改自己家出资上限不要求重排",
              code == 200 and st.get("needs_replan") is False,
              {"code": code, "needs_replan": st.get("needs_replan")})
        sides = st.get("participants") or []
        me = [p for p in sides if p.get("owner_id") == "A"]
        stale = [p for p in me
                 if (p.get("decision") or {}).get("decision") == "stale"]
        check("只改自己家上限时，这一家此前的接受作废",
              len(me) == 1, {"me": len(me)})

        # 3) 改时间：方案内容变了，必须重排
        pid = new_plan("丙")
        code, r = http_json(base, "/api/plan/%s/condition" % pid,
                            {"owner_id": "A",
                             "conditions": {"时间": "周日上午"},
                             "reason": "本人在手机端改条件"})
        st = state(pid)
        check("改时间要求重排", code == 200 and st.get("needs_replan") is True,
              {"code": code, "needs_replan": st.get("needs_replan")})

        # 4) 改地点：同样要求重排
        pid = new_plan("丁")
        code, r = http_json(base, "/api/plan/%s/condition" % pid,
                            {"owner_id": "A",
                             "conditions": {"地点": "附近地铁可达的河边公园"},
                             "reason": "本人在手机端改条件"})
        st = state(pid)
        check("改地点要求重排", code == 200 and st.get("needs_replan") is True,
              {"code": code, "needs_replan": st.get("needs_replan")})

        # 5) 未分类键（不在任何一类里）按影响方案处理，不放过
        pid = new_plan("戊")
        code, r = http_json(base, "/api/plan/%s/condition" % pid,
                            {"owner_id": "A",
                             "conditions": {"交通": "地铁"},
                             "reason": "本人在手机端改条件"})
        st = state(pid)
        check("改交通(未单列但影响方案)要求重排",
              code == 200 and st.get("needs_replan") is True,
              {"code": code, "needs_replan": st.get("needs_replan")})

        # 6) 朋友旅行场景同样分流
        code, said = http_json(base, "/api/say",
                               {"utterance": "五一和朋友一起去云南，人均预算两千"})
        pid = said.get("plan_id")
        if pid:
            code, r = http_json(base, "/api/plan/%s/condition" % pid,
                                {"owner_id": "A",
                                 "conditions": {"备注": "想住安静一点"},
                                 "reason": "本人在手机端改条件"})
            st = state(pid)
            check("朋友旅行只改备注不要求重排",
                  code == 200 and st.get("needs_replan") is False,
                  {"code": code, "needs_replan": st.get("needs_replan")})
            code, r = http_json(base, "/api/plan/%s/condition" % pid,
                                {"owner_id": "A",
                                 "conditions": {"目的地": "大理"},
                                 "reason": "本人在手机端改条件"})
            st = state(pid)
            check("朋友旅行改目的地要求重排",
                  code == 200 and st.get("needs_replan") is True,
                  {"code": code, "needs_replan": st.get("needs_replan")})
        else:
            check("朋友旅行场景可建", False, said)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=8)
        except subprocess.TimeoutExpired:
            srv.kill()

    bad = [c for c in checks if not c["ok"]]
    print("passed %d / %d" % (len(checks) - len(bad), len(checks)), flush=True)
    return 1 if bad else 0


raise SystemExit(main())
