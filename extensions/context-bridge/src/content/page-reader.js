/**
 * 页面内容读取器。运行在 content script 里，只做一件事：
 * 把当前页面「页面自己显示给所有人看的文字」取出来，交给插件面板展示。
 *
 * 三条硬边界，缺一条都不叫「读这一页」：
 *
 * 1. 只读页面内容，不读访问者自己填进去的值。
 *    表单控件的 value 是这位用户刚打的字——收货地址、电话、鞋码、
 *    甚至密码。那些不是页面内容，是用户隐私。所以这里一律不读
 *    input/textarea/select 的当前值，只读控件旁边那个说明标签。
 *    页面里 <option>30 码</option> 这种选项文字是页面写死的，
 *    照常读；访问者选中了哪一项不读。
 *
 * 2. 只读看得见的文字。display:none、visibility:hidden、
 *    尺寸为 0 的元素，以及 aria-hidden 的内容一律跳过——
 *    前者是页面藏起来的调试信息，后者是给读屏软件用的重复文本。
 *    closed 的 <details> 内容同样不读：页面自己把它收起来了。
 *
 * 3. 不替用户做任何判断。读到的只是文字，不推断需求、不比对参数，
 *    也不自动发去任何地方。复制和代填仍然由用户主动点。
 *
 * 不点任何按钮、不改任何页面状态、不发起网络请求。
 */
