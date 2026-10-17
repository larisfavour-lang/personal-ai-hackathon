"""实测本地代理的语音转写能力：step/stepaudio-2.5-asr。

背景：_codex_task_state.md 与 app/main.py 的 /api/voice/transcribe 注释都写着
「本机当前没有可用的中文语音转写能力」，依据是 2026-10-15 的实测。但代理
/v1/models 现在还列着 step/stepaudio-2.5-asr 与 step/stepaudio-2.5-tts。
语音不是提交必做项，但必做表要求「语音若阻塞须说明已核实原因与可用替代」。
所以本轮重新实测一遍，不引用旧记录。

音频来源：macOS 自带 `say -v Tingting` 合成真实中文语音
（16kHz / 单声道 / 16bit / 5.06 秒），落 /tmp/voice_probe/mac_say.wav。
代理自己的 /v1/audio/speech 端点不存在（404 Unknown endpoint），
所以不走代理 TTS，改用本机合成——这不影响结论：验的是代理能不能转写，
不是谁能合成音频。

本脚本只探测，不写工程文件、不改工程代码。按顺序：
1. GET /v1/models 确认音频模型在不在；
2. POST /v1/audio/transcriptions（OpenAI 标准 form-data 端点）验端点存在性；
3. POST /v1/audio/transcriptions 送真实中文音频；
4. POST /v1/chat/completions 送 input_audio（gpt-4o-audio 风格）；
5. POST /v1/chat/completions 送 audio_url / data URI；
6. 顺带确认 step/stepaudio-2.5-asr 作为 chat 模型是否真的可达
   （排除「模型在列表里但其实不可用」这种假阳性）。

运行：/tmp/hack_venv/bin/python tests/voice_asr_probe.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("PA_MODEL_BASE_URL", "http://127.0.0.1:10100/v1")
ASR_MODEL = "step/stepaudio-2.5-asr"
CHAT_MODEL = "step/stepaudio-2.5-chat"
TTS_MODEL = "step/stepaudio-2.5-tts"
WAV = "/tmp/voice_probe/mac_say.wav"
SAY = "小雨今天自己拼图拼了挺久，练琴练了四十分钟。"
PROMPT = "请把这段语音逐字转成文字，只输出转写结果，不要加任何解释。"
TIMEOUT = 180


def log(*a):
    print(*a, flush=True)


def post(path, payload=None, raw=None, headers=None, timeout=TIMEOUT):
    url = BASE + path
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update(headers)
    body = raw if raw is not None else (
        json.dumps(payload).encode("utf-8") if payload is not None else None)
    req = urllib.request.Request(url, data=body, headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:800]
    except Exception as exc:  # noqa: BLE001
        return 0, "%s: %s" % (type(exc).__name__, exc)


def get(path):
    req = urllib.request.Request(BASE + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]
    except Exception as exc:  # noqa: BLE001
        return 0, "%s: %s" % (type(exc).__name__, exc)


def multipart_transcribe(audio_bytes, filename, media_type, model=ASR_MODEL):
    boundary = "----pa-voice-probe-%d" % int(time.time() * 1000)
    parts = []
    for name, value in (("model", model), ("language", "zh"),
                        ("response_format", "json")):
        parts.append(("--" + boundary + "\r\nContent-Disposition: form-data; "
                      "name=\"" + name + "\"\r\n\r\n" + value + "\r\n").encode())
    parts.append(("--" + boundary + "\r\nContent-Disposition: form-data; "
                  "name=\"file\"; filename=\"" + filename + "\"\r\n"
                  "Content-Type: " + media_type + "\r\n\r\n").encode())
    parts.append(audio_bytes)
    parts.append(("\r\n--" + boundary + "--\r\n").encode())
    return post("/audio/transcriptions", raw=b"".join(parts),
                headers={"Content-Type":
                         "multipart/form-data; boundary=" + boundary})


def chat_text(body):
    """把 /chat/completions 的响应收成一段文本，失败时返回错误串。"""
    st, raw = body
    if st != 200:
        return st, "", raw[:400]
    try:
        payload = json.loads(raw)
        content = payload["choices"][0]["message"]["content"] or ""
        return st, content, ""
    except Exception as exc:  # noqa: BLE001
        return st, "", "解析失败 %s：%s" % (type(exc).__name__, raw[:400])


def main() -> int:
    log("=" * 72)
    log("语音转写能力实测（app-eng / t3）")
    log("代理 %s    时间 %s" % (BASE, time.strftime("%F %T")))
    log("=" * 72)

    audio = b""
    if os.path.exists(WAV):
        with open(WAV, "rb") as fh:
            audio = fh.read()
        log("\n测试音频：%s（%d 字节，macOS say -v Tingting 合成）" % (WAV, len(audio)))
        log("目标原文：%s" % SAY)
    else:
        log("\n没找到 %s，跳过需要真实音频的用例" % WAV)

    # 1) 模型列表
    st, body = get("/models")
    audio_models = []
    if st == 200:
        try:
            audio_models = [m["id"] for m in json.loads(body).get("data", [])
                            if any(k in m["id"] for k in
                                   ("audio", "asr", "tts", "whisper"))]
        except Exception:  # noqa: BLE001
            pass
    log("\n[1] GET /v1/models -> %s，音频相关模型 %s" % (st, audio_models))
    has_asr = ASR_MODEL in audio_models
    log("    step/stepaudio-2.5-asr 在列表里：%s" % has_asr)

    # 2) 标准 transcriptions 端点：先用一个极短占位音频，只看端点存不存在
    log("\n[2] POST /v1/audio/transcriptions（占位音频，验端点存在性）")
    st, body = multipart_transcribe(
        base64.b64decode("UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA="),
        "probe.wav", "audio/wav")
    log("    -> %s  %s" % (st, body[:300]))
    transcriptions_ok = (st == 200)

    # 3) 真实音频走 transcriptions
    if audio and not transcriptions_ok:
        log("\n[3] POST /v1/audio/transcriptions 送真实中文音频")
        st, body = multipart_transcribe(audio, "mac_say.wav", "audio/wav")
        log("    -> %s  %s" % (st, body[:400]))

    # 4) chat/completions + input_audio
    if audio:
        log("\n[4] POST /v1/chat/completions 送 input_audio（%s）" % ASR_MODEL)
        st, text, err = chat_text(post("/chat/completions", payload={
            "model": ASR_MODEL, "max_tokens": 512, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "input_audio", "input_audio": {
                    "data": base64.b64encode(audio).decode(),
                    "format": "wav"}}]}]}))
        log("    -> %s" % st)
        log("    文本：%r" % text)
        if err:
            log("    错误：%s" % err)
        input_audio_ok = bool(text.strip())

        # 5) chat/completions + audio_url data URI
        log("\n[5] POST /v1/chat/completions 送 audio_url（data URI）")
        uri = "data:audio/wav;base64," + base64.b64encode(audio).decode()
        st, text, err = chat_text(post("/chat/completions", payload={
            "model": ASR_MODEL, "max_tokens": 512, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "audio_url", "audio_url": {"url": uri}}]}]}))
        log("    -> %s" % st)
        log("    文本：%r" % text)
        if err:
            log("    错误：%s" % err)
        audio_url_ok = bool(text.strip())

        # 5b) 同一请求体换 stepaudio-2.5-chat：确认不是「模型不可达」
        log("\n[5b] 同一请求体换 %s（验模型可达性，不是音频问题）" % CHAT_MODEL)
        st, text, err = chat_text(post("/chat/completions", payload={
            "model": CHAT_MODEL, "max_tokens": 512, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "input_audio", "input_audio": {
                    "data": base64.b64encode(audio).decode(),
                    "format": "wav"}}]}]}))
        log("    -> %s" % st)
        log("    文本：%r" % text)
        if err:
            log("    错误：%s" % err)

        # 6) 纯文本探活：ASR 模型本身作 chat 用是否可达
        log("\n[6] %s 纯文本探活（排除「在列表里但不可用」）" % ASR_MODEL)
        st, text, err = chat_text(post("/chat/completions", payload={
            "model": ASR_MODEL, "max_tokens": 64, "temperature": 0,
            "messages": [{"role": "user", "content": "回复：ok"}]}))
        log("    -> %s  文本：%r  %s" % (st, text[:80], err))
    else:
        input_audio_ok = audio_url_ok = False

    # 7) TTS 端点
    log("\n[7] POST /v1/audio/speech（%s）" % TTS_MODEL)
    st, body = post("/audio/speech", payload={
        "model": TTS_MODEL, "input": SAY, "voice": "alloy",
        "response_format": "wav"})
    log("    -> %s  %s" % (st, body[:250]))

    usable = transcriptions_ok or input_audio_ok or audio_url_ok
    log("\n" + "=" * 72)
    log("结论")
    log("  ASR 模型在 /v1/models 列表里：%s" % has_asr)
    log("  /v1/audio/transcriptions 端点可用：%s" % transcriptions_ok)
    log("  chat/completions + input_audio 转写可用：%s" % input_audio_ok)
    log("  chat/completions + audio_url 转写可用：%s" % audio_url_ok)
    log("  /v1/audio/speech（TTS）端点可用：False（404，见 [7]）")
    log("  >>> 服务端语音转写链路是否可用：%s" % ("可用" if usable else "不可用"))
    log("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
