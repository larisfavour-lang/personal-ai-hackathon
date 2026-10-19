"""FastAPI 入口：资料录入/纠正、创建任务、推进协商、读取任务、确认提案。

前端身份切换只服务本地演示；owner_id 一律由服务端会话决定，不接受模型输出切换。
"""

from __future__ import annotations

import os
import secrets
import base64
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agents
from . import db as dbm
from . import service
from . import model_client
from .model_client import ModelClient

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PA_DATA_DIR") or (BASE_DIR.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "hackathon.db"

db = dbm.DB(DB_PATH)
dbm.seed(db)
client = ModelClient()


def _apply_model_config():
    """把库里保存的模型配置套到运行中的 client 上。

    优先级：库里的配置 > 环境变量 > 代码默认值。用户在界面上改过一次，
    重启服务后仍然是他选的那个，不会悄悄退回默认模型。
    """
    cfg = dbm.model_config_get(db)
    if cfg.get("base_url"):
        client.base_url = cfg["base_url"].rstrip("/")
    if cfg.get("model_name"):
        client.model = cfg["model_name"]
    if cfg.get("api_key"):
        client.api_key = cfg["api_key"]


_apply_model_config()

app = FastAPI(title="Personal AI 黑客松作品", version="0.1.0")
# 静态 HTML 不许被浏览器缓存。之前只有 / 和 /apps 这两个路由带了 no-store，
# 而 /static/index.html 走的是 StaticFiles 挂载，响应里没有 cache-control，
# 浏览器按启发式缓存——改了页面用户在手机/桌面上看到的还是上一版，
# 「改过的地方像没生效」。这个中间件给所有 /static 下的 HTML 补上头。
@app.middleware("http")
async def _no_store_static_html(request, call_next):
    response = await call_next(request)
    try:
        path = request.url.path or ""
        if path.startswith("/static/") and (path.endswith(".html") or path.endswith(".webmanifest")):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
    except Exception:
        pass
    return response


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------------------------------------------------------------------------
# 服务端本人会话。
#
# 产品里只有用户本人一个身份：谁在用自己的助理，由服务端会话说，不由请求参数说。
# 之前普通接口都从 ?owner_id= 取身份，把 B/C 传进去就能读到别人的内容，
# 这不是「切换成别人」，而是没校验请求来源。现在：
#   - 普通接口从本会话 cookie 解析本人，不接受 URL / 表单 / 模型输出指定别人；
#   - A/B/C 三个演示身份只服务于本地三家庭演示，放在明确标注的测试路径
#     /api/demo/* 下，正常入口不提供；
#   - 插件接口从设备凭证解析本人。
# 公开部署的真实账号认证未实现前，这里仍是本地单用户，demo_mode 会明确标注。
DEMO_OWNER = os.environ.get("PA_DEMO_OWNER", "A")
SESSION_COOKIE = "pa_session"
SESSIONS: dict[str, str] = {}


def current_user(request: Request) -> str:
    """解析当前会话的本人。没有会话时发一个，固定绑到本人。"""
    sid = request.cookies.get(SESSION_COOKIE) or ""
    owner = SESSIONS.get(sid)
    if owner:
        return owner
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = DEMO_OWNER
    # 记下要给这个响应种下的会话，下面统一 set_cookie。
    request.state.new_session = sid
    return DEMO_OWNER


def _with_session(request: Request, payload: Any) -> Any:
    """把新会话 cookie 带到响应上。登录态只在服务端，前端不存身份。"""
    sid = getattr(request.state, "new_session", "")
    if not sid:
        return payload
    return JSONResponse(payload, headers={
        "Set-Cookie": "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400"
                      % (SESSION_COOKIE, sid)})


def demo_owner_or_403(owner_id: str) -> str:
    """演示身份校验。只给测试路径用，且必须显式隔离。"""
    if owner_id not in ("A", "B", "C"):
        raise HTTPException(status_code=400,
                            detail="演示身份只能是 A / B / C，且仅用于标注的测试路径")
    return owner_id


class SessionBody(BaseModel):
    owner_id: str = Field(pattern="^[AB]$")


# 普通入口的身份一律来自服务端会话（current_user），不要求前端在 body 里带
# owner_id。这里做成可选、带默认空串：带了就按会话校验（防止有人手改请求
# 冒充别家），没带就用会话本人。
#
# 原先这些字段是必填的，而 handler 一律从 current_user 取本人，两者矛盾：
# 前端按会话本来的设计只发业务字段，于是每个普通接口都返回 422，
# detail 是校验错误数组，页面拼出来就是「出了点问题：[object Object]」。
# 用户看到一句乱码，还以为自己那句话有问题。
def _session_owner(body_owner: str, request: Request) -> str:
    """按会话解析本人；body 里带了 owner_id 时必须和会话一致。"""
    owner = current_user(request)
    if body_owner and body_owner != owner:
        raise HTTPException(status_code=403, detail="这个会话不是该身份，不能替别家操作")
    return owner


class UtteranceBody(BaseModel):
    owner_id: str = ""
    utterance: str = Field(min_length=1, max_length=500)


class TaskBody(BaseModel):
    owner_id: str = ""
    utterance: str = Field(min_length=1, max_length=500)


class ConfirmBody(BaseModel):
    owner_id: str = ""
    task_id: str
    proposal_id: str
    accept_counter: bool = False


class CandidateBody(BaseModel):
    owner_id: str = ""
    task_id: str
    product_id: str


class OrderBody(BaseModel):
    owner_id: str = ""
    task_id: str
    order_id: str
    action: str


ATTRIBUTE_LABELS = {
    "likes": "喜好",
    "shoe_size": "鞋码",
    "foot_length": "脚长",
    "note": "备注",
    "已有物品": "已有物品",
}


def _attr_label(attribute):
    """把属性代码翻成中文，界面上不出现 likes 这类英文键名。"""
    if not attribute:
        return "记录"
    return ATTRIBUTE_LABELS.get(attribute, attribute)


def _record_reply(result):
    """把录入结果翻成人话，写进对话流。"""
    if result.get("kind") == "correction":
        parts = []
        for change in result.get("changes", []):
            # unchanged 表示这句话和已有记录一致，不必再向用户汇报一次。
            if change.get("unchanged"):
                continue
            if change.get("superseded_fact_id"):
                parts.append("%s的%s已改为「%s」" % (
                    change["subject"], _attr_label(change["attribute"]), change["value"]))
            else:
                parts.append("记下了：%s%s" % (
                    change["subject"], _attr_label(change["attribute"]) + change.get("value", "")))
        text = "已更正。" + "；".join(parts) if parts else "已记录。"
        if result.get("ambiguous_subjects"):
            text += "有几个人名我没对上，你能再说说是谁吗？"
        return text
    if result.get("kind") == "new_person":
        return "好，我记下这位新朋友了。"
    changes = result.get("changes", [])
    if changes:
        return "记下了：" + "；".join(
            "%s%s" % (c["subject"], _attr_label(c["attribute"]) + c.get("value", ""))
            for c in changes if not c.get("unchanged"))
    # 走到这里说明这条话里没有可记的内容：既没有主体也没有值。
    # 原来回一句「我记下了。」，用户以为存上了，实际什么都没落——
    # 那是拿假成功冒充结果，比不回复更糟。
    # 但修正版也不能写成训话：不复述他刚说的那句话，不数落「什么都没记」，
    # 直接说清要什么、给一个能照着写的例子就够。
    return ("记孩子的事，得告诉我是谁、做了什么。"
            "比如「小雨这周自己读完了两本绘本」。")


@app.get("/")
def index():
    # 首页每次改动都要立刻在手机上生效：不留给浏览器缓存的余地。
    # 否则用户在手机上看到的还是上一版页面，改过的地方像没生效。
    return _html_with_revision("index.html")


@app.get("/apps")
def apps_page():
    """两个轻量应用的入口：各自看自己记了什么、授权出去哪些字段。

    和主界面分开一页：这两个页面服务的是「我的记录被别处复用」这条主线，
    混在对话界面里会和送礼事项抢注意力。
    """
    return FileResponse(
        str(BASE_DIR / "static" / "apps.html"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        })


@app.get("/api/health")
def health():
    return {"ok": True, "model": service.model_status(client)}


class ModelConfigBody(BaseModel):
    # None 表示这一项不覆盖原值。空串在 base_url/model_name 上表示清空，
    # 在 api_key 上表示保持不变——清空密钥要走专门的 clear_key，
    # 否则一个没填全的表单就能把密钥抹掉。
    base_url: str | None = None
    model_name: str | None = None
    api_key: str | None = None


@app.get("/api/model-config")
def api_model_config_get():
    """读当前模型配置。密钥只返回「有没有」，不返回内容。

    密钥顺着接口回前端，就等于把它暴露在浏览器和网络里；界面只需要知道
    有没有配过，所以这里只给一个布尔值。
    """
    cfg = dbm.model_config_get(db)
    return {"base_url": cfg["base_url"], "model_name": cfg["model_name"],
            "has_api_key": bool(cfg["api_key"]),
            "effective_base_url": client.base_url, "effective_model": client.model,
            "env_base_url": os.environ.get("PA_MODEL_BASE_URL") or "",
            "env_model": os.environ.get("PA_MODEL_NAME") or ""}


@app.post("/api/model-config")
def api_model_config_set(body: ModelConfigBody):
    """保存模型配置并立即生效，然后探活。

    探活是必须的：地址或模型名写错时当场说「连不上」，不留着一个
    看起来配好了、一办事就 model_failed 的状态。
    """
    cfg = dbm.model_config_get(db)
    base_url = (body.base_url if body.base_url is not None
                else cfg["base_url"]).strip()
    model_name = (body.model_name if body.model_name is not None
                  else cfg["model_name"]).strip()
    api_key = (body.api_key if body.api_key is not None else cfg["api_key"])
    if not base_url or not model_name:
        raise HTTPException(status_code=422, detail="接口地址和模型名都要填")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422,
                            detail="接口地址要以 http:// 或 https:// 开头")
    dbm.model_config_set(db, base_url, model_name, api_key)
    _apply_model_config()
    status = service.model_status(client)
    return {"saved": True, "model": status,
            "base_url": client.base_url, "model_name": client.model,
            "has_api_key": bool(client.api_key)}


