"""服务端语音转写：走 Step 的 audio chat 模型，直连。

背景（2026-10-19 实测）：本地代理 opencodex 的入站解析只放行 image_url 一种
content part，音频 part 在 src/chat/inbound.ts:36 就被丢掉，路由表里也没有
/v1/audio/*。所以转写不能走本地代理，必须直连 Step。

实测可用的组合，三条都踩过坑：
  ① 模型 ID 不带 step/ 前缀。Step 的 /models 返回的就是 stepaudio-2.5-chat；
     带前缀返回 model_invalid「does not exist or you do not have access to it」。
     那个前缀是 opencodex 自己加的命名空间，不是 Step 的。
  ② 用的是 stepaudio-2.5-chat，不是 stepaudio-2.5-asr。后者列表里有、调用即
     model_invalid；前者同一条 /chat/completions 链路能吃音频。
  ③ 提示词必须明确禁止添加描述和语气标签，否则它会在转写前面缀一段
     「（温柔亲切，带着一丝阳光般的暖意）」之类的情绪说明。

运行：.venv/bin/python tests/test_voice_transcribe.py
"""
from __future__ import annotations

import base64
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


def make_wav(path):
    """造一段真实语音：macOS 的 say 合成中文，不是手搓的假音频头。"""
    subprocess.run(["say", "-o", str(path), "--data-format=LEI16@16000",
                    "今天天气不错，我们一起去公园散步"], check=False)
    return path.exists() and path.stat().st_size > 1000


def main():
    checks = []

    def check(name, ok, detail=None):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(json.dumps(checks[-1], ensure_ascii=False), flush=True)

    key = os.environ.get("PA_ASR_API_KEY") or ""
    if not key:
        # 没给 key 就跑「不可用」那一半：验的是失败时如实报原因、不冒充成功。
        # 转写正确性要 key，跳过而不是记失败。
        print(json.dumps({"name": "未设 PA_ASR_API_KEY，本轮只验不可用路径",
                          "ok": True, "detail": None}, ensure_ascii=False), flush=True)

    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="pa-voice-")
    env = dict(os.environ)
    env.update(PA_DATA_DIR=data_dir, PA_BIND="127.0.0.1",
               PA_DEMO_OWNER="A", PYTHONUNBUFFERED="1")
    if key:
        env["PA_ASR_API_KEY"] = key
    log = open("/tmp/pa-voice-%d.log" % port, "wb")
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

        wav = pathlib.Path(tempfile.gettempdir()) / ("pa-voice-%d.wav" % port)
        made = make_wav(wav)
        check("造出真实语音样本", made, None if made else "say 合成失败")
        if not made:
            return 1
        audio = base64.b64encode(wav.read_bytes()).decode()

        code, body = http_json(base, "/api/voice/transcribe",
                               {"audio_b64": audio, "media_type": "audio/wav"})
        if key:
            check("配了 key 时转写可用", code == 200 and body.get("available") is True,
                  {"code": code, "available": body.get("available"),
                   "reason": body.get("reason")})
            text = (body.get("text") or "").strip()
            check("转写内容就是音频里的话",
                  "天气不错" in text and "公园" in text, text[:80])
            check("转写结果不带情绪标签",
                  "（" not in text and "温柔" not in text and "语气" not in text,
                  text[:80])
        else:
            check("没配 key 时如实报不可用",
                  code == 200 and body.get("available") is False
                  and bool(body.get("reason")),
                  {"available": body.get("available"), "reason": body.get("reason")})
            check("不可用时给出替代办法",
                  "打字" in (body.get("hint") or ""), (body.get("hint") or "")[:60])
            check("不可用时不返回冒充成功的文字",
                  (body.get("text") or "") == "", repr(body.get("text"))[:40])

        # 坏 base64 不能 500，要给出可读原因
        code, body = http_json(base, "/api/voice/transcribe",
                               {"audio_b64": "!!!not-base64!!!",
                                "media_type": "audio/wav"})
        check("坏 base64 不 500，给可读原因",
              code in (200, 422) and "base64" in json.dumps(body, ensure_ascii=False),
              {"code": code, "body": str(body)[:120]})
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
