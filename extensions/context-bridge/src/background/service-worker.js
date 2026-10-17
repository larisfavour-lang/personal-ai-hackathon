/**
 * 后台 service worker：唯一和核心服务对话的地方。
 *
 * popup 和 content script 都只发消息给它，由它统一带凭证请求。
 * 网页脚本拿不到 token，也无法让后台替它访问任意接口——
 * 每个动作都要对应一次用户看过的操作。
 *
 * 一条代填的生命周期是固定的：
 *   预览（prepare）→ 绑定用户眼前这一页（bind-page）
 *   → 用户点「确认并代填」→ 服务端实时校验（validate）
 *   → 写页面（fill）→ 回读页面真实状态（read-state）→ 回执（receipt）
 * 中途页面被关掉或跳走，绑定立即失效，必须重新预览，不把参数写到别的页上。
 */

importScripts("../lib/contract.js", "../lib/config.js", "../lib/api.js");

const api = CBApi.createClient();

/** 页面适配器注册表：适配器自己声明能填哪些字段，后台不猜。 */
// 一个 host 一条：对象字面量里写重复键，后一条会静默覆盖前一条，
// 增删端口时很容易漏改其中一处，所以这里每个 host 只出现一次。
const ADAPTERS = {
  // 真实联调：核心服务 EXT_ALLOWED_ORIGINS 只放行 9000 的模拟商店。
  "127.0.0.1:8097": {
    adapterId: "mock-shop-v1",
    fields: CBContract.ADAPTER_FIELDS,
  },
  "localhost:8097": {
    adapterId: "mock-shop-v1",
    fields: CBContract.ADAPTER_FIELDS,
  },
  "127.0.0.1:9000": {
    adapterId: "mock-shop-v1",
    fields: CBContract.ADAPTER_FIELDS,
  },
  "localhost:9000": {
    adapterId: "mock-shop-v1",
    fields: CBContract.ADAPTER_FIELDS,
  },
};

/**
 * 只有扩展自身页面（popup、后台）能发起身份、授权与代填动作。
 * 商店页面里的 content script 即使拿到消息通道，也只能读页面状态，
 * 不能反过来驱动配对或代填——否则网页脚本就成了插件的遥控器。
 */
const PRIVILEGED = new Set([
  "cb:pair", "cb:revoke", "cb:tasks", "cb:prepare", "cb:bind-page",
  "cb:fill", "cb:receipt", "cb:validate", "cb:context", "cb:read-page",
]);
// cb:context 是特权动作：它读的是同一个后端需求接口，只能由扩展页面发起。
// 网页脚本即使拿到消息通道也调不到——否则任意商店页都能替用户取需求。
const READ_ONLY = new Set(["cb:read-state", "cb:status", "cb:find-target"]);

/** 本次代填会话绑定的页面。预览时定下来，之后读、填、回读、回执都只认这一页。 */
let bound = null; // {tabId, origin, productId, snapshotId}

function resetBinding() {
  bound = null;
}

function bindingFor(snapshotId) {
  return bound && bound.snapshotId === snapshotId ? bound : null;
}

function adapterFor(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch (err) {
    return null;
  }
  return ADAPTERS[parsed.host] || null;
}

function reply(sendResponse, result) {
  try {
    sendResponse(result);
  } catch (err) {
    // 页面可能已经关掉，忽略即可。
  }
}

function errorPayload(err) {
  return {
    ok: false,
    error: {
      code: (err && err.code) || CBContract.ERROR_CODES.UNAVAILABLE,
      message: (err && err.message) || "未知错误",
      retryable: Boolean(err && err.retryable),
    },
  };
}

/** 预览阶段：把用户眼前这一页绑进会话。 */
/**
 * 预览阶段：绑定用户指定的那一页。
 *
 * tabId 必须来自用户眼前的标签页，不能由后台「搜第一个适配页」决定：
 * 同时开着两个商品页时，猜中的可能不是用户在看的那个。
 * 没给 tabId 就拒绝，让调用方（popup）先问清是哪一页。
 */
