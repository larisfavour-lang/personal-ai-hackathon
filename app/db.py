"""SQLite 存储：人、有来源的事实、送礼往来、任务与提案、授权与确认。

设计原则：
- 每条事实都记录说的是谁（事实主体）、来源发言、提供者、适用时间与状态。
- 旧事实不删除，标记为被新记录替代，保留原始证据。
- 模拟订单有去重键和回执，重复确认不会产生第二单。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_user (
    owner_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    blurb TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person (
    person_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    name TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    aliases TEXT NOT NULL DEFAULT '[]',
    attributes_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS relation (
    relation_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    from_person_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    to_person_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    source_utterance TEXT NOT NULL DEFAULT '',
    observed_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS occasion (
    occasion_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    title TEXT NOT NULL,
    happened_on TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'meeting',
    participant_ids_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    source_utterance TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    -- 家庭活动（plan 链）另存一列，不塞进 task_id：点「打开事项」时
    -- 拿 task_id 去查 task 表会查不到，用户点开的是一条不存在的记录。
    plan_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_owner_seq ON conversation(owner_id, seq);

CREATE TABLE IF NOT EXISTS pending_action (
    action_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    prompt TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS fact (
    fact_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value TEXT NOT NULL,
    source_utterance TEXT NOT NULL,
    provider TEXT NOT NULL,
    observed_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    superseded_by TEXT
);

CREATE TABLE IF NOT EXISTS gift_record (
    gift_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL DEFAULT '',
    owner_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    spec TEXT NOT NULL DEFAULT '',
    occasion TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'delivered',
    feedback TEXT NOT NULL DEFAULT '',
    happened_on TEXT NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS product (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price_cny REAL NOT NULL,
    has_size INTEGER NOT NULL DEFAULT 0,
    size_options TEXT NOT NULL DEFAULT '[]',
    age_hint TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    -- 家长核对礼物时要看的两个商品属性：是否发声、是否便于收纳。
    makes_noise INTEGER NOT NULL DEFAULT 0,
    easy_to_store INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS task (
    task_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    utterance TEXT NOT NULL,
    extracted_json TEXT NOT NULL DEFAULT '{}',
    stage TEXT NOT NULL DEFAULT 'created',
    outcome TEXT NOT NULL DEFAULT '',
    -- 这件事被改走家庭活动时，记下新的 plan_id：旧事项不删，
    -- 用户点开旧的仍看得到原话，并有一个能点回去的入口。
    plan_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposal (
    proposal_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    proposer TEXT NOT NULL,
    product_id TEXT,
    reason TEXT NOT NULL DEFAULT '',
    asked_size TEXT NOT NULL DEFAULT '',
    shared_conditions_json TEXT NOT NULL DEFAULT '[]',
    facts_used_json TEXT NOT NULL DEFAULT '[]',
    decision TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT NOT NULL DEFAULT '',
    decision_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_log (
    msg_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS authorization (
    auth_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL DEFAULT '',
    attributes_json TEXT NOT NULL DEFAULT '[]',
    counterparty TEXT NOT NULL DEFAULT '',
    valid_until TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);

-- 家庭近况事件。来自某个标注的测试来源（学习任务、日常确认、手表模拟数据），
-- 说的是某个人、授权给谁看、看哪些字段。事件自己带来源标识和发生时间，
-- 不并进 person.attributes_json：同一家人可能从两个 App 各报一件事，
-- 合并成一份就分不清是谁说的、什么时候说的、授权范围是什么。
CREATE TABLE IF NOT EXISTS life_event (
    event_id TEXT PRIMARY KEY,
    -- 来源标识：来自哪个小 App。重复导入同一来源的同一事件时靠它去重。
    source_id TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'app',
    -- 事件说的是谁。按人物实体关联，不靠名字猜。
    subject_id TEXT NOT NULL,
    -- 这条事件记在谁的名下（记忆所有者）。
    owner_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '',
    -- 事件内容：只存这个来源自己报的字段，不替它补全。
    payload_json TEXT NOT NULL DEFAULT '{}',
    happened_on TEXT NOT NULL DEFAULT '',
    -- 提供者：谁说的。本人确认、家长转述、设备推导要分开。
    provider TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    superseded_by TEXT,
    created_at TEXT NOT NULL
);

-- 事件授权：这条事件的哪些字段可以给谁看。和 authorization 分开：
-- authorization 管的是「关于这个人的某类属性」，这里管「这一条事件」。
-- 同一个人从学习 App 报的进度可以给子女看，手表报的睡眠就不一定。
CREATE TABLE IF NOT EXISTS event_grant (
    grant_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    -- 授权给谁看。
    viewer_id TEXT NOT NULL,
    -- 这条事件里授权可见的字段。空数组表示这个字段都没授权。
    fields_json TEXT NOT NULL DEFAULT '[]',
    valid_until TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 同一条事件只登记一次通知/事项：重复导入不重复打扰用户。
CREATE TABLE IF NOT EXISTS event_notice (
    notice_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    viewer_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'imported',
    created_at TEXT NOT NULL
);

-- 资料与事项的关联。一条资料（life_event）可以明确用于某件未完事项：
-- 保存资料之后由本人点「用于这件事」，不是系统替他决定。
-- 关联本身不产生任何权限：不等于授权对外分享、不等于建立他人承诺，
-- 也不等于可以执行。它只记录「这次办事我用了哪条资料」，
-- 让事项详情能回看依据，也让协商上下文真的拿到这条内容。
-- 同一事件对同一事项只留一条：重复点不叠记录。
CREATE TABLE IF NOT EXISTS event_task_link (
    link_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    owner_id TEXT NOT NULL DEFAULT '',
    -- 本人确认这次用它。关联只能是本人点的，不能由系统或对方代填。
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(event_id, task_id)
);

-- 事件配图。默认不存图：只有本人在核对界面里明确点了「保留为配图」，
-- 图片才落到这里。识别用的那张图不等于愿意长期保存的那张。
-- owner_id 跟 life_event 一致，取图时按本人身份校验，不是拿到链接就能读。
CREATE TABLE IF NOT EXISTS event_attachment (
    attachment_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    owner_id TEXT NOT NULL DEFAULT '',
    -- media 是 image/jpeg、image/png、image/webp 之一，取图时据此给 content-type。
    media TEXT NOT NULL DEFAULT 'image/jpeg',
    -- 原图 base64。演示数据量小，直接内联；不做缩略图，避免存两份对不上。
    b64 TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 显式的人物关联：两侧记录的是同一个人时，由本人确认后建立链接。
-- 不能按名字自动合并：两个用户记录里同名的人不一定是同一个人，
-- 自动合并会把 A 的事件挂到 B 记录的另一个人头上。
CREATE TABLE IF NOT EXISTS person_link (
    link_id TEXT PRIMARY KEY,
    person_id_a TEXT NOT NULL,
    person_id_b TEXT NOT NULL,
    -- 谁确认的这条关联，保留来源。
    confirmed_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

-- 多人事项：一次活动由多个家庭共同决定。
-- 和送礼任务分开：这里每个参与方有自己的条件和回复，
-- 约定必须各方都接受同一版本，缺一方就仍待确认。
CREATE TABLE IF NOT EXISTS plan (
    plan_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    title TEXT NOT NULL,
    utterance TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'group_activity',
    stage TEXT NOT NULL DEFAULT 'draft',
    outcome TEXT NOT NULL DEFAULT '',
    -- 有家庭改了自己的条件、方案还没重排时置 1。
    -- 这个状态下不能算约定，也不能对旧版本表态：条件已经变了，
    -- 各家原来接受的是旧条件排出来的方案。重排成功后才清 0。
    -- 放在 plan 表里而不是只靠 stage 字符串：刷新、重试、
    -- 重新打开页面后这个事实都还在，不会因为状态被改写成 awaiting 就丢掉。
    needs_replan INTEGER NOT NULL DEFAULT 0,
    -- 家庭活动的目标结构与当前缺口：用户说了几家、哪几家已选、哪几家还没选、
    -- 哪些孩子说来说不来。放这里而不是只放在回复文本里——刷新、重新打开、
    -- 从别处跟进时这些事实都还在，不会退回到「一直是个空草稿」。
    -- 形如 {"requested_families": 3, "invited": ["f_p_azhou"],
    --        "unknown_families": 1, "child_attendance": {}}
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

-- 参与方：谁参加、代表谁、这一方给出的条件、是否必要。
-- 缺必要参与方时不能显示已约定。
CREATE TABLE IF NOT EXISTS plan_participant (
    participant_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    -- 这一方的条件：时间、预算、孩子安排等，由本人授权提供。
    conditions_json TEXT NOT NULL DEFAULT '{}',
    -- 必要 / 可选。必要方没确认，整件事就不能算约定。
    required INTEGER NOT NULL DEFAULT 1,
    -- candidate=这一方被选进事项、还没回应；confirmed=这一方本人确认参与。
    -- 两者必须分开：把 candidate 显示成 confirmed，就是替别人答应了。
    status TEXT NOT NULL DEFAULT 'pending',
    -- 这一方在本事项里的角色：family=代表一个家庭，self=只有本人一方，
    -- unknown=用户说了但还没有记录的那一方（占位，不冒充任何已知家庭）。
    role TEXT NOT NULL DEFAULT 'family',
    -- 孩子出席：{"person_id": "coming"|"not_coming"|"unknown"}，由各家自己说。
    attendance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

-- 方案版本：和送礼的 proposal 一样按版本走，
-- 一方改了时间，受影响的旧接受必须失效，不能拿旧接受当约定。
CREATE TABLE IF NOT EXISTS plan_proposal (
    plan_proposal_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    -- 这一版是谁提的（参与方 owner_id，或系统汇总）。
    proposer TEXT NOT NULL DEFAULT '',
    content_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 参与方对某个版本的回复：接受 / 拒绝 / 反提案 / 未回复。
-- 未回复就是未回复，不能默认同意。
CREATE TABLE IF NOT EXISTS plan_decision (
    plan_decision_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    decision TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    -- 这一方改了什么，用于让受影响的旧接受失效。
    changes_json TEXT NOT NULL DEFAULT '{}',
    -- 这条表态是谁做的：本人（person）在手机上点的，还是这一方的
    -- 个人代表（representative）按这一方条件审阅出来的。
    -- 两者都只代表这一方，但来源必须分开，不能把代表审阅当成本人确认。
    decided_by TEXT NOT NULL DEFAULT 'person',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sim_order (
    order_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    proposal_version INTEGER NOT NULL,
    confirmed_by TEXT NOT NULL,
    product_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    spec TEXT NOT NULL DEFAULT '',
    recipient_subject_id TEXT NOT NULL,
    amount_cny REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'purchased',
    delivery_state TEXT NOT NULL DEFAULT 'simulated',
    paid_at TEXT NOT NULL DEFAULT '',
    paid_amount_cny REAL NOT NULL DEFAULT 0,
    ship_no TEXT NOT NULL DEFAULT '',
    shipped_at TEXT NOT NULL DEFAULT '',
    delivered_at TEXT NOT NULL DEFAULT '',
    dedup_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (dedup_key)
);

-- 候选商品：一次交办里模型给出的可选项，以及用户最终选了哪一个。
-- 没有它，用户看不到「还有什么可选」，也不清楚最后为什么是这一件。
CREATE TABLE IF NOT EXISTS order_candidate (
    candidate_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    product_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    price_cny REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    checked INTEGER NOT NULL DEFAULT 0,
    chosen INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_once
ON order_candidate(task_id, product_id, version);

-- 模型服务配置。只存一份，服务启动时读它决定连哪个模型接口。
-- 之前只能靠环境变量 PA_MODEL_BASE_URL / PA_MODEL_NAME / PA_MODEL_API_KEY，
-- 界面上没有任何地方能改，用户看不到当前连的是谁，也没法在演示时换一个。
-- 密钥单独一列、只在服务端用：读给前端时要抹掉，不能顺着接口流出去。
CREATE TABLE IF NOT EXISTS model_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    base_url TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

-- 浏览器插件：配对码、设备凭证、参数包快照、代填回执。
-- 这四个表只服务插件链。普通应用请求不用它们：应用的身份来自服务端会话，
-- 插件的身份来自应用本人批准后发出的设备凭证，两者都不接受 URL 或表单里的
-- owner_id/viewer_id 指定用户。
CREATE TABLE IF NOT EXISTS ext_pair_code (
    code TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    client_name TEXT NOT NULL DEFAULT '',
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ext_device (
    client_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    client_name TEXT NOT NULL DEFAULT '',
    access_token TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT NOT NULL DEFAULT '',
    revoked_by TEXT NOT NULL DEFAULT ''
);

-- 参数包快照：后端在「准备」这一步把事项、约定版本、字段和来源冻结下来。
-- 校验和回执都对着这一份快照核对，插件自己带过来的字段不算依据。
CREATE TABLE IF NOT EXISTS ext_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    target_origin TEXT NOT NULL,
    target_url TEXT NOT NULL DEFAULT '',
    contract_version TEXT NOT NULL DEFAULT '1',
    proposal_version INTEGER NOT NULL DEFAULT 0,
    subject_person_id TEXT NOT NULL DEFAULT '',
    subject_label TEXT NOT NULL DEFAULT '',
    product_id TEXT NOT NULL DEFAULT '',
    fields_json TEXT NOT NULL DEFAULT '[]',
    source_summary_json TEXT NOT NULL DEFAULT '[]',
    missing_fields_json TEXT NOT NULL DEFAULT '[]',
    can_fill INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    validated_at TEXT NOT NULL DEFAULT '',
    validated_by TEXT NOT NULL DEFAULT '',
    observed_product_id TEXT NOT NULL DEFAULT ''
);

-- 代填回执：插件把页面实际读回的值交给服务端，由服务端复核后才算 verified。
-- 客户端自己报的 client_status 只是 filled/partial/failed，不能决定 verified。
CREATE TABLE IF NOT EXISTS ext_receipt (
    receipt_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    target_origin TEXT NOT NULL,
    observed_fields_json TEXT NOT NULL DEFAULT '{}',
    client_status TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'partial',
    mismatches_json TEXT NOT NULL DEFAULT '[]',
    idempotency_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    -- 幂等作用域和查询一致：同一设备、同一参数包内同键才算同一次发送。
    -- 只对 key 做唯一约束时，另一个设备合法使用同一个键会直接 SQL 冲突，
    -- 而不是按作用域查重后给出可读的拒绝原因。
    UNIQUE (idempotency_key, client_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS plan_invitation (
 invitation_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, issuer TEXT NOT NULL,
 token_hash TEXT UNIQUE NOT NULL, label TEXT NOT NULL, guest_id TEXT UNIQUE NOT NULL,
 expires_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 claimed_name TEXT NOT NULL DEFAULT '', participant_id TEXT NOT NULL DEFAULT '',
 approved INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_guest_session (
 session_hash TEXT PRIMARY KEY, invitation_id TEXT NOT NULL, plan_id TEXT NOT NULL,
 guest_id TEXT NOT NULL, csrf_token TEXT NOT NULL, expires_at REAL NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS event_plan_link (
 link_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, plan_id TEXT NOT NULL,
 owner_id TEXT NOT NULL, selected_fields_json TEXT NOT NULL DEFAULT '[]',
 public_conditions_json TEXT NOT NULL DEFAULT '{}', source_fingerprint TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
 UNIQUE(event_id,plan_id,owner_id)
);
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL DEFAULT '',
    owner_id TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """把 now_iso 写进去的时间读回来，用来推算演示里的后续时间。

    读不回来时退回当前时间，不抛异常：这里算的是演示用的编排时间，
    不是真实配送承诺，不能让一个坏时间把整条支付流程带崩。
    """
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).astimezone()


def new_id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex[:12]


class DB:
    def __init__(self, path):
        self.path = str(path)
        self._local = threading.local()
        self._init_schema()

    @contextmanager
    def connect(self):
        active = getattr(self._local, "conn", None)
        if active is not None:
            yield active
            return
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def atomic(self):
        if getattr(self._local, "conn", None) is not None:
            yield self
            return
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._local.conn = conn
            try:
                yield self
            finally:
                self._local.conn = None

    def _init_schema(self):
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            for table, col, ddl in (("plan_proposal", "provenance_json", "TEXT NOT NULL DEFAULT '{}'"),):
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(" + table + ")")}
                if col not in cols:
                    conn.execute("ALTER TABLE " + table + " ADD COLUMN " + col + " " + ddl)
            # 旧库补 plan.meta_json 与 plan_participant.role/attendance_json：
            # 不补的话，家庭活动的目标家庭数、候选/确认、孩子出席存不进去，
            # 刷新后又回到「只有一个发起方的空草稿」。
            pm = {r["name"] for r in
                  conn.execute("PRAGMA table_info(plan)").fetchall()}
            if "meta_json" not in pm:
                conn.execute("ALTER TABLE plan ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")
            ppm = {r["name"] for r in
                   conn.execute("PRAGMA table_info(plan_participant)").fetchall()}
            if "role" not in ppm:
                conn.execute("ALTER TABLE plan_participant ADD COLUMN role"
                             " TEXT NOT NULL DEFAULT 'family'")
            if "attendance_json" not in ppm:
                conn.execute("ALTER TABLE plan_participant ADD COLUMN attendance_json"
                             " TEXT NOT NULL DEFAULT '{}'")
            # 旧库补 task.plan_id：不补的话，误当成送礼的旧事项转成家庭活动后
            # 没有地方记新事项的 id，用户点开旧的只能看到一句话，回不去。
            task_cols = {r["name"] for r in
                         conn.execute("PRAGMA table_info(task)").fetchall()}
            if "plan_id" not in task_cols:
                conn.execute("ALTER TABLE task ADD COLUMN plan_id TEXT NOT NULL DEFAULT ''")
            # 旧库补 plan_id：不补的话，家庭活动那一轮对话存不进去，
            # 刷新后聊天记录里看不到这件事，用户以为话白说了。
            conv_cols = {r["name"] for r in
                         conn.execute("PRAGMA table_info(conversation)").fetchall()}
            if "plan_id" not in conv_cols:
                conn.execute(
                    "ALTER TABLE conversation ADD COLUMN plan_id TEXT NOT NULL DEFAULT ''")
            # 旧库补齐新增列，避免升级后已有数据不可用。
            have = {r["name"] for r in
                    conn.execute("PRAGMA table_info(proposal)").fetchall()}
            if "asked_size" not in have:
                conn.execute("ALTER TABLE proposal ADD COLUMN asked_size TEXT NOT NULL DEFAULT ''")
            # 旧库补齐商品的两个家长核对属性。
            prod_cols = {r["name"] for r in
                         conn.execute("PRAGMA table_info(product)").fetchall()}
            if "makes_noise" not in prod_cols:
                conn.execute("ALTER TABLE product ADD COLUMN makes_noise INTEGER NOT NULL DEFAULT 0")
            if "easy_to_store" not in prod_cols:
                conn.execute(
                    "ALTER TABLE product ADD COLUMN easy_to_store INTEGER NOT NULL DEFAULT 1")
            # 送礼记录关联到模拟订单：登记之后要能顺着订单回看当时约定了什么。
            gift_cols = {r["name"] for r in
                         conn.execute("PRAGMA table_info(gift_record)").fetchall()}
            if "order_id" not in gift_cols:
                conn.execute(
                    "ALTER TABLE gift_record ADD COLUMN order_id TEXT NOT NULL DEFAULT ''")
            # 旧库补齐表态来源：本人在手机上点的，还是这一方的代表审阅出来的。
            # 两者都只代表这一方，来源不能混。
            dec_cols = {r["name"] for r in
                        conn.execute("PRAGMA table_info(plan_decision)").fetchall()}
            if "decided_by" not in dec_cols:
                conn.execute(
                    "ALTER TABLE plan_decision ADD COLUMN decided_by"
                    " TEXT NOT NULL DEFAULT 'person'")
            # 旧库补齐订单后续状态：支付、物流单号、发货、送达。
            # 没有这些列时，已登记的旧订单只显示「已登记」，看不出后续走到哪一步。
            order_cols = {r["name"] for r in
                          conn.execute("PRAGMA table_info(sim_order)").fetchall()}
            for col, ddl in (
                ("paid_at", "TEXT NOT NULL DEFAULT ''"),
                ("paid_amount_cny", "REAL NOT NULL DEFAULT 0"),
                ("ship_no", "TEXT NOT NULL DEFAULT ''"),
                ("shipped_at", "TEXT NOT NULL DEFAULT ''"),
                ("delivered_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in order_cols:
                    conn.execute("ALTER TABLE sim_order ADD COLUMN %s %s" % (col, ddl))
            # 插件参数包表补列。CREATE TABLE IF NOT EXISTS 对已存在的表
            # 不会加列：不补的话，插件一调 prepare 就报
            # no column named target_url，整条联调链断在这里。
            snap_cols = {r["name"] for r in
                         conn.execute("PRAGMA table_info(ext_snapshot)").fetchall()}
            if "target_url" not in snap_cols:
                conn.execute(
                    "ALTER TABLE ext_snapshot ADD COLUMN target_url TEXT NOT NULL DEFAULT ''")
            audit_cols = {r["name"] for r in
                          conn.execute("PRAGMA table_info(audit_log)").fetchall()}
            if "owner_id" not in audit_cols:
                conn.execute(
                    "ALTER TABLE audit_log ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
            DB._migrate_receipt_uniqueness(conn)
            DB._migrate_device_lifetime(conn)

    @staticmethod
    def _migrate_device_lifetime(conn):
        """把仍然有效的旧设备凭证改成不设固定到期日。

        用户要求「连接一次持续使用」。只改签发逻辑的话，已经连着的那台
        设备还挂着旧的 24 小时到期时间，到点照样被踢下线——用户会看到
        一个「明明刚连上却要求重新配对」的结果，正是要避免的那种摩擦。

        只搬还活着的那一批：已撤销的必须继续拒绝，已经过期的也必须继续
        拒绝。少了到期判断的话，这次迁移会把「到期但没人撤销」的旧凭证
        一并改成永久有效——失效的凭证就此复活，等于悄悄延长了它的寿命。
        """
        cur = conn.execute(
            "UPDATE ext_device SET expires_at=''"
            " WHERE expires_at<>'' AND revoked_at='' AND expires_at > ?",
            (datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),))
        return cur.rowcount

    @staticmethod
    def _migrate_receipt_uniqueness(conn):
        """把回执表的唯一约束改成 (key, client_id, snapshot_id)。

        幂等只在「同一设备、同一参数包」内成立。旧库只对 idempotency_key
        做唯一约束，另一个设备合法使用同一个键会直接 SQL 冲突，
        而不是按作用域查重后给出可读的拒绝原因。
        ALTER TABLE 不能改约束，所以按标准四步重建表，数据先搬过去。
        """
        idx = [dict(r) for r in conn.execute("PRAGMA index_list(ext_receipt)")]
        for i in idx:
            if not i.get("unique"):
                continue
            cols = [c[2] for c in
                    conn.execute("PRAGMA index_info(%s)" % i["name"]).fetchall()]
            if cols == ["idempotency_key"]:
                break
        else:
            return  # 已经是新约束（或没有该唯一索引），不用动
        conn.execute("ALTER TABLE ext_receipt RENAME TO ext_receipt_old")
        conn.execute("""
            CREATE TABLE ext_receipt (
                receipt_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                target_origin TEXT NOT NULL,
                observed_fields_json TEXT NOT NULL DEFAULT '{}',
                client_status TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'partial',
                mismatches_json TEXT NOT NULL DEFAULT '[]',
                idempotency_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE (idempotency_key, client_id, snapshot_id)
            )
        """)
        conn.execute(
            "INSERT OR IGNORE INTO ext_receipt SELECT receipt_id, snapshot_id, owner_id,"
            " client_id, task_id, target_origin, observed_fields_json, client_status,"
            " status, mismatches_json, idempotency_key, created_at FROM ext_receipt_old")
        conn.execute("DROP TABLE ext_receipt_old")

    def rows(self, sql, params=()):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def row(self, sql, params=()):
        found = self.rows(sql, params)
        return found[0] if found else None

    def execute(self, sql, params=()):
        with self.connect() as conn:
            conn.execute(sql, params)

    def audit(self, task_id, actor, action, detail, owner_id=""):
        """写审计。owner_id 是这条记录归属的本人，普通入口只读本人的。

        旧库没有 owner_id 列时先补列：没有它，普通审计入口一读就是全库。
        """
        with self.connect() as conn:
            cols = {r["name"] for r in
                    conn.execute("PRAGMA table_info(audit_log)").fetchall()}
            if "owner_id" not in cols:
                conn.execute(
                    "ALTER TABLE audit_log ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "INSERT INTO audit_log(audit_id, task_id, owner_id, actor, action,"
                " detail_json, created_at) VALUES(?,?,?,?,?,?,?)",
                (new_id("aud"), task_id, owner_id, actor, action,
                 json.dumps(detail, ensure_ascii=False), now_iso()))


DEMO_USERS = [
    ("A", "我", "giver", "记住我关心的人和往来，少解释地把事情办好。"),
    ("B", "小周", "parent", "我掌握孩子的最新情况，只按授权范围分享，并核对对方方案。"),
    # C 不是第三个家庭：DEMO_PERSON_LINKS 里写明「送礼者认识的小林和家长记录的
    # 配偶是同一个人」，小周与配偶小林是同一个家庭。这一条的作用是让切到 C 时
    # 侧栏和对话不是空的，能看出同一家庭里另一位家长的条件是分开维护的——
    # 不是多出一个家庭。界面与材料一律说「两家三身份」。
    ("C", "小林", "same_family_second_adult",
     "小周的配偶，和小周是同一个家庭；我只给本家庭的条件，不替别家决定。"),
]

# 人物实体。attributes_json 存异质属性：性别、年龄、年级、身高、脚长、已有物品等。
# 属性只存「关于这个人的」，关系放 relation 表；小周的职业属于小周，不是孩子的偏好。
PERSONS = [
    ("p_me", "A", "我", "self", "送礼者本人。", '[]',
     '{"city": "上海", "收货人": "测试用户", "收货电话": "13800000000",'
     ' "收货地址": "上海市浦东新区演示路 1 号（虚构）"}'),
    ("p_azhou", "A", "小周", "friend", "我以前的朋友，两个孩子的父亲。", '["老周","周哥"]',
     '{"职业": "建筑设计师", "城市": "上海"}'),
    ("p_xiaolin", "A", "小林", "friend_spouse", "小周的配偶，我通过小周认识她。", '[]',
     '{"职业": "小学教师"}'),
    ("p_xiaoyu", "A", "小雨", "friend_child", "小周的女儿，8 岁。", '[]',
     '{"性别": "女", "年龄": 8, "年级": "小学二年级（2026 秋季）", "身高": "128 cm", "体重": "26 kg",'
     ' "脚长": "20.5 cm", "脚型": "偏宽", "已有物品": ["24 色彩笔"],'
     ' "家长本次要求": "礼物不发声、好收纳", "试穿记录": "上学期某品牌 31 码，现在是否合脚待核实"}'),
    ("p_xiaohan", "A", "小宇", "friend_child", "小周的儿子，小雨的哥哥。", '["小翰"]',
     '{"性别": "男", "年龄": 10, "年级": "小学四年级（2026 秋季）", "已有物品": ["100 片城市拼图"]}'),
    ("p_azhou_b", "B", "我", "self", "小周本人。", '[]',
     '{"职业": "建筑设计师", "城市": "上海"}'),
    ("p_xiaolin_b", "B", "小林", "spouse", "我的配偶，小学教师。", '[]',
     '{"职业": "小学教师"}'),
    ("p_xiaoyu_b", "B", "小雨", "own_child", "我的女儿，8 岁。", '[]',
     '{"性别": "女", "年龄": 8, "年级": "小学二年级（2026 秋季）", "身高": "128 cm", "体重": "26 kg",'
     ' "脚长": "20.5 cm", "脚型": "偏宽", "已有物品": ["24 色彩笔"],'
     ' "家长本次要求": "礼物不发声、好收纳", "试穿记录": "上学期某品牌 31 码，现在是否合脚待核实"}'),
    ("p_xiaohan_b", "B", "小宇", "own_child", "我的儿子，10 岁。", '[]',
     '{"性别": "男", "年龄": 10, "年级": "小学四年级（2026 秋季）", "已有物品": ["100 片城市拼图"]}'),
    # C 侧：第三个家庭自己记录的人和关系。和 A/B 的记录各自独立，
    # 同一个人在小林家这边的角色由这一方自己写，不沿用别家的措辞。
    ("p_me_c", "C", "我", "self", "小林本人。", '[]',
     '{"职业": "小学教师"}'),
    ("p_azhou_c", "C", "小周", "spouse", "我的配偶，建筑设计师。", '["老周","周哥"]',
     '{"职业": "建筑设计师"}'),
    ("p_xiaoyu_c", "C", "小雨", "own_child", "我的女儿，8 岁。", '[]',
     '{"性别": "女", "年龄": 8, "年级": "小学二年级（2026 秋季）"}'),
    ("p_xiaohan_c", "C", "小宇", "own_child", "我的儿子，10 岁。", '["小翰"]',
     '{"性别": "男", "年龄": 10, "年级": "小学四年级（2026 秋季）"}'),
]

# 关系：谁 → 什么关系 → 谁。职业不进这里，关系起止与来源保留。
RELATIONS = [
    ("r_me_azhou", "A", "p_me", "朋友", "p_azhou", "我以前的朋友", "小周是我以前的朋友", "2026-10-01"),
    ("r_me_xiaolin", "A", "p_me", "朋友", "p_xiaolin", "通过小周认识", "我通过小周认识小林", "2026-10-01"),
    ("r_azhou_xiaoyu", "A", "p_azhou", "父亲", "p_xiaoyu", "小雨的父亲", "小周是雨的父亲", "2026-10-01"),
    ("r_azhou_xiaohan", "A", "p_azhou", "父亲", "p_xiaohan", "小宇的父亲", "小周是雨的父亲", "2026-10-01"),
    ("r_xiaolin_xiaoyu", "A", "p_xiaolin", "母亲", "p_xiaoyu", "小雨的母亲", "小林是雨的母亲", "2026-10-01"),
    ("r_xiaolin_xiaohan", "A", "p_xiaolin", "母亲", "p_xiaohan", "小宇的母亲", "小林是雨的母亲", "2026-10-01"),
    ("r_xiaoyu_xiaohan", "A", "p_xiaoyu", "妹妹", "p_xiaohan", "小宇的妹妹", "小雨是小宇的妹妹", "2026-10-01"),
    # C 侧关系：从小林的角度写。她在这一方是「我」，不是「朋友的配偶」。
    ("r_c_me_azhou", "C", "p_me_c", "配偶", "p_azhou_c", "我的配偶", "小周是我的配偶", "2026-10-01"),
    ("r_c_me_xiaoyu", "C", "p_me_c", "母亲", "p_xiaoyu_c", "小雨的母亲", "我是小雨的母亲", "2026-10-01"),
    ("r_c_me_xiaohan", "C", "p_me_c", "母亲", "p_xiaohan_c", "小宇的母亲", "我是小宇的母亲", "2026-10-01"),
    ("r_c_azhou_xiaoyu", "C", "p_azhou_c", "父亲", "p_xiaoyu_c", "小雨的父亲", "小周是雨的父亲", "2026-10-01"),
    ("r_c_azhou_xiaohan", "C", "p_azhou_c", "父亲", "p_xiaohan_c", "小宇的父亲", "小周是雨的父亲", "2026-10-01"),
    ("r_c_xiaoyu_xiaohan", "C", "p_xiaoyu_c", "妹妹", "p_xiaohan_c", "小宇的妹妹", "小雨是小宇的妹妹", "2026-10-01"),
]

# 关于人的事实。fact_owner 是记忆所有者，subject_id 是事实主体。
# 提供者区分「本人说」「母亲说」「我的旧记录」，性格描述不写成客观诊断。
FACTS = [
    # A 侧：我过去的错误记录，需要被纠正
    ("f_xy_puzzle_old", "A", "p_xiaoyu", "likes", "拼图",
     "小雨挺喜欢拼图的，买拼图准没错", "我的旧记录", "2026-08-01", "superseded"),
    ("f_xy_puzzle_old2", "A", "p_xiaoyu", "likes", "拼图",
     "小雨挺喜欢拼图的，买拼图准没错", "我的旧记录", "2026-08-01", "superseded"),
    ("f_xh_draw_old", "A", "p_xiaohan", "likes", "画画",
     "小宇爱画画", "我的旧记录", "2026-08-01", "superseded"),
    # A 侧：我过去的送礼记录
    ("f_xy_book", "A", "p_xiaoyu", "已有物品", "动物绘本（我 2025 年生日送的）",
     "我 2025 年生日送过一本动物绘本，母亲说她经常翻", "我本人记录", "2026-09-20", "active"),
    ("f_xy_book_caveat", "A", "p_xiaoyu", "偏好边界", "不能因为她翻绘本就说她喜欢所有绘本",
     "没有证据说明她喜欢所有绘本", "我本人记录", "2026-09-20", "active"),
    # A 侧早期信息：我自己记的旧尺码，来源和日期都在，需要时向对方核实当前值。
    ("f_xy_size_old", "A", "p_xiaoyu", "shoe_size", "31 码",
     "去年给小雨买鞋是 31 码，穿得还行", "我的旧记录", "2026-03-15", "active"),
    # B 侧：家长提供的孩子情况，标注提供者与日期
    ("f_xy_draw", "B", "p_xiaoyu_b", "likes", "画画",
     "小雨自己说喜欢画画、紫色和动物题材", "小雨本人", "2026-10-10", "active"),
    ("f_xy_color", "B", "p_xiaoyu_b", "喜欢颜色", "紫色",
     "小雨自己说喜欢紫色", "小雨本人", "2026-10-10", "active"),
    ("f_xy_theme", "B", "p_xiaoyu_b", "喜欢题材", "动物题材",
     "小雨自己说喜欢动物题材", "小雨本人", "2026-10-10", "active"),
    ("f_xy_shy", "B", "p_xiaoyu_b", "性格描述", "初见陌生人时慢热，熟悉后愿意分享作品",
     "妈妈说她初见陌生人时慢热，熟悉后会主动分享自己的画", "母亲描述", "2026-10-10", "active"),
    ("f_xy_foot", "B", "p_xiaoyu_b", "foot_length", "20.5 cm，脚偏宽",
     "母亲 2026-10-10 提供：身高 128 cm、体重 26 kg、脚长 20.5 cm、脚偏宽", "母亲提供", "2026-10-10", "active"),
    ("f_xy_foot_old", "B", "p_xiaoyu_b", "试穿记录", "上学期某品牌 31 码穿过",
     "上学期某品牌 31 码穿过，现在是否合脚待核实", "母亲提供", "2026-10-10", "active"),
    ("f_xy_family_rule", "B", "p_xiaoyu_b", "家长本次要求", "礼物不发声、好收纳",
     "家长本次要求礼物不发声、好收纳", "家长要求", "2026-10-12", "active"),
    ("f_xh_puzzle", "B", "p_xiaohan_b", "likes", "拼图",
     "小宇最近迷上拼图，能一个人拼 100 片", "小宇本人", "2026-10-02", "active"),
    ("f_xh_have", "B", "p_xiaohan_b", "已有物品", "100 片城市拼图",
     "家里已经有 100 片城市拼图", "家长记录", "2026-10-02", "active"),
    # C 侧：小林的记录和 B 侧各自独立，不共用同一行。
    # 三家庭示范里，这一方给出的条件要和别家分开，才看得出没有互相代填。
    ("f_c_xy_draw", "C", "p_xiaoyu_c", "likes", "画画",
     "小雨喜欢画画，最近在学画动物", "小林本人记录", "2026-10-12", "active"),
    ("f_c_xy_color", "C", "p_xiaoyu_c", "喜欢颜色", "紫色",
     "小雨喜欢紫色", "小林本人记录", "2026-10-12", "active"),
    ("f_c_xh_puzzle", "C", "p_xiaohan_c", "likes", "拼图",
     "小宇在拼 100 片城市拼图", "小林本人记录", "2026-10-12", "active"),
    ("f_c_weekend", "C", "p_me_c", "本周可安排时间", "周末都可以",
     "周末都行，平时下班晚", "小林本人", "2026-10-13", "active"),
]

GIFTS = [
    ("g_xy_book_2025", "A", "p_xiaoyu", "pr_animal_book", "动物绘本 4 册", "4 册",
     "2025 年生日", "delivered", "母亲说她经常翻", "2025-10-20"),
    ("g_xh_puzzle", "A", "p_xiaohan", "pr_puzzle_100", "100 片城市拼图", "100 片",
     "今年春节", "delivered", "拼了一段时间", "2026-02-10"),
]

# 商品：覆盖画材、拼图、有规格要求的鞋类。全部标注测试数据。
PRODUCTS = [
    # 每件商品带两个家长核对要用的属性：是否发声、是否便于收纳。
    # 家长代表只按自己授权范围内的记录判断，商品属性由程序提供，不靠猜。
    ("pr_draw_set", "儿童绘画套装（画册+颜料）", "art", 168.0, 0, "[]", "4-8 岁", "测试商品", 0, 1),
    ("pr_crayon_36", "36 色丝滑蜡笔", "art", 89.0, 0, "[]", "3-8 岁", "测试商品", 0, 1),
    ("pr_draw_bag", "儿童画具收纳包", "art", 59.0, 0, "[]", "4-10 岁", "测试商品", 0, 1),
    ("pr_watercolor", "固体水彩 24 色", "art", 79.0, 0, "[]", "5-12 岁", "测试商品", 0, 1),
    ("pr_puzzle_500", "500 片世界地图拼图", "puzzle", 129.0, 0, "[]", "6-10 岁", "测试商品", 0, 1),
    ("pr_puzzle_100", "100 片动物拼图", "puzzle", 45.0, 0, "[]", "3-6 岁", "测试商品", 0, 1),
    ("pr_blocks", "大颗粒积木桶", "toy", 199.0, 0, "[]", "3-6 岁", "测试商品", 0, 1),
    ("pr_books", "儿童科普绘本 4 册", "book", 118.0, 0, "[]", "5-9 岁", "测试商品", 0, 1),
    ("pr_ball", "4 号儿童足球", "sport", 99.0, 0, "[]", "5-10 岁", "测试商品", 1, 1),
    ("pr_water_bottle", "儿童保温水壶", "daily", 139.0, 0, "[]", "3-10 岁", "测试商品", 0, 1),
    ("pr_sneakers", "儿童运动鞋", "shoes", 259.0, 1, '["30 码","31 码","32 码","33 码"]',
     "4-8 岁", "测试商品，需选尺码", 0, 1),
    ("pr_headphones", "儿童护耳耳机", "digital", 249.0, 0, "[]", "5-12 岁", "测试商品", 0, 1),
]

# 家庭近况演示数据。两个标注的测试来源写同一个人的不同事件，
# 再加一条手表模拟数据。全部是虚构演示资料，不是真实设备或真实 App 的数据。
#
# 每个来源只报自己那部分字段，授权也按字段给：
# 学习 App 的进度可以给送礼者看，软件自己判定的「困难点」不给；
# 妈妈日常记录里她愿意分享的那句可以看，睡眠自述不给；
# 手表数据是设备推导，必须标出来，不能当成本人自述或医学判断。
DEMO_LIFE_EVENTS = [
    ("study_app", "app", "p_xiaoyu_b", "study_progress",
     {"已完成项目": "数学练习 20 题", "完成时间": "2026-10-12 19:30",
      "软件报告的困难点": "进位加法容易错"},
     "2026-10-12", "孩子本人在学习软件里确认",
     [("A", ["已完成项目", "完成时间"])]),
    ("mom_diary", "app", "p_xiaoyu_b", "daily_note",
     {"本人愿意分享": "今天自己整理了书包，说明天想画画",
      "睡眠自述": "九点前睡了"},
     "2026-10-12", "妈妈本人确认后分享",
     [("A", ["本人愿意分享"])]),
    ("watch_sync", "watch", "p_xiaoyu_b", "sleep",
     {"睡眠时长": "8.2 小时", "静息心率": "72"},
     "2026-10-11", "手表原厂 App 同步",
     [("A", ["睡眠时长"])]),
]

# 演示用的人物关联：两侧记录的是同一个人时，由本人确认后建立链接。
# 查近况、核对事实都走这条链接，不按名字匹配——
# 两个用户记录里同名的人不一定是同一个人。
DEMO_PERSON_LINKS = [
    ("p_xiaoyu", "p_xiaoyu_b", "送礼者记录的小雨和家长记录的女儿是同一个人"),
    ("p_xiaohan", "p_xiaohan_b", "送礼者记录的小宇和家长记录的儿子是同一个人"),
    ("p_xiaolin", "p_xiaolin_b", "送礼者认识的小林和家长记录的配偶是同一个人"),
]

AUTHORIZATIONS = [
    ("B", "share", "p_xiaoyu_b", '["likes", "喜欢颜色", "喜欢题材", "foot_length", "试穿记录",'
     ' "已有物品", "家长本次要求"]', "A", "2026-12-31",
     "允许把小雨的兴趣、颜色偏好、题材、脚长、试穿情况、已有物品和本次家长要求告诉 A，用于准备礼物。"),
    ("B", "share", "p_xiaohan_b", '["likes", "已有物品"]', "A", "2026-12-31",
     "允许把小宇的兴趣和已有物品告诉 A。"),
    ("B", "verify", "", "[]", "A", "2026-12-31",
     "允许核对 A 的方案是否与家里已有的重复、是否符合本次家长要求。"),
    ("A", "prepare", "", "[]", "", "", "允许 A 的 Agent 准备礼物方案。"),
    ("A", "purchase", "", "[]", "", "", "购买必须 A 本人确认后才执行。"),
    # 收货资料单独一条：覆盖到字段级，只授权本次代填要用的三个字段，
    # 不是把整份个人资料都交出去。
    ("A", "delivery", "", '["收货地址", "收货人", "收货电话"]', "", "",
     "允许把这三个收货字段用于已批准网页的代填。"),
]

def seed(db):
    """写入演示资料。已存在数据不重复写入，返回写入数量。"""
    counts = {"users": 0, "persons": 0, "relations": 0, "facts": 0, "gifts": 0,
              "products": 0, "auths": 0}
    existing = {r["owner_id"] for r in db.rows("SELECT owner_id FROM demo_user")}
    known_persons = {r["person_id"] for r in db.rows("SELECT person_id FROM person")}
    known_facts = {r["fact_id"] for r in db.rows("SELECT fact_id FROM fact")}
    known_gifts = {r["gift_id"] for r in db.rows("SELECT gift_id FROM gift_record")}
    known_products = {r["product_id"] for r in db.rows("SELECT product_id FROM product")}
    known_relations = {r["relation_id"] for r in db.rows("SELECT relation_id FROM relation")}
    with db.connect() as conn:
        for uid, name, role, blurb in DEMO_USERS:
            if uid not in existing:
                conn.execute(
                    "INSERT INTO demo_user(owner_id,name,role,blurb) VALUES(?,?,?,?)",
                    (uid, name, role, blurb))
                counts["users"] += 1
        for pid, owner, name, relation, note, aliases, attrs in PERSONS:
            if pid in known_persons:
                # 已存在的人物补齐后续版本新增的属性键，不覆盖用户改过的值。
                # 只在键缺失时补：把整份 attributes_json 覆盖回去会抹掉演示中的修改。
                cur = conn.execute(
                    "SELECT attributes_json FROM person WHERE person_id=?", (pid,)).fetchone()
                if cur is not None:
                    try:
                        cur_attrs = json.loads(cur["attributes_json"] or "{}")
                    except (ValueError, TypeError):
                        cur_attrs = {}
                    try:
                        want_attrs = json.loads(attrs or "{}")
                    except (ValueError, TypeError):
                        want_attrs = {}
                    added = {k: v for k, v in want_attrs.items() if k not in cur_attrs}
                    if added:
                        cur_attrs.update(added)
                        conn.execute(
                            "UPDATE person SET attributes_json=? WHERE person_id=?",
                            (json.dumps(cur_attrs, ensure_ascii=False), pid))
                continue
            conn.execute(
                "INSERT INTO person(person_id,owner_id,name,relation,note,aliases,attributes_json)"
                " VALUES(?,?,?,?,?,?,?)",
                (pid, owner, name, relation, note, aliases, attrs))
            counts["persons"] += 1
        for rid, owner, frm, rtype, to, label, src, observed in RELATIONS:
            if rid not in known_relations:
                conn.execute(
                    "INSERT INTO relation(relation_id,owner_id,from_person_id,relation_type,"
                    "to_person_id,label,source_utterance,observed_on) VALUES(?,?,?,?,?,?,?,?)",
                    (rid, owner, frm, rtype, to, label, src, observed))
                counts["relations"] += 1
        for fid, owner, subject, attr, value, src, provider, observed, status in FACTS:
            if fid not in known_facts:
                conn.execute(
                    "INSERT INTO fact(fact_id,owner_id,subject_id,attribute,value,source_utterance,"
                    "provider,observed_on,status) VALUES(?,?,?,?,?,?,?,?,?)",
                    (fid, owner, subject, attr, value, src, provider, observed, status))
                counts["facts"] += 1
        for gid, owner, subject, pid, pname, spec, occasion, status, fb, on in GIFTS:
            if gid not in known_gifts:
                conn.execute(
                    "INSERT INTO gift_record(gift_id,owner_id,subject_id,product_id,product_name,"
                    "spec,occasion,status,feedback,happened_on,simulated) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                    (gid, owner, subject, pid, pname, spec, occasion, status, fb, on))
                counts["gifts"] += 1
        for row in PRODUCTS:
            pid, name, cat, price, has_size, sizes, age, note = row[:8]
            makes_noise = row[8] if len(row) > 8 else 0
            easy_to_store = row[9] if len(row) > 9 else 1
            if pid not in known_products:
                conn.execute(
                    "INSERT INTO product(product_id,name,category,price_cny,has_size,size_options,"
                    "age_hint,note,makes_noise,easy_to_store) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (pid, name, cat, price, has_size, sizes, age, note,
                     makes_noise, easy_to_store))
                counts["products"] += 1
        # 授权按「同人同范围同说明」判重，不按整表是否为空。
        # 只判断整表为空时，往 AUTHORIZATIONS 追加新授权对已有库不生效，
        # 依赖新授权的功能（例如收货资料代填）会一直报缺参数。
        have_auth = {(r["owner_id"], r["scope_kind"], r["note"])
                     for r in db.rows("SELECT owner_id, scope_kind, note FROM authorization")}
        for owner, kind, subject, attrs, cp, until, note in AUTHORIZATIONS:
            if (owner, kind, note) in have_auth:
                continue
            conn.execute(
                "INSERT INTO authorization(auth_id,owner_id,scope_kind,subject_id,attributes_json,"
                "counterparty,valid_until,note) VALUES(?,?,?,?,?,?,?,?)",
                (new_id("auth"), owner, kind, subject, attrs, cp, until, note))
            counts["auths"] += 1
        # 家庭近况演示数据：两个标注的测试来源写同一个人的不同事件，
        # 加上一条手表模拟数据。都带来源、时间、提供者和按字段的授权。
        if db.rows("SELECT COUNT(*) AS n FROM life_event")[0]["n"] == 0:
            for ev in DEMO_LIFE_EVENTS:
                (src, kind, subj, etype, payload, on, provider, grants) = ev
                eid = new_id("evt")
                conn.execute(
                    "INSERT INTO life_event(event_id, source_id, source_kind, subject_id,"
                    " owner_id, event_type, payload_json, happened_on, provider, status,"
                    " created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, src, kind, subj, "B", etype,
                     json.dumps(payload, ensure_ascii=False), on, provider, "active",
                     now_iso()))
                for viewer, fields in grants:
                    conn.execute(
                        "INSERT INTO event_grant(grant_id, event_id, viewer_id, fields_json,"
                        " valid_until, note, created_at) VALUES(?,?,?,?,?,?,?)",
                        (new_id("grant"), eid, viewer,
                         json.dumps(fields, ensure_ascii=False), "", "", now_iso()))
                counts["life_events"] = counts.get("life_events", 0) + 1
        # 演示用的人物关联：两侧记录的是同一个孩子/同一位家长，由本人确认后建立。
        # 不按名字自动合并——两个用户记录里同名的人不一定是同一个人。
        if db.rows("SELECT COUNT(*) AS n FROM person_link")[0]["n"] == 0:
            for a_id, b_id, note in DEMO_PERSON_LINKS:
                conn.execute(
                    "INSERT INTO person_link(link_id, person_id_a, person_id_b,"
                    " confirmed_by, note, status, created_at) VALUES(?,?,?,?,?,?,?)",
                    (new_id("plink"), a_id, b_id, "B", note, "active", now_iso()))
                counts["person_links"] = counts.get("person_links", 0) + 1
    return counts


def model_config_get(db):
    """读当前模型配置。没有配过就返回空串，由调用方回落到环境变量。"""
    row = db.row("SELECT base_url, model_name, api_key, updated_at FROM model_config WHERE id=1")
    if row is None:
        return {"base_url": "", "model_name": "", "api_key": "", "updated_at": ""}
    return dict(row)


def model_config_set(db, base_url, model_name, api_key):
    """保存模型配置。None 或空串表示不覆盖原值，方便只改其中一项。

    api_key 的「空」一律按「保持不变」处理，只有显式调 model_config_clear_key
    才算清空。界面上密钥框留空是最常见的情况——表单总是把这个字段发出来，
    按「空串即覆盖」做的话，用户只是想改个模型名，密钥就被悄悄抹掉了，
    下一次请求全部变成匿名请求。要清空有专门的「清空密钥」按钮。
    """
    with db.connect() as conn:
        # 必须在同一个连接里读。db.row() 会另开一条连接，看不到本次事务里
        # 刚 INSERT OR IGNORE 出来的那一行，返回 None，第一次保存就直接
        # TypeError 崩掉。实测在全新库上就是这么挂的。
        conn.execute(
            "INSERT OR IGNORE INTO model_config(id, base_url, model_name, api_key,"
            " updated_at) VALUES(1,'','','','')")
        cur = conn.execute(
            "SELECT base_url, model_name, api_key FROM model_config WHERE id=1"
        ).fetchone()
        keep = lambda new, old: new if new else old
        conn.execute(
            "UPDATE model_config SET base_url=?, model_name=?, api_key=?,"
            " updated_at=? WHERE id=1",
            (keep(base_url, cur["base_url"]),
             keep(model_name, cur["model_name"]),
             keep(api_key, cur["api_key"]),
             now_iso()))


def model_config_clear_key(db):
    """显式清空密钥。"""
    with db.connect() as conn:
        conn.execute("UPDATE model_config SET api_key='', updated_at=? WHERE id=1",
                     (now_iso(),))


def reset_demo(db):
    """清空演示库，只留 seed 里的示例参数。

    表清单必须覆盖全部业务表。漏一张的后果是「重置了但测试痕迹还在」：
    之前就漏了 plan 四张表、order_candidate、event_attachment、
    event_task_link 和插件五张表，用户点完重置，侧栏里的事项和共同事项
    照样是测试里造出来的那几条，看着就像重置没生效。

    顺序按外键依赖从子到父，最后统一 commit；中途失败不回滚半份数据。
    """
    tables = (
        # 共同事项：先清表态和方案，再清参与方和事项本身。
        "plan_guest_session", "plan_invitation", "event_plan_link",
        "plan_decision", "plan_proposal", "plan_participant", "plan",
        # 送礼事项的从属数据。
        "order_candidate", "event_task_link", "event_attachment",
        "sim_order", "proposal", "message_log", "pending_action",
        "task",
        # 人物与授权。
        "person_link", "authorization", "event_grant", "event_notice",
        "life_event", "fact", "gift_record", "occasion", "relation",
        "person",
        # 插件链路。
        "ext_receipt", "ext_snapshot", "ext_device", "ext_pair_code",
        # 会话、审计、商品与用户放最后。
        "conversation", "audit_log", "product", "demo_user",
        # model_config 故意不在清单里：那是用户自己配的模型连接，
        # 不是演示数据。重置演示资料不该顺手把他的接口地址也清了。
    )
    with db.connect() as conn:
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in tables:
            if table in existing:
                conn.execute("DELETE FROM " + table)
    return seed(db)