(function () {
  // 浏览器里重复注入时直接退出；Node 单元测试没有 window，跳过这个守卫，
  // 否则整个模块加载就抛 ReferenceError，一条边界都测不到。
  const hasWindow = typeof window !== "undefined";
  if (hasWindow && window.__cbPageReaderLoaded) return;
  if (hasWindow) window.__cbPageReaderLoaded = true;

  // 节点类型常量。浏览器里取全局 Node；Node 单元测试没有这个全局，
  // 用同样的数值兜底，避免整个模块加载就失败。
  const ELEMENT_NODE = 1;
  const TEXT_NODE = 3;

  /** 取一层元素自身的文字，不含子元素——标题、单元格这类容器靠它分开。 */
  function ownText(el) {
    const parts = [];
    el.childNodes.forEach((node) => {
      if (node.nodeType === TEXT_NODE) {
        const t = (node.textContent || "").replace(/\s+/g, " ").trim();
        if (t) parts.push(t);
      }
    });
    return parts.join(" ");
  }

  /**
   * 这个元素对普通访问者是否看得见。
   * 只看本元素和祖先链的 display/visibility，不递归整棵树，
   * 因为判断「这一段整体收起来了」只需要最近的隐藏祖先。
   */
  function isVisible(el) {
    let node = el;
    while (node && node.nodeType === ELEMENT_NODE) {
      const style = node.ownerDocument.defaultView.getComputedStyle(node);
      if (!style) return false;
      if (style.display === "none" || style.visibility === "hidden") return false;
      if (node.getAttribute && node.getAttribute("aria-hidden") === "true") return false;
      if (node.tagName === "DETAILS" && !node.open) return false;
      if (node.tagName === "TEMPLATE" || node.tagName === "SCRIPT"
          || node.tagName === "STYLE" || node.tagName === "NOSCRIPT") return false;
      node = node.parentElement;
    }
    const box = el.getBoundingClientRect();
    // 尺寸为 0 的元素等于没显示，但用 absolute 拉到屏外的也要算看不见。
    if (box.width === 0 && box.height === 0) return false;
    return true;
  }

  /** 页面里不该被当成正文的容器：导航、页脚、脚本、模板。 */
  const SKIP_TAGS = new Set([
    "SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "IFRAME", "OBJECT", "EMBED",
    "NAV", "FOOTER", "SVG", "CANVAS", "VIDEO", "AUDIO", "PICTURE", "SOURCE",
  ]);

  /**
   * 表单控件的说明文字。取 label 元素的文字，不取 value：
   * 「联系电话」这四个字是页面写的，「13800000000」是这位用户填的。
   * 密码控件整个跳过：连说明都不给，页面内容用不到它。
   */
  function controlLabel(control) {
    if (control.type === "password" || control.type === "hidden") return "";
    const label = control.labels && control.labels.length
      ? control.labels[0]
      : control.closest("label");
    if (!label) return "";
    return ownText(label).trim();
  }

  /** 取下拉框里页面写死的选项文字。选中的那一项不单独标出来。 */
  function selectOptions(select) {
    if (select.type === "password") return [];
    const out = [];
    Array.from(select.options || []).forEach((o) => {
      const text = (o.textContent || "").replace(/\s+/g, " ").trim();
      if (text && out.indexOf(text) < 0) out.push(text);
    });
    return out;
  }

  /**
   * 从给定根节点往下读。root 默认是 document.body。
   * 测试传一个人造节点列表进来，验的是边界规则而不是真实页面。
   */
  function readFrom(root) {
    const texts = [];
    const seen = new Set();
    let hiddenCount = 0;

    const push = (kind, text, el) => {
      const clean = (text || "").replace(/\s+/g, " ").trim();
      if (!clean) return;
      const key = kind + "|" + clean;
      if (seen.has(key)) return;   // 同一段文字被多个容器包着，只算一次
      seen.add(key);
      texts.push({ kind, text: clean });
    };

    const walk = (el) => {
      if (el.nodeType !== ELEMENT_NODE) return;
      if (SKIP_TAGS.has(el.tagName)) return;
      if (!isVisible(el)) {
        hiddenCount += 1;
        return;
      }
      const tag = el.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") {
        // 只收控件说明，不收用户填的值。没有 label 的控件什么都不收。
        push("表单说明", controlLabel(el), el);
        return;
      }
      if (tag === "SELECT") {
        const label = controlLabel(el);
        const options = selectOptions(el);
        if (label) push("表单说明", label + "：" + options.join("、"), el);
        else if (options.length) push("表单说明", options.join("、"), el);
        return;
      }
      if (tag === "BUTTON") {
        push("按钮", ownText(el) || (el.getAttribute("aria-label") || ""), el);
        return;
      }
      const own = ownText(el);
      if (own) push(tag === "TD" || tag === "TH" ? "表格" : "正文", own, el);
      Array.from(el.children).forEach(walk);
    };

    const fromTest = root !== null && root !== undefined;
    if (fromTest) {
      if (Array.isArray(root)) { root.forEach(walk); } else { walk(root); }
    } else {
      walk(document.body || document.documentElement);
    }

    // 标题与地址单独给，用户要能确认读的是自己正在看的那一页。
    // 单元测试没有真实 document，那两项留空即可——测试验的是边界规则。
    let title = "";
    let description = "";
    let url = "";
    let origin = "";
    if (typeof document !== "undefined") {
      title = (document.title || "").trim();
      const meta = document.querySelector('meta[name="description"]');
      description = meta ? (meta.getAttribute("content") || "").trim() : "";
    }
    if (typeof location !== "undefined") {
      url = location.href;
      origin = location.origin;
    }

    return {
      url,
      origin,
      title,
      description,
      texts,
      hidden_count: hiddenCount,
      read_at: new Date().toISOString(),
    };
  }

  /** 读当前整页。 */
  function readPage() {
    return readFrom(null);
  }

  /**
   * 把读到的内容压成一段可直接复制的中文文本。
   * 表格里的单元格用「标签：值」写清楚，用户粘到别处也好读。
   */
  function pageText(page) {
    const lines = [];
    if (page.title) lines.push("页面标题：" + page.title);
    if (page.description) lines.push("页面说明：" + page.description);
    lines.push("页面地址：" + page.url);
    lines.push("");
    (page.texts || []).forEach((item) => {
      const prefix = item.kind === "正文" ? "" : (item.kind + "：");
      lines.push(prefix + item.text);
    });
    return lines.join("\n").trim();
  }

  const api = { readPage, pageText };

  // 测试入口：只在 Node 单元测试里用，真实浏览器路径不走这里。
  // 它复用同一套 walk/isVisible/ownText，所以测的就是真实规则。
  if (typeof module === "object" && module.exports) {
    api.__testReadFrom = (nodes) => readFrom(Array.isArray(nodes) ? nodes : [nodes]);
  }

  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else if (hasWindow) {
    window.CBPageReader = api;
  }

  // 网页侧不监听任何消息：读取由插件面板发起，网页脚本无法驱使。
  // 扩展页面通过 chrome.tabs.sendMessage 直接调下面的消息入口。
  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
      if (!message || message.type !== "cb:read-page") return false;
      try {
        const page = readPage();
        sendResponse({ ok: true, data: page, text: pageText(page) });
      } catch (err) {
        sendResponse({
          ok: false,
          error: { code: "read_failed", message: "读取这一页失败：" + (err && err.message ? err.message : "未知错误"), retryable: true },
        });
      }
      return false;
    });
  }
})();
