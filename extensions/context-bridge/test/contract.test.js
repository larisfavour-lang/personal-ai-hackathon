/**
 * 契约库与比对逻辑的单元测试。用 Node 内置 test runner，不引入外部依赖。
 *
 * 跑法：node --test extensions/context-bridge/test/
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const C = require(path.join(__dirname, "..", "src", "lib", "contract.js"));

function baseCtx(over) {
  return Object.assign({
    task_id: "task-1",
    purpose: "shopping_help",
    context_revision: "ctx-1",
    subject_label: "小雨",
    task_status: "agreed",
    summary: "300 元以内的儿童运动鞋",
    requirements: [
      { key: "budget", label: "预算", value: "300 元以内", state: "known" },
      { key: "shoe_size", label: "鞋码", value: "", state: "pending" },
    ],
    missing_fields: ["shoe_size"],
    search_terms: ["儿童运动鞋 32码"],
    export_text: "预算 300 元以内",
    source_summary: [{ field: "budget", source_label: "本人交办时说明", confirmed_at: "2026-10-15T00:00:00Z" }],
    generated_at: "2026-10-15T00:00:00Z",
  }, over || {});
}

function basePkg(over) {
  return Object.assign({
    contract_version: "1",
    snapshot_id: "snap-1",
    task_id: "task-1",
    proposal_version: 2,
    action_type: "shopping_fill",
    target_origin: "http://127.0.0.1:8097",
    subject_label: "小雨",
    product: { id: "pr_sneakers", title: "儿童运动鞋" },
    fields: [
      { key: "size", value: "32 码", label: "规格" },
      { key: "quantity", value: "1", label: "数量" },
    ],
    source_summary: [{ field: "size", source_label: "家长本人核对", confirmed_at: "2026-10-14T00:00:00Z" }],
    missing_fields: [],
    can_fill: true,
    expires_at: "2026-10-14T12:00:00Z",
  }, over || {});
}

test("合法参数包通过校验", () => {
  const r = C.validatePackage(basePkg());
  assert.equal(r.ok, true, r.problems.join("；"));
});

test("契约版本不符被拒绝", () => {
  const r = C.validatePackage(basePkg({ contract_version: "2" }));
  assert.equal(r.ok, false);
  assert.ok(r.problems.some((p) => p.includes("契约版本")));
});

test("白名单外字段被拒绝，而不是照填", () => {
  const r = C.validatePackage(basePkg({
    fields: [{ key: "id_card", value: "310101..." }],
  }));
  assert.equal(r.ok, false);
  assert.ok(r.problems.some((p) => p.includes("白名单外字段")));
});

test("缺 snapshot_id 被拒绝", () => {
  const r = C.validatePackage(basePkg({ snapshot_id: "" }));
  assert.equal(r.ok, false);
  assert.ok(r.problems.some((p) => p.includes("snapshot_id")));
});

test("重复字段键被拒绝", () => {
  const r = C.validatePackage(basePkg({
    fields: [{ key: "size", value: "32 码" }, { key: "size", value: "33 码" }],
  }));
  assert.equal(r.ok, false);
  assert.ok(r.problems.some((p) => p.includes("字段重复")));
});

test("can_fill=false 时 fillableFields 仍只给有值的字段", () => {
  const pkg = basePkg({
    fields: [{ key: "size", value: "32 码" }, { key: "quantity", value: "" }],
    can_fill: false,
  });
  const got = C.fillableFields(pkg);
  assert.deepEqual(got.map((f) => f.key), ["size"]);
});

test("字段比对：一致时 matched", () => {
  const expected = [{ key: "size", value: "32 码" }, { key: "quantity", value: "1" }];
  const r = C.compareFields(expected, { size: "32 码", quantity: "1" });
  assert.equal(r.matched, true);
  assert.equal(r.mismatches.length, 0);
});

test("字段比对：数量允许 1 与 1.0 等价", () => {
  const expected = [{ key: "quantity", value: "1" }];
  const r = C.compareFields(expected, { quantity: "1.0" });
  assert.equal(r.matched, true);
});

test("字段比对：值不一致时逐项给出差异", () => {
  const expected = [{ key: "size", value: "32 码" }];
  const r = C.compareFields(expected, { size: "33 码" });
  assert.equal(r.matched, false);
  assert.equal(r.mismatches[0].field, "size");
  assert.equal(r.mismatches[0].expected, "32 码");
  assert.equal(r.mismatches[0].observed, "33 码");
});

test("字段比对：页面没读回该字段算未处理", () => {
  const expected = [{ key: "delivery_address", value: "上海市徐汇区示例路 1 号" }];
  const r = C.compareFields(expected, {});
  assert.equal(r.matched, false);
  assert.equal(r.mismatches[0].reason, "页面未读回该字段");
});

test("页面多出的字段被标出来，便于发现插件误填", () => {
  const expected = [{ key: "size", value: "32 码" }];
  const r = C.compareFields(expected, { size: "32 码", note: "x" });
  assert.deepEqual(r.extra_fields, ["note"]);
});

test("字段标签全部有中文名", () => {
  C.FIELD_KEYS.forEach((k) => {
    assert.ok(C.FIELD_LABELS[k] && /[\u4e00-\u9fa5]/.test(C.FIELD_LABELS[k]), k + " 缺中文标签");
  });
});

test("完整需求卡通过校验", () => {
  const r = C.validateContext(baseCtx());
  assert.equal(r.ok, true, r.problems.join("；"));
});

test("需求卡缺字段被逐项指出", () => {
  const ctx = baseCtx();
  delete ctx.search_terms;
  delete ctx.generated_at;
  const r = C.validateContext(ctx);
  assert.equal(r.ok, false);
  assert.ok(r.problems.some((p) => p.includes("search_terms")));
  assert.ok(r.problems.some((p) => p.includes("generated_at")));
});

test("待确认项被标成 known 却没取值时拒绝，不当地确定规格用", () => {
  const r = C.validateContext(baseCtx({
    requirements: [{ key: "shoe_size", label: "鞋码", value: "", state: "known" }],
  }));
  assert.equal(r.ok, false);
  assert.ok(r.problems.some((p) => p.includes("标为已知却没有取值")));
});

test("state 取值超出 known/pending 被拒绝", () => {
  const r = C.validateContext(baseCtx({
    requirements: [{ key: "x", label: "某", value: "1", state: "maybe" }],
  }));
  assert.equal(r.ok, false);
  assert.ok(r.problems.some((p) => p.includes("known 或 pending")));
});

test("重复的需求键被拒绝", () => {
  const r = C.validateContext(baseCtx({
    requirements: [
      { key: "budget", label: "预算", value: "300", state: "known" },
      { key: "budget", label: "预算", value: "250", state: "known" },
    ],
  }));
  assert.equal(r.ok, false);
  assert.ok(r.problems.some((p) => p.includes("需求重复")));
});

test("返回的不是请求的那件事时拒绝，避免复制到别人的需求", () => {
  const r = C.validateContext(baseCtx({ task_id: "task-2" }),
                              { task_id: "task-1", purpose: "shopping_help" });
  assert.equal(r.ok, false);
  assert.ok(r.problems.some((p) => p.includes("返回的不是当前这件事")));
});

test("返回的用途与请求不一致时拒绝", () => {
  const r = C.validateContext(baseCtx({ purpose: "other_help" }),
                              { task_id: "task-1", purpose: "shopping_help" });
  assert.equal(r.ok, false);
  assert.ok(r.problems.some((p) => p.includes("用途与请求不一致")));
});

test("task_id 与 purpose 都对得上时通过", () => {
  const r = C.validateContext(baseCtx(), { task_id: "task-1", purpose: "shopping_help" });
  assert.equal(r.ok, true, r.problems.join("；"));
});

test("两份需求无差异时 changed 为假", () => {
  const a = baseCtx();
  const r = C.contextDiff(a, JSON.parse(JSON.stringify(a)));
  assert.equal(r.changed, false);
  assert.deepEqual(r.items, []);
});

test("取值变化被逐项列出来，供用户核对", () => {
  const before = baseCtx();
  const after = baseCtx({
    requirements: [
      { key: "budget", label: "预算", value: "250 元以内", state: "known" },
      { key: "shoe_size", label: "鞋码", value: "", state: "pending" },
    ],
  });
  const r = C.contextDiff(before, after);
  assert.equal(r.changed, true);
  assert.equal(r.items.length, 1);
  assert.deepEqual(r.items[0], {
    key: "budget", label: "预算", from: "300 元以内", to: "250 元以内", kind: "取值变化",
  });
});

test("待确认项变成已知时也算变化", () => {
  const before = baseCtx();
  const after = baseCtx({
    requirements: [
      { key: "budget", label: "预算", value: "300 元以内", state: "known" },
      { key: "shoe_size", label: "鞋码", value: "32 码", state: "known" },
    ],
  });
  const r = C.contextDiff(before, after);
  assert.equal(r.changed, true);
  assert.equal(r.items[0].key, "shoe_size");
  assert.ok(r.items[0].to.includes("32 码"));
});

test("搜索词变化也算变化", () => {
  const before = baseCtx();
  const after = baseCtx({ search_terms: ["儿童运动鞋 33码"] });
  const r = C.contextDiff(before, after);
  assert.equal(r.changed, true);
  assert.ok(r.items.some((i) => i.key === "search_terms"));
});

test("需求被移除时也算变化", () => {
  const before = baseCtx();
  const after = baseCtx({ requirements: [before.requirements[0]] });
  const r = C.contextDiff(before, after);
  assert.equal(r.changed, true);
  assert.deepEqual(r.items[0].kind, "已移除");
});

test("搜索词文本一行一个，空词被滤掉", () => {
  const text = C.searchTermsText(baseCtx({ search_terms: ["儿童运动鞋", "", "   "] }));
  assert.equal(text, "儿童运动鞋");
});

test("需求说明为空时不复制", () => {
  assert.equal(C.exportText(baseCtx({ export_text: "   " })), "");
});

test("适配器字段是白名单子集", () => {
  C.ADAPTER_FIELDS.forEach((k) => assert.ok(C.FIELD_KEYS.includes(k), k + " 不在白名单"));
});

// ---------------------------------------------------------------------------
// 页面内容读取器（src/content/page-reader.js）
//
// 这一组验的是硬边界，不是实现细节：用户填进输入框的值、密码、
// 页面藏起来的内容，一律不能出现在读取结果里。少了任何一条，
// 「读这一页」就会把用户隐私一起读走。
// ---------------------------------------------------------------------------

const fsx = require("fs");
const pathx = require("path");
const Modulex = require("module");

/**
 * 不需要完整 DOM：直接构造 reader 依赖的那几个节点接口。
 * textContent 通过一个 TEXT_NODE 子节点提供——读取器读的是 childNodes，
 * 和真实浏览器一致；直接挂 textContent 属性是造了个浏览器里不存在的结构。
 */