@app.post("/api/model-config/clear-key")
def api_model_config_clear_key():
    """清空密钥，回落到匿名请求。

    只清库不够：库里空了，运行中的 client 还留着上一个密钥，请求照旧带着
    旧的 Authorization 发出去——界面说「已清空，现在按匿名请求」，
    实际并没有匿名，用户被这句话骗了。所以这里必须显式把 client 的密钥
    也抹掉。_apply_model_config 不能代劳：它按「空即不覆盖」处理，
    分不清「从没配过」（该回落环境变量）和「刚清空」（该匿名）。
    """
    dbm.model_config_clear_key(db)
    client.api_key = ""
    return {"saved": True, "has_api_key": bool(client.api_key),
            "model": service.model_status(client)}


def _ui_revision():
    """界面版本号。只用文件内容算，不用启动时间或随机值。

    改了前端代码，这个号就变；没改就不变。开着不动的标签页靠它发现自己
    看到的是旧界面——服务重启、换机器、清缓存都不影响这个判断。
    """
    import hashlib
    h = hashlib.sha1()
    for name in ("index.html", "apps.html"):
        p = BASE_DIR / "static" / name
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(name.encode())
    return h.hexdigest()[:16]


def _html_with_revision(name: str):
    """把界面版本号写进 HTML 再返回，并禁止浏览器缓存。

    不缓存的响应头是必须的：服务端明明换了新界面，浏览器拿一份本地副本
    出来，用户看到的还是上一版，改过的地方像没生效。

    注入版本号也是必须的：页面要拿「我这份是什么版本」去和服务端比对，
    只靠缓存头的话，已经打开着的标签页永远不会知道版本变了——
    而用户报的「点击没反应」正是这类旧页面造成的。
    """
    path = BASE_DIR / "static" / name
    text = path.read_text(encoding="utf-8")
    tag = ('<script>window.__paUiRevision="%s";</script>' % _ui_revision())
    # 版本号要赶在任何脚本之前：index.html 里那段轮询脚本在 body 末尾，
    # 注入点放在 <body> 之后，读到的就一定是当前版本。
    marker = "<body>"
    if marker in text:
        text = text.replace(marker, marker + tag, 1)
    else:
        text = tag + text
    return HTMLResponse(text, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/api/ui-revision")
def api_ui_revision():
    """界面版本。前端轮询它，发现变了才提示更新。

    不返回内容，只返回版本号：这个接口被轮询，返回体会进日志，
    没必要把整份 HTML 的摘要摊出来。
    """
    return {"revision": _ui_revision()}


def _advance_reply(result):
    """把协商结果翻成人话，写进对话流。状态必须与真实结果一致。"""
    stage = result.get("stage")
    if stage == "agreed":
        return "对方接受了这个方案，等你确认。"
    if stage == "counter_pending":
        return "对方提了个新方案：" + (result.get("reason") or "") + "你看看行不行。"
    if stage == "conflict":
        return "这次没谈成：" + (result.get("outcome") or "")
    if stage == "failed":
        return "没办成：" + (result.get("outcome") or "")
    if stage == "blocked":
        return result.get("outcome") or "需要你补充一句。"
    if stage == "model_failed":
        return "刚才没连上模型，可以再试一次。"
    return result.get("outcome") or "正在核实。"


@app.get("/api/conversation")
def api_conversation(request: Request):
    # 身份由服务端会话决定。之前这里读 ?owner_id=，把 B/C 传进去就能读到
    # 别人的对话流：那不是「切换身份」，是没校验请求来源。
    return _with_session(request, {"turns": service.conversation(db, current_user(request))})


@app.get("/api/matters")
def api_matters(request: Request):
    owner_id = current_user(request)
    return _with_session(request, {
        "matters": service.matters(db, owner_id),
        # 在等谁、等什么。未回应不是同意，也不显示成已约好，
        # 所以单独给一份，让用户看到当前真正卡在哪。
        "waiting": service.waiting_for(db, owner_id)})


@app.get("/api/person/{person_id}")
def api_person(person_id: str, request: Request):
    detail = service.person_detail(db, current_user(request), person_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="人物不存在")
    return _with_session(request, detail)


@app.get("/api/overview")
def api_overview(request: Request):
    """首页侧栏数据。只返回本人的，不把别家资料一起带出去。"""
    return _with_session(request, service.overview(db, current_user(request)))


@app.get("/api/persons")
def api_persons(request: Request, q: str = ""):
    """找人：按姓名、别名、关系或备注搜我记录的人物。

    空 q 时返回全部，带上每个人所在的分组。分组只是浏览方式，
    一个人可以同时出现在多个组里，不授予任何权限。
    """
    owner_id = current_user(request)
    return _with_session(request, {
        "persons": service.search_persons(db, owner_id, q),
        "groups": service.person_groups(db, owner_id),
        "query": q})


@app.get("/api/life-events")
def api_life_events(subject_id: str, request: Request):
    # 查看者只能是服务端会话的本人：之前 ?viewer_id=B 就能换成家长一侧，
    # 看到未授权的字段。
    return _with_session(request, {
        "events": service.life_events_for(db, subject_id, current_user(request))})


@app.get("/api/plans")
def api_plans(request: Request):
    return _with_session(request, {"plans": service.plans(db, current_user(request))})


@app.get("/api/plan/{plan_id}")
def api_plan(plan_id: str, request: Request):
    """事项详情。只给这一事项的参与方看。

    原来任何会话都能读到全部详情，包括发起人那一侧的家庭与孩子资料——
    「被拉进一件事」不等于「把整本人物档案授权给对方看」。
    非参与方一律 404（不暴露这件事是否存在），参与者按自己那一方过滤。
    """
    me = _session_owner("", request)
    detail = service.plan_detail(db, plan_id, viewer=me)
    if detail is None:
        raise HTTPException(status_code=404, detail="事项不存在")
    return _with_session(request, detail)


class PlanBody(BaseModel):
    # 规划事项里 A/B/C 是三个演示身份，但不是三个家庭：B（小周）和 C（小林）
    # 是配偶、同一个家庭（DEMO_PERSON_LINKS 明记同一个人）。三家示范是
    # 「两家三身份」；真要第三家，那一方目前没有记录，只能补问。
    owner_id: str = ""
    title: str = Field(min_length=1, max_length=200)
    utterance: str = ""
    participants: list[dict[str, Any]] = []


@app.post("/api/plan/create")
def api_plan_create(body: PlanBody, request: Request):
    """发起一个多人事项：一个事项、多个家庭各自给条件。

    每个参与方带自己的 owner_id、称呼和这一方授权给出的条件。
    不替任何一方补条件，也不把别家的条件安到这一方头上。

    两条硬规则：
    1. 先校验完整份请求，再写任何一个字。原来 create_plan 先落一条 plan，
      再循环插参与方，插到第三个发现身份不对就抛——库里已经躺着一条
      只有半个身体的草稿，用户刷新后看到一件点不开的事。现在校验全过才动手。
    2. 只有本人这一方可以带条件。别家的条件必须由别家从自己的会话补：
      发起人在自己页面上把三家的表单一次填完，不等于三家都表了态。
    """
    me = _session_owner(body.owner_id, request)
    # 同一标题的未完结事项会被复用：告诉前端这一件是不是已经存在的，
    # 前端就不再重复生成第一版方案，否则会在已有版本上又叠一版，
    # 让各家对旧版本的接受白白失效。
    reused = service.find_open_plan(db, me, body.title) is not None

    # ---- 先校验，后落库 ----
    checked = []
    if len(body.participants) > 6:
        raise HTTPException(status_code=400, detail="一次最多 6 个参与身份，不能截断请求")
    for p in body.participants:
        owner = str(p.get("owner_id") or "")
        label = str(p.get("label") or "")
        if owner not in ("A", "B", "C"):
            raise HTTPException(status_code=400,
                                detail="参与方身份只能是演示身份 A / B / C")
        if not label:
            raise HTTPException(status_code=400, detail="参与方要有一个称呼")
        conditions = p.get("conditions") or {}
        if not isinstance(conditions, dict):
            raise HTTPException(status_code=400, detail="条件必须是字段和值的对象")
        # 条件键走白名单，旧预算按值里措辞确认口径；含糊值拒绝：
        # 一家的出资上限和整场活动的总预算不是一个数，混在一个键里，
        # 方案排出来每个数都说不清是谁的。
        conditions, unknown, confirm = service.normalize_conditions(conditions)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail="条件只能填这些：" + "、".join(service.PLAN_CONDITION_KEYS)
                       + "；不认识的键：" + "、".join(unknown))
        if confirm:
            raise HTTPException(
                status_code=400,
                detail="「%s」这句预算说不清是「本人出资上限」还是「活动总预算」，"
                       "请说清是「我家最多出多少」还是「活动一共多少」"
                       % "、".join(confirm))
        if owner != me and conditions:
            raise HTTPException(
                status_code=403,
                detail="只能填你自己这一方的条件。%s 的条件要由 %s 从自己的会话补，"
                       "你在这里代填不等于他已经同意。" % (label, label))
        checked.append((owner, label, conditions, bool(p.get("required", True))))

    # ---- 校验全过，才开始写 ----
    plan_id = service.create_plan(db, me, body.title, body.utterance)
    for owner, label, conditions, required in checked:
        # add_plan_participant 按 (plan_id, owner_id) 查重：同一件重复创建
        # 不会多出参与方，也不会把已经约好的事翻成待确认。
        service.add_plan_participant(db, plan_id, owner, label, conditions, required)
    return {"plan_id": plan_id, "reused": reused,
            "status": service.plan_status(db, plan_id)}


class PlanProposeBody(BaseModel):
    owner_id: str = ""


@app.post("/api/plan/{plan_id}/propose")
def api_plan_propose(plan_id: str, body: PlanProposeBody, request: Request):
    """按各方授权条件提出第一版方案。

    只校验「我是 A」不够，还要看这个事项里有没有我这一方：否则拿别家的
    事项 id 来 propose，会话身份校验照样通过，别家的条件会被排成一版方案，
    别家却什么都没做过。replan 早就这么校验了，propose 漏了。
    """
    me = _session_owner(body.owner_id, request)
    mine = db.row(
        "SELECT participant_id FROM plan_participant WHERE plan_id=? AND owner_id=?",
        (plan_id, me))
    if mine is None:
        raise HTTPException(status_code=403,
                            detail="你在这个事项里没有代表的一方，不能给它排方案")
    try:
        return agents.plan_propose(db, client, plan_id, me)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class PlanReplanBody(BaseModel):
    owner_id: str = ""
    feedback: dict[str, Any] = {}


