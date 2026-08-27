/* OIDC prompt=none 静默续期回调页。
 * 只做一件事：校验 state 后把授权码回传给同源父窗口（工作台主页面）。
 * 授权码通过 postMessage 限定在本 origin，不经过 BroadcastChannel，也不写入日志。
 * Token 交换与 issuer/audience 校验由父窗口执行（见 session.js exchangeToken）。
 */
(function () {
  "use strict";
  var query = new URLSearchParams(location.search);
  var expectedState = null;
  try { expectedState = sessionStorage.getItem("silent_state"); } catch (error) {}
  var message;
  if (!query.get("state") || !expectedState || query.get("state") !== expectedState) {
    message = { type: "coifesp:silent", ok: false, error: "state" };
  } else if (query.has("error")) {
    message = { type: "coifesp:silent", ok: false, error: query.get("error") };
  } else if (!query.get("code")) {
    message = { type: "coifesp:silent", ok: false, error: "code" };
  } else {
    message = { type: "coifesp:silent", ok: true, code: query.get("code") };
  }
  try { sessionStorage.removeItem("silent_state"); } catch (error) {}
  try {
    parent.postMessage(message, location.origin);
  } catch (error) {
    window.postMessage(message, location.origin);
  }
})();