async function bindTargetTab(snapshotId, tabId) {
  if (typeof chrome === "undefined" || !chrome.tabs) {
    return errorPayload({ code: "no_tabs", message: "当前环境没有标签页接口" });
  }
  if (typeof tabId !== "number") {
    return errorPayload({ code: "no_target_tab", message: "没有指定要代填的标签页，请重新预览", retryable: false });
  }
  let tab = null;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (err) {
    return errorPayload({ code: "tab_gone", message: "这个标签页已关闭", retryable: false });
  }
  if (!tab || !tab.url || !adapterFor(tab.url)) {
    return errorPayload({ code: "no_adapter", message: "这个页面没有适配器，请在模拟商店打开商品页", retryable: false });
  }
  const state = await readPageState(tab.id);
  if (!state.ok) {
    return errorPayload(state.error);
  }
  bound = {
    tabId: tab.id,
    origin: new URL(tab.url).origin,
    url: tab.url,
    // 预览时读到的商品只作展示；代填前会重新读页面实际值再比。
    previewProductId: state.data.product_id || "",
    snapshotId,
  };
  return { ok: true, data: bound };
}

/**
 * 页面还是不是预览那一页：同源但换了商品也要认出来。
 * 只看 origin 不够——同一标签里从运动鞋跳到绘画套装，origin 没变，
 * 参数包还是运动鞋的，这时必须拒绝而不是接着写。
 */
async function tabStillBound(tabId, origin, expectedProductId) {
  if (typeof chrome === "undefined" || !chrome.tabs) return false;
  let tab = null;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (err) {
    return false;
  }
  if (!tab || !tab.url) return false;
  let parsed = null;
  try {
    parsed = new URL(tab.url);
  } catch (err) {
    return false;
  }
  if (parsed.origin !== origin) return false;
  if (!adapterFor(tab.url)) return false;
  if (expectedProductId) {
    const state = await readPageState(tabId);
    if (!state.ok) return false;
    if ((state.data.product_id || "") !== expectedProductId) return false;
  }
  return true;
}

/**
 * 用户在看的那个适配页。绑定目标只从「当前窗口的当前网页」来。
 *
 * 规则（按监督要求，不猜、不跨窗口、不用末项兜底）：
 * - 只看当前窗口的标签页，别的窗口再活跃也不算；
 * - 取当前窗口的激活标签页；
 * - 该页是适配页就用它；不是适配页就明确失败，
 *   让用户先打开商品页——不能顺手选到另一个商店；
 * - 只有一种例外：当前窗口里恰好只有一个适配页时，可以认为是它，
 *   这时用户没有第二个可选，不存在猜错的可能。
 */
async function activeAdapterTab(excludeTabId) {
  if (typeof chrome === "undefined" || !chrome.tabs) return null;
  const tabs = await chrome.tabs.query({ currentWindow: true });
  const mine = tabs.filter((t) => !(typeof excludeTabId === "number" && t.id === excludeTabId));
  const candidates = mine.filter((t) => t.url && adapterFor(t.url));
  const activeTab = mine.find((t) => t.active) || null;

  if (activeTab && activeTab.url && adapterFor(activeTab.url)) {
    return { tab: activeTab, adapter: adapterFor(activeTab.url), count: candidates.length };
  }
  if (candidates.length === 1) {
    return { tab: candidates[0], adapter: adapterFor(candidates[0].url), count: 1 };
  }
  // 当前页不是适配页，或多个适配页又分不出是哪个：都要用户明确选。
  return {
    needsChoice: true,
    count: candidates.length,
    activeIsAdapter: Boolean(activeTab && activeTab.url && adapterFor(activeTab.url)),
    choices: await Promise.all(candidates.map(async (t) => {
      const state = await readPageState(t.id);
      return {
        tabId: t.id,
        url: t.url,
        productId: (state.ok && state.data.product_id) || "",
        title: (state.ok && state.data.page_status && state.data.page_status.title) || "",
      };
    })),
  };
}


