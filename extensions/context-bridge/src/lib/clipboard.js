/**
 * 复制到剪贴板。扩展页面里 navigator.clipboard 需要文档处于焦点状态，
 * 而 popup 刚打开时未必拿到焦点；两种办法都试，失败就明确说没复制成功。
 *
 * 这里不引入第三种兜底：execCommand("copy") 在 Chrome 里仍可用，
 * 但必须在用户点击的同一轮同步调用才能保留权限。
 */

(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(typeof self !== "undefined" ? self : this);
  } else {
    root.CBClipboard = factory(root);
  }
})(typeof self !== "undefined" ? self : this, function (root) {
  function legacyCopy(text) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (err) {
      return false;
    }
  }

  /**
   * 复制文本。返回 {ok, method, reason}，不抛异常——
   * 调用方要的是「告诉用户成没成功」，不是一个异常。
   */
  async function copy(text) {
    if (typeof text !== "string" || !text.trim()) {
      return { ok: false, method: "", reason: "empty" };
    }
    if (root.navigator && root.navigator.clipboard && root.navigator.clipboard.writeText) {
      try {
        await root.navigator.clipboard.writeText(text);
        return { ok: true, method: "clipboard-api" };
      } catch (err) {
        // 焦点没拿到时这里会失败，退回 execCommand 再试一次。
      }
    }
    if (root.document && typeof root.document.execCommand === "function") {
      if (legacyCopy(text)) { return { ok: true, method: "exec-command" }; }
    }
    return { ok: false, method: "", reason: "denied" };
  }

  return { copy };
});