function el(tag, opts = {}) {
  const node = {
    tagName: tag.toUpperCase(),
    nodeType: 1,
    children: [],
    childNodes: [],
    parentElement: null,
    ownerDocument: { defaultView: { getComputedStyle: () => node.__style || { display: "block", visibility: "visible" } } },
    getAttribute: (k) => (node.__attrs && node.__attrs[k]) || null,
    getBoundingClientRect: () => ({ width: node.__style && node.__style.display === "none" ? 0 : 100, height: 20 }),
    textContent: opts.textContent || "",
    ...opts,
  };
  if (opts.textContent) {
    const text = { nodeType: 3, textContent: opts.textContent };
    node.childNodes = [text];
    // 元素自身的文字来自文本子节点；有子元素时由各子元素分别贡献。
    if (opts.children && opts.children.length) {
      node.childNodes = node.childNodes.concat(opts.children);
    }
  } else if (opts.children) {
    node.childNodes = opts.children.slice();
  }
  return node;
}

function loadReader() {
  const file = pathx.join(__dirname, "..", "src", "content", "page-reader.js");
  const src = fsx.readFileSync(file, "utf8");
  const mod = new Modulex("page-reader");
  mod._compile(src, file);
  return mod.exports;
}

test("读取器不读输入框里用户已填的值，只读控件说明", () => {
  const reader = loadReader();
  const input = el("input", { type: "text", value: "13800000000", labels: [] });
  const label = el("label", { textContent: "联系电话" });
  input.labels = [label];
  const out = reader.__testReadFrom([input]);
  const text = out.texts.map((t) => t.text).join(" ");
  assert.ok(text.includes("联系电话"), "应读到控件说明：" + text);
  assert.ok(!text.includes("13800000000"), "不能读到用户填的电话：" + text);
});

