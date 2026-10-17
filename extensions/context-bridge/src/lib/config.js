/**
 * 核心服务地址。整个插件只有一个地址，且只在这里定义。
 *
 * 默认指向真实主服务（8000）。联调用的契约替身
 * （fixtures/contract-stub-server.py，默认 8098）不写进这里：
 * 测试需要时会复制一份扩展到临时目录、只改这一行，明确标注是开发副本。
 *
 * 这里不能读网页传进来的地址。当前页面想把自己伪装成核心服务时，
 * 后台不能跟着改——这是凭证不被网页挪用的前提。
 */

(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.CBConfig = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  return {
    CORE_ORIGIN: "http://127.0.0.1:8000",
    // 开发副本用：置成替身地址后，这个副本只连替身，不进提交。
    DEV_CORE_ORIGIN: null,
  };
});