function readPageState(tabId) {
  return new Promise((resolve) => {
    if (typeof chrome === "undefined" || !chrome.tabs) {
      resolve(errorPayload({ code: "no_tabs", message: "当前环境没有标签页接口" }));
      return;
    }
    chrome.tabs.sendMessage(tabId, { type: "cb:read-state" }, (resp) => {
      const err = chrome.runtime.lastError;
      if (err || !resp) {
        resolve(errorPayload({ code: "no_adapter", message: "这个页面没有加载适配器，请先在模拟商店打开商品页", retryable: false }));
        return;
      }
      resolve({ ok: true, data: resp.state || {} });
    });
  });
}

/**
 * 读一个标签页的页面内容。
 *
 * 两条路，按顺序试：
 *   1. 这个页面已经加载过 page-reader（manifest 的 content_scripts 会注入），
 *      直接发消息问它；
 *   2. 没注入过（比如页面比插件先打开、或注入失败），用 scripting 现注一个。
 *      注入的是读取器本身，不执行页面里的任何脚本，也不带参数。
 *
 * 都不通就返回明确的错误码，让界面说「这一页读不了」，
 * 不猜内容、不用缓存顶替——缓存的那份不是用户此刻看到的页面。
 */
async function readPageContent(tabId) {
  const direct = await askContent(tabId, { type: "cb:read-page" });
  if (direct && direct.ok) {
    return { ok: true, data: direct.data || {}, text: direct.text || "" };
  }
  // 页面没有回应读取器时才现注入。若刚才是读取失败，注入也不会变好，
  // 还会把同一次点击拖慢——所以这里只认「没有回应」这一种情况。
  if (direct && direct.ok === false && direct.error && direct.error.code === "read_failed") {
    return { ok: false, error: direct.error };
  }
  if (typeof chrome === "undefined" || !chrome.scripting || !chrome.scripting.executeScript) {
    return { ok: false, error: { code: "no_scripting", message: "当前浏览器版本不支持按需注入，读不了这一页", retryable: false } };
  }
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["src/content/page-reader.js"],
    });
  } catch (err) {
    return {
      ok: false,
      error: {
        code: "inject_failed",
        message: "这一页不让注入脚本，读不了：" + ((err && err.message) || "未知原因"),
        retryable: false,
      },
    };
  }
  const again = await askContent(tabId, { type: "cb:read-page" });
  if (again && again.ok) {
    return { ok: true, data: again.data || {}, text: again.text || "" };
  }
  return {
    ok: false,
    error: (again && again.error) || { code: "read_failed", message: "这一页没有返回内容", retryable: true },
  };
}

function askContent(tabId, message) {
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tabId, message, (resp) => {
      const err = chrome.runtime.lastError;
      if (err || !resp) {
        resolve(errorPayload({ code: "no_adapter", message: "页面没有回应，请确认商品页已打开" }));
        return;
      }
      resolve(resp);
    });
  });
}

