/**
 * 模拟商店页面自己的业务逻辑。
 *
 * 重点是：这些状态由页面算，不由插件算。插件只写控件值，
 * 总价重算、库存、能否提交都是页面自己的规则——这样回读时
 * 看到的"页面业务状态"才是真的，而不是插件自说自话。
 */
(function () {
  // 单价从页面 DOM 读，两个商品页共用这一份脚本才不会算错。
  var priceNode = document.querySelector('[data-cb-price]');
  var PRICE = priceNode ? parseInt(priceNode.textContent, 10) : 0;
  var size = document.querySelector('[data-cb-field="size"]');
  var qty = document.querySelector('[data-cb-field="quantity"]');
  var name = document.querySelector('[data-cb-field="recipient_name"]');
  var phone = document.querySelector('[data-cb-field="recipient_phone"]');
  var addr = document.querySelector('[data-cb-field="delivery_address"]');
  var total = document.querySelector("[data-cb-total]");
  var stock = document.querySelector("[data-cb-stock]");
  var btn = document.querySelector("[data-cb-order-submit]");
  var hint = document.getElementById("shop-hint");
  var extState = document.getElementById("ext-state");

  function recalc() {
    var n = parseInt(qty.value, 10);
    if (!n || n < 1) n = 1;
    total.textContent = String(PRICE * n);
    // 业务规则：规格、收货人、地址都填了才开放提交；数量超出上限视为缺货。
    var overStock = n > 5;
    stock.textContent = overStock ? "库存不足" : "有货";
    // 业务规则：有规格的商品要选规格，收货人、电话、地址都填了才开放提交；
    // 数量超出上限视为缺货。商品编号是只读的，不参与「是否可提交」。
    // 没有规格控件的商品（如绘画套装）不要求选规格。
    var sizeOk = size ? Boolean(size.value) : true;
    var ready = sizeOk && Boolean(name.value.trim())
      && Boolean(phone.value.trim()) && Boolean(addr.value.trim()) && !overStock;
    btn.disabled = !ready;
    hint.textContent = ready
      ? "参数已齐，可提交（模拟）。这一步由用户本人点，插件不代点。"
      : (size ? "规格和收货资料填齐后才会开放提交。" : "收货资料填齐后才会开放提交。");
  }

  [size, qty, name, phone, addr].filter(Boolean).forEach(function (el) {
    el.addEventListener("input", recalc);
    el.addEventListener("change", recalc);
  });

  btn.addEventListener("click", function () {
    hint.textContent = "这是模拟商店，没有真实下单、付款或配送。";
  });

  function detectExtension() {
    // content script 会在 documentElement 上打标记；有标记才说明扩展真的装上了。
    var marker = document.documentElement.getAttribute("data-cb-extension");
    extState.textContent = marker ? "已加载（" + marker + "）" : "未加载";
    extState.style.color = marker ? "#146c43" : "#b3261e";
  }

  detectExtension();
  // 扩展注入可能晚于页面脚本，稍后再看一次。
  setTimeout(detectExtension, 600);
  recalc();
})();