@app.post("/api/plan/{plan_id}/replan")
def api_plan_replan(plan_id: str, body: PlanReplanBody, request: Request):
    """有家庭改条件后，按各家当前条件重排一版。

    失败时不落新版本，事项保持「需要重排」，
    各家也不能对旧版本表态——真实失败状态要留着，不能假装排好了。

    重排要按会话校验本人：没有这一步，任何一个会话都能让别家的事项重排。
    """
    me = _session_owner(body.owner_id, request)
    # 只校验「我是 A」不够，还要看这个事项里有没有我这一方。
    # 否则拿别家的事项 id 来重排，会话身份校验照样通过，
    # 别家的条件会被重排，别家却什么都没做过。
    mine = db.row(
        "SELECT participant_id FROM plan_participant WHERE plan_id=? AND owner_id=?",
        (plan_id, me))
    if mine is None:
        raise HTTPException(status_code=403,
                            detail="你在这个事项里没有代表的一方，不能让它重排")
    try:
        return agents.plan_replan(db, client, plan_id, body.feedback or None)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class PlanDecideBody(BaseModel):
    owner_id: str = ""
    participant_id: str
    version: int
    decision: str
    reason: str = ""


@app.post("/api/plan/{plan_id}/decide")
def api_plan_decide(plan_id: str, body: PlanDecideBody, request: Request):
    """某一方对当前版本表态。只能本人表态，旧版本不接受。"""
    try:
        did = service.decide_plan(db, plan_id, body.participant_id, body.version,
                                  body.decision, body.reason,
                                  actor_id=_session_owner(body.owner_id, request))
        return {"plan_decision_id": did, "status": service.plan_status(db, plan_id)}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class PlanConditionBody(BaseModel):
    owner_id: str = ""
    conditions: dict[str, Any]
    reason: str = ""


@app.post("/api/plan/{plan_id}/condition")
def api_plan_condition(plan_id: str, body: PlanConditionBody, request: Request):
    """某一方本人更新自己的条件。改条件不等于接受当前方案。"""
    try:
        return service.update_plan_condition(db, plan_id,
                                             _session_owner(body.owner_id, request),
                                             body.conditions, body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class PlanReviewBody(BaseModel):
    owner_id: str = ""
    participant_id: str
    version: int


@app.post("/api/plan/{plan_id}/review")
def api_plan_review(plan_id: str, body: PlanReviewBody, request: Request):
    """模型代替某一个家庭审阅当前版本：接受、不接受或提修改。

    审阅结论只代表这一方，且只能由这一方自己的会话发起：
    没有这层校验，任何一个会话都能让别家的代表出面审阅别家的事项。
    """
    me = _session_owner(body.owner_id, request)
    part = db.row("SELECT owner_id FROM plan_participant WHERE participant_id=?"
                  " AND plan_id=?", (body.participant_id, plan_id))
    if part is None:
        raise HTTPException(status_code=404, detail="这个事项里没有这一方")
    if part["owner_id"] != me:
        raise HTTPException(status_code=403,
                            detail="只能让你自己这一方的代表审阅")
    try:
        return agents.plan_review(db, client, plan_id, body.participant_id,
                                  body.version)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 手机访问入口。
#
# 手机上要能打开这个页面，靠的是「电脑和手机连同一局域网 + 电脑的局域网地址」。
# 服务默认监听 0.0.0.0（见 start_server.py），所以这一步只是把地址和二维码
# 摊给用户看，不额外开服务、不要求域名或 HTTPS。
# 没有 HTTPS 时 PWA 不能安装到主屏，普通网页和拍照记录仍然可用——
# 这一点在接口里如实说明，不承诺「可以添加到主屏」。
def _lan_addresses(port):
    """本机可被局域网访问的地址列表。取不到就返回空，由调用方说明。

    顺序很重要：真实 Wi-Fi/以太网在前，虚拟网卡在后。

    原先用 `socket.connect(192.0.2.1)` 取「出口网卡」地址，本意是保底，
    但它取到的是**默认路由**对应的接口。开着 VPN 时默认路由指向 utun*，
    于是给出的是 VPN 隧道接口的地址——手机和电脑并不在同一个
    物理网络上，扫码、手打都打不开，而用户看到的是一个格式完全正常的地址，
    只会以为是自己输错了。真实 Wi-Fi 的地址反而被排在后面。

    现在按 ifconfig 列出的真实物理接口取地址，并按「在用的」优先：
    1. 状态为 active 的 en*（Wi-Fi / 以太网）；
    2. 其余 UP 的 en*、bridge*；
    3. 最后才用默认路由兜底，并明确标注它可能来自 VPN。
    utun*、lo*、awdl*、gif*/stf* 等虚拟/点对点接口一律排除——手机不在那些
    网络上。拿不到可信地址时不猜，交由来调用方说明。
    """
    import socket
    out = []
    seen = set()

    def add(ip):
        if not ip or ip.startswith("127.") or ":" in ip:
            return
        url = "http://%s:%s" % (ip, port)
        if url not in seen:
            seen.add(url)
            out.append(url)

    def phys_ips(prefer_active):
        """从 ifconfig 读物理接口地址。prefer_active 时只取状态 active 的。"""
        import re
        import subprocess
        try:
            txt = subprocess.run(["ifconfig"], capture_output=True, text=True,
                                 timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        found = []
        name = ""
        active = False
        for line in txt.splitlines():
            head = re.match(r"^([A-Za-z0-9]+): flags=\S+<([^>]*)>", line)
            if head:
                name = head.group(1)
                active = "RUNNING" in head.group(2).split(",")
                continue
            m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", line)
            if not m:
                continue
            # 只要真实物理接口：en* 是以太网/Wi-Fi，bridge* 是桥接。
            # utun*/gif*/stf* 是 VPN/隧道，awdl* 是 AirDrop，lo* 是回环。
            if not (name.startswith("en") or name.startswith("bridge")):
                continue
            if prefer_active and not active:
                continue
            found.append(m.group(1))
        return found

    for ip in phys_ips(prefer_active=True):
        add(ip)
    for ip in phys_ips(prefer_active=False):
        add(ip)
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP):
            addr = info[4][0]
            if addr.startswith("127.") or ":" in addr:
                continue
            # 主机名可能解析到 VPN 隧道地址，只在前面没拿到真实地址时用。
            if not out:
                add(addr)
    except OSError:
        pass
    # 最后兜底：默认路由对应的接口。VPN 开着时这就是隧道地址。
    # 已经拿到真实 Wi-Fi/以太网地址时不再把它加进来——多列一个打不开的
    # 地址，用户会去试它，然后以为是自己手机或网络的问题。
    # 只有真实地址一个都没有时，才把它作为「疑似地址」给出并标注。
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.3)
        sock.connect(("192.0.2.1", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if not out and ip and not ip.startswith("127.") and ":" not in ip:
            add(ip)
    except OSError:
        pass
    return out


def _qr_svg(text):
    """把地址渲染成 SVG 二维码。用 SVG 而不是 PNG：不引入图片库作为运行依赖。

    缺 qrcode 库时返回空串并让调用方说明，不要让整个接口失败：
    二维码只是省一次输入，没有它用户照样能手打地址打开页面。
    """
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=10, border=2)
        return img.to_string().decode("utf-8")
    except ImportError:
        return ""


@app.get("/api/phone-access")
def api_phone_access(request: Request):
    """手机怎么打开这个页面：局域网地址、二维码，以及哪些能用哪些不能。

    PWA 安装到主屏需要 HTTPS（或 localhost）。局域网 http 地址下，
    manifest 和 service worker 的行为取决于浏览器，这里不承诺能安装，
    只承诺普通网页和拍照记录可用。
    """
    port = os.environ.get("PORT") or "8000"
    urls = _lan_addresses(port)
    payload = {
        "port": port,
        "local_url": "http://127.0.0.1:%s" % port,
        "phone_urls": urls,
        "https": False,
        "pwa_install_note": ("当前是局域网 http 地址，浏览器可能不允许把页面安装到主屏；"
                            "普通网页访问和拍照记录都不受影响。"),
        # 排在第一位的是真实 Wi-Fi/以太网地址，二维码只对它生成。
        # 不拿列表后面的地址生成二维码：那可能是 VPN 隧道地址，
        # 扫出来打不开，用户会以为自己手机有问题。
        "qr_url": urls[0] if urls else "",
        "steps": [
            "手机和电脑连同一个 Wi-Fi",
            "用手机相机扫下面的二维码，或在浏览器里输入上面的地址",
            "打开后就能看事项、和助理对话；侧栏里「拍一张记录」会尝试调起系统相机，"
            "也可以从相册选图片",
        ],
    }
    if not urls:
        payload["lan_note"] = ("没取到局域网地址。可设 PA_LAN_IP 手动指定电脑的"
                               "Wi-Fi 地址后重启；手机与电脑需连同一局域网。")
    elif len(urls) > 1:
        payload["lan_note"] = ("检测到多个地址，第一个是当前在用的 Wi-Fi/以太网；"
                               "若打不开，请改用与手机同一局域网的那个地址。")
    if urls:
        payload["qr_svg"] = _qr_svg(urls[0])
        if not payload["qr_svg"]:
            payload["qr_note"] = ("二维码库没装（pip install qrcode），"
                                 "请直接在手机浏览器里输入上面的地址。")
    return _with_session(request, payload)


# 拍照记录：手机浏览器直接调起相机，不装原生 App。
# 图片走 base64 内联进来，本应用不写文件、不存库；但识别要把图送到所配置的
# 模型服务，这一点在界面上如实说明，不写成「不外传」。上限 6MB：手机原图
# 再大就不像「随手拍一张记录」，多半是误选了别的文件。
MAX_PHOTO_B64 = 6 * 1024 * 1024


class PhotoReadBody(BaseModel):
    image_b64: str = Field(min_length=1, max_length=MAX_PHOTO_B64 * 2)
    media_type: str = Field(default="image/jpeg", pattern="^image/(jpeg|png|webp|heic)$")


class PhotoSaveBody(BaseModel):
    subject_id: str
    # 图片可选：记事本式录入里，一条纯文字记录同样要能存。
    # 原来 min_length=1 是硬性要求，等于把「只打字」这条路堵死，
    # 用户必须先拍一张图才能记一件事。
    image_b64: str = Field(default="", max_length=MAX_PHOTO_B64 * 2)
    media_type: str = Field(default="image/jpeg", pattern="^image/(jpeg|png|webp|heic)$")
    captured_by: str = ""
    # 这条记录是怎么来的。记事本式录入下同一份草稿可能同时有文字、图、语音，
    # 来源写清实际用了哪些，不能一律写成「拍照记录」——
    # 用户只打了一行字却被标成拍照，回看时会以为图丢了。
    input_kinds: list[str] = []
    # 语音转写。没传就是没有语音输入；是否真能转写由 /api/voice/transcribe
    # 如实返回，界面上不把不能用的链路写成已支持。
    voice_text: str = ""
    confirmed_fields: dict[str, Any] | None = None
    read: dict[str, Any] | None = None
    grants: list[dict[str, Any]] = []
    # 本人核对后可能改过的正文。空串表示正文没有改动。
    draft_text: str = ""
    # 本人核对后确认的发生日期。空串表示读不清、保持未知。
    happened_on: str = ""
    # 本人明确选择保留为配图的图片。没点「保留」就是空数组，
    # 服务端据此判断这条记录要不要落附件——识别用图不等于愿意长期保存。
    attachments: list[dict[str, Any]] = []


@app.post("/api/photo/read")
def api_photo_read(body: PhotoReadBody):
    """只识别，不写库。识别结果先给本人核对。

    这一步和落库分开，是为了不让「拍一下」直接变成一条事实：
    模型读到的内容本人得先看一眼，确认过的字段才会被记进去。
    """
    try:
        read = service.read_photo_record(db, client, body.image_b64, body.media_type)
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "ModelUnavailable":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise
    return read


@app.post("/api/photo/save")
def api_photo_save(body: PhotoSaveBody, request: Request):
    """本人核对后落库。拍照者、记忆所有者、记录主体分开保存。"""
    try:
        return service.save_photo_record(
            db, client, current_user(request), body.subject_id, body.image_b64,
            body.media_type, body.captured_by, body.grants, body.confirmed_fields,
            read=body.read, attachments=body.attachments,
            draft_text=body.draft_text, happened_on=body.happened_on,
            input_kinds=body.input_kinds, voice_text=body.voice_text)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "ModelUnavailable":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise


# 语音转写。这一条链先声明清楚再实现：
# 本机当前没有可用的中文语音转写能力。实测记录（2026-10-15）：
# - 本地代理的模型列表里有 step/stepaudio-2.5-asr，实际调用返回
#   「does not exist or you do not have access to it」（HTTP 404）；
# - /v1/audio/transcriptions 端点不存在（Unknown endpoint）；
# - 换 step/stepaudio-2.5-chat 送真实中文语音，模型不转写，只回了
#   「没问题，你发过来吧」——它把音频当成对话在接话，不是转写。
# 所以这里不假装支持：available=false 时界面只说「现在还不能转语音」，
# 给出可用的替代（直接打字、或用输入法自带的语音转文字），
# 绝不把一段静态录音说成已转写、也不编一段文字冒充转写结果。
# 等真的接入 ASR，只要把 available 判据换成真实探测即可，界面不用改。
MAX_AUDIO_B64 = 8 * 1024 * 1024


class VoiceTranscribeBody(BaseModel):
    audio_b64: str = Field(min_length=1, max_length=MAX_AUDIO_B64 * 2)
    media_type: str = Field(default="audio/webm", pattern="^audio/(webm|mp4|mpeg|wav|ogg)$")


@app.post("/api/voice/transcribe")
def api_voice_transcribe(body: VoiceTranscribeBody):
    """语音转文字。走 Step 的 audio chat 模型，直连，不经本地代理。

    本地代理 opencodex 只放行 image_url 一种 content part，音频在入站解析时就被丢掉，
    所以这里直连 Step。未配 PA_ASR_API_KEY 或调用失败时，如实返回不可用与原因，
    不把空结果冒充成功，也不编一段文字当转写。
    """
    ok, text, reason = model_client.transcribe_audio(body.audio_b64, body.media_type)
    if not ok:
        return {"available": False, "text": "", "reason": reason,
                "hint": "可以先直接打字，或用手机输入法自带的语音转文字，"
                        "转好后粘贴进来。转写服务可用时这里会自动变成可转写。"}
    return {"available": True, "text": text, "reason": "", "hint": ""}


@app.get("/api/event/{event_id}/attachments")
def api_event_attachments(event_id: str, request: Request):
    """读一条记录的配图。按本人身份取，不是拿到 event_id 就能看图。"""
    owner_id = current_user(request)
    rows = service.event_attachments(db, event_id, owner_id)
    if not rows:
        raise HTTPException(status_code=404, detail="没有可看的配图")
    return {"event_id": event_id,
            "attachments": [{"attachment_id": r["attachment_id"],
                             "media": r["media"], "note": r["note"],
                             "created_at": r["created_at"],
                             "b64": r["b64"]} for r in rows]}


@app.get("/api/event/{event_id}/attachment/{attachment_id}")
def api_event_attachment(event_id: str, attachment_id: str, request: Request):
    """按 ID 取单张配图，直接返回图片本体，便于 <img src> 使用。"""
    owner_id = current_user(request)
    rows = service.event_attachments(db, event_id, owner_id)
    hit = next((r for r in rows if r["attachment_id"] == attachment_id), None)
    if hit is None:
        raise HTTPException(status_code=404, detail="没有这张配图")
    return Response(content=base64.b64decode(hit["b64"]),
                    media_type=hit["media"])


class LifeEventBody(BaseModel):
    source_id: str
    subject_id: str
    event_type: str = "note"
    payload: dict[str, Any]
    happened_on: str = ""
    provider: str = ""
    owner_id: str = Field(default="B", pattern="^[AB]$")
    source_kind: str = "app"
    grants: list[dict[str, Any]] = []


@app.post("/api/life-events")
def api_import_life_event(body: LifeEventBody, request: Request):
    try:
        event_id, created = service.import_life_event(
            db, body.source_id, body.subject_id, body.event_type, body.payload,
            body.happened_on or dbm.now_iso()[:10], body.provider, current_user(request),
            body.grants, body.source_kind)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"event_id": event_id, "created": created}


