/**
 * 契约 v1 的共享定义。扩展后台、页面适配器与测试都以这里为准，
 * 不允许各处各写一份字段清单——两处漂移就等于契约悄悄改了。
 *
 * 字段白名单按交接方案第 3 节冻结。适配器只处理名单内的字段，
 * 名单外的一律不填：参数包里出现未知键说明后端或网络出了问题，
 * 此时继续填就是把未授权内容写进页面。
 */

(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.CBContract = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  const CONTRACT_VERSION = "1";

  // 交接方案第 3 节冻结的字段白名单。
  const FIELD_KEYS = [
    "sku",
    "size",
    "color",
    "quantity",
    "recipient_name",
    "recipient_phone",
    "delivery_address",
  ];

  const FIELD_LABELS = {
    sku: "商品",
    size: "规格",
    color: "颜色",
    quantity: "数量",
    recipient_name: "收货人",
    recipient_phone: "收货电话",
    delivery_address: "收货地址",
  };

  // 适配器声明页面用哪种控件承载哪个字段。适配器与字段白名单的交集之外，
  // 即使后端给了值也不填。
  // sku 是只读控件：页面自己标注当前商品编号，插件只能核对、不能改。
  // 真实主服务的参数包会带 recipient_phone，适配器没有这个控件就永远 partial，
  // 所以这里必须把它列进来，缺控件时由页面侧明确报「没有这个字段的控件」。
  const ADAPTER_FIELDS = ["sku", "size", "color", "quantity",
                          "recipient_name", "recipient_phone", "delivery_address"];

  // 插件只能报告这三种页面结果，不说"购买成功"。
  const CLIENT_STATUS = ["filled", "partial", "failed"];

  // 需求项的状态只有两种：known 是已知并已核对的取值，pending 是还没确认的。
  // 界面必须把两者分开显示——把「对方还没回应」的鞋码画成确定规格，
  // 用户就会拿它去下单，而这正是这套机制要避免的事。
  const REQUIREMENT_STATES = ["known", "pending"];

  // 需求卡必须带的顶层字段。缺一项就说明后端给的不是完整需求，
  // 此时界面上的「复制这次需求」会漏内容，宁可报契约不符也不复制。
  const CONTEXT_REQUIRED_KEYS = [
    "task_id", "purpose", "context_revision", "subject_label",
    "task_status", "summary", "requirements", "missing_fields",
    "search_terms", "export_text", "source_summary", "generated_at",
  ];

  const ERROR_CODES = {
    UNAUTHORIZED: "unauthorized",
    FORBIDDEN: "forbidden",
    CONFLICT: "conflict",
    UNPROCESSABLE: "unprocessable",
    UNAVAILABLE: "unavailable",
    NETWORK: "network_error",
    CONTRACT: "contract_mismatch",
  };

  function isNonEmptyString(value) {
    return typeof value === "string" && value.trim().length > 0;
  }

  /**
   * 参数包入库前的结构检查。返回 {ok, problems}。
   * 只查结构和字段白名单，不判断业务正确性——业务版本对不对由服务端实时校验。
   */
  function validatePackage(pkg) {
    const problems = [];
    if (!pkg || typeof pkg !== "object") {
      return { ok: false, problems: ["参数包不是对象"] };
    }
    if (pkg.contract_version !== CONTRACT_VERSION) {
      problems.push("契约版本不是 " + CONTRACT_VERSION);
    }
    ["snapshot_id", "task_id", "action_type", "target_origin"].forEach((key) => {
      if (!isNonEmptyString(pkg[key])) {
        problems.push("缺少 " + key);
      }
    });
    if (typeof pkg.proposal_version !== "number" || pkg.proposal_version < 1) {
      problems.push("proposal_version 不是有效版本号");
    }
    if (!Array.isArray(pkg.fields)) {
      problems.push("fields 不是数组");
    }
    if (!Array.isArray(pkg.missing_fields)) {
      problems.push("missing_fields 不是数组");
    }
    if (typeof pkg.can_fill !== "boolean") {
      problems.push("can_fill 不是布尔值");
    }
    const fields = Array.isArray(pkg.fields) ? pkg.fields : [];
    const unknown = fields
      .filter((f) => !FIELD_KEYS.includes(f && f.key))
      .map((f) => f && f.key);
    if (unknown.length) {
      problems.push("参数包含白名单外字段：" + unknown.join("、"));
    }
    fields.forEach((f, i) => {
      if (!isNonEmptyString(f && f.key) || !isNonEmptyString(f && f.value)) {
        problems.push("第 " + (i + 1) + " 个字段缺少 key 或 value");
      }
    });
    const keys = fields.map((f) => f && f.key);
    const dup = keys.filter((k, i) => keys.indexOf(k) !== i);
    if (dup.length) {
      problems.push("字段重复：" + dup.join("、"));
    }
    if (Array.isArray(pkg.source_summary)) {
      pkg.source_summary.forEach((s, i) => {
        if (!isNonEmptyString(s && s.field) || !isNonEmptyString(s && s.source_label)) {
          problems.push("来源说明第 " + (i + 1) + " 条缺少 field 或 source_label");
        }
      });
    } else {
      problems.push("source_summary 不是数组");
    }
    return { ok: problems.length === 0, problems };
  }

  /** 从参数包里取出适配器能处理的字段，按白名单过滤。 */
  function fillableFields(pkg) {
    if (!pkg || !Array.isArray(pkg.fields)) return [];
    return pkg.fields.filter((f) => ADAPTER_FIELDS.includes(f.key) && isNonEmptyString(f.value));
  }

  /** 回执里的 observed_fields 只保留本次字段，键值都转成字符串，方便比对。 */
  function normalizeObserved(observed) {
    const out = {};
    if (!observed || typeof observed !== "object") return out;
    Object.keys(observed).forEach((k) => {
      const v = observed[k];
      out[k] = v === null || v === undefined ? "" : String(v);
    });
    return out;
  }

  /**
   * 比对预期字段与页面实际读回值。返回 {matched, mismatches}。
   * 数量这类字段允许 "1" 与 "1.0" 这类等价写法，其余按去空格后的字符串比。
   */
  function compareFields(expectedFields, observed) {
    const obs = normalizeObserved(observed);
    const mismatches = [];
    let compared = 0;
    (expectedFields || []).forEach((f) => {
      if (!obs[f.key] && obs[f.key] !== 0) {
        mismatches.push({ field: f.key, expected: f.value, observed: "", reason: "页面未读回该字段" });
        return;
      }
      compared += 1;
      const same = f.key === "quantity"
        ? Number(obs[f.key]) === Number(f.value)
        : String(obs[f.key]).trim() === String(f.value).trim();
      if (!same) {
        mismatches.push({ field: f.key, expected: f.value, observed: obs[f.key], reason: "与页面实际值不一致" });
      }
    });
    const extra = Object.keys(obs).filter((k) => !(expectedFields || []).some((f) => f.key === k));
    return {
      matched: mismatches.length === 0 && compared > 0,
      compared,
      mismatches,
      extra_fields: extra,
    };
  }

  /**
   * 需求卡（POST /api/extension/context 的返回）结构检查。返回 {ok, problems}。
   *
   * 这里只查结构和状态取值，不判断需求本身对不对——取值是否最新由服务端决定，
   * 复制前重新取一次版本就是为此。
   */
  function validateContext(ctx, expected) {
    const problems = [];
    if (!ctx || typeof ctx !== "object") {
      return { ok: false, problems: ["需求内容不是对象"] };
    }
    // 回的和要的是不是同一件事。异步返回时界面可能已经换了事项，
    // 这时旧响应若被当成新事项的内容用，用户就会复制到别人的需求。
    const want = expected || {};
    if (want.task_id && ctx.task_id !== want.task_id) {
      problems.push("返回的不是当前这件事（请求 " + want.task_id + "，返回 " + ctx.task_id + "）");
    }
    if (want.purpose && ctx.purpose !== want.purpose) {
      problems.push("返回的用途与请求不一致（请求 " + want.purpose + "，返回 " + ctx.purpose + "）");
    }
    CONTEXT_REQUIRED_KEYS.forEach((key) => {
      if (!(key in ctx)) { problems.push("缺少 " + key); }
    });
    ["task_id", "purpose", "context_revision", "generated_at"].forEach((key) => {
      if (!isNonEmptyString(ctx[key])) { problems.push(key + " 不是有效内容"); }
    });
    ["subject_label", "task_status", "summary", "export_text"].forEach((key) => {
      if (key in ctx && typeof ctx[key] !== "string") { problems.push(key + " 不是文本"); }
    });
    const reqs = Array.isArray(ctx.requirements) ? ctx.requirements : [];
    if (!Array.isArray(ctx.requirements)) { problems.push("requirements 不是数组"); }
    const seen = {};
    reqs.forEach((r, i) => {
      const at = "第 " + (i + 1) + " 项需求";
      if (!isNonEmptyString(r && r.key)) { problems.push(at + "缺少 key"); return; }
      if (typeof r.label !== "string") { problems.push(at + "的 label 不是文本"); }
      if (typeof r.value !== "string") { problems.push(at + "的 value 不是文本"); }
      if (!REQUIREMENT_STATES.includes(r && r.state)) {
        problems.push(at + "的 state 只能是 known 或 pending，当前是 " + JSON.stringify(r && r.state));
        return;
      }
      if (r.state === "known" && !isNonEmptyString(r.value)) {
        problems.push(at + "标为已知却没有取值，不能当确定规格用");
      }
      if (seen[r.key]) { problems.push("需求重复：" + r.key); }
      seen[r.key] = true;
    });
    if (!Array.isArray(ctx.missing_fields)) { problems.push("missing_fields 不是数组"); }
    else {
      ctx.missing_fields.forEach((m, i) => {
        if (typeof m !== "string" || !m.trim()) { problems.push("missing_fields 第 " + (i + 1) + " 项不是有效内容"); }
      });
    }
    if (!Array.isArray(ctx.search_terms)) { problems.push("search_terms 不是数组"); }
    else {
      ctx.search_terms.forEach((s, i) => {
        if (!isNonEmptyString(s)) { problems.push("search_terms 第 " + (i + 1) + " 项不是有效搜索词"); }
      });
    }
    if (!Array.isArray(ctx.source_summary)) { problems.push("source_summary 不是数组"); }
    else {
      ctx.source_summary.forEach((s, i) => {
        if (!isNonEmptyString(s && s.field) || !isNonEmptyString(s && s.source_label)) {
          problems.push("来源说明第 " + (i + 1) + " 条缺少 field 或 source_label");
        }
      });
    }
    return { ok: problems.length === 0, problems };
  }

  /**
   * 两份需求卡的差异。复制前重新取到新版本时，用它把变化摊给用户看，
   * 而不是悄悄复制一份和界面上不一样的内容。
   */
  function contextDiff(before, after) {
    const items = [];
    const prev = {};
    (before && Array.isArray(before.requirements) ? before.requirements : [])
      .forEach((r) => { prev[r.key] = r; });
    const next = {};
    (after && Array.isArray(after.requirements) ? after.requirements : [])
      .forEach((r) => { next[r.key] = r; });
    Object.keys(next).forEach((key) => {
      const b = prev[key];
      const a = next[key];
      const label = a.label || key;
      if (!b) {
        items.push({ key, label, from: "", to: a.value || "（待确认）", kind: "新增" });
        return;
      }
      if (b.value !== a.value || b.state !== a.state) {
        const show = (r) => (r.value || "（待确认）") + (r.state === "pending" ? "（待确认）" : "");
        items.push({ key, label, from: show(b), to: show(a), kind: "取值变化" });
      }
    });
    Object.keys(prev).forEach((key) => {
      if (!next[key]) {
        items.push({ key, label: prev[key].label || key, from: prev[key].value || "（待确认）", to: "", kind: "已移除" });
      }
    });
    const beforeTerms = (before && Array.isArray(before.search_terms) ? before.search_terms : []).join("、");
    const afterTerms = (after && Array.isArray(after.search_terms) ? after.search_terms : []).join("、");
    if (beforeTerms !== afterTerms) {
      items.push({ key: "search_terms", label: "搜索词", from: beforeTerms, to: afterTerms, kind: "取值变化" });
    }
    if ((before && before.export_text) !== (after && after.export_text)) {
      items.push({ key: "export_text", label: "需求说明", from: "（原文见上）", to: "（原文见上）", kind: "取值变化" });
    }
    return { changed: items.length > 0, items };
  }

  /** 复制用的搜索词文本：一行一个，用户粘到别处也好读。 */
  function searchTermsText(ctx) {
    const terms = (ctx && Array.isArray(ctx.search_terms) ? ctx.search_terms : []).filter((s) => isNonEmptyString(s));
    return terms.join("\n");
  }

  /** 复制用的需求说明。空内容不复制，避免用户粘一个空串还以为成功了。 */
  function exportText(ctx) {
    const text = (ctx && typeof ctx.export_text === "string") ? ctx.export_text.trim() : "";
    return text;
  }

  return {
    CONTRACT_VERSION,
    FIELD_KEYS,
    FIELD_LABELS,
    ADAPTER_FIELDS,
    CLIENT_STATUS,
    ERROR_CODES,
    isNonEmptyString,
    validatePackage,
    fillableFields,
    normalizeObserved,
    compareFields,
    REQUIREMENT_STATES,
    CONTEXT_REQUIRED_KEYS,
    validateContext,
    contextDiff,
    searchTermsText,
    exportText,
  };
});
