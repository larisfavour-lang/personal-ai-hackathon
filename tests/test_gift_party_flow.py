"""凑份子送礼：多人一起送走协商链，我一个人买走送礼链。

用户给的例子是「妈妈的生日快到了，要讨论下送什么好吗」。原先这句话会被
当成我一个人的送礼交办，接着被要求选一个送礼对象——多人一起商量的事
没有对应的协商入口。

这一版加了 gift_party 场景：收礼人、场合、共凑总预算、本人出资上限、
谁去买、什么时候送，各参与方分别说。

判据的坑（第一版就踩了）：不能看「提到了几个人」。「给三家一起出游的
孩子们准备礼物」里有「一起」，但做事的是我一个人，那是送礼交办。
必须看有没有把动作指向多人的说法：凑份子、合买、一起送、一起商量。

运行：.venv/bin/python tests/test_gift_party_flow.py
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
from app.service import gift_party_intent  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http_json(base, path, obj=None, timeout=180):
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

    # ── 判据：只看共同行动措辞，不看提到几个人 ──
    PARTY = ["妈妈生日快到了，大家一起商量送什么",
             "想和妹妹合买个包送她",
             "我们凑份子给老师买束花"]
    SOLO = ["给三家一起出游的孩子们准备礼物",
            "给妈妈买双走路舒服的鞋，300以内",
            "给小周的女儿准备生日礼物，300元以内，别和我上次送的重复",
            "周末三家一起带孩子出去玩，我出100以内",
            "五一约朋友去大理，人均预算两千"]
    check("凑份子句识别为多方", all(gift_party_intent(t) for t in PARTY),
          {t: gift_party_intent(t) for t in PARTY})
    check("单人送礼与活动句不误判",
          not any(gift_party_intent(t) for t in SOLO),
          {t: gift_party_intent(t) for t in SOLO})

    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="pa-giftparty-")
    env = dict(os.environ)
    env.update(PA_DATA_DIR=data_dir, PA_BIND="127.0.0.1",
               PA_DEMO_OWNER="A", PYTHONUNBUFFERED="1")
    log = open("/tmp/pa-giftparty-%d.log" % port, "wb")
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

        code, d = http_json(base, "/api/say",
                            {"utterance": "妈妈生日快到了，大家一起商量送什么"})
        check("凑份子句走 gift_party",
              code == 200 and d.get("intent") == "gift_party"
              and d.get("kind") == "gift_party",
              {"intent": d.get("intent"), "kind": d.get("kind")})
        pid = d.get("plan_id")
        check("建成了事项", bool(pid), pid)
        reply = d.get("reply") or ""
        check("回复说清各人报什么",
              all(k in reply for k in ("收礼人", "场合", "谁去买")), reply[:120])

        code, d2 = http_json(base, "/api/say",
                             {"utterance": "给妈妈买双走路舒服的鞋，300以内"})
        check("同一主题的单人送礼仍走送礼链",
              code == 200 and d2.get("kind") == "task",
              {"kind": d2.get("kind")})

        # 条件字段用的是凑份子送礼那套
        if pid:
            code, r = http_json(base, "/api/plan/%s/condition" % pid,
                                {"owner_id": "A",
                                 "conditions": {"收礼人": "妈妈",
                                                "场合": "生日",
                                                "本人出资上限": "200 元以内"}})
            check("凑份子条件字段可用",
                  code == 200 and "本人出资上限" in (r.get("changed_fields") or []),
                  {"code": code, "changed": r.get("changed_fields")})
            code, r2 = http_json(base, "/api/plan/%s/condition" % pid,
                                 {"owner_id": "A",
                                  "conditions": {"目的地": "大理"}})
            check("未知字段被拒，不静默接受",
                  code in (409, 422)
                  and "条件只能包含这些字段" in json.dumps(r2, ensure_ascii=False),
                  {"code": code})
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