class EventActionBody(BaseModel):
    owner_id: str = Field(default="B", pattern="^[AB]$")


@app.post("/api/life-events/{event_id}/revoke")
def api_revoke_life_event(event_id: str, body: EventActionBody, request: Request):
    try:
        service.revoke_life_event(db, event_id, current_user(request))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"revoked": True}


class CorrectEventBody(BaseModel):
    payload: dict[str, Any]
    owner_id: str = Field(default="B", pattern="^[AB]$")


@app.post("/api/life-events/{event_id}/correct")
def api_correct_life_event(event_id: str, body: CorrectEventBody, request: Request):
    try:
        new_id = service.correct_life_event(db, event_id, body.payload,
                                            current_user(request))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"new_event_id": new_id}


# ==================== 送礼对象档案（t49） ====================
# 复用 life_event/person，不建平行表；关系分类存 person.attributes_json.gift_target。
# 身份一律来自服务端会话；body 带 owner_id 时按会话校验，没带就用会话本人。
class GiftTargetBody(BaseModel):
    owner_id: str = ""
    name: str = ""
    relationship: str = ""
    person_id: str = ""
    note: str = ""


class GiftEntryBody(BaseModel):
    owner_id: str = ""
    category: str
    value: str = Field(min_length=1, max_length=300)
    happened_on: str = ""
    provider: str = "我本人"
    source_utterance: str = ""


class GiftCorrectBody(BaseModel):
    owner_id: str = ""
    value: str = Field(min_length=1, max_length=300)


class GiftRelationshipBody(BaseModel):
    owner_id: str = ""
    relationship: str


class GiftGrantBody(BaseModel):
    owner_id: str = ""
    viewer_id: str
    fields: list[str] = []


class SocialClassBody(BaseModel):
    # 通用人际关系分类（t53 壳体）：可多选，空列表表示不分类。
    # 取值在 service.SOCIAL_CLASSES 内校验，未知值 422 且不改原值。
    owner_id: str = ""
    classes: list[str] = []


@app.get("/api/gift-targets")
def api_gift_targets(request: Request):
    # 纯数据路由：按关系分组列出送礼对象。不调模型。
    owner_id = current_user(request)
    return service.list_gift_targets(db, owner_id)