test("读取器不读密码控件", () => {
  const reader = loadReader();
  const label = el("label", { textContent: "登录密码" });
  const pwd = el("input", { type: "password", value: "secret123", labels: [label] });
  const out = reader.__testReadFrom([pwd]);
  const text = out.texts.map((t) => t.text).join(" ");
  assert.ok(!text.includes("secret123"), "密码值不能出现：" + text);
  assert.ok(!text.includes("登录密码"), "密码控件整块跳过：" + text);
});

test("读取器不读 display:none 的隐藏内容", () => {
  const reader = loadReader();
  const hidden = el("div", { textContent: "隐藏文字不应出现" });
  hidden.__style = { display: "none", visibility: "visible" };
  const out = reader.__testReadFrom([hidden]);
  const text = out.texts.map((t) => t.text).join(" ");
  assert.ok(!text.includes("隐藏文字不应出现"), "隐藏内容不能读：" + text);
  assert.ok(out.hidden_count >= 1, "隐藏块要计数");
});

test("读取器不读 aria-hidden 与 script/style", () => {
  const reader = loadReader();
  const script = el("script", { textContent: "var token='abc'" });
  const style = el("style", { textContent: "body{color:red}" });
  const aria = el("span", { textContent: "读屏专用文本" });
  aria.__attrs = { "aria-hidden": "true" };
  const out = reader.__testReadFrom([script, style, aria]);
  const text = out.texts.map((t) => t.text).join(" ");
  assert.ok(!text.includes("var token"), "脚本内容不能读");
  assert.ok(!text.includes("body{color"), "样式不能读");
  assert.ok(!text.includes("读屏专用文本"), "aria-hidden 不能读");
});

test("读取器读页面正文，并按页面顺序去重", () => {
  const reader = loadReader();
  const p1 = el("p", { textContent: "先量孩子现在穿的鞋垫长度。" });
  const p2 = el("p", { textContent: "支持七天无理由退换。" });
  const out = reader.__testReadFrom([p1, p2, p1]);
  assert.deepStrictEqual(out.texts.map((t) => t.text),
    ["先量孩子现在穿的鞋垫长度。", "支持七天无理由退换。"]);
});

test("pageText 带标题与地址，用户能确认读的是哪一页", () => {
  const reader = loadReader();
  const text = reader.pageText({
    title: "选购说明",
    url: "http://127.0.0.1:8097/plain.html",
    texts: [{ kind: "正文", text: "先量脚长。" }],
  });
  assert.ok(text.includes("页面标题：选购说明"));
  assert.ok(text.includes("http://127.0.0.1:8097/plain.html"));
  assert.ok(text.includes("先量脚长。"));
});
