/**
 * 后台客户端的凭证轮换与存储边界。用 Node 内置 test runner，不引入外部依赖。
 *
 * 覆盖两条和「连接成本」直接相关的行为（交接方案第 5 节第 7 条）：
 *   1. 服务端在响应头里静默换发新凭证时，插件必须换掉本地那份，
 *      否则轮换后旧凭证在服务端已失效，用户没断开却被要求重新配对；
 *   2. 响应头没有这个字段时不能把本地凭证清空——空串写进去等于断开连接。
 *
 * 存储用假的 chrome.storage 注入：api.js 在加载时读一次 chrome.storage.local，
 * 所以必须在 require 之前把 global.chrome 摆好。
 *
 * 跑法：node --test extensions/context-bridge/test/
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const TOKEN_KEY = "cb_access_token";
const CLIENT_KEY = "cb_client";
const RENEWED_HEADER = "X-PA-Device-Token";

/** 假的 chrome.storage.local：保留 setAccessLevel 调用，便于确认收紧过。 */
function makeStorage() {
  const data = {};
  const access = { local: null, session: null };
  return {
    data,
    access,
    local: {
      get(keys, cb) {
        const out = {};
        (Array.isArray(keys) ? keys : [keys]).forEach((k) => {
          if (data[k] !== undefined) out[k] = data[k];
        });
        cb(out);
      },
      set(obj, cb) {
        Object.keys(obj).forEach((k) => { data[k] = obj[k]; });
        if (cb) cb();
      },
      remove(keys, cb) {
        (Array.isArray(keys) ? keys : [keys]).forEach((k) => { delete data[k]; });
        if (cb) cb();
      },
      setAccessLevel(opt) { access.local = opt.accessLevel; },
    },
  };
}

/** 假的 fetch：按脚本给出一轮响应，并记录每次请求带了什么凭证。 */
function makeFetch(script) {
  const seen = [];
  const impl = (url, options) => {
    seen.push({
      url,
      auth: (options && options.headers && options.headers.Authorization) || "",
    });
    const step = script.shift() || { status: 200, body: {}, headers: {} };
    const headers = new Map();
    Object.keys(step.headers || {}).forEach((k) => headers.set(k, step.headers[k]));
    return Promise.resolve({
      ok: step.status >= 200 && step.status < 300,
      status: step.status,
      headers: { get: (k) => headers.get(k) || "" },
      text: () => Promise.resolve(JSON.stringify(step.body === undefined ? {} : step.body)),
    });
  };
  return { impl, seen };
}

function loadApi(storage) {
  global.chrome = { storage: { local: storage.local, session: null }, runtime: { id: "x" } };
  const apiPath = path.join(__dirname, "..", "src", "lib", "api.js");
  delete require.cache[require.resolve(apiPath)];
  return require(apiPath);
}

test("响应头带回新凭证时，本地凭证被换成新的那一把", async () => {
  const storage = makeStorage();
  storage.data[TOKEN_KEY] = "old-token";
  const api = loadApi(storage);
  const { impl, seen } = makeFetch([
    { status: 200, body: { tasks: [] }, headers: { [RENEWED_HEADER]: "new-token" } },
  ]);
  const client = api.createClient({ fetchImpl: impl });

  const res = await client.listTasks();

  assert.equal(res.ok, true);
  // 第一次请求仍然带旧凭证：换发是服务端在这一次响应里告诉插件的。
  assert.equal(seen[0].auth, "Bearer old-token");
  // 之后本地必须是新凭证，否则下一次请求就是 401。
  assert.equal(storage.data[TOKEN_KEY], "new-token");
});

test("响应头没有新凭证时，不动本地凭证", async () => {
  const storage = makeStorage();
  storage.data[TOKEN_KEY] = "keep-me";
  const api = loadApi(storage);
  const { impl } = makeFetch([
    { status: 200, body: { tasks: [] }, headers: {} },
  ]);
  const client = api.createClient({ fetchImpl: impl });

  const res = await client.listTasks();

  assert.equal(res.ok, true);
  assert.equal(storage.data[TOKEN_KEY], "keep-me");
});

test("失败响应同样换凭证：轮换发生在服务端解析设备时，与本次成败无关", async () => {
  const storage = makeStorage();
  storage.data[TOKEN_KEY] = "old-token";
  const api = loadApi(storage);
  const { impl } = makeFetch([
    { status: 409, body: { error: { code: "conflict", message: "参数包已过期" } },
      headers: { [RENEWED_HEADER]: "new-token" } },
  ]);
  const client = api.createClient({ fetchImpl: impl });

  const res = await client.listTasks();

  assert.equal(res.ok, false);
  assert.equal(res.error.code, "conflict");
  assert.equal(storage.data[TOKEN_KEY], "new-token");
});

test("轮换后同一个 client 的后续请求带新凭证", async () => {
  const storage = makeStorage();
  storage.data[TOKEN_KEY] = "old-token";
  const api = loadApi(storage);
  const { impl, seen } = makeFetch([
    { status: 200, body: { tasks: [] }, headers: { [RENEWED_HEADER]: "new-token" } },
    { status: 200, body: { tasks: [] }, headers: {} },
  ]);
  const client = api.createClient({ fetchImpl: impl });

  await client.listTasks();
  await client.listTasks();

  assert.equal(seen[0].auth, "Bearer old-token");
  assert.equal(seen[1].auth, "Bearer new-token");
});