@app.post("/api/gift-targets")
def api_create_gift_target(body: GiftTargetBody, request: Request):
    owner_id = _session_owner(body.owner_id, request)
    try:
        return service.create_gift_target(db, owner_id, name=body.name,
                                          relationship=body.relationship,
                                          person_id=body.person_id, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/gift-targets/{person_id}/relationship")
def api_set_gift_relationship(person_id: str, body: GiftRelationshipBody, request: Request):
    owner_id = _session_owner(body.owner_id, request)
    try:
        block = service.set_gift_relationship(db, owner_id, person_id, body.relationship)
        return {"person_id": person_id, "gift_target": block}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/gift-targets/{person_id}/entries")
def api_add_gift_entry(person_id: str, body: GiftEntryBody, request: Request):
    owner_id = _session_owner(body.owner_id, request)
    try:
        return service.add_gift_profile_entry(
            db, owner_id, person_id, body.category, body.value,
            happened_on=body.happened_on, provider=body.provider,
            source_utterance=body.source_utterance)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/gift-targets/{person_id}")
def api_gift_target_detail(person_id: str, request: Request):
    owner_id = current_user(request)
    target = db.row("SELECT person_id, name FROM person WHERE person_id=? AND owner_id=?",
                    (person_id, owner_id))
    if target is None:
        raise HTTPException(status_code=404, detail="送礼对象不在你的记录里")
    return {"person_id": person_id, "name": target["name"],
            "entries": service.gift_target_entries(db, owner_id, person_id),
            "history": service.gift_target_entries(db, owner_id, person_id, include_history=True),
            "context": service.gift_target_context(db, owner_id, person_id, viewer_id=owner_id)}


@app.post("/api/gift-targets/entries/{event_id}/correct")
def api_correct_gift_entry(event_id: str, body: GiftCorrectBody, request: Request):
    owner_id = _session_owner(body.owner_id, request)
    try:
        return service.correct_gift_profile_entry(db, owner_id, event_id, body.value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/gift-targets/entries/{event_id}/revoke")
def api_revoke_gift_entry(event_id: str, request: Request):
    # 撤回只需 event_id + 会话身份，不需要 body。
    owner_id = current_user(request)
    try:
        service.revoke_gift_profile_entry(db, owner_id, event_id)
        return {"revoked": True, "event_id": event_id}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/gift-targets/entries/{event_id}/grant")
def api_grant_gift_entry(event_id: str, body: GiftGrantBody, request: Request):
    owner_id = _session_owner(body.owner_id, request)
    try:
        service.grant_gift_profile_field(db, owner_id, event_id, body.viewer_id, body.fields)
        return {"granted": True, "event_id": event_id, "viewer_id": body.viewer_id}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/social-classes")
def api_social_classes(request: Request):
    # 通用人际关系分类壳体（t53）：可选值、建设中说明、每人当前分类。
    # 纯数据路由，不调模型；分类不驱动任何能力，界面据此标注「建设中」。
    owner_id = current_user(request)
    return service.list_person_social_classes(db, owner_id)


@app.post("/api/persons/{person_id}/social-class")
def api_set_social_class(person_id: str, body: SocialClassBody, request: Request):
    owner_id = _session_owner(body.owner_id, request)
    try:
        block = service.set_person_social_classes(db, owner_id, person_id, body.classes)
        return {"person_id": person_id, "classes": block.get("classes", [])}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/record")
def api_record(body: UtteranceBody, request: Request):
    owner_id = _session_owner(body.owner_id, request)
    try:
        service.save_turn(db, owner_id, "user", body.utterance)
        result = service.record_utterance(db, client, owner_id, body.utterance)
        service.save_turn(db, owner_id, "system",
                          _record_reply(result), task_id=result.get("task_id", ""))
        return result
    except Exception as exc:  # noqa: BLE001 - 需要把模型失败如实返回前端
        if type(exc).__name__ == "ModelUnavailable":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise


@app.post("/api/task")
def api_task(body: TaskBody, request: Request):
    owner_id = _session_owner(body.owner_id, request)
    try:
        service.save_turn(db, owner_id, "user", body.utterance)
        return service.create_task(db, client, owner_id, body.utterance)
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "ModelUnavailable":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise


@app.post("/api/say")
def api_say(body: UtteranceBody, request: Request):
    """聊天入口：先判断这句话是交办、记录纠正还是问进展，再走对应流程。"""
    owner_id = _session_owner(body.owner_id, request)
    try:
        service.save_turn(db, owner_id, "user", body.utterance)
        intent = service.classify_intent(db, client, owner_id, body.utterance)
        # 用户只报了一个名字，而恰好有件事卡在「要送给谁」：
        # 这是在同一件事上补信息，要接回那件事，不能另开一件、也不能只回答一句追问。
        # 放在意图判断之后、clarify 回复之前：名字对得上就继续办，对不上才说没这个人。
        # 已经登记过订单时，用户说「孩子很喜欢」「送到了」是在补事后反馈，
        # 要接到那条送礼记录上，不能当成新的交办或纠正。
        # 但只在意图就是「补充反馈」时才走：带新目标的句子（例如
        # 「小周说孩子很喜欢画画，给她买一套颜料，100元以内」）必须按 task 办，
        # 不能被关键词抢先记成上一单的反馈。
        if intent == "followup" \
                and not service.is_meta_ask(body.utterance) \
                and service._recent_purchased(db, owner_id) is not None:
            followed = service.record_followup(db, owner_id, body.utterance)
            if followed is not None:
                service.save_turn(db, owner_id, "system", followed["reply"],
                                  task_id=followed.get("task_id", ""))
                return {"intent": "followup", "kind": "followup",
                        "reply": followed["reply"], "task_id": followed.get("task_id", ""),
                        "order_id": followed.get("order_id", "")}

        pending_now = service._pending_clarify(db, owner_id)
        # 卡在尺码上时，家长补的「她现在穿 32 码」要接回同一件事。
        # 元提问要先放过：「补充什么？」是在问缺什么，不是在报尺码。
        if pending_now is not None and not service.is_meta_ask(body.utterance) \
                and ("鞋码" in (pending_now.get("outcome") or "")
                     or "尺码" in (pending_now.get("outcome") or "")):
            answered = service.answer_blocked_with_size(
                db, client, owner_id, body.utterance)
            if answered is not None:
                service.save_turn(db, owner_id, "system", answered["reply"],
                                  task_id=answered.get("task_id", ""))
                return {"intent": "clarify", "kind": "clarify",
                        "reply": answered["reply"],
                        "task_id": answered.get("task_id", ""),
                        "candidates": answered.get("candidates", []),
                        "resolved": bool(answered.get("resolved"))}
        if service._looks_like_name_only(body.utterance) \
                and service._pending_clarify(db, owner_id) is not None:
            answered = service.answer_blocked_with_name(
                db, client, owner_id, body.utterance)
            if answered is not None:
                service.save_turn(db, owner_id, "system", answered["reply"],
                                  task_id=answered.get("task_id", ""))
                return {"intent": "clarify", "kind": "clarify",
                        "reply": answered["reply"],
                        "task_id": answered.get("task_id", ""),
                        "candidates": answered.get("candidates", []),
                        "resolved": bool(answered.get("resolved"))}
        if intent == "record":
            result = service.record_utterance(db, client, owner_id, body.utterance)
            reply = _record_reply(result)
            service.save_turn(db, owner_id, "system", reply)
            return {"intent": intent, "kind": "record", "reply": reply, "result": result}
        if intent == "status":
            reply = service.status_summary(db, owner_id)
            service.save_turn(db, owner_id, "system", reply)
            return {"intent": intent, "kind": "status", "reply": reply}
        # 问家人近况：按授权范围汇总，不让用户重填人物关系和背景。
        # 和 status 分开：那个答「我的事办到哪一步」，这个答「家人这边有什么事」。
        if intent == "family_update":
            info = service.family_update_summary(db, owner_id, body.utterance)
            service.save_turn(db, owner_id, "system", info["reply"])
            return {"intent": intent, "kind": "family_update",
                    "reply": info["reply"], "person_id": info.get("person_id", ""),
                    "events": info.get("events", []),
                    "resolved": bool(info.get("resolved"))}
        if intent == "clarify":
            # 用户在追问「要补什么」。回答必须落到具体缺口和可点的人名上，
            # 并且不新建任务：同一句重复交办已经会在库里堆出重复事项。
            info = service.clarify_reply(db, owner_id)
            reply = info["reply"]
            service.save_turn(db, owner_id, "system", reply, task_id=info.get("task_id", ""))
            return {"intent": intent, "kind": "clarify", "reply": reply,
                    "task_id": info.get("task_id", ""), "candidates": info.get("candidates", [])}
        # 已经在办一件家庭活动时，后续这句话先试着接回同一件：
        # 「小周家和另外一家」「我家2个孩子」「小雨来小宇不来」都是补信息，
        # 不是新办一件事。接不上才按新活动或新交办走。
        if intent != "record":
            cont = service.continue_group_activity(db, owner_id, body.utterance)
            if cont is not None:
                service.save_turn(db, owner_id, "system", cont["reply"],
                                  plan_id=cont["plan_id"])
                return {"intent": "group_activity", "kind": "group_activity",
                        "reply": cont["reply"], "plan_id": cont["plan_id"],
                        "continued": True, "changes": cont["changes"]}
        if intent == "group_activity":
            # 多家庭活动：直接开家庭活动草稿，不进送礼链。
            # 用户原话「周末三家一起带孩子出去玩，我出100以内」以前会被当成
            # 送礼交办，接着被要求「点一个孩子」——那正是用户明确不要的。
            # 缺口（哪几家、什么时间）留在同一件事里追问，不新开一件。
            started = service.start_group_activity(db, client, owner_id, body.utterance)
            return {"intent": intent, "kind": "group_activity",
                    "reply": started["reply"], "plan_id": started["plan_id"],
                    "reused": started["reused"], "title": started["title"],
                    "own_cap": started["own_cap"],
                    "families": started["families"],
                    "missing_families": started["missing_families"]}
        if intent == "friend_trip":
            # 约朋友一起旅行：复用多方协调引擎，条件字段用朋友旅行那套。
            # 原先进不来这条——friend_trip_intent 定义了却没接进分派，
            # 主对话说「约朋友去大理」只会落进家庭活动或送礼。
            started = service.start_friend_trip(db, client, owner_id, body.utterance)
            return {"intent": intent, "kind": "friend_trip",
                    "reply": started["reply"], "plan_id": started["plan_id"],
                    "reused": started["reused"], "title": started["title"],
                    "own_cap": started["own_cap"],
                    "companions": started.get("companions", [])}
        task = service.create_task(db, client, owner_id, body.utterance)
        return {"intent": intent, "kind": "task", "task_id": task["task_id"],
                "extracted": task["extracted"], "reused": bool(task.get("reused"))}
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "ModelUnavailable":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise


@app.post("/api/task/advance")
def api_advance(body: dict[str, Any], request: Request):
    # 推进事项只能由本人的会话发起：这一步会让系统去和对方助理协商，
    # 不按会话校验的话，任何会话都能推动别家的事。
    task_id = str(body.get("task_id") or "")
    task = db.row("SELECT owner_id FROM task WHERE task_id=?", (task_id,))
    if task is not None:
        _session_owner(task["owner_id"], request)
    try:
        result = service.advance_task(db, client, task_id)
        if task is not None:
            service.save_turn(db, task["owner_id"], "system",
                              _advance_reply(result), task_id=task_id)
        return result
    except ValueError as exc:
        # 任务不存在、状态不对这类业务拒绝要说清原因。漏了这个分支，
        # ValueError 会一路抛成 500，用户看到的是「Internal Server Error」，
        # 既不知道哪错了，也不知道下一步能干什么。
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "ModelUnavailable":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise


class ResolveBody(BaseModel):
    owner_id: str = ""
    task_id: str
    person_id: str
    extra_utterance: str = ""


@app.post("/api/task/resolve-subject")
def api_resolve_subject(body: ResolveBody, request: Request):
    # 送礼对象只能由本人的会话指定：这里按会话校验身份，
    # 不允许拿别人的会话把对象安到别家的事项上。
    _session_owner(body.owner_id, request)
    try:
        return service.resolve_subject(db, client, body.task_id, body.person_id,
                                       body.extra_utterance)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "ModelUnavailable":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise


@app.get("/api/task/{task_id}")
def api_task_card(task_id: str, request: Request):
    card = service.task_card(db, task_id)
    if card is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _with_session(request, card)


class RecoverBody(BaseModel):
    owner_id: str = ""


@app.post("/api/task/{task_id}/to-group-activity")
def api_task_to_group_activity(task_id: str, body: RecoverBody, request: Request):
    """把一条被误当成送礼的事项，改走家庭活动。

    旧事项不删：原话和当时系统的错误回应都要留着，那才是历史。
    只在新活动与旧事项之间建立关联，并在旧事项卡片上给一个可点的入口。
    """
    me = _session_owner(body.owner_id, request)
    try:
        out = service.recover_group_activity(db, me, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if out is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return out


@app.get("/api/note/linkable-tasks")
def api_linkable_tasks(request: Request):
    """保存资料之后，可以选「用于哪件事」的本人未完事项。

    只列本人的、还没办完的。已买到的不列：资料是给接下来还要办的事用的。
    没有明确对象的事项也列出来并标明——它缺的可能正是对象。
    """
    return _with_session(request, {
        "tasks": service.linkable_tasks(db, current_user(request))})


class EventLinkBody(BaseModel):
    event_id: str
    task_id: str
    owner_id: str = ""
    note: str = ""


@app.post("/api/note/link-task")
def api_note_link_task(body: EventLinkBody, request: Request):
    """把一条资料明确用于某件事项。只有本人能建立这个关联。

    关联只是记录「这次办事我用了哪条资料」：不因此可以对外分享、
    不因此建立对他人的承诺、也不因此获得执行权。
    """
    me = _session_owner(body.owner_id, request)
    try:
        out = service.link_event_to_task(db, me, body.event_id, body.task_id,
                                         body.note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _with_session(request, out)


@app.post("/api/note/unlink-task")
def api_note_unlink_task(body: EventLinkBody, request: Request):
    """取消「用于这件事」。资料本身不动。"""
    me = _session_owner(body.owner_id, request)
    try:
        out = service.unlink_event_from_task(db, me, body.event_id, body.task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _with_session(request, out)


@app.get("/api/task/{task_id}/linked-notes")
def api_task_linked_notes(task_id: str, request: Request):
    """这件事用到过哪些资料。按查看者身份过滤，不是拿到 id 就能看。"""
    return _with_session(request, {
        "task_id": task_id,
        "notes": service.linked_events(db, task_id, current_user(request))})


@app.get("/api/product/{product_id}")
def api_product_detail(product_id: str, task_id: str = ""):
    """商品详情：摊开会影响决策的属性，以及这件在当前事项下过不过得了检查。"""
    detail = service.product_detail(db, product_id, task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    return detail


@app.post("/api/confirm")
def api_confirm(body: ConfirmBody, request: Request):
    try:
        order = service.confirm_and_purchase(db, client, body.task_id, body.proposal_id,
                                              _session_owner(body.owner_id, request),
                                              body.accept_counter)
        return order
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "ModelUnavailable":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise


@app.post("/api/reset")
def api_reset():
    return dbm.reset_demo(db)


@app.post("/api/task/choose-candidate")
def api_choose_candidate(body: CandidateBody, request: Request):
    """从候选商品里换一件。换商品要重走硬条件检查，不通过就原样不动。

    换商品等于改了方案，对方原来接受的是另一件。为了让用户不用自己判断
    「接下来该打字还是点哪个按钮」，换成功之后直接把新方案再推一轮：
    谈成了就在对话流里说清要点「确认购买」，谈不成就如实说卡在哪。
    用户的操作从「读状态→猜下一步→自己点」减成一步。
    """
    me = _session_owner(body.owner_id, request)
    # 换商品会改方案、让整件事退回重新协商，所以先核对这件事是不是本人的。
    # 少了这一步，拿别家的事项 id 也能换：别家的方案被改、对方被要求重新确认，
    # 而别家本人什么都没做过。
    task = db.row("SELECT owner_id FROM task WHERE task_id=?", (body.task_id,))
    if task is None:
        raise HTTPException(status_code=404, detail="事项不存在")
    if task["owner_id"] != me:
        raise HTTPException(status_code=403,
                            detail="这不是你的事项，不能给它换商品")
    try:
        swapped = service.choose_candidate(db, body.task_id, body.product_id,
                                          me)
        result = service.advance_task(db, client, body.task_id)
        if task is not None:
            service.save_turn(db, task["owner_id"], "system",
                              _choose_reply(swapped, result), task_id=body.task_id)
        swapped["stage"] = result.get("stage")
        swapped["next_hint"] = _choose_reply(swapped, result)
        return swapped
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "ModelUnavailable":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise




def _choose_reply(swapped, result):
    """换商品之后，把「现在该点什么」直接写成人话。

    这句话要说清三件事：换了什么、对方原来的接受已经不算数、下一步点什么。
    少了第三件，用户读完还是不知道该怎么办。
    """
    name = swapped.get("product_name") or "这件"
    stage = result.get("stage")
    if stage == "agreed":
        return "已换成%s，对方也确认了这一件。要点「确认购买（模拟）」下单。" % name
    if stage == "counter_pending":
        return "已换成%s，对方提了个新方案：" % name + (result.get("reason") or "")
    if stage == "blocked":
        return "已换成%s，但还缺一点：" % name + (result.get("outcome") or "对方想再确认")
    if stage == "model_failed":
        return "已换成%s，但刚才没连上模型，可以再试一次。" % name
    return "已换成%s，这次没谈成：" % name + (result.get("outcome") or "")


@app.post("/api/order/advance")
def api_order_advance(body: OrderBody, request: Request):
    """推进订单：支付、发货、送达。按顺序走，全程本地模拟。"""
    if body.action not in ("pay", "ship", "deliver"):
        raise HTTPException(status_code=400, detail="不支持的订单操作")
    try:
        return service.advance_order(db, body.task_id, body.order_id, body.action,
                                     _session_owner(body.owner_id, request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/audit")
def api_audit(request: Request):
    """本人相关的审计。只能看到自己的记录，不是全库。

    之前这里没有按本人过滤，普通入口一读就是三个演示家庭的全部审计，
    包括别家的事项和授权记录。
    """
    owner_id = current_user(request)
    # 只按 owner_id 过滤。之前用 OR actor=? 兜底，会把「归属明确是 B、
    # 但动作由 A 触发」的行也返回给 A：那不是 A 的记录，A 不该看到。
    # actor 兜底只用于 owner_id 为空的存量行（加列之前写入的），
    # 新写入一律带 owner_id。
    rows = db.rows(
        "SELECT * FROM audit_log WHERE owner_id=?"
        " OR (owner_id='' AND actor=?)"
        " ORDER BY created_at DESC LIMIT 200", (owner_id, owner_id))
    return _with_session(request, rows)


@app.get("/api/dev/audit")
def api_dev_audit():
    """开发区用的全库审计。默认关闭，避免普通路径读到别家记录。"""
    if os.environ.get("PA_DEV_VIEWS") != "1":
        raise HTTPException(status_code=404, detail="这个入口未启用")
    return db.rows("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 500")


# ---------------------------------------------------------------------------
# 浏览器插件接口。
#
# 这里没有任何「传 owner_id 指定用户」的入口：本人会话用会话决定用户，
# 插件用 Authorization: Bearer 的设备凭证决定用户。
# 身份/授权拒绝 401/403，旧版本或目标不符 409，缺参数 422，服务故障 503。
# 错误统一为 {error:{code,message,retryable}}，不把失败改成成功。
# ---------------------------------------------------------------------------

EXT_ALLOWED_ORIGINS = {
    # 插件后台地址在扩展里固定；这里只放行已知的扩展来源，
    # 不开放任意来源通配。开发期本地插件走 chrome-extension:// 时按需追加。
    "http://127.0.0.1:8000",
    "http://localhost:8000",
}


@app.exception_handler(service.ExtError)
async def ext_error_handler(request: Request, exc: service.ExtError):
    """把插件接口错误按冻结契约输出：顶层 error，不包在 detail 里。

    HTTPException 的 detail 会被 FastAPI 包成 {"detail": {...}}，
    插件读 data.error 会读空，具体中文失败原因就丢了。
    """
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message,
                           "retryable": exc.retryable}})


def _ext_error(exc: service.ExtError) -> ExtError:
    """占位：改由异常处理器统一处理，这里直接抛出。"""
    return exc


class PairCodeBody(BaseModel):
    client_name: str = ""


class PairBody(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    client_name: str = ""


class SuggestBody(BaseModel):
    subject_id: str = Field(min_length=1, max_length=64)


class RevokeBody(BaseModel):
    client_id: str = Field(min_length=1, max_length=64)


class ExtPrepareBody(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
    action_type: str = Field(default="shopping_fill", max_length=32)
    target_origin: str = Field(min_length=1, max_length=300)


class ExtContextBody(BaseModel):
    """跨页面需求卡请求。

    purpose 不给默认值之外的自由度：本期只有购物协助一种用途，
    由插件固定传 shopping_help，不由网页指定——网页能指定用途，
    就等于网页能决定拿哪些字段出去。
    """
    task_id: str = Field(min_length=1, max_length=64)
    purpose: str = Field(default="shopping_help", max_length=32)


class ExtValidateBody(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=64)
    target_origin: str = Field(min_length=1, max_length=300)
    observed_product_id: str = ""
    user_confirmed: bool = False


class ExtReceiptBody(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=64)
    target_origin: str = Field(min_length=1, max_length=300)
    observed_fields: dict[str, Any] = {}
    client_status: str = Field(pattern="^(filled|partial|failed)$")
    idempotency_key: str = Field(min_length=1, max_length=120)


def _ext_bearer(authorization: str = Header(default="")) -> dict:
    """从 Bearer 凭证解析设备。失败时抛 ExtError，由异常处理器转成顶层 error。"""
    return service.device_from_request(db, authorization)


def _ext_payload(device: dict, payload: Any) -> Any:
    """插件接口的统一出口：需要时把后台换发的新凭证带回去。

    续期发生在服务端，插件只管读这个头并替换本地存的那一个。
    没有续期时返回原始 payload，不额外包一层——插件按冻结契约读字段，
    多一层包装就全读空了。
    """
    renewed = device.get("renewed_token") or ""
    if not renewed:
        return payload
    return JSONResponse(payload, headers={
        "X-PA-Device-Token": renewed,
        "Cache-Control": "no-store",
    })


@app.get("/api/me")
def api_me(request: Request):
    """本人身份。应用每次打开都问服务端，前端不存身份、也没有身份下拉菜单。"""
    try:
        return _with_session(request, service.me(db, current_user(request)))
    except service.ExtError as exc:
        raise exc


@app.post("/api/extension/pair-codes")
def api_ext_pair_codes(body: PairCodeBody, request: Request):
    """本人会话生成一次性配对码。"""
    try:
        return _with_session(request, service.create_pair_code(
            db, current_user(request), body.client_name))
    except service.ExtError as exc:
        raise exc


@app.post("/api/extension/pair")
def api_ext_pair(body: PairBody, request: Request):
    """插件用配对码换设备凭证。一次兑换，重复兑换失败。

    兑换出的用户由配对码记录推导，插件不能额外指定是谁。
    """
    try:
        return _with_session(request, service.pair_device(
            db, current_user(request), body.code, body.client_name))
    except service.ExtError as exc:
        raise exc


@app.get("/api/extension/devices")
def api_ext_devices(request: Request):
    """本人的已配对设备。只给撤销要用的字段，不给凭证。"""
    try:
        return _with_session(request, service.list_devices(
            db, current_user(request)))
    except service.ExtError as exc:
        raise exc


@app.post("/api/extension/revoke")
def api_ext_revoke(body: RevokeBody, request: Request,
                   authorization: str = Header(default="")):
    """撤销设备。本人会话可撤销本人的设备；设备凭证只能撤销自己。"""
    device = None
    if authorization:
        try:
            device = service.device_from_request(db, authorization)
        except service.ExtError as exc:
            raise exc
    try:
        return _with_session(request, service.revoke_device(
            db, current_user(request), body.client_id, device))
    except service.ExtError as exc:
        raise exc


@app.get("/api/extension/tasks")
def api_ext_tasks(authorization: str = Header(default="")):
    """本人可办事项摘要。用户从凭证解析，请求体里不能带用户参数。"""
    device = _ext_bearer(authorization)
    try:
        return _ext_payload(device, service.extension_tasks(db, device))
    except service.ExtError as exc:
        raise exc


@app.post("/api/extension/prepare")
def api_ext_prepare(body: ExtPrepareBody, authorization: str = Header(default="")):
    """准备参数包：后端筛字段、冻结版本和来源，给用户预览。"""
    device = _ext_bearer(authorization)
    try:
        return _ext_payload(device, service.prepare_snapshot(
            db, device, body.task_id, body.action_type, body.target_origin))
    except service.ExtError as exc:
        raise exc


@app.post("/api/extension/context")
def api_ext_context(body: ExtContextBody, authorization: str = Header(default="")):
    """跨页面需求卡：换页面复用预算、偏好、排除项和待确认项。

    和 prepare 的分工见 service.extension_context 的注释。这里只做
    设备凭证解析和归属校验，不额外接受 owner_id——身份由凭证决定。
    """
    device = _ext_bearer(authorization)
    try:
        return _ext_payload(device, service.extension_context(
            db, device, body.task_id, body.purpose))
    except service.ExtError as exc:
        raise exc


@app.post("/api/extension/validate")
def api_ext_validate(body: ExtValidateBody, authorization: str = Header(default="")):
    """写页面之前的校验：实时核对版本、主体、授权和商品。"""
    device = _ext_bearer(authorization)
    try:
        return _ext_payload(device, service.validate_snapshot(
            db, device, body.snapshot_id, body.target_origin,
            body.observed_product_id, body.user_confirmed))
    except service.ExtError as exc:
        raise exc


@app.post("/api/extension/receipts")
def api_ext_receipts(body: ExtReceiptBody, authorization: str = Header(default="")):
    """保存代填回执。verified 由服务端复核实际读回值后给出。"""
    device = _ext_bearer(authorization)
    try:
        return _ext_payload(device, service.save_receipt(
            db, device, body.snapshot_id, body.target_origin,
            body.observed_fields, body.client_status, body.idempotency_key))
    except service.ExtError as exc:
        raise exc


@app.get("/api/task/{task_id}/receipts")
def api_task_receipts(task_id: str, request: Request):
    """应用侧读某个事项的代填回执。只能读本人的事项。"""
    try:
        return _with_session(request, service.task_receipts(
            db, current_user(request), task_id))
    except service.ExtError as exc:
        raise exc


# ---------------------------------------------------------------------------
# 演示身份隔离入口。
#
# 三家庭演示需要 A/B/C 三个身份看各自的条件和协商，这是验收用的虚构数据，
# 不是生产多用户认证，也不进入正常使用路径：普通接口全部从服务端会话取本人，
# 只有明确标注 /api/demo/* 的这些入口接受演示身份，且只读、只服务于演示。
# 把它们藏在普通接口后面会让「切换成别人」看起来像正常功能。
DEMO_READ_ROUTES = (
    ("/api/conversation", service.conversation),
    ("/api/matters", None),
)


@app.get("/api/demo/session")
def api_demo_session(request: Request):
    """当前会话本人，以及可选的演示身份列表。"""
    return _with_session(request, {
        "user_id": current_user(request),
        "demo_identities": [{"owner_id": u[0], "name": u[1]} for u in dbm.DEMO_USERS],
        "note": "A/B/C 是本地演示的两个家庭三个身份，不是多用户登录。"})


class DemoSwitchBody(BaseModel):
    owner_id: str = Field(pattern="^[ABC]$")


@app.post("/api/demo/switch")
def api_demo_switch(body: DemoSwitchBody, request: Request):
    """把本会话切到某个演示身份。只改本机演示会话，不产生任何授权。

    默认关闭：这是给验收准备的三家庭演示通路，不是普通功能入口。
    开着它，普通用户点开演示设置就能变成 B/C，那不是产品行为。
    """
    if os.environ.get("PA_DEV_VIEWS") != "1":
        raise HTTPException(status_code=404,
                            detail="演示身份切换未启用（设置 PA_DEV_VIEWS=1 后仅用于验收）")
    owner_id = demo_owner_or_403(body.owner_id)
    sid = request.cookies.get(SESSION_COOKIE) or ""
    if not sid or sid not in SESSIONS:
        sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = owner_id
    user = db.row("SELECT name FROM demo_user WHERE owner_id=?", (owner_id,))
    payload = {"user_id": owner_id, "display_name": (user or {}).get("name") or owner_id,
               "switched": True}
    return JSONResponse(payload, headers={
        "Set-Cookie": "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400"
                      % (SESSION_COOKIE, sid)})


@app.get("/api/demo/conversation")
def api_demo_conversation(owner_id: str = "A", request: Request = None):
    """演示用：读指定身份的对话流。只读，只用于三家庭示范。"""
    demo_owner_or_403(owner_id)
    return {"turns": service.conversation(db, owner_id),
            "_demo": True, "_note": "演示身份读取，不属于正常使用路径"}


@app.get("/api/demo/matters")
def api_demo_matters(owner_id: str = "A", request: Request = None):
    demo_owner_or_403(owner_id)
    return {"matters": service.matters(db, owner_id),
            "waiting": service.waiting_for(db, owner_id),
            "_demo": True, "_note": "演示身份读取，不属于正常使用路径"}


@app.get("/api/demo/plans")
def api_demo_plans(owner_id: str = "A", request: Request = None):
    demo_owner_or_403(owner_id)
    return {"plans": service.plans(db, owner_id), "_demo": True}


@app.get("/api/demo/person/{person_id}")
def api_demo_person(person_id: str, owner_id: str = "A", request: Request = None):
    demo_owner_or_403(owner_id)
    detail = service.person_detail(db, owner_id, person_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="人物不存在")
    return detail


@app.post("/api/suggestion")
def api_suggestion(body: SuggestBody, request: Request):
    """第二个小应用自己的产出：按授权近况给下一步建议，并列出依据。

    和送礼协商无关，也不重填人物背景：只用这个人名下已授权的近况。
    没有可用依据时不调模型，如实说没有；模型不可用时报 model_failed，
    不返回空话冒充成功。
    """
    try:
        return _with_session(request, service.app_suggestion(
            db, client, current_user(request), body.subject_id))
    except service.ExtError as exc:
        raise exc


@app.get("/api/source/{source_id}")
def api_source_view(source_id: str, request: Request):
    """小应用自己看自己的记录与授权范围。只能看本人的来源。

    归属在 service 的 SQL 里就限定了：这里不再读全量再筛，
    也不返回「共有几条别人的记录」——那是在透露不属于本人的信息。
    """
    return _with_session(request, service.app_source_view(
        db, source_id, current_user(request)))


@app.get("/api/demo/life-events")
def api_demo_life_events(subject_id: str, viewer_id: str = "A",
                         request: Request = None):
    demo_owner_or_403(viewer_id)
    return {"events": service.life_events_for(db, subject_id, viewer_id), "_demo": True}


@app.get("/api/demo/persons")
def api_demo_persons(owner_id: str = "A", q: str = "", request: Request = None):
    demo_owner_or_403(owner_id)
    return {"persons": service.search_persons(db, owner_id, q),
            "groups": service.person_groups(db, owner_id), "query": q, "_demo": True}


@app.get("/api/demo/plan/{plan_id}")
def api_demo_plan(plan_id: str, owner_id: str = "A", request: Request = None):
    demo_owner_or_403(owner_id)
    detail = service.plan_detail(db, plan_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="事项不存在")
    return detail


# Capability-limited local participants. These routes never call current_user.
MATTER_COOKIE = 'pa_matter'


@app.middleware('http')
async def isolate_matter_sessions(request: Request, call_next):
    # Presence, not validity: an expired/revoked guest plus an old A cookie must fail closed.
    if (MATTER_COOKIE in request.cookies or 'pa_matter_mode' in request.cookies) and request.url.path.startswith('/api/') and not request.url.path.startswith('/api/participation/'):
        return JSONResponse({'detail':'受邀事项会话不能读取个人资料或管理接口'},status_code=403)
    response = await call_next(request)
    if request.url.path.startswith('/api/participation/') or request.url.path.endswith('participate.html'):
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
    return response


def _same_origin(request):
    expected = str(request.base_url).rstrip('/')
    if request.headers.get('Origin') != expected:
        raise HTTPException(status_code=403,detail='本事项操作只接受当前页面同源请求')


def _guest(request, write=False):
    try:
        token=request.cookies.get(MATTER_COOKIE) or ''
        session=service.matter_session(db,token)
    except ValueError as exc:
        raise HTTPException(status_code=403,detail=str(exc)) from exc
    if write:
        _same_origin(request)
        if not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),session['csrf_token']):
            raise HTTPException(status_code=403,detail='事项会话校验失败，请刷新后重试')
    session['raw_token']=token
    return session


def _cap_error(exc):
    return HTTPException(status_code=409,detail=str(exc))


class InviteBody(BaseModel):
    model_config={'extra':'forbid'}
    label:str=Field(min_length=1,max_length=40)


class ApproveInviteBody(BaseModel):
    model_config={'extra':'forbid'}
    replace_participant_id:str=''


class AcceptInviteBody(BaseModel):
    model_config={'extra':'forbid'}
    token:str=Field(min_length=30,max_length=200)
    display_name:str=Field(min_length=1,max_length=40)


@app.post('/api/plan/{plan_id}/invitations')
def api_invite_create(plan_id:str,body:InviteBody,request:Request):
    _same_origin(request)
    try:
        return _with_session(request,service.invitation_create(db,current_user(request),plan_id,body.label))
    except ValueError as exc:
        raise _cap_error(exc)


@app.get('/api/plan/{plan_id}/invitations')
def api_invites(plan_id:str,request:Request):
    try:
        return _with_session(request,{'invitations':service.invitations_for(db,current_user(request),plan_id)})
    except ValueError as exc:
        raise _cap_error(exc)


@app.post('/api/plan/{plan_id}/invitations/{iid}/approve')
def api_invite_approve(plan_id:str,iid:str,body:ApproveInviteBody,request:Request):
    _same_origin(request)
    try:
        return service.invitation_approve(db,current_user(request),plan_id,iid,body.replace_participant_id)
    except ValueError as exc:
        raise _cap_error(exc)


@app.post('/api/plan/{plan_id}/invitations/{iid}/revoke')
def api_invite_revoke(plan_id:str,iid:str,request:Request):
    _same_origin(request)
    try:
        return service.invitation_revoke(db,current_user(request),plan_id,iid)
    except ValueError as exc:
        raise _cap_error(exc)


@app.post('/api/participation/accept')
def api_participation_accept(body:AcceptInviteBody,request:Request):
    _same_origin(request)
    try:
        out=service.invitation_accept(db,body.token,body.display_name)
    except ValueError as exc:
        raise _cap_error(exc)
    session=out.pop('session_token')
    response=JSONResponse(out)
    response.set_cookie(MATTER_COOKIE,session,max_age=86400,httponly=True,samesite='strict',secure=request.url.scheme=='https')
    # Persist a deny-only marker after the capability cookie naturally expires.
    response.set_cookie('pa_matter_mode','1',max_age=31536000,httponly=True,samesite='strict',secure=request.url.scheme=='https')
    return response


@app.get('/api/participation/plan')
def api_participation_plan(request:Request):
    return service.participation_plan(db,_guest(request))


class GuestConditionBody(BaseModel):
    model_config={'extra':'forbid'}
    conditions:dict[str,Any]
    children:list[dict[str,Any]]=[]
    children_count:int | None=Field(default=None,ge=0,le=20)


class TripPartyBody(BaseModel):
    """朋友旅行：本人声明这一方的人数与出席，并自己决定要不要说出随行是谁。"""
    model_config={'extra':'forbid'}
    headcount:dict[str,Any]
    conditions:dict[str,Any]
    companions:list[Any]=Field(default_factory=list)


class GuestDecisionBody(BaseModel):
    model_config={'extra':'forbid'}
    version:int=Field(ge=1)
    decision:str=Field(pattern='^(accept|reject|counter)$')
    reason:str=Field(default='',max_length=500)


class GuestReviewBody(BaseModel):
    model_config={'extra':'forbid'}
    version:int=Field(ge=1)


@app.post('/api/participation/condition')
def api_participation_condition(body:GuestConditionBody,request:Request):
    try:
        return service.guest_condition(db,_guest(request,True),body.conditions,body.children,body.children_count)
    except ValueError as exc:
        raise _cap_error(exc)


@app.post('/api/participation/trip-party')
def api_participation_trip_party(body:TripPartyBody,request:Request):
    try:
        return service.declare_trip_party(db,_guest(request,True),body.headcount,
                                          body.conditions,body.companions)
    except ValueError as exc:
        raise _cap_error(exc)


@app.post('/api/participation/decide')
def api_participation_decide(body:GuestDecisionBody,request:Request):
    session=_guest(request,True)
    try:
        with db.atomic():
            live=service.matter_session(db,session['raw_token'])
            if not live['approved']:
                raise ValueError('发起人尚未认可本人自称参与方')
            service.decide_plan(db,live['plan_id'],live['participant_id'],body.version,body.decision,body.reason,actor_id=live['guest_id'])
        return {'ok':True}
    except ValueError as exc:
        raise _cap_error(exc)


@app.post('/api/participation/review')
def api_participation_review(body:GuestReviewBody,request:Request):
    session=_guest(request,True)
    if not session['approved']:
        raise HTTPException(status_code=409,detail='发起人尚未认可本人自称参与方')
    try:
        return agents.plan_review(db,client,session['plan_id'],session['participant_id'],body.version)
    except ValueError as exc:
        raise _cap_error(exc)


class NotePlanBody(BaseModel):
    model_config={'extra':'forbid'}
    event_id:str
    plan_id:str
    selected_fields:list[str]=[]
    public_conditions:dict[str,Any]={}
    publish_confirmed:bool=False
    replace_link_id:str=''
    owner_id:str=''


class NotePlanCreateBody(BaseModel):
    model_config={'extra':'forbid'}
    title:str=Field(min_length=1,max_length=200)
    event_id:str
    selected_fields:list[str]=[]
    public_conditions:dict[str,Any]={}
    publish_confirmed:bool=False
    owner_id:str=''


@app.get('/api/note/linkable-plans')
def api_linkable_plans(request:Request):
    return _with_session(request,{'plans':service.linkable_plans(db,current_user(request))})


@app.get('/api/note/{event_id}/fields')
def api_note_fields(event_id:str,request:Request):
    try:
        return _with_session(request,service.note_fields(db,current_user(request),event_id))
    except ValueError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@app.post('/api/note/link-plan')
def api_note_link_plan(body:NotePlanBody,request:Request):
    _same_origin(request)
    try:
        return service.link_event_to_plan(db,_session_owner(body.owner_id,request),body.event_id,body.plan_id,body.selected_fields,body.public_conditions,body.publish_confirmed,body.replace_link_id)
    except ValueError as exc:
        raise _cap_error(exc)


@app.post('/api/note/unlink-plan')
def api_note_unlink_plan(body:NotePlanBody,request:Request):
    _same_origin(request)
    try:
        return service.unlink_event_from_plan(db,_session_owner(body.owner_id,request),body.event_id,body.plan_id)
    except ValueError as exc:
        raise _cap_error(exc)


@app.get('/api/plan/{plan_id}/linked-notes')
def api_plan_linked_notes(plan_id:str,request:Request):
    try:
        return _with_session(request,{'notes':service.linked_plan_events(db,plan_id,current_user(request))})
    except ValueError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@app.post('/api/plan/create-with-note')
def api_create_plan_with_note(body:NotePlanCreateBody,request:Request):
    _same_origin(request)
    try:
        return service.create_plan_with_note(db,_session_owner(body.owner_id,request),body.title,body.event_id,body.selected_fields,body.public_conditions,body.publish_confirmed)
    except ValueError as exc:
        raise _cap_error(exc)


# Never echo submitted values on connector validation errors (including URLs/tokens).
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler


@app.exception_handler(RequestValidationError)
async def connector_validation_error(request: Request, exc: RequestValidationError):
    if request.url.path.startswith('/api/connectors'):
        return JSONResponse(status_code=422, content={'detail': [{'loc': list(e['loc']), 'msg': e['msg'], 'type': e['type']} for e in exc.errors()]})
    return await request_validation_exception_handler(request, exc)


# Connector APIs stay inside ordinary owner sessions and same-origin writes.
CONNECTOR_CSRF: dict[str, str] = {}


class ConnectorBody(BaseModel):
    model_config = {'extra': 'forbid'}
    kind: str
    name: str = Field(min_length=1, max_length=60)
    base_url: str = Field(default='', max_length=300)
    path: str = Field(default='', max_length=200)
    field_map: dict[str, str] = {}
    auth_token: str = Field(default='', max_length=2000)


class ConnectorGrantBody(BaseModel):
    model_config = {'extra': 'forbid'}
    fields: list[str] = Field(max_length=20)


def _connector_owner(request, write=False):
    owner = current_user(request)
    sid = request.cookies.get(SESSION_COOKIE) or getattr(request.state, 'new_session', '')
    if write:
        _same_origin(request)
        expected = CONNECTOR_CSRF.get(sid)
        if not expected or not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), expected):
            raise HTTPException(status_code=403, detail='数据接入会话校验失败，请刷新页签后重试')
    return owner, sid


@app.get('/api/connectors')
def api_connectors(request: Request):
    owner, sid = _connector_owner(request)
    token = CONNECTOR_CSRF.setdefault(sid, secrets.token_urlsafe(24))
    return _with_session(request, {'connectors': service.connector_list(db, owner),
        'catalog': [{'key': k, **{f: v[f] for f in ('name', 'description')}, 'simulated': True} for k,v in service.CONNECTOR_CATALOG.items()],
        'csrf_token': token, 'demo_base_url': str(request.base_url).rstrip('/'),
        'demo_path': '/api/connectors/demo-data'})


@app.get('/api/connectors/demo-data')
def api_connector_demo_data(request: Request):
    owner, _ = _connector_owner(request)
    return _with_session(request, {'items': service.connector_demo_items(db, owner), 'simulated': True,
        'note': '内置本地 HTTP 演示接口，不是真实用户应用数据'})


@app.post('/api/connectors')
def api_connector_create(body: ConnectorBody, request: Request):
    owner, _ = _connector_owner(request, write=True)
    try:
        cid = service.connector_create(db, owner, **body.model_dump())
        return {'connector_id': cid}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post('/api/connectors/{cid}/test')
def api_connector_test(cid: str, request: Request):
    owner, _ = _connector_owner(request, write=True)
    try:
        return service.connector_test(db, owner, cid)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post('/api/connectors/{cid}/sync')
def api_connector_sync(cid: str, request: Request):
    owner, _ = _connector_owner(request, write=True)
    try:
        return service.connector_sync(db, owner, cid)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post('/api/connectors/{cid}/revoke')
def api_connector_revoke(cid: str, request: Request):
    owner, _ = _connector_owner(request, write=True)
    try:
        return service.connector_revoke(db, owner, cid)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post('/api/connectors/{cid}/records/{eid}/authorize')
def api_connector_authorize(cid: str, eid: str, body: ConnectorGrantBody, request: Request):
    owner, _ = _connector_owner(request, write=True)
    try:
        return service.connector_authorize(db, owner, cid, eid, body.fields)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post('/api/connectors/{cid}/records/{eid}/correct')
def api_connector_correct(cid: str, eid: str, body: CorrectEventBody, request: Request):
    owner, _ = _connector_owner(request, write=True)
    try:
        service._connector(db, owner, cid)
        if not db.row('SELECT 1 FROM connector_record WHERE connector_id=? AND event_id=?', (cid, eid)):
            raise ValueError('这条记录不属于连接器')
        return {'new_event_id': service.correct_life_event(db, eid, body.payload, owner)}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post('/api/connectors/{cid}/records/{eid}/revoke')
def api_connector_record_revoke(cid: str, eid: str, request: Request):
    owner, _ = _connector_owner(request, write=True)
    try:
        service._connector(db, owner, cid)
        if not db.row('SELECT 1 FROM connector_record WHERE connector_id=? AND event_id=?', (cid, eid)):
            raise ValueError('这条记录不属于连接器')
        service.revoke_life_event(db, eid, owner)
        return {'revoked': True}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
