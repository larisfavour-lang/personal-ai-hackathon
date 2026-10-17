/**
 * 后台专用的核心服务客户端。
 *
 * 凭证只在这里和 chrome.storage.local 之间流动：不写进页面 DOM、
 * 不发给 content script、不进日志。网页脚本因此无法直接拿到 token
 * 去调接口——它要先经过后台，后台也不会替网页发起请求。
 *
 * 后端地址写死在代码里，读页面消息里的 origin 不可信：
 * 当前网页想把自己伪装成核心服务，后台不能跟着改。
 */

(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(require("./contract.js"), require("./config.js"));
  } else {
    root.CBApi = factory(root.CBContract, root.CBConfig);
  }
})(typeof self !== "undefined" ? self : this, function (contract, config) {
  const CORE = (config && config.DEV_CORE_ORIGIN) || (config && config.CORE_ORIGIN) || "http://127.0.0.1:8000";

  const TOKEN_KEY = "cb_access_token";
  const CLIENT_KEY = "cb_client";
  const CORE_KEY = "cb_core_origin";
  // 服务端在后台静默轮换设备凭证时放在响应头里的新凭证
  // （见 app/main.py 的 _ext_payload 与 service._maybe_renew_device）。
  // 插件不读它，轮换后旧凭证在服务端已经失效，用户明明没断开连接，
  // 下一次请求却变成 401、被要求重新配对——正好违反「连接一次就一直连着」。
  const RENEWED_HEADER = "X-PA-Device-Token";

  /**
   * 凭证存放位置：chrome.storage.local，并把访问级别降到 TRUSTED_CONTEXTS。
   *
   * 为什么不用 chrome.storage.session：那是内存存储，浏览器一关就清，
   * 于是每次重开 Chrome 都要重新配对——服务端给多长的凭证期限都白费。
   * 用户要的是「连接一次就一直连着」，所以必须用跨重启存活的 local。
   *
   * 安全上不打折：local 默认对 content script 也可读，这里显式
   * setAccessLevel(TRUSTED_CONTEXTS)，只有扩展页面和后台拿得到凭证，
   * 商店页面里的脚本即使跑了也读不到 token。session 只作为兜底：
   * 旧版本留下的会话先迁移过来，不清空已有连接。
   */
  const store = (typeof chrome !== "undefined" && chrome.storage && chrome.storage.local)
    ? chrome.storage.local : null;
  const sessionStore = (typeof chrome !== "undefined" && chrome.storage && chrome.storage.session)
    ? chrome.storage.session : null;

  function tightenAccess() {
    if (store && store.setAccessLevel) {
      store.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
    }
    if (sessionStore && sessionStore.setAccessLevel) {
      sessionStore.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
    }
  }

  /** 一次完整连接要迁移的键。token 和 client 必须一起迁，不能拆。 */
  const SESSION_KEYS = [TOKEN_KEY, CLIENT_KEY];
  let migration = null;   // 一次迁移进行中；完成后置 null，允许下一批

  /**
   * 把 session 里的旧会话真正搬进持久存储。
   *
   * 为什么必须一起搬：getToken 只读 TOKEN_KEY，getClient 只读 CLIENT_KEY，
   * 分两次调用。若按单键迁移，先搬 token 时把 session 清了，第二次读 client
   * 已经拿不到——客户端信息就丢了，重开浏览器后虽然 token 还在，
   * 界面上的「已连接：某人」和设备信息会缺一半。
   * 所以这里始终按 SESSION_KEYS 整批读、整批写、整批清。
   */
  function migrateFromSession() {
    if (!store || !sessionStore) { return Promise.resolve({}); }
    if (migration) { return migration; }
    migration = new Promise((resolve) => {
      tightenAccess();
      sessionStore.get(SESSION_KEYS, (legacy) => {
        legacy = legacy || {};
        const carried = {};
        SESSION_KEYS.forEach((k) => { if (legacy[k]) { carried[k] = legacy[k]; } });
        if (!Object.keys(carried).length) {
          migration = null;
          resolve({});
          return;
        }
        // 持久库里已有的值优先：同一个键两处都有时，以 local 为准，
        // 不拿 session 的旧值覆盖较新的连接。
        tightenAccess();
        store.get(SESSION_KEYS, (existing) => {
          existing = existing || {};
          const toWrite = {};
          SESSION_KEYS.forEach((k) => {
            if (!existing[k] && carried[k]) { toWrite[k] = carried[k]; }
          });
          const finish = () => {
            migration = null;
            resolve(Object.assign({}, toWrite));
          };
          if (!Object.keys(toWrite).length) {
            // 没有要写的，也要把 session 旧值清掉，避免下次再搬一遍。
            tightenAccess();
            sessionStore.remove(SESSION_KEYS, finish);
            return;
          }
          tightenAccess();
          store.set(toWrite, () => {
            tightenAccess();
            sessionStore.remove(SESSION_KEYS, finish);
          });
        });
      });
    });
    return migration;
  }

  /** 持久库里先找，缺的键由旧 session 迁移补齐。 */
  function storageGet(keys) {
    return new Promise((resolve) => {
      if (store) {
        tightenAccess();
        store.get(keys, (got) => {
          got = got || {};
          const missing = keys.filter((k) => !got[k]);
          if (!missing.length || !sessionStore) { resolve(got); return; }
          // 整批迁移：token 与 client 一起搬，避免拆成两次读丢一半。
          migrateFromSession().then((carried) => {
            resolve(Object.assign({}, carried, got));
          });
        });
        return;
      }
      if (sessionStore) {
        tightenAccess();
        sessionStore.get(keys, resolve);
        return;
      }
      const out = {};
      keys.forEach((k) => {
        out[k] = (root.localStorage && root.localStorage.getItem(k)) || null;
      });
      resolve(out);
    });
  }

  function storageSet(obj) {
    return new Promise((resolve) => {
      if (store) {
        tightenAccess();
        store.set(obj, resolve);
        return;
      }
      if (sessionStore) {
        tightenAccess();
        sessionStore.set(obj, resolve);
        return;
      }
      if (root.localStorage) {
        Object.keys(obj).forEach((k) => { root.localStorage.setItem(k, JSON.stringify(obj[k])); });
      }
      resolve();
    });
  }

  function storageRemove(keys) {
    return new Promise((resolve) => {
      // 断开设备必须两边都清：只清 local 的话，session 里的旧凭证会在下次
      // 读取时被当成「待迁移的会话」又写回 local，撤销就白做了。
      const alsoClearLegacy = () => {
        if (sessionStore) { sessionStore.remove(keys, () => resolve()); }
        else { resolve(); }
      };
      if (store) {
        tightenAccess();
        store.remove(keys, alsoClearLegacy);
        return;
      }
      if (sessionStore) {
        tightenAccess();
        sessionStore.remove(keys, resolve);
        return;
      }
      if (root.localStorage) { keys.forEach((k) => { root.localStorage.removeItem(k); }); }
      resolve();
    });
  }

  function createClient(overrides) {
    const opts = overrides || {};
    let coreOrigin = opts.coreOrigin || CORE;

    async function request(path, options) {
      const cfg = options || {};
      const headers = Object.assign({ "Content-Type": "application/json" }, cfg.headers || {});
      const token = await getToken();
      if (token && !headers.Authorization) {
        headers.Authorization = "Bearer " + token;
      }
      const fetchImpl = opts.fetchImpl || (typeof fetch !== "undefined" ? fetch : null);
      if (!fetchImpl) {
        return { ok: false, error: { code: contract.ERROR_CODES.UNAVAILABLE, message: "当前环境没有 fetch，无法调用核心服务", retryable: false } };
      }
      let resp;
      try {
        resp = await fetchImpl(coreOrigin + path, {
          method: cfg.method || "GET",
          headers,
          body: cfg.body ? JSON.stringify(cfg.body) : undefined,
        });
      } catch (err) {
        return {
          ok: false,
          error: { code: contract.ERROR_CODES.NETWORK, message: "连不上核心服务（" + coreOrigin + "）。请确认服务已启动。", retryable: true },
        };
      }
      const text = await resp.text();
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch (err) {
        data = null;
      }
      // 服务端在后台静默轮换设备凭证时，会把新凭证放在这个响应头里。
      // 插件必须在这里换掉本地存的那一个：轮换后旧凭证在服务端已经失效，
      // 不换的下一次请求就是 401，用户明明没断开连接却被要求重新配对。
      // 详见 _ext_payload 与 service._maybe_renew_device。
      await adoptRenewedToken(resp);
      if (!resp.ok) {
        const apiError = data && data.error ? data.error : {};
        return {
          ok: false,
          status: resp.status,
          error: {
            code: apiError.code || (resp.status === 401 ? contract.ERROR_CODES.UNAUTHORIZED : resp.status === 403 ? contract.ERROR_CODES.FORBIDDEN : resp.status === 409 ? contract.ERROR_CODES.CONFLICT : resp.status === 422 ? contract.ERROR_CODES.UNPROCESSABLE : contract.ERROR_CODES.UNAVAILABLE),
            message: apiError.message || ("核心服务返回 " + resp.status),
            retryable: resp.status >= 500 || resp.status === 503,
          },
        };
      }
      return { ok: true, status: resp.status, data: data };
    }

    /** 响应头里带回了新凭证就换掉本地那份；没有就什么都不做。 */
    async function adoptRenewedToken(resp) {
      let renewed = "";
      try {
        renewed = (resp && resp.headers && resp.headers.get)
          ? (resp.headers.get(RENEWED_HEADER) || "") : "";
      } catch (err) {
        renewed = "";
      }
      if (!renewed) { return; }
      // 只认非空串：空串写进去会把连接清成未配对。
      await storageSet({ [TOKEN_KEY]: renewed });
    }

    async function getToken() {
      const got = await storageGet([TOKEN_KEY]);
      return got[TOKEN_KEY] || null;
    }

    async function getClient() {
      const got = await storageGet([CLIENT_KEY]);
      const raw = got[CLIENT_KEY];
      if (!raw) return null;
      try {
        return JSON.parse(raw);
      } catch (err) {
        return null;
      }
    }

    async function saveSession(pairResult) {
      const client = {
        client_id: pairResult.client_id,
        expires_at: pairResult.expires_at,
        // 设备名单独存：配对时用户给这台电脑起的名字（如「我的电脑 Chrome」）。
        // 少了它，界面只能显示「已连接：某人」，说不出连的是哪台设备、
        // 在应用里要撤销的是哪一台。
        client_name: pairResult.client_name || "",
        user_id: pairResult.user && pairResult.user.user_id,
        display_name: pairResult.user && pairResult.user.display_name,
      };
      await storageSet({
        [TOKEN_KEY]: pairResult.access_token,
        [CLIENT_KEY]: JSON.stringify(client),
      });
      return client;
    }

    async function clearSession() {
      await storageRemove([TOKEN_KEY, CLIENT_KEY]);
    }

    async function pair(code, clientName) {
      const res = await request("/api/extension/pair", {
        method: "POST",
        body: { code: code, client_name: clientName || "随身助手" },
      });
      if (!res.ok) return res;
      const check = contract.validatePackage;
      void check;
      const data = res.data || {};
      if (!data.access_token || !data.client_id || !data.user) {
        return { ok: false, error: { code: contract.ERROR_CODES.CONTRACT, message: "配对返回缺少凭证或用户信息", retryable: false } };
      }
      await saveSession(data);
      return { ok: true, data: await getClient() };
    }

    async function revoke() {
      const client = await getClient();
      if (!client) {
        return { ok: false, error: { code: contract.ERROR_CODES.UNAUTHORIZED, message: "还没有连接任何设备", retryable: false } };
      }
      const res = await request("/api/extension/revoke", {
        method: "POST",
        body: { client_id: client.client_id },
      });
      await clearSession();
      return res;
    }

    async function listTasks() {
      const res = await request("/api/extension/tasks");
      if (!res.ok) return res;
      const tasks = res.data && Array.isArray(res.data.tasks) ? res.data.tasks : [];
      return { ok: true, data: { tasks } };
    }

    /**
     * 本次需求（跨页面复用）。用户在别的页面继续办同一件事时，
     * 从这里取当前有效的预算、用途、偏好、排除项和待确认项。
     *
     * purpose 固定由插件给出，不由页面或用户填：这个字段决定后端
     * 按哪种用途筛选字段，页面脚本改不了它。
     */
    async function context(taskId, purpose) {
      const res = await request("/api/extension/context", {
        method: "POST",
        body: { task_id: taskId, purpose: purpose || "shopping_help" },
      });
      if (!res.ok) return res;
      const ctx = res.data;
      const check = contract.validateContext(ctx);
      if (!check.ok) {
        return {
          ok: false,
          error: {
            code: contract.ERROR_CODES.CONTRACT,
            message: "需求内容不符合约定：" + check.problems.join("；"),
            retryable: false,
          },
        };
      }
      return { ok: true, data: ctx };
    }

    async function prepare(taskId, actionType, targetOrigin) {
      const res = await request("/api/extension/prepare", {
        method: "POST",
        body: { task_id: taskId, action_type: actionType, target_origin: targetOrigin },
      });
      if (!res.ok) return res;
      const pkg = res.data;
      const check = contract.validatePackage(pkg);
      if (!check.ok) {
        return { ok: false, error: { code: contract.ERROR_CODES.CONTRACT, message: "参数包不符合契约：" + check.problems.join("；"), retryable: false } };
      }
      return { ok: true, data: pkg };
    }

    async function validate(snapshotId, targetOrigin, observedProductId) {
      const res = await request("/api/extension/validate", {
        method: "POST",
        body: {
          snapshot_id: snapshotId,
          target_origin: targetOrigin,
          observed_product_id: observedProductId,
          user_confirmed: true,
        },
      });
      if (!res.ok) return res;
      const data = res.data || {};
      return {
        ok: true,
        data: {
          valid: data.valid === true,
          reasons: Array.isArray(data.reasons) ? data.reasons : [],
          validated_at: data.validated_at || "",
        },
      };
    }

    async function receipt(snapshotId, targetOrigin, observedFields, clientStatus, idempotencyKey) {
      const res = await request("/api/extension/receipts", {
        method: "POST",
        body: {
          snapshot_id: snapshotId,
          target_origin: targetOrigin,
          observed_fields: observedFields,
          client_status: clientStatus,
          idempotency_key: idempotencyKey,
        },
      });
      if (!res.ok) return res;
      const data = res.data || {};
      return {
        ok: true,
        data: {
          receipt_id: data.receipt_id || "",
          status: data.status || "",
          mismatches: Array.isArray(data.mismatches) ? data.mismatches : [],
        },
      };
    }

    return {
      request,
      getToken,
      getClient,
      saveSession,
      clearSession,
      pair,
      revoke,
      listTasks,
      context,
      prepare,
      validate,
      receipt,
      setCoreOrigin(next) {
        if (typeof next === "string" && /^https?:\/\//.test(next)) {
          coreOrigin = next.replace(/\/+$/, "");
        }
      },
      getCoreOrigin() {
        return coreOrigin;
      },
    };
  }

  return { createClient, CORE_ORIGIN: CORE, storageGet, storageSet, storageRemove };
});
