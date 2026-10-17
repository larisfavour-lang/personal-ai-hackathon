/**
 * 模拟商店页面适配器。只做三件事：按字段白名单填控件、读回页面真实状态、
 * 把状态回给后台。它不点「提交订单」，也不改价格和库存——
 * 那些是页面自己的业务状态，代填无权改动。
 *
 * 适配器只认 data-cb-field 标记的控件。页面上没有对应控件的字段
 * 一律算未处理，并在回读里标出来，不静默跳过。
 */

(function () {
  if (window.__cbAdapterLoaded) return;
  window.__cbAdapterLoaded = true;

  const ADAPTER_ID = "mock-shop-v1";
  const FIELDS = (window.CBContract && window.CBContract.ADAPTER_FIELDS) || [];

  /** 规格下拉框的真实选项，用于核对填入值是不是页面认的一个。 */
  function readSelectOptions(select) {
    return Array.from(select.options).map((o) => ({ value: o.value, label: o.textContent.trim() }));
  }

  function readProductId() {
    const node = document.querySelector("[data-cb-product-id]");
    return node ? node.getAttribute("data-cb-product-id") : "";
  }

  function readPageState() {
    const fields = {};
    const controls = {};
    FIELDS.forEach((key) => {
      const node = document.querySelector('[data-cb-field="' + key + '"]');
      if (!node) {
        controls[key] = { present: false };
        return;
      }
      const value = node.value === undefined || node.value === null ? "" : String(node.value);
      fields[key] = value;
      controls[key] = {
        present: true,
        value,
        tag: node.tagName.toLowerCase(),
        // 页面业务状态：这一项是不是被禁用、是不是必填、有没有选项可选。
        disabled: Boolean(node.disabled),
        readonly: Boolean(node.readOnly),
        options: node.tagName === "SELECT" ? readSelectOptions(node) : undefined,
      };
    });
    const orderBtn = document.querySelector("[data-cb-order-submit]");
    return {
      adapter_id: ADAPTER_ID,
      product_id: readProductId(),
      fields,
      controls,
      page_status: {
        // 这一页的标题与库存、价格由页面自己算，插件只读不改。
        title: (document.querySelector("[data-cb-title]") || {}).textContent || "",
        stock: (document.querySelector("[data-cb-stock]") || {}).textContent || "",
        total: (document.querySelector("[data-cb-total]") || {}).textContent || "",
        can_submit: Boolean(orderBtn && !orderBtn.disabled),
      },
      url: location.href,
    };
  }

  /**
   * 填字段。缺控件、值不在选项里都算失败，不猜也不改页面业务状态。
   *
   * 只读控件（商品编号）不写：页面自己标注当前是哪一件商品，
   * 插件写了就等于伪造商品。这里改成核对——值和参数包不一致时
   * 明确报出来，让上层按「页面商品与约定不符」拒绝。
   */
  function fillFields(fields) {
    const filled = {};
    const problems = [];
    (fields || []).forEach((f) => {
      const node = document.querySelector('[data-cb-field="' + f.key + '"]');
      if (!node) {
        problems.push({ field: f.key, reason: "页面没有这个字段的控件" });
        return;
      }
      if (node.readOnly || node.disabled) {
        const current = node.value === undefined || node.value === null ? "" : String(node.value);
        if (current.trim() !== String(f.value).trim()) {
          problems.push({ field: f.key, reason: "页面标注的是 " + (current || "空") + "，和参数包不一致" });
          return;
        }
        filled[f.key] = current;
        return;
      }
      if (node.tagName === "SELECT") {
        const match = Array.from(node.options).find((o) => o.value === f.value || o.textContent.trim() === f.value);
        if (!match) {
          problems.push({ field: f.key, reason: "页面没有这个选项：" + f.value });
          return;
        }
        node.value = match.value;
      } else {
        node.value = f.value;
      }
      // 让页面自己的脚本照常重算总价等显示值，插件不代算。
      node.dispatchEvent(new Event("input", { bubbles: true }));
      node.dispatchEvent(new Event("change", { bubbles: true }));
      filled[f.key] = String(node.value);
    });
    return { filled, problems };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message || typeof message.type !== "string") return false;
    if (message.type === "cb:read-state") {
      sendResponse({ ok: true, state: readPageState() });
      return false;
    }
    if (message.type === "cb:fill-fields") {
      const allowed = (message.fields || []).filter((f) => FIELDS.includes(f.key));
      const rejected = (message.fields || []).filter((f) => !FIELDS.includes(f.key));
      const result = fillFields(allowed);
      rejected.forEach((f) => result.problems.push({ field: f.key, reason: "该字段不在本适配器白名单内" }));
      sendResponse({
        ok: result.problems.length === 0,
        data: {
          filled: result.filled,
          problems: result.problems,
          state: readPageState(),
        },
      });
      return false;
    }
    return false;
  });

  // 页面自己会读这个标记判断扩展在不在，用于安装验证。
  document.documentElement.setAttribute("data-cb-extension", ADAPTER_ID);
})();