function handleMessage(message, sender, sendResponse) {
  if (!message || typeof message.type !== "string") return false;

  if (sender && sender.id && sender.id !== chrome.runtime.id) {
    reply(sendResponse, errorPayload({ code: "forbidden", message: "非本扩展来源的请求已忽略" }));
    return false;
  }
  // 判断依据是消息来自网页还是扩展页面，不是有没有 tab：
  // 插件面板也可能以标签页形式打开，那时 sender.tab 同样存在，
  // 但 sender.url 是 chrome-extension:// 开头。
  const fromWebPage = Boolean(
    sender && sender.tab && typeof sender.url === "string"
    && !sender.url.startsWith("chrome-extension://"));
  if (PRIVILEGED.has(message.type) && fromWebPage) {
    reply(sendResponse, errorPayload({ code: "forbidden", message: "网页脚本不能发起这个操作，请在插件面板里操作" }));
    return false;
  }
  if (!PRIVILEGED.has(message.type) && !READ_ONLY.has(message.type)) {
    reply(sendResponse, errorPayload({ code: "unknown_message", message: "不支持的请求：" + message.type }));
    return false;
  }

  (async () => {
    switch (message.type) {
      case "cb:status": {
        const client = await api.getClient();
        reply(sendResponse, { ok: true, data: { paired: Boolean(client), client, core: api.getCoreOrigin() } });
        return;
      }
      case "cb:pair": {
        const res = await api.pair(message.code, message.client_name);
        reply(sendResponse, res.ok ? { ok: true, data: res.data } : errorPayload(res.error));
        return;
      }
      case "cb:revoke": {
        const res = await api.revoke();
        resetBinding();
        reply(sendResponse, res.ok ? { ok: true, data: { revoked: true } } : errorPayload(res.error));
        return;
      }
      case "cb:tasks": {
        const res = await api.listTasks();
        reply(sendResponse, res.ok ? { ok: true, data: res.data } : errorPayload(res.error));
        return;
      }
      case "cb:context": {
        // task_id 必须是插件自己从 /api/extension/tasks 取到的那个，
        // 不能由页面指定：否则网页脚本就能借用户的凭证读别的事项。
        const res = await api.context(message.task_id, message.purpose);
        reply(sendResponse, res.ok ? { ok: true, data: res.data } : errorPayload(res.error));
        return;
      }
      case "cb:prepare": {
        resetBinding();
        const res = await api.prepare(message.task_id, message.action_type, message.target_origin);
        reply(sendResponse, res.ok ? { ok: true, data: res.data } : errorPayload(res.error));
        return;
      }
      case "cb:bind-page": {
        const res = await bindTargetTab(message.snapshot_id, message.tab_id);
        reply(sendResponse, res.ok ? { ok: true, data: res.data } : errorPayload(res.error));
        return;
      }
      case "cb:validate": {
        const bind = bindingFor(message.snapshot_id);
        if (!bind) {
          reply(sendResponse, errorPayload({ code: "no_binding", message: "请先在预览里确认这一页" }));
          return;
        }
        const live = await readPageState(bind.tabId);
        if (!live.ok) {
          resetBinding();
          reply(sendResponse, errorPayload({ code: "page_changed", message: "预览的页面已关闭或已跳转，请重新打开并预览" }));
          return;
        }
        const liveProductId = live.data.product_id || "";
        if (bind.previewProductId && liveProductId !== bind.previewProductId) {
          resetBinding();
          reply(sendResponse, errorPayload({ code: "product_changed", message: "这个页面已经不是预览时的商品，请重新预览", retryable: false }));
          return;
        }
        if (!(await tabStillBound(bind.tabId, bind.origin, bind.previewProductId))) {
          resetBinding();
          reply(sendResponse, errorPayload({ code: "page_changed", message: "预览的页面已关闭或已跳转，请重新打开并预览" }));
          return;
        }
        // 送校验的是页面此刻真实的商品号，不是预览时缓存的那个。
        const res = await api.validate(message.snapshot_id, bind.origin, liveProductId);
        reply(sendResponse, res.ok ? { ok: true, data: res.data } : errorPayload(res.error));
        return;
      }
      case "cb:fill": {
        // 只接受绑定页 + 用户本次确认 + 服务端实时校验通过的代填。
        const bind = bindingFor(message.snapshot_id);
        if (!bind) {
          reply(sendResponse, errorPayload({ code: "no_binding", message: "请先在预览里确认这一页，再代填", retryable: false }));
          return;
        }
        if (message.user_confirmed !== true) {
          reply(sendResponse, errorPayload({ code: "not_confirmed", message: "代填需要用户本次明确确认", retryable: false }));
          return;
        }
        const live = await readPageState(bind.tabId);
        if (!live.ok) {
          resetBinding();
          reply(sendResponse, errorPayload({ code: "page_changed", message: "预览的页面已关闭或已跳转，请重新打开并预览", retryable: false }));
          return;
        }
        const liveProductId = live.data.product_id || "";
        if (bind.previewProductId && liveProductId !== bind.previewProductId) {
          resetBinding();
          reply(sendResponse, errorPayload({ code: "product_changed", message: "这个页面已经不是预览时的商品，请重新预览", retryable: false }));
          return;
        }
        if (!(await tabStillBound(bind.tabId, bind.origin, bind.previewProductId))) {
          resetBinding();
          reply(sendResponse, errorPayload({ code: "page_changed", message: "预览的页面已关闭或已跳转，请重新打开并预览", retryable: false }));
          return;
        }
        const check = await api.validate(message.snapshot_id, bind.origin, liveProductId);
        if (!check.ok) {
          reply(sendResponse, errorPayload(check.error));
          return;
        }
        if (!check.data.valid) {
          reply(sendResponse, { ok: false, error: { code: CBContract.ERROR_CODES.CONFLICT, message: "这次不能代填：" + (check.data.reasons || ["参数包已不适用"]).join("；"), retryable: false } });
          return;
        }
        const fields = (message.fields || []).filter((f) => CBContract.ADAPTER_FIELDS.includes(f.key));
        const res = await askContent(bind.tabId, { type: "cb:fill-fields", fields });
        reply(sendResponse, res);
        return;
      }
      case "cb:read-page": {
        // 读用户指定的那一页。tabId 必须来自用户眼前的标签页，
        // 不能由后台自己挑一个：用户可能开着好几个标签，
        // 猜中的那一页不是他要读的。
        const tabId = message.tab_id;
        if (typeof tabId !== "number") {
          reply(sendResponse, errorPayload({
            code: "no_target_tab",
            message: "没有指定要读取的标签页",
            retryable: false,
          }));
          return;
        }
        let tab = null;
        try {
          tab = await chrome.tabs.get(tabId);
        } catch (err) {
          reply(sendResponse, errorPayload({
            code: "tab_gone",
            message: "这个标签页已关闭",
            retryable: false,
          }));
          return;
        }
        // chrome:// 与扩展自身页面读不了，也不该读：那不是用户要读的内容。
        if (!tab || !tab.url || !/^https?:\/\//.test(tab.url)) {
          reply(sendResponse, errorPayload({
            code: "page_not_readable",
            message: "这一页读不了：浏览器内部页面和扩展页面不在可读范围",
            retryable: false,
          }));
          return;
        }
        const read = await readPageContent(tabId);
        if (!read.ok) {
          reply(sendResponse, errorPayload(read.error));
          return;
        }
        // text 必须一起带回去：popup 的「复制这一页内容」复制的是这份文本，
        // 只回 data 的话 readText 是空的，复制按钮永远是禁用的，
        // 用户点了没反应，而界面明明已经摊开了内容。
        reply(sendResponse, { ok: true, data: read.data, text: read.text || "" });
        return;
      }
      case "cb:read-state": {
        const bind = bindingFor(message.snapshot_id);
        if (!bind) {
          reply(sendResponse, errorPayload({ code: "no_binding", message: "请先在预览里确认这一页", retryable: false }));
          return;
        }
        if (!(await tabStillBound(bind.tabId, bind.origin, bind.previewProductId))) {
          resetBinding();
          reply(sendResponse, errorPayload({ code: "page_changed", message: "预览的页面已关闭或已跳转，请重新打开并预览", retryable: false }));
          return;
        }
        const state = await readPageState(bind.tabId);
        reply(sendResponse, state);
        return;
      }
      case "cb:find-target": {
        const found = await activeAdapterTab(sender && sender.tab ? sender.tab.id : undefined);
        if (found && found.needsChoice) {
          reply(sendResponse, { ok: true, data: { needsChoice: true, count: found.count, choices: found.choices } });
          return;
        }
        reply(sendResponse, { ok: true, data: found ? { url: found.tab.url, tabId: found.tab.id, adapter: found.adapter } : null });
        return;
      }
      case "cb:receipt": {
        const res = await api.receipt(message.snapshot_id, message.target_origin, message.observed_fields, message.client_status, message.idempotency_key);
        reply(sendResponse, res.ok ? { ok: true, data: res.data } : errorPayload(res.error));
        return;
      }
      default:
        reply(sendResponse, errorPayload({ code: "unknown_message", message: "不支持的请求：" + message.type }));
    }
  })().catch((err) => {
    reply(sendResponse, errorPayload(err));
  });

  return true;
}

chrome.runtime.onMessage.addListener(handleMessage);

// 测试钩子：让验证脚本调到与 popup 完全相同的处理入口。
// 只是把同一个函数交出来，不新增任何网络或授权路径。
if (typeof globalThis !== "undefined") {
  globalThis.cbHandleMessage = handleMessage;
}
