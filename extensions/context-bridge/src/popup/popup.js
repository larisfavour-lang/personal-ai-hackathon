/**
 * popup：用户唯一看到的界面。只呈现后端给的事实，不做业务判断。
 *
 * 顺序是硬约束：选事项 → 预览参数 → 用户点「确认并代填」→ 页面填写 → 回执。
 * 没有预览就没有代填；用户没点就不写页面。任何一步失败都停在这一步，
 * 不把失败说成成功，也不自动重试不可逆动作。
 */

(function () {
  const C = window.CBContract;
  const CB = window.CBClipboard;
  const $ = (id) => document.getElementById(id);

  const views = {
    pair: $("view-pair"),
    tasks: $("view-tasks"),
    context: $("view-context"),
    read: $("view-read"),
    preview: $("view-preview"),
    result: $("view-result"),
  };
  const err = $("error");
  const who = $("who");

  let tasks = [];
  let current = null; // {task, pkg}
  let idempotencyKey = "";
  // 需求卡状态。revision 记录界面当前展示的是哪一版，
  // 复制前重新取到的新版本若与它不同，先把差异摊给用户看。
  let ctxState = null; // {task, ctx, want}
  // 每次请求需求都自增。返回时序号不符说明用户已经换了事项或回到列表，
  // 那份旧响应直接作废，不能覆盖新事项、也不能拿去复制。
  let ctxSeq = 0;
  let lastCopied = { terms: "", export: "" };
  // 读页状态：当前读的是哪个标签页、读到的那份文本。
  // readTab 记下来，用户换了一页再点「重新读一次」时读的是新页面。
  let readTab = null;
  let readText = "";

  function show(name) {
    Object.keys(views).forEach((k) => { views[k].hidden = k !== name; });
  }

  function fail(message) {
    err.textContent = message;
    err.hidden = false;
  }

  function clearError() {
    err.hidden = true;
    err.textContent = "";
  }

  function msg(payload) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(payload, (resp) => {
        const last = chrome.runtime.lastError;
        if (last) {
          resolve({ ok: false, error: { code: "bg_unreachable", message: "插件后台没有回应：" + last.message, retryable: false } });
          return;
        }
        resolve(resp || { ok: false, error: { code: "empty_reply", message: "插件后台没有返回内容", retryable: false } });
      });
    });
  }

  /** 后端错误转成用户能操作的中文提示。 */
  function friendly(result) {
    const e = result && result.error ? result.error : {};
    const map = {
      // 分开三种「连不上」：服务端把设备撤了、凭证到期、只是没连过。
      // 都写成「重新生成配对码」会让用户以为每次都要配一次，
      // 而正常情况是连接一次后一直有效，只有主动断开才要重连。
      device_revoked: "这个设备已被撤销，请在应用里重新连接浏览器插件。",
      device_expired: "连接已过期，请在应用里重新连接浏览器插件。",
      bad_credentials: "这个连接已失效，请在应用里重新连接浏览器插件。",
      unauthorized: "连接已失效，请在应用里重新连接浏览器插件。",
      forbidden: "这个设备没有权限做这个操作。",
      conflict: "参数包已过期或约定变了，请回到应用重新生成。",
      unprocessable: "这次要填的参数还不齐，请先在应用里补齐。",
      network_error: "连不上个人助理服务，请确认应用已启动。",
      contract_mismatch: "插件和服务器的约定不一致，请更新插件或联系开发者。",
      no_target_page: "还没有打开可以代填的商店页面。",
      no_adapter: "这个页面没有适配器，请在模拟商店打开商品页。",
      no_tabs: "当前浏览器环境拿不到标签页。",
      no_binding: "请先在预览里确认这一页，再代填。",
      page_changed: "预览的页面已关闭或已跳转，请重新打开并预览。",
      product_changed: "这个页面已经不是预览时的商品，请重新预览。",
      tab_gone: "预览的那个标签页已经关掉了，请重新打开商品页。",
      no_target_tab: "没有指定要代填的标签页，请重新预览。",
      not_confirmed: "代填需要你本次明确确认，请重新点一次「确认并代填」。",
      not_found: "服务端还没有这个接口，暂时用不了这项能力。",
      page_not_readable: "这一页读不了：浏览器内部页面（地址栏、设置页等）不在可读范围。",
      no_scripting: "当前浏览器不支持读取这一页，请更新浏览器后重试。",
      inject_failed: "这一页不让读取，换个普通网页再试。",
      read_failed: "这一页读取失败，可以重试或换一页再读。",
    };
    return map[e.code] || e.message || "操作失败，请重试。";
  }

  async function refreshStatus() {
    const res = await msg({ type: "cb:status" });
    if (!res.ok) { fail(friendly(res)); return; }
    const client = res.data && res.data.client;
    if (client) {
      who.textContent = "已连接：" + (client.display_name || client.user_id || "本人");
      show("tasks");
      await loadTasks();
    } else {
      who.textContent = "未连接";
      show("pair");
    }
  }

  async function loadTasks() {
    clearError();
    const res = await msg({ type: "cb:tasks" });
    if (!res.ok) { fail(friendly(res)); return; }
    tasks = res.data.tasks || [];
    const list = $("task-list");
    list.textContent = "";
    if (!tasks.length) {
      const li = document.createElement("li");
      li.className = "is-blocked";
      li.innerHTML = '<div class="cb-task-title">没有可办的事项</div><div class="cb-task-status">先在应用里交办并达成约定。</div>';
      list.appendChild(li);
      return;
    }
    tasks.forEach((t) => {
      const li = document.createElement("li");
      const canPrepare = t.can_prepare === true;
      // task_id 落到 DOM 上（data 属性，不显示）：同名事项同时存在时，
      // 标题、状态都可能一模一样，只有 task_id 能指认是哪一件。
      // 界面不展示它，验证与定位靠它。
      if (t.task_id) { li.setAttribute("data-task-id", t.task_id); }
      // 需求卡不看 can_prepare：没协商完的事项照样能看已知需求、
      // 复制搜索词和需求说明，只是不能代填。把整行置灰会让
      // 「还没谈成的时候我能做什么」没有答案。
      li.className = canPrepare ? "" : "is-blocked";
      const title = document.createElement("div");
      title.className = "cb-task-title";
      title.textContent = t.title || t.task_id;
      const status = document.createElement("div");
      status.className = "cb-task-status";
      status.textContent = t.status || "";
      li.appendChild(title);
      li.appendChild(status);
      if (!canPrepare && t.blocked_reason) {
        const why = document.createElement("div");
        why.className = "cb-task-why";
        why.textContent = t.blocked_reason;
        li.appendChild(why);
      }
      // 三件事分开：点标题看需求；「读这一页」把当前页的文字带回来；
      // 有代填资格时再「去代填」。互不替代，也不互相触发。
      li.addEventListener("click", () => openContext(t));
      const read = document.createElement("button");
      // 独立类名，不共用 cb-task-go：那个类专指「去代填」，
      // 被读页按钮分走之后，定位「去代填」的脚本和辅助技术都会点错。
      read.className = "cb-task-read";
      read.textContent = "读这一页";
      read.addEventListener("click", (ev) => {
        ev.stopPropagation();
        readCurrentPage();
      });
      li.appendChild(read);
      if (canPrepare) {
        const go = document.createElement("button");
        go.className = "cb-task-go";
        go.textContent = "去代填";
        go.addEventListener("click", (ev) => {
          ev.stopPropagation();
          pickTask(t);
        });
        li.appendChild(go);
      }
      list.appendChild(li);
    });
  }

  /**
   * 打开一件事的需求卡。
   *
   * 这里不看 can_prepare：还没协商完的事项，用户照样需要知道已经定了什么、
   * 还缺什么，并把已知的部分复制到别的页面继续。执行资格由代填那条路把关。
   */
  async function openContext(task) {
    clearError();
    // 记下这次请求要的是哪件事、哪种用途。返回后要核对，
    // 否则用户在这期间换了事项，旧响应会被当成新事项的内容显示。
    const want = { task_id: task.task_id, purpose: "shopping_help" };
    ctxSeq += 1;
    const seq = ctxSeq;
    const res = await msg({ type: "cb:context", task_id: want.task_id, purpose: want.purpose });
    if (seq !== ctxSeq) { return; }   // 已经换了事项，旧响应作废
    if (!res.ok) { fail(friendly(res)); return; }
    const ctx = res.data;
    const check = C.validateContext(ctx, want);
    if (!check.ok) {
      fail("需求内容不完整：" + check.problems.join("；"));
      return;
    }
    ctxState = { task, ctx, want };
    lastCopied = { terms: "", export: "" };
    renderContext(task, ctx);
    show("context");
  }

  /** 待确认项单独列出来，不和已知需求混在一张表里。 */
  function renderPending(ctx) {
    const box = $("cx-pending");
    const pending = (ctx.requirements || []).filter((r) => r.state === "pending");
    const missing = Array.isArray(ctx.missing_fields) ? ctx.missing_fields : [];
    const lines = [];
    pending.forEach((r) => lines.push(r.label || r.key));
    missing.forEach((m) => { if (!lines.some((l) => l === m)) { lines.push(m); } });
    if (!lines.length) {
      box.hidden = true;
      box.textContent = "";
      return;
    }
    box.hidden = false;
    box.textContent = "还没定的：" + lines.join("、") + "。这些不能当确定规格用。";
  }

  function renderContext(task, ctx) {
    $("cx-title").textContent = task.title || task.task_id;
    $("cx-subject").textContent = ctx.subject_label || "未提供";
    $("cx-status").textContent = ctx.task_status || "";
    $("cx-summary").textContent = ctx.summary || "";
    const body = $("cx-reqs");
    body.textContent = "";
    const srcOf = (key) => {
      const hit = (ctx.source_summary || []).find((s) => s.field === key);
      if (!hit) { return "—"; }
      const at = hit.confirmed_at ? "（" + hit.confirmed_at.slice(0, 10) + "）" : "";
      return hit.source_label + at;
    };
    (ctx.requirements || []).forEach((r) => {
      const tr = document.createElement("tr");
      const tdKey = document.createElement("td");
      tdKey.textContent = r.label || r.key;
      const tdVal = document.createElement("td");
      const known = r.state === "known";
      tdVal.textContent = known ? r.value : "待确认";
      tdVal.className = known ? "" : "cb-pending";
      const tdSrc = document.createElement("td");
      tdSrc.className = "cb-src";
      tdSrc.textContent = known ? srcOf(r.key) : "还没确认，没有来源";
      tr.appendChild(tdKey);
      tr.appendChild(tdVal);
      tr.appendChild(tdSrc);
      body.appendChild(tr);
    });
    renderPending(ctx);
    $("cx-diff").hidden = true;
    $("cx-diff").textContent = "";
    // 先把两份可复制内容摊开。复制是外发动作，用户在看到内容之前
    // 不该有东西被写进剪贴板——所以这里只展示，不复制。
    const terms = C.searchTermsText(ctx);
    const exportText = C.exportText(ctx);
    const termsBox = $("cx-terms-box");
    termsBox.hidden = !terms;
    termsBox.textContent = terms ? "可复制的搜索词：\n" + terms : "";
    const exportBox = $("cx-export-box");
    exportBox.hidden = !exportText;
    exportBox.textContent = exportText ? "可复制的需求说明：\n" + exportText : "";
    $("btn-copy-terms").disabled = !terms;
    $("btn-copy-export").disabled = !exportText;
  }

  /**
   * 复制前重新取一次当前版本。
   *
   * 为什么不直接复制界面上那份：用户可能在打开插件之后去应用里改了预算，
   * 界面上的还是旧值。直接复制等于把已过时的内容发给别人，
   * 所以这里重新取，变了就先摊开差异让人核对。
   */
  /**
   * 复制前重新取一次当前版本。返回 {ctx, blocked}。
   *
   * blocked 为真表示「内容变了，这次不能复制」：变化必须由用户先核对，
   * 再由他主动点一次复制。同一次点击里既展示差异又写剪贴板，
   * 等于把用户没看过的内容发出去。
     */
  async function refreshContextForCopy() {
    if (!ctxState) { return { ctx: null, blocked: false, noTask: true }; }
    ctxSeq += 1;
    const seq = ctxSeq;
    const want = ctxState.want;
    const res = await msg({
      type: "cb:context",
      task_id: want.task_id,
      purpose: want.purpose,
    });
    if (seq !== ctxSeq) { return { ctx: null, blocked: false, stale: true }; }
    if (!res.ok) {
      fail("没能取到最新需求：" + friendly(res) + "。可以重试，界面仍显示上次看到的内容。");
      return { ctx: null, blocked: false };
    }
    const ctx = res.data;
    const check = C.validateContext(ctx, want);
    if (!check.ok) {
      fail("最新需求内容不完整：" + check.problems.join("；"));
      return { ctx: null, blocked: false };
    }
    const before = ctxState.ctx;
    const diff = C.contextDiff(before, ctx);
    ctxState = { task: ctxState.task, ctx, want };
    renderContext(ctxState.task, ctx);
    if (diff.changed) {
      const box = $("cx-diff");
      box.hidden = false;
      box.textContent = "需求和上次看到的不一样，请先核对，再点一次复制："
        + diff.items.map((d) => d.label + " " + d.from + " → " + d.to).join("；");
      // 这次不复制：用户还没看过新内容。
      return { ctx: null, blocked: true };
    }
    return { ctx, blocked: false };
  }

  /**
   * 复制文本，并始终把这份文本摊在界面上。
   *
   * 顺序很重要：先显示、后复制。用户第一次点之前就要看到即将外发的内容，
   * 而不是复制成功之后才知道刚才发出去的是什么。
   *
   * 复制结果用单独一行写在同一框里，并且写在 renderContext 之后：
   * renderContext 每次刷新需求都会把框重置成「可复制的内容」，
   * 如果把复制结果写在刷新之前，用户点完看到的是旧文案，不知道成没成。
   */
  async function copyOut(getText, boxId, emptyMessage) {
    const text = getText();
    if (!text) { fail(emptyMessage); return null; }
    const done = await CB.copy(text);
    return { ok: Boolean(done && done.ok), text };
  }

  /** 把复制结果写进框里：成功说成功，失败就明确让用户手动选。 */
  function showCopyResult(boxId, text, ok) {
    const box = $(boxId);
    if (!box) { return; }
    box.hidden = false;
    box.textContent = (ok
      ? "已复制。内容如下，也可手动选择："
      : "没能自动复制，请手动选择下面内容：") + "\n" + text;
  }

  async function copyTerms() {
    clearError();
    const fresh = await refreshContextForCopy();
    if (fresh.blocked || fresh.stale || fresh.noTask || !fresh.ctx) { return; }
    const ctx = fresh.ctx;
    const text = C.searchTermsText(ctx);
    if (!text) { fail("这个事项还没有可用的搜索词。"); return; }
    const r = await copyOut(() => text, "cx-terms-box", "");
    showCopyResult("cx-terms-box", text, r && r.ok);
    if (r && r.ok) { lastCopied.terms = r.text; }
  }

  async function copyExport() {
    clearError();
    const fresh = await refreshContextForCopy();
    if (fresh.blocked || fresh.stale || fresh.noTask || !fresh.ctx) { return; }
    const ctx = fresh.ctx;
    const text = C.exportText(ctx);
    if (!text) { fail("这个事项还没有可用的需求说明。"); return; }
    const r = await copyOut(() => text, "cx-export-box", "");
    showCopyResult("cx-export-box", text, r && r.ok);
    if (r && r.ok) { lastCopied.export = r.text; }
  }

  /**
   * 用户此刻在看的适配页。没有、或有多个且分不清时返回 null 并说明原因，
   * 由界面给出可点的页面列表让用户选——不猜，也不退回「第一个」。
   */
  async function activeTargetTab() {
    const found = await msg({ type: "cb:find-target" });
    if (!found.ok) { fail(friendly(found)); return null; }
    const data = found.data || {};
    if (data.needsChoice) {
      const n = data.count || 0;
      if (n === 0) {
        fail("当前页面不是可以代填的商品页，请先在商品页再选事项。");
      } else {
        fail("当前窗口打开了 " + n + " 个商品页，请先点一下要填的那个页面，再回来选事项。");
      }
      return null;
    }
    if (!data.tabId) return null;
    return { tabId: data.tabId, origin: new URL(data.url).origin, adapter: data.adapter };
  }

  async function pickTask(task) {
    clearError();
    ctxState = null;
    ctxSeq += 1;   // 作废还在路上的需求请求，旧响应不得覆盖新事项
    // 用户此刻在看哪个标签页，就代填哪个。后台不接受「自己搜一个」。
    const active = await activeTargetTab();
    if (!active) {
      fail("先打开可以代填的商店页面，再回来选事项。");
      return;
    }
    const origin = active.origin;
    const res = await msg({
      type: "cb:prepare",
      task_id: task.task_id,
      action_type: "shopping_fill",
      target_origin: origin,
    });
    if (!res.ok) { fail(friendly(res)); return; }
    const pkg = res.data;
    const check = C.validatePackage(pkg);
    if (!check.ok) {
      fail("参数包不完整：" + check.problems.join("；"));
      return;
    }
    // 预览出来就把用户眼前这一页绑进会话，之后读、填、回读都只认这一页。
    const bound = await msg({ type: "cb:bind-page", snapshot_id: pkg.snapshot_id, tab_id: active.tabId });
    if (!bound.ok) { fail(friendly(bound)); return; }
    current = { task, pkg };
    // 验证脚本要从 popup 作用域里拿到当前参数包，才能复现「关掉预览页后再点代填」。
    // 只暴露 snapshot_id 一个值，不开新的网络或授权路径。
    window.__cbLastSnap = pkg.snapshot_id;
    // 幂等键跟着参数包走，不掺时间戳：同一个参数包重复代填
    // 只会更新同一份回执，不会在应用里堆出一串新记录。
    idempotencyKey = pkg.snapshot_id;
    renderPreview(pkg, task, bound.data);
    show("preview");
  }

  function sourceFor(pkg, key) {
    const hit = (pkg.source_summary || []).find((s) => s.field === key);
    if (!hit) return "";
    const at = hit.confirmed_at ? "（" + hit.confirmed_at.slice(0, 10) + "）" : "";
    return hit.source_label + at;
  }

  function renderPreview(pkg, task, binding) {
    $("pv-title").textContent = task.title || task.task_id;
    $("pv-subject").textContent = pkg.subject_label || "未提供";
    $("pv-product").textContent = pkg.product && pkg.product.title ? pkg.product.title : "未提供";
    $("pv-version").textContent = "第 " + pkg.proposal_version + " 版";
    const body = $("pv-fields");
    body.textContent = "";
    (pkg.fields || []).forEach((f) => {
      const tr = document.createElement("tr");
      const tdKey = document.createElement("td");
      tdKey.textContent = C.FIELD_LABELS[f.key] || f.key;
      const tdVal = document.createElement("td");
      tdVal.textContent = f.value;
      const tdSrc = document.createElement("td");
      tdSrc.className = "cb-src";
      tdSrc.textContent = sourceFor(pkg, f.key) || "—";
      tr.appendChild(tdKey);
      tr.appendChild(tdVal);
      tr.appendChild(tdSrc);
      body.appendChild(tr);
    });
    const missing = $("pv-missing");
    if (pkg.missing_fields && pkg.missing_fields.length) {
      missing.hidden = false;
      missing.textContent = "还缺：" + pkg.missing_fields.map((m) => C.FIELD_LABELS[m] || m).join("、") + "。补齐前不能代填。";
      $("btn-fill").disabled = true;
    } else {
      missing.hidden = true;
      $("btn-fill").disabled = !pkg.can_fill;
    }
    $("pv-page").textContent = "填到这个页面：" + ((binding && binding.origin) || pkg.target_origin)
      + "（商品 " + ((binding && binding.productId) || "未识别") + "）";
    $("btn-fill").textContent = "确认并代填";
  }

  /**
   * 把「页面此刻到底接不接受这些参数」评估成一份结论。
   *
   * 首发和重发必须走同一套判定：重发时页面业务状态可能已经变了
   * （数量超库存、页面拒绝提交），如果重发只按字段是否相同就报 filled，
   * 应用里那份回执会从 partial 悄悄变回 verified，而页面其实不接受这些值。
   * 不写死某个数量值——页面自己的规则才算依据。
   */
  function assessPage(pkg, state, extraProblems) {
    const problems = (extraProblems || []).slice();
    const observedFields = {};
    (pkg.fields || []).forEach((f) => {
      observedFields[f.key] = (state.fields || {})[f.key] || "";
    });
    // 控件被禁用或只读时，页面不会接受写入，不能算填上了。
    const controls = state.controls || {};
    Object.keys(observedFields).forEach((key) => {
      const c = controls[key];
      if (c && (c.disabled || c.readonly) && !observedFields[key]) {
        problems.push({ field: key, reason: "页面控件不可编辑" });
      }
    });
    // 页面业务状态不接受：库存不足、拒绝提交，都要记成问题。
    if (state.page_status && state.page_status.can_submit === false) {
      problems.push({ field: "quantity", reason: "页面不接受当前数量（提示库存不足），这个数量没有真正生效" });
    }
    if (state.page_status && state.page_status.stock === "库存不足") {
      problems.push({ field: "quantity", reason: "页面显示库存不足，请减少数量后重试" });
    }

    let filledCount = 0;
    (pkg.fields || []).forEach((f) => {
      if (observedFields[f.key] && observedFields[f.key] === f.value) filledCount += 1;
    });
    // 有问题就不是 filled：只报 filled 会让应用显示 verified，
    // 而页面其实不接受这些值。
    let clientStatus = "filled";
    if (filledCount === 0) clientStatus = "failed";
    else if (filledCount < (pkg.fields || []).length || problems.length > 0) clientStatus = "partial";
    return { observedFields, problems, filledCount, clientStatus };
  }

  async function doFill() {
    clearError();
    if (!current) return;
    const { task, pkg } = current;
    const btn = $("btn-fill");
    btn.disabled = true;
    btn.textContent = "正在校验…";

    // 1. 服务端实时校验 + 页面仍是预览那一页，由后台统一把关。
    const fill = await msg({
      type: "cb:fill",
      snapshot_id: pkg.snapshot_id,
      fields: C.fillableFields(pkg),
      user_confirmed: true,
    });
    if (!fill.ok) {
      fail(friendly(fill));
      btn.disabled = false;
      btn.textContent = "确认并代填";
      return;
    }
    const problems = (fill.data && fill.data.problems) || [];

    // 2. 回读页面真实状态。按参数包里全部预期字段核对，
    //    不只是适配器支持的那几个：没控件的字段要算成没填上。
    btn.textContent = "正在回读页面…";
    const after = await msg({ type: "cb:read-state", snapshot_id: pkg.snapshot_id });
    if (!after.ok) {
      fail(friendly(after));
      btn.disabled = false;
      btn.textContent = "确认并代填";
      return;
    }
    const state = after.data || {};
    // 首发与重发共用同一套判定，避免两边规则漂移。
    const seen = assessPage(pkg, state, problems);
    const observedFields = seen.observedFields;
    const filledCount = seen.filledCount;
    const clientStatus = seen.clientStatus;

    // 3. 回执。失败也要把页面可能已填的事实讲清楚，不悄悄重做。
    const rcpt = await msg({
      type: "cb:receipt",
      snapshot_id: pkg.snapshot_id,
      target_origin: pkg.target_origin,
      observed_fields: observedFields,
      client_status: clientStatus,
      idempotency_key: idempotencyKey,
    });
    const receiptOk = rcpt.ok;
    const mismatch = receiptOk && rcpt.data && Array.isArray(rcpt.data.mismatches) ? rcpt.data.mismatches : [];

    renderResult({
      task,
      pkg,
      observedFields,
      problems,
      receipt: receiptOk ? rcpt.data : null,
      receiptError: receiptOk ? "" : friendly(rcpt),
      mismatch,
      filledCount,
    });
    show("result");
  }

  function renderResult(ctx) {
    const banner = $("rs-banner");
    const body = $("rs-fields");
    body.textContent = "";

    if (!ctx.receipt) {
      banner.className = "cb-banner is-part";
      banner.textContent = "页面可能已填，但回执没送到应用：" + ctx.receiptError;
    } else if (ctx.receipt.status === "verified" && !ctx.problems.length) {
      banner.className = "cb-banner is-ok";
      banner.textContent = "已代填，页面参数与约定一致。请本人核对后自行提交，本插件不提交订单、不付款。";
    } else {
      banner.className = "cb-banner is-part";
      banner.textContent = "部分字段没填上或与约定不一致，请按页面实际值核对。";
    }

    (ctx.pkg.fields || []).forEach((f) => {
      const tr = document.createElement("tr");
      const tdKey = document.createElement("td");
      tdKey.textContent = C.FIELD_LABELS[f.key] || f.key;
      const tdVal = document.createElement("td");
      const actual = ctx.observedFields[f.key];
      tdVal.textContent = actual || "（未填）";
      tdVal.className = actual === f.value ? "cb-good" : "cb-bad";
      const tdRes = document.createElement("td");
      const problem = ctx.problems.find((p) => p.field === f.key);
      const mismatch = ctx.mismatch.find((m) => m.field === f.key);
      tdRes.textContent = problem ? problem.reason : (mismatch ? mismatch.reason : (actual === f.value ? "一致" : "不一致"));
      tdRes.className = actual === f.value && !problem ? "cb-good" : "cb-bad";
      tr.appendChild(tdKey);
      tr.appendChild(tdVal);
      tr.appendChild(tdRes);
      body.appendChild(tr);
    });

    $("btn-retry-receipt").hidden = false;
    const note = $("rs-receipt");
    if (ctx.receipt) {
      note.textContent = "回执已存到应用里这件事（编号 " + ctx.receipt.receipt_id.slice(0, 8) + "，状态 " + ctx.receipt.status + "）。重复发送只会更新同一份。";
    } else {
      note.textContent = "可以重新点一次「确认并代填」补发回执；页面已经填过的值不会因此改变业务状态。";
    }
  }

  /**
   * 回执没送到应用时重发。用同一个 snapshot 和同一个幂等键，
   * 服务端只会更新那一份回执，不会因为重试多出几条。
   * 页面已经填过的值不动，不重新执行不可逆操作。
   */
  async function retryReceipt() {
    clearError();
    if (!current) return;
    const { pkg } = current;
    const btn = $("btn-retry-receipt");
    btn.disabled = true;
    btn.textContent = "正在重发…";

    const after = await msg({ type: "cb:read-state", snapshot_id: pkg.snapshot_id });
    if (!after.ok) { fail(friendly(after)); btn.disabled = false; btn.textContent = "重发回执"; return; }
    const state = after.data || {};
    // 重发也按页面当前真实状态判定，不是只看字段是否相同：
    // 首发之后页面业务状态可能已经变了（数量超库存、拒绝提交），
    // 这时仍要报 partial，不能把应用里那份回执悄悄变回 verified。
    const seen = assessPage(pkg, state, []);
    const observedFields = seen.observedFields;
    const filledCount = seen.filledCount;
    const clientStatus = seen.clientStatus;

    const rcpt = await msg({
      type: "cb:receipt",
      snapshot_id: pkg.snapshot_id,
      target_origin: pkg.target_origin,
      observed_fields: observedFields,
      client_status: clientStatus,
      idempotency_key: idempotencyKey,
    });
    btn.disabled = false;
    btn.textContent = "重发回执";
    if (!rcpt.ok) {
      fail(friendly(rcpt));
      return;
    }
    renderResult({
      task: current.task,
      pkg,
      observedFields,
      problems: seen.problems,
      receipt: rcpt.data,
      receiptError: "",
      mismatch: (rcpt.data && rcpt.data.mismatches) || [],
      filledCount,
    });
    show("result");
  }

  /**
   * 读当前这一页。
   *
   * 入口在事项行：「读这一页」和「看需求」「去代填」并列，因为它是同一件
   * 事的第三步——用户在一个页面里看到说不清的内容，要把它带回来接着办。
   *
   * tabId 取当前窗口的激活标签页，不用「搜一个适配页」：要读的就是
   * 用户眼前这一页，可能是普通网页，不是商品页。
   */
  async function readCurrentPage() {
    clearError();
    const active = await activePlainTab();
    if (!active) {
      const tabs = await chrome.tabs.query({ currentWindow: true });
      const many = (tabs || []).filter((t) => /^https?:\/\//.test(t.url || "")).length;
      fail(many > 1
        ? "当前窗口打开了 " + many + " 个网页，请先点一下要读的那一页，再回来点「读这一页」。"
        : "没找到可以读取的网页，请先打开要读的页面。");
      return;
    }
    readTab = active.tabId;
    await doReadPage(active.tabId);
  }

  /**
   * 用户此刻在看的那一页。只看一眼，不缓存——用户随时可能换页。
   *
   * 先取当前窗口的激活标签页。真实浏览器里插件面板不是标签页，
   * 激活的就是用户刚在看的那一页；但在「面板被当成标签页打开」的环境里
   * （自动化验证就是这种），激活的会是面板自己，那一页读不了。
   * 这时退回「窗口里唯一一个可读网页」：只有一页可读时不存在猜错的可能；
   * 有多页可读就明确让用户先点一下要读的那一页，不替用户挑。
   */
  async function activePlainTab() {
    if (typeof chrome === "undefined" || !chrome.tabs) return null;
    const tabs = await chrome.tabs.query({ currentWindow: true });
    const readable = (tabs || []).filter((t) => typeof t.id === "number"
      && typeof t.url === "string" && /^https?:\/\//.test(t.url));
    const active = readable.find((t) => t.active);
    if (active) return { tabId: active.id, url: active.url };
    if (readable.length === 1) return { tabId: readable[0].id, url: readable[0].url };
    return null;
  }

  async function doReadPage(tabId) {
    const btn = $("btn-read-copy");
    const refresh = $("btn-read-refresh");
    btn.disabled = true;
    refresh.disabled = true;
    const res = await msg({ type: "cb:read-page", tab_id: tabId });
    if (!res.ok) {
      fail(friendly(res));
      btn.disabled = false;
      refresh.disabled = false;
      return;
    }
    const data = res.data || {};
    const text = res.text || "";
    readText = text;
    $("rd-title").textContent = data.title || "（无标题）";
    $("rd-url").textContent = data.url || "";
    const box = $("rd-text");
    box.textContent = "";
    (data.texts || []).forEach((item) => {
      const p = document.createElement("p");
      p.className = "cb-read-" + (item.kind === "正文" ? "body" : "label");
      p.textContent = (item.kind === "正文" ? "" : item.kind + "：") + item.text;
      box.appendChild(p);
    });
    const hidden = Number(data.hidden_count || 0);
    const note = $("rd-note");
    note.textContent = hidden > 0
      ? "这一页有 " + hidden + " 处收起来的内容没读（隐藏的调试信息、折叠区）。"
      : "已读完这一页看得到的文字。";
    note.hidden = false;
    btn.disabled = !text;
    refresh.disabled = false;
    show("read");
  }

  /**
   * 复制结果写在读取说明那一行上，但它必须写在读取结果之后：
   * 「重新读一次」会重跑 doReadPage 并把那一行重置成读取说明，
   * 如果把复制结果写在别处，用户点完复制再刷新就看不到自己复制过什么。
   */

  async function copyPageText() {
    clearError();
    if (!readText) { fail("这一页还没有读到可复制的内容。"); return; }
    const done = await CB.copy(readText);
    const note = $("rd-note");
    note.hidden = false;
    note.textContent = (done && done.ok
      ? "已复制到剪贴板。内容就在上面，也可手动选择。"
      : "没能自动复制，请手动选择上面的内容。");
  }

  $("btn-pair").addEventListener("click", async () => {
    clearError();
    const code = $("pair-code").value.trim();
    if (!code) { fail("请先填配对码。"); return; }
    $("btn-pair").disabled = true;
    const res = await msg({ type: "cb:pair", code, client_name: $("pair-name").value.trim() || "随身助手" });
    $("btn-pair").disabled = false;
    if (!res.ok) { fail(friendly(res)); return; }
    who.textContent = "已连接：" + (res.data.display_name || "本人");
    show("tasks");
    await loadTasks();
  });

  $("btn-fill").addEventListener("click", doFill);
  $("btn-back").addEventListener("click", () => { current = null; show("tasks"); });
  $("btn-done").addEventListener("click", () => { current = null; show("tasks"); loadTasks(); });
  $("btn-retry-receipt").addEventListener("click", retryReceipt);
  $("btn-context-back").addEventListener("click", async () => {
    ctxState = null;
    ctxSeq += 1;
    show("tasks");
    // 回列表要重新取一次：can_prepare 会随协商进度变，
    // 沿用旧列表会让「去代填」按钮出现在已经不能代填的事项上。
    await loadTasks();
  });
  $("btn-read-copy").addEventListener("click", copyPageText);
  $("btn-read-refresh").addEventListener("click", async () => {
    clearError();
    if (typeof readTab === "number") { await doReadPage(readTab); }
  });
  $("btn-read-back").addEventListener("click", () => {
    readTab = null;
    readText = "";
    show("tasks");
  });
  $("btn-copy-terms").addEventListener("click", copyTerms);
  $("btn-copy-export").addEventListener("click", copyExport);
  $("btn-revoke").addEventListener("click", async () => {
    clearError();
    const res = await msg({ type: "cb:revoke" });
    if (!res.ok) { fail(friendly(res)); return; }
    who.textContent = "未连接";
    show("pair");
  });

  refreshStatus();
})();
