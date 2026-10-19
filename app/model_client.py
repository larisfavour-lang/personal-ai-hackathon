"""模型调用：统一 OpenAI 兼容接口，输出必须通过结构校验。

约定：
- 只允许模型输出已知主体 ID、已知商品 ID、条件引用、理由、决策。
- 模型输出不执行代码，全部字段按白名单校验。
- 模型不可用时抛出 ModelUnavailable，由上层显示重试入口。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:10100/v1"
# 默认模型用本地代理上的 Step：评分要求 Step 真实承担作品的智能环节，
# 换标签不算，所以默认值直接落在 Step 上，跑不通时再由启动命令显式指定别的。
# 需要临时换回别的模型时设 PA_MODEL_NAME 即可，不必改这里。
DEFAULT_MODEL = "step/step-5-preview"

ALLOWED_DECISIONS = {"accept", "reject", "counter", "ask_clarify"}

# 语音转写。走 Step 的 audio chat 模型：本地代理 opencodex 只放行 image_url 一种
# content part，音频 part 在入站解析时就被丢掉，所以转写必须直连 Step。
# 实测可用组合（2026-10-19）：model=stepaudio-2.5-chat，audio 用 input_audio/wav。
# 注意两点：
#   ① 模型 ID 不带 step/ 前缀。列表里就是 stepaudio-2.5-chat；带前缀会返回
#      model_invalid——那个前缀是 opencodex 自己加的命名空间，不是 Step 的。
#   ② 提示词必须明确禁止添加描述和语气标签，否则它会在转写前面缀一段
#      「（温柔亲切，带着一丝阳光般的暖意）」之类的情绪说明。
DEFAULT_ASR_BASE_URL = "https://api.stepfun.com/step_plan/v1"
DEFAULT_ASR_MODEL = "stepaudio-2.5-chat"
ASR_PROMPT = ("把这段音频里的话原样写成文字。不要加任何描述、标签、语气说明或解释，"
              "只输出说的内容本身。")


def transcribe_audio(audio_b64, media_type="audio/webm", timeout=120):
    """把 base64 音频转成文字。返回 (ok, text, reason)。

    失败时 ok=False 并给出可读原因，由上层决定怎么显示——不返回空字符串冒充成功，
    也不把一段静态录音说成已转写。
    """
    import base64
    import urllib.error
    import urllib.request

    base = (os.environ.get("PA_ASR_BASE_URL") or DEFAULT_ASR_BASE_URL).rstrip("/")
    model = os.environ.get("PA_ASR_MODEL") or DEFAULT_ASR_MODEL
    key = os.environ.get("PA_ASR_API_KEY") or os.environ.get("PA_MODEL_API_KEY") or ""
    fmt = {"audio/webm": "webm", "audio/mp4": "mp4", "audio/mpeg": "mp3",
           "audio/wav": "wav", "audio/ogg": "ogg"}.get(media_type, "webm")
    try:
        base64.b64decode(audio_b64, validate=True)
    except Exception:
        return False, "", "音频数据不是有效的 base64"
    payload = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": ASR_PROMPT},
        {"type": "input_audio", "input_audio": {"data": audio_b64, "format": fmt}},
    ]}]}
    req = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
        method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = str(json.loads(e.read().decode("utf-8")).get("error", {}).get("message", ""))
        except Exception:
            pass
        if e.code in (401, 403):
            return False, "", "转写服务拒绝了密钥（%s）" % (detail or e.code)
        if e.code == 404:
            return False, "", "转写模型或接口不可用（%s）" % (detail or e.code)
        return False, "", "转写服务返回 %s（%s）" % (e.code, detail)
    except Exception as e:
        return False, "", "转写服务没连上（%s）" % type(e).__name__
    text = str((body.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    if not text:
        return False, "", "转写结果是空的"
    return True, text, ""


class ModelUnavailable(RuntimeError):
    pass


class ModelClient:
    # 上游代理偶发 502/503 与断流，重试两次；仍失败则如实报错，不伪装成功。
    RETRY_STATUS = (502, 503, 504)
    MAX_ATTEMPTS = 3
    BACKOFF_SECONDS = 1.5

    def __init__(self, base_url=None, model=None, timeout=90):
        self.base_url = (base_url or os.environ.get("PA_MODEL_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("PA_MODEL_NAME") or DEFAULT_MODEL
        self.timeout = timeout
        # 直连自己的模型接口时要带密钥。文档承诺了这个变量，
        # 不读它的话用户配了密钥却静默按匿名请求发出去，
        # 表现是「连不上」但看不出原因。
        self.api_key = os.environ.get("PA_MODEL_API_KEY") or ""
        # 记录每次实际发给模型的输入，供隔离验收直接检查真实输入，
        # 而不是只检查库里存的输出。
        self.sent_inputs = []

    def _headers(self):
        """请求头。带了密钥才加 Authorization，不把密钥写进日志或返回给前端。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def chat(self, messages, max_tokens=800, temperature=0.2):
        self.sent_inputs.append({
            "messages": json.loads(json.dumps(messages, ensure_ascii=False)),
            "at": time.time(),
        })
        # 空内容不证明是预算不足；在既有三次尝试内有限提高预算，
        # 最后一次空响应重试使用既有 8000 fallback，不增加请求次数。
        # 调用方显式给出的更高预算必须保留，不能被 fallback 缩小。
        budget = max_tokens
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps({
                "model": self.model,
                "messages": messages,
                "max_tokens": budget,
                "temperature": temperature,
            }).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        last_error = ""
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                try:
                    content = payload["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise ModelUnavailable("模型响应缺少内容字段") from exc
                # 空内容要当成失败重试，不能直接返回。
                # 本地代理上的 step 系列偶发把 token 预算用满后返回
                # content=""，finish_reason 仍是 stop——直接返回的话，
                # 上层拿到的是空串，整条链路报「未返回可解析的 JSON」，
                # 看起来像模型坏了，其实重试一次就好。
                if content and content.strip():
                    return content
                last_error = "模型返回了空内容"
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                last_error = "模型返回 HTTP %s：%s" % (exc.code, detail)
                # 图片请求要把代理侧的瞬时 400 也纳入重试。
                # 实测同一个 data URI 连发 6 次偶尔会返回
                # 「url invalid. url: data:...」，而 URL 本身格式正确、
                # 紧接着重发就成功。这不是请求写错，是代理偶发，
                # 不重试的话拍照链在演示时会随机失败。
                transient = exc.code in self.RETRY_STATUS or (
                    exc.code == 400 and "url invalid" in detail)
                if not transient or attempt == self.MAX_ATTEMPTS:
                    raise ModelUnavailable(last_error) from exc
            except ModelUnavailable:
                raise
            except Exception as exc:
                last_error = "模型调用失败：%s" % type(exc).__name__
                if attempt == self.MAX_ATTEMPTS:
                    raise ModelUnavailable(last_error) from exc
            if attempt < self.MAX_ATTEMPTS:
                # 空内容这一路把预算放大再发，别的失败原因原样重试。
                if last_error == "模型返回了空内容":
                    # attempt counts the failed request; its next request is final
                    # when attempt + 1 == MAX_ATTEMPTS. Other failures keep budget.
                    fallback = 8000 if attempt + 1 == self.MAX_ATTEMPTS else min(budget * 2, 8000)
                    budget = max(budget, fallback)
                    req = urllib.request.Request(
                        self.base_url + "/chat/completions",
                        data=json.dumps({
                            "model": self.model,
                            "messages": messages,
                            "max_tokens": budget,
                            "temperature": temperature,
                        }).encode("utf-8"),
                        headers=self._headers(),
                        method="POST",
                    )
                time.sleep(self.BACKOFF_SECONDS * attempt)
        raise ModelUnavailable(last_error)

    def chat_vision(self, prompt, image_b64, media_type="image/jpeg",
                    max_tokens=800, temperature=0.2,
                    user_prompt="请识别这张图片里的内容。"):
        """带一张图片调用模型，返回模型对这张图的文字回复。

        图片走 OpenAI 兼容的 image_url 段，data URI 内联，本应用不落盘；
        图确实会送到所配置的模型服务，界面上如实说明，不写成「不外传」。
        已实测本地代理上的 step/step-5-preview 与 step/step-3.7-flash
        都能收到并正确读出图中文字（见 README「Step 的使用情况」）。

        走 chat() 而不是另写请求逻辑，才能复用同一套重试、
        空内容放大预算和 Authorization 处理。
        """
        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url",
                     "image_url": {"url": "data:%s;base64,%s" % (media_type, image_b64)}},
                ],
            },
        ]
        return self.chat(messages, max_tokens=max_tokens, temperature=temperature)

    def chat_vision_json(self, prompt, image_b64, media_type="image/jpeg",
                         max_tokens=1600, temperature=0.2,
                         user_prompt="请识别这张图片里的内容。"):
        """带图片调用并要求 JSON 输出，解析失败时做一次修正重试。

        prompt 是系统指令，user_prompt 是跟着图片一起发的用户提问，
        两者分开：识别类任务的指令很长，塞进 user 消息会让模型
        分不清哪句是要求、哪句是待识别的内容。

        默认 1600：图片请求本身更占预算，Step 系列在小预算下会用满后
        返回空 content。重试时保留图片，不能只把图片丢掉重发——
        那样模型看到的是另一个问题，答案自然对不上。
        """
        raw = self.chat_vision(prompt, image_b64, media_type,
                               user_prompt=user_prompt,
                               max_tokens=max_tokens, temperature=temperature)
        parsed = parse_json_loose(raw)
        if parsed is None:
            retry_user = (user_prompt + "\n\n请只输出一个 JSON 对象，"
                          "不要解释、不要代码块、不要多余文字。")
            raw = self.chat_vision(prompt, image_b64, media_type,
                                   user_prompt=retry_user,
                                   max_tokens=max(max_tokens, 2400), temperature=0)
            parsed = parse_json_loose(raw)
        if parsed is None:
            raise ModelUnavailable("模型两次未返回可解析的 JSON")
        return parsed

    def chat_json(self, messages, max_tokens=1600, temperature=0.2):
        """请求 JSON 输出，并在解析失败时做一次修正重试。

        默认 1600 而不是 800：本地代理上的 step 系列模型在 800 的预算下
        会把 token 用满后返回空 content（finish_reason 仍是 stop，
        看不出是超限），整条链路就变成「模型未返回可解析的 JSON」。
        提高默认值是先解决这个坑，再靠下面的空内容重试兜底。
        """
        raw = self.chat(messages, max_tokens=max_tokens, temperature=temperature)
        parsed = parse_json_loose(raw)
        if parsed is None:
            retry_budget = max(max_tokens, 2400)
            raw = self.chat(
                messages + [
                    {"role": "user", "content": "上一次输出不是合法 JSON。请只输出一个 JSON 对象，"
                     "不要解释、不要代码块、不要多余文字。"}
                ],
                max_tokens=retry_budget, temperature=0)
            parsed = parse_json_loose(raw)
        if parsed is None:
            raise ModelUnavailable("模型两次未返回可解析的 JSON")
        return parsed


def parse_json_loose(text):
    if text is None:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None


def normalize_decision(value):
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in ALLOWED_DECISIONS:
        return v
    aliases = {
        "接受": "accept", "同意": "accept", "可行": "accept", "ok": "accept",
        "拒绝": "reject", "不同意": "reject", "不行": "reject",
        "反提案": "counter", "counter_proposal": "counter", "counterproposal": "counter",
        "请求澄清": "ask_clarify", "澄清": "ask_clarify", "clarify": "ask_clarify",
    }
    return aliases.get(v)
