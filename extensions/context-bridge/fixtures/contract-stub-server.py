#!/usr/bin/env python3
"""契约替身服务：按交接方案第 3 节实现插件侧接口，供并行开发与自动断言。

这不是主服务，不连数据库、不调用模型。联调时必须把插件的核心地址
指向真实主服务（默认 http://127.0.0.1:8000），并停掉这个替身。
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

CONTRACT_VERSION = "1"
FIELD_WHITELIST = {"sku", "size", "color", "quantity",
                   "recipient_name", "recipient_phone", "delivery_address"}
PKG_TTL_SECONDS = 300
CODE_TTL_SECONDS = 300


def iso(seconds_from_now: float = 0) -> str:
    return datetime.fromtimestamp(time.time() + seconds_from_now, timezone.utc).isoformat()


class State:
    def __init__(self) -> None:
        self.pair_codes: dict[str, dict] = {}
        self.clients: dict[str, dict] = {}
        self.context_revision = 0
        self.tokens: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.task = {
            "task_id": "task_1799961efdf3",
            "title": "给小周的女儿准备生日礼物（300 元以内）",
            "status": "agreed",
            "can_prepare": True,
            "blocked_reason": "",
        }
        # 需求卡替身。requirements 里故意放一项 pending（对方还没回应的鞋码），
        # 用来验证插件把「待确认」和「已知」分开显示，而不是都画成确定规格。
        self.context = {
            "task_id": self.task["task_id"],
            "purpose": "shopping_help",
            "context_revision": "ctx-1",
            "subject_label": "小雨",
            "task_status": "agreed",
            "summary": "300 元以内的儿童运动鞋，别和上次送的重复。",
            "requirements": [
                {"key": "budget", "label": "预算", "value": "300 元以内", "state": "known"},
                {"key": "recipient", "label": "送给谁", "value": "小雨（小周的女儿）", "state": "known"},
                {"key": "avoid_repeat", "label": "排除项", "value": "别和上次送的重复", "state": "known"},
                {"key": "shoe_size", "label": "鞋码", "value": "", "state": "pending"},
                {"key": "color_pref", "label": "颜色偏好", "value": "", "state": "pending"},
            ],
            "missing_fields": ["shoe_size"],
            "search_terms": ["儿童运动鞋 32码 300元以内", "小雨 儿童运动鞋"],
            # 档案里真实存在、但导出时不该出现的资料。测试用它们做探针：
            # 导出文本里出现任何一个，都说明把没授权的内容带出去了。
            "profile_private": {
                "recipient_phone": "13800000000",
                "delivery_address": "上海市徐汇区示例路 1 号 302 室（虚构）",
            },
            "export_text": (
                "需求说明（本人授权，仅用于本次选购）：\n"
                "对象：小雨\n"
                "预算：300 元以内\n"
                "排除项：别和上次送的重复\n"
                "待确认：鞋码、颜色偏好\n"
                "以上内容由本人从个人助理复制，未经对方确认的条件已标注。"
            ),
            # 每个 known 需求都要有来源。缺来源的 known 项在界面上只能显示「—」，
            # 用户没法判断这个值是核对过的还是猜的——所以这里逐项给全。
            "source_summary": [
                {"field": "budget", "source_label": "本人交办时说明",
                 "confirmed_at": iso(-7200)},
                {"field": "recipient", "source_label": "本人交办时说明",
                 "confirmed_at": iso(-7200)},
                {"field": "avoid_repeat", "source_label": "本人交办时说明",
                 "confirmed_at": iso(-7200)},
            ],
            "generated_at": iso(),
        }
        self.package = {
            "contract_version": CONTRACT_VERSION,
            "task_id": self.task["task_id"],
            "proposal_version": 1,
            "action_type": "shopping_fill",
            "target_origin": "http://127.0.0.1:8097",
            "subject_label": "小雨",
            "product": {"id": "pr_sneakers", "title": "儿童运动鞋"},
            "fields": [
                {"key": "sku", "value": "pr_sneakers", "label": "商品"},
                {"key": "size", "value": "32 码", "label": "规格"},
                {"key": "quantity", "value": "1", "label": "数量"},
                {"key": "recipient_name", "value": "周雨萱", "label": "收货人"},
                {"key": "recipient_phone", "value": "13800000000", "label": "收货电话"},
                {"key": "delivery_address", "value": "上海市徐汇区示例路 1 号 302 室（虚构）",
                 "label": "收货地址"},
            ],
            "source_summary": [
                {"field": "sku", "source_label": "本次协商选定的商品", "confirmed_at": iso(-7200)},
                {"field": "size", "source_label": "家长本人核对", "confirmed_at": iso(-3600)},
                {"field": "quantity", "source_label": "本次事项约定", "confirmed_at": iso(-600)},
                {"field": "recipient_name", "source_label": "本人填写的测试收货资料",
                 "confirmed_at": iso(-600)},
                {"field": "recipient_phone", "source_label": "本人填写的测试收货资料",
                 "confirmed_at": iso(-600)},
                {"field": "delivery_address", "source_label": "本人填写的测试收货资料",
                 "confirmed_at": iso(-600)},
            ],
            "missing_fields": [],
            "can_fill": True,
            "expires_at": iso(PKG_TTL_SECONDS),
        }
        # 供测试注入反例的开关。
        self.scenario = "happy"


STATE = State()


def err(code: str, message: str, retryable: bool = False, status: int = 400) -> dict:
    return {"error": {"code": code, "message": message, "retryable": retryable},
            "_status": status}


def ok(data: dict) -> dict:
    out = dict(data)
    out["_status"] = 200
    return out


def token_of(authorization: str) -> dict | None:
    if not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer "):].strip()
    entry = STATE.tokens.get(token)
    if not entry:
        return None
    if entry["expires_at"] < time.time() or entry["revoked"]:
        return None
    return entry


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静一点，输出里不放凭证
        sys.stderr.write("[stub] %s\n" % (fmt % args))

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _send(self, payload: dict) -> None:
        status = payload.pop("_status", 200)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8097")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8097")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/__scenario":
            self._send(ok({"scenario": STATE.scenario}))
            return
        if path == "/api/extension/tasks":
            entry = token_of(self.headers.get("Authorization") or "")
            if entry is None:
                self._send(err("unauthorized", "设备凭证无效或已撤销", False, 401))
                return
            task = dict(STATE.task)
            if STATE.scenario == "blocked_missing_fields":
                task["can_prepare"] = False
                task["blocked_reason"] = "对方还没回应，约定未达成"
            self._send(ok({"tasks": [task]}))
            return
        self._send(err("not_found", "没有这个接口", False, 404))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._body()

        if path == "/__scenario":
            STATE.scenario = str(body.get("scenario") or "happy")
            self._send(ok({"scenario": STATE.scenario}))
            return

        if path == "/api/extension/context":
            # 需求卡接口替身。它不带 target_origin：需求是跨页面复用的，
            # 绑死在某个页面上就违背了「换页面少解释」。
            entry = token_of(self.headers.get("Authorization") or "")
            if entry is None:
                self._send(err("unauthorized", "设备凭证无效或已撤销", False, 401))
                return
            if str(body.get("task_id") or "") != STATE.task["task_id"]:
                self._send(err("forbidden", "这个事项不属于当前用户", False, 403))
                return
            ctx = dict(STATE.context)
            # 版本号表示「内容变没变」，不是「被请求过几次」。
            # 无变化时保持稳定，插件才能靠它判断要不要重新摊差异；
            # 每次都换版会让界面永远显示「需求有变化」。
            if STATE.context_revision == 0:
                STATE.context_revision = 1
            ctx["context_revision"] = "ctx-%d" % STATE.context_revision
            # 反例场景：backend 侧改动预算，用来验证插件复制前重新取到新版本时
            # 会把差异摊给用户，而不是悄悄复制旧值。
            if STATE.scenario == "budget_changed":
                # 预算变了，能复制出去的内容必须一起变：只改 requirements
                # 会让用户看着 250 却复制到写着 300 的搜索词和需求说明。
                # 版本号也只在内容真的变了时才换，不是每次请求都换。
                STATE.context_revision += 1
                reqs = []
                for r in ctx["requirements"]:
                    if r["key"] == "budget":
                        r = dict(r, value="250 元以内")
                    reqs.append(r)
                ctx["requirements"] = reqs
                ctx["search_terms"] = ["儿童运动鞋 32码 250元以内", "小雨 儿童运动鞋"]
                ctx["export_text"] = (
                    "需求说明（本人授权，仅用于本次选购）：\n"
                    "对象：小雨\n"
                    "预算：250 元以内\n"
                    "排除项：别和上次送的重复\n"
                    "待确认：鞋码、颜色偏好\n"
                    "以上内容由本人从个人助理复制，未经对方确认的条件已标注。"
                )
                ctx["context_revision"] = "ctx-%d" % STATE.context_revision
            # 反例场景：把未回应的鞋码标成 known 却没给值，
            # 验证插件按契约拒绝，不把待确认项当确定规格展示。
            if STATE.scenario == "pending_as_known":
                reqs = []
                for r in ctx["requirements"]:
                    if r["key"] == "shoe_size":
                        r = dict(r, state="known", value="")
                    reqs.append(r)
                ctx["requirements"] = reqs
            self._send(ok(ctx))
            return

        if path == "/api/extension/pair-codes":
            code = uuid.uuid4().hex[:4].upper() + "-" + uuid.uuid4().hex[:4].upper()
            STATE.pair_codes[code] = {"expires_at": time.time() + CODE_TTL_SECONDS, "used": False}
            self._send(ok({"code": code, "expires_at": iso(CODE_TTL_SECONDS)}))
            return

        if path == "/api/extension/pair":
            code = str(body.get("code") or "").strip().upper()
            entry = STATE.pair_codes.get(code)
            if entry is None or entry["used"] or entry["expires_at"] < time.time():
                self._send(err("unprocessable", "配对码无效、已用过或已过期", False, 422))
                return
            entry["used"] = True
            token = "devtoken-" + uuid.uuid4().hex
            client_id = "client-" + uuid.uuid4().hex[:8]
            STATE.tokens[token] = {"client_id": client_id, "user_id": "u_local_A",
                                   "expires_at": time.time() + 86400, "revoked": False}
            STATE.clients[client_id] = {"token": token,
                                        "user": {"user_id": "u_local_A", "display_name": "我（本地演示）"}}
            self._send(ok({"access_token": token, "client_id": client_id,
                           "expires_at": iso(86400),
                           "user": {"user_id": "u_local_A", "display_name": "我（本地演示）"}}))
            return

        if path == "/api/extension/revoke":
            entry = token_of(self.headers.get("Authorization") or "")
            if entry is None:
                self._send(err("unauthorized", "设备凭证无效", False, 401))
                return
            entry["revoked"] = True
            self._send(ok({"revoked": True}))
            return

        if path == "/api/extension/prepare":
            entry = token_of(self.headers.get("Authorization") or "")
            if entry is None:
                self._send(err("unauthorized", "设备凭证无效或已撤销", False, 401))
                return
            if STATE.scenario == "blocked_missing_fields":
                self._send(err("unprocessable",
                               "这个事项还缺参数：对方未回应，约定未达成", False, 422))
                return
            if STATE.scenario == "stale":
                self._send(err("conflict", "相关事实已更新，参数包已失效，请重新生成", False, 409))
                return
            if str(body.get("task_id") or "") != STATE.task["task_id"]:
                self._send(err("forbidden", "这个事项不属于当前用户", False, 403))
                return
            snapshot_id = "snap-" + uuid.uuid4().hex[:12]
            pkg = dict(STATE.package)
            # 反例场景：参数包里带一个页面会拒绝的数量（6 件，超过页面上限 5）。
            # 用来证明页面业务状态不接受时，首发和重发都不会报 verified。
            # 不写死 9——页面规则是「超过 5 件即库存不足」，取 6 才是边界值。
            if STATE.scenario == "over_stock":
                pkg["fields"] = [
                    dict(f, value="6") if f["key"] == "quantity" else dict(f)
                    for f in pkg["fields"]
                ]
            pkg["snapshot_id"] = snapshot_id
            pkg["expires_at"] = iso(PKG_TTL_SECONDS)
            STATE.snapshots[snapshot_id] = {"pkg": pkg, "expires_at": time.time() + PKG_TTL_SECONDS}
            self._send(ok(pkg))
            return

        if path == "/api/extension/validate":
            entry = token_of(self.headers.get("Authorization") or "")
            if entry is None:
                self._send(err("unauthorized", "设备凭证无效或已撤销", False, 401))
                return
            snap = STATE.snapshots.get(str(body.get("snapshot_id") or ""))
            if snap is None:
                self._send(err("conflict", "参数包不存在或已失效", False, 409))
                return
            if snap["expires_at"] < time.time():
                self._send(err("conflict", "参数包已过期，请重新生成", False, 409))
                return
            reasons = []
            if str(body.get("target_origin") or "") != snap["pkg"]["target_origin"]:
                reasons.append("当前页面不是这次代填的目标页面")
            observed = str(body.get("observed_product_id") or "")
            if observed != snap["pkg"]["product"]["id"]:
                reasons.append("页面商品与约定商品不一致")
            if body.get("user_confirmed") is not True:
                reasons.append("缺少用户本次确认")
            if STATE.scenario == "revoked":
                reasons.append("授权已撤销，不能继续代填")
            if reasons:
                self._send(ok({"valid": False, "reasons": reasons, "validated_at": iso()}))
                return
            self._send(ok({"valid": True, "reasons": [], "validated_at": iso()}))
            return

        if path == "/api/extension/receipts":
            entry = token_of(self.headers.get("Authorization") or "")
            if entry is None:
                self._send(err("unauthorized", "设备凭证无效或已撤销", False, 401))
                return
            snap = STATE.snapshots.get(str(body.get("snapshot_id") or ""))
            if snap is None:
                self._send(err("conflict", "参数包不存在或已失效", False, 409))
                return
            key = str(body.get("idempotency_key") or "")
            if key and key in STATE.receipts:
                self._send(ok(dict(STATE.receipts[key], duplicate=True)))
                return
            expected = {f["key"]: f["value"] for f in snap["pkg"]["fields"]}
            observed = body.get("observed_fields") or {}
            mismatches = []
            for k, v in expected.items():
                got = observed.get(k)
                if got is None:
                    mismatches.append({"field": k, "expected": v, "observed": "", "reason": "页面未读回该字段"})
                elif str(got).strip() != str(v).strip():
                    mismatches.append({"field": k, "expected": v, "observed": str(got), "reason": "与页面实际值不一致"})
            client_status = str(body.get("client_status") or "failed")
            status = "failed"
            if client_status == "filled" and not mismatches:
                status = "verified"
            elif not mismatches:
                status = client_status
            receipt = {"receipt_id": "rcpt-" + uuid.uuid4().hex[:12], "status": status,
                       "mismatches": mismatches, "snapshot_id": snap["pkg"]["snapshot_id"],
                       "task_id": snap["pkg"]["task_id"]}
            if key:
                STATE.receipts[key] = receipt
            self._send(ok(receipt))
            return

        self._send(err("not_found", "没有这个接口", False, 404))


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8098
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    sys.stderr.write("[stub] contract stub listening on http://127.0.0.1:%d\n" % port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
