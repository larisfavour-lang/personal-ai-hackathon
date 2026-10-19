"""跟进句要接着原来那件办，不新开没有主体的事项。

缺陷：`create_task` 只在原话一字不差时复用。用户说「给爸爸买双走路舒服的鞋」
之后跟一句「300 以内」，抽得出预算、抽不出收礼人，于是新建一条
`subject_name=null` 的孤儿事项，预算孤零零挂在那件上，原来那件的预算还是空的。
两三句闲聊下来，事项列表里躺着好几件同一件事的残片。

修法：抽不出主体（ambiguous_subject）且没带新场合，并且未完结事项恰好只有一件时，
把跟进句抽出的条件并进那件；否则仍按原路径新建。合并不覆盖已有主体与原 occasion。

运行：.venv/bin/python tests/test_task_followup_merge.py
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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def j(base, path, obj=None, timeout=90):
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


def matters(base):
    code, body = j(base, "/api/matters")
    return [m for m in (body.get("matters") or [])]


def main():
    checks = []

    def check(name, ok, detail=None):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(json.dumps(checks[-1], ensure_ascii=False), flush=True)

    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="pa-followup-")
    env = dict(os.environ)
    env.update(PA_DATA_DIR=data_dir, PA_BIND="127.0.0.1",
               PA_DEMO_OWNER="A", PYTHONUNBUFFERED="1")
    log = open("/tmp/pa-followup-%d.log" % port, "wb")
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
        cwd=str(ROOT), env=env, stdout=log, stderr=log)
    base = "http://127.0.0.1:%d" % port
    try:
        ok = False
        for _ in range(120):
            try:
                ok = j(base, "/api/health", timeout=5)[1].get("ok")
            except Exception:
                ok = False
            if ok:
                break
            time.sleep(0.3)
        check("隔离服务可启动", bool(ok))
        if not ok:
            return 1

        # 先有一个送礼对象，好让首句抽得出主体
        j(base, "/api/gift-targets", {"name": "爸爸", "relationship": "长辈"})

        code, first = j(base, "/api/say",
                        {"utterance": "给爸爸买双走路舒服的鞋"})
        t1 = first.get("task_id")
        check("首句建成事项", code == 200 and bool(t1), t1)

        code, second = j(base, "/api/say", {"utterance": "300 以内"})
        t2 = second.get("task_id")
        check("跟进句复用同一件", t2 == t1 and second.get("reused") is True,
              {"t1": t1, "t2": t2, "reused": second.get("reused")})

        code, third = j(base, "/api/say", {"utterance": "要软底的"})
        check("第二句跟进仍然复用", third.get("task_id") == t1,
              {"t3": third.get("task_id")})

        rows = matters(base)
        check("事项总数保持 1，不产生残片", len(rows) == 1,
              [r["utterance"] for r in rows])

        only = rows[0] if rows else {}
        check("主体没有被跟进句抹掉", only.get("subject_name") == "爸爸",
              only.get("subject_name"))
        check("预算并进了原来那件", only.get("budget_max") == 300.0,
              only.get("budget_max"))

        # 明确的新交办仍然新开一件，不被跟进逻辑吞掉
        j(base, "/api/gift-targets", {"name": "妈妈", "relationship": "长辈"})
        code, fresh = j(base, "/api/say",
                        {"utterance": "给妈妈买双走路舒服的鞋"})
        check("换了收礼人的新交办仍然新建",
              fresh.get("task_id") not in (None, t1),
              {"new": fresh.get("task_id"), "old": t1})

        rows2 = matters(base)
        check("新交办后事项总数为 2", len(rows2) == 2,
              [r["utterance"] for r in rows2])

        # 原话一字不差再来一次，沿用既有的幂等复用
        code, again = j(base, "/api/say",
                        {"utterance": "给妈妈买双走路舒服的鞋"})
        check("同一句原话重复仍然幂等复用",
              again.get("task_id") == fresh.get("task_id"),
              {"again": again.get("task_id"), "fresh": fresh.get("task_id")})
        check("重复原话不增加事项数", len(matters(base)) == 2,
              len(matters(base)))
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
