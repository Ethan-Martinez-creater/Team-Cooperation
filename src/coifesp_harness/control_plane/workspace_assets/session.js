/* COIFESP 工作台会话生命周期协调器
 * 职责：静默续期（single-flight）、401 恢复与重放策略、失效提示与路由恢复、
 *       多标签页退出同步、全局退出（OIDC end-session / builtin-local 服务端撤销）。
 * 安全约定：Token 只保存在浏览器 sessionStorage 与内存，绝不写入 BroadcastChannel
 *           消息；issuer/audience 校验在令牌交换后执行；非幂等 POST 不自动重放。
 * 依赖注入：storage / channel / now / renew 均可替换，纯逻辑部分可在 Node 中单测。
 */
(function (global) {
  "use strict";
  var CHANNEL_NAME = "coifesp-session";
  var ROUTE_KEY = "coifesp_return_route";
  var SILENT_STATE_KEY = "silent_state";
  var PKCE_VERIFIER_KEY = "pkce_verifier";
  var ID_TOKEN_KEY = "id_token";
  var RENEW_BEFORE_MS = 60000;
  var SILENT_TIMEOUT_MS = 20000;

  function randomB64(bytes) {
    var values = new Uint8Array(bytes);
    global.crypto.getRandomValues(values);
    var binary = "";
    for (var i = 0; i < values.length; i++) binary += String.fromCharCode(values[i]);
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function b64urlEncode(bytes) {
    return btoa(String.fromCharCode.apply(null, bytes))
      .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function sha256B64(value) {
    var data = new TextEncoder().encode(value);
    return global.crypto.subtle.digest("SHA-256", data).then(function (digest) {
      return b64urlEncode(new Uint8Array(digest));
    });
  }

  function decodePayload(token) {
    try {
      var body = token.split(".")[1];
      return JSON.parse(atob(body.replace(/-/g, "+").replace(/_/g, "/")));
    } catch (error) {
      return null;
    }
  }

  /* ---- 纯函数（可在 Node 中直接测试） ---- */
  function shouldReplay(method, idempotencyKey) {
    var m = String(method || "GET").toUpperCase();
    if (m === "GET" || m === "HEAD" || m === "OPTIONS") return true;
    return !!(idempotencyKey && String(idempotencyKey).length > 0);
  }

  function validateSilentCallback(params, expectedState) {
    if (!params || !expectedState) return { ok: false, reason: "missing" };
    if (params.error) return { ok: false, reason: "provider:" + params.error };
    if (!params.state || params.state !== expectedState) return { ok: false, reason: "state" };
    if (!params.code) return { ok: false, reason: "code" };
    return { ok: true, code: params.code };
  }

  function validateIdToken(idToken, expectedIssuer, expectedAudience, nowMs) {
    if (!idToken) return { ok: false, reason: "missing" };
    var claims = decodePayload(idToken);
    if (!claims) return { ok: false, reason: "malformed" };
    if (!expectedIssuer || claims.iss !== expectedIssuer.replace(/\/+$/, ""))
      return { ok: false, reason: "issuer" };
    var audience = claims.aud;
    var audiences = Array.isArray(audience) ? audience : [audience];
    if (!expectedAudience || audiences.indexOf(expectedAudience) < 0)
      return { ok: false, reason: "audience" };
    if (typeof claims.exp !== "number" || claims.exp * 1000 <= nowMs)
      return { ok: false, reason: "expired" };
    return { ok: true, claims: claims };
  }

  function hasSameSubject(currentToken, renewedIdToken) {
    var current = decodePayload(currentToken);
    var renewed = decodePayload(renewedIdToken);
    return !!(current && renewed && current.sub && current.sub === renewed.sub);
  }

  function normalizeRoute(route) {
    if (!route || typeof route !== "object") return null;
    var view = String(route.view || "");
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(view)) return null;
    var normalized = { view: view };
    if (route.project_id && /^[A-Za-z0-9._:-]{1,128}$/.test(String(route.project_id)))
      normalized.project_id = String(route.project_id);
    if (route.run_id && /^[A-Za-z0-9._:-]{1,128}$/.test(String(route.run_id)))
      normalized.run_id = String(route.run_id);
    return normalized;
  }

  function saveRoute(storage, route) {
    var normalized = normalizeRoute(route);
    if (!normalized) return;
    try { storage.setItem(ROUTE_KEY, JSON.stringify(normalized)); } catch (error) {}
  }

  function takeRoute(storage) {
    try {
      var raw = storage.getItem(ROUTE_KEY);
      storage.removeItem(ROUTE_KEY);
      return raw ? normalizeRoute(JSON.parse(raw)) : null;
    } catch (error) {
      return null;
    }
  }

  /* ---- 协调器 ---- */
  function createCoordinator(options) {
    var storage = options.storage || global.sessionStorage;
    var now = options.now || function () { return Date.now(); };
    var channel = options.channel || (
      typeof global.BroadcastChannel !== "undefined"
        ? new global.BroadcastChannel(CHANNEL_NAME)
        : null
    );
    var renewer = options.renew;
    var onToken = options.onToken || null;
    var onExpired = options.onExpired || null;
    var logger = options.logger || null;

    var token = null;
    var expiresAt = null;
    var authMode = null;
    var oidcConfig = null;
    var renewPromise = null;
    var timer = null;

    function log(message) { if (logger) logger(message); }

    function attach(nextToken, nextExpiresAt, nextMode, nextOidcConfig) {
      token = nextToken;
      expiresAt = nextExpiresAt ? new Date(nextExpiresAt).getTime() : null;
      authMode = nextMode;
      oidcConfig = nextOidcConfig || null;
      scheduleRenewal();
    }

    function getToken() { return token; }

    function renewNow() {
      if (renewPromise) return renewPromise;
      if (!renewer) { return Promise.reject(new Error("会话续期不可用")); }
      renewPromise = Promise.resolve()
        .then(function () {
          return renewer({ authMode: authMode, oidcConfig: oidcConfig, currentToken: token });
        })
        .then(function (result) {
          token = result.access_token;
          expiresAt = result.expires_at
            ? new Date(result.expires_at).getTime()
            : null;
          if (onToken) onToken(token, expiresAt);
          if (channel) {
            try { channel.postMessage({ type: "renewed", at: now() }); } catch (error) {}
          }
          log("session.renewed");
          return { ok: true, token: token };
        })
        .catch(function (error) {
          log("session.renew_failed " + (error && error.message ? error.message : ""));
          throw error;
        })
        .finally(function () { renewPromise = null; });
      return renewPromise;
    }

    function onUnauthorized(request) {
      var method = request.method || "GET";
      var idempotencyKey = request.idempotencyKey || null;
      return renewNow()
        .then(function () {
          if (shouldReplay(method, idempotencyKey)) {
            return request.replay();
          }
          var error = new Error("会话已续期，请重试刚才的操作");
          error.needsRetry = true;
          throw error;
        })
        .catch(function (error) {
          if (error && error.needsRetry) throw error;
          if (onExpired) onExpired(error);
          throw new Error("登录已过期，请重新登录");
        });
    }

    function scheduleRenewal() {
      if (timer !== null) { clearTimeout(timer); timer = null; }
      if (!authMode || !expiresAt) return;
      var remaining = expiresAt - now();
      if (remaining <= RENEW_BEFORE_MS) {
        renewNow().catch(function () {});
        return;
      }
      timer = setTimeout(function () {
        renewNow().catch(function () {});
      }, Math.min(2147000000, Math.max(1000, remaining - RENEW_BEFORE_MS)));
    }

    function dispose() {
      if (timer !== null) { clearTimeout(timer); timer = null; }
      if (channel && typeof channel.close === "function") { channel.close(); }
    }

    function localLogout() {
      if (authMode === "builtin" && token) {
        try {
          fetch("/v1/sessions/current", {
            method: "DELETE",
            headers: { Authorization: "Bearer " + token },
          }).catch(function () {});
        } catch (error) {}
      } else if (authMode === "local" && token) {
        try {
          fetch("/app/local-session:revoke", {
            method: "POST",
            headers: { Authorization: "Bearer " + token },
          }).catch(function () {});
        } catch (error) {}
      }
      var idToken = null;
      try { idToken = storage.getItem(ID_TOKEN_KEY); } catch (error) {}
      try { storage.clear(); } catch (error) {}
      token = null;
      expiresAt = null;
      if (authMode === "oidc" && oidcConfig && oidcConfig.end_session_endpoint) {
        try {
          var target = new URL(oidcConfig.end_session_endpoint);
          var params = new URLSearchParams();
          if (idToken) params.set("id_token_hint", idToken);
          if (oidcConfig.post_logout_redirect_uri) {
            params.set("post_logout_redirect_uri", oidcConfig.post_logout_redirect_uri);
          }
          if (params.toString()) target.search = params.toString();
          global.location.assign(target.toString());
          return;
        } catch (error) {}
      }
      global.location.assign("/app/");
    }

    function logout() {
      log("session.logout");
      if (channel) {
        try { channel.postMessage({ type: "logout", at: now() }); } catch (error) {}
      }
      localLogout();
    }

    if (channel) {
      channel.onmessage = function (event) {
        var message = event.data || {};
        if (message.type === "logout") {
          log("session.logout_from_other_tab");
          try { storage.clear(); } catch (error) {}
          token = null;
          expiresAt = null;
          global.location.assign("/app/");
        } else if (message.type === "renewed") {
          log("session.renewed_in_other_tab");
        }
      };
    }

    return {
      attach: attach,
      getToken: getToken,
      onUnauthorized: onUnauthorized,
      logout: logout,
      renewNow: renewNow,
      scheduleRenewal: scheduleRenewal,
      dispose: dispose,
      saveRoute: function (route) { return saveRoute(storage, route); },
      takeRoute: function () { return takeRoute(storage); },
    };
  }

  /* ---- OIDC prompt=none 静默续期（浏览器环境专用） ---- */
  function oidcRenew(options) {
    var oidcConfig = options.oidcConfig;
    if (!oidcConfig || !oidcConfig.issuer) return Promise.reject(new Error("OIDC 未配置"));
    var storage = options.storage || global.sessionStorage;
    var state = randomB64(18);
    var verifier = randomB64(32);
    try {
      storage.setItem(SILENT_STATE_KEY, state);
      storage.setItem(PKCE_VERIFIER_KEY, verifier);
    } catch (error) {
      return Promise.reject(new Error("浏览器存储不可用"));
    }
    return sha256B64(verifier).then(function (challenge) {
      var callbackUrl = global.location.origin + "/app/silent-callback.html";
      var authUrl = new URL(oidcConfig.issuer.replace(/\/+$/, "") + "/protocol/openid-connect/auth");
      authUrl.search = new URLSearchParams({
        client_id: oidcConfig.client_id,
        response_type: "code",
        scope: "openid",
        redirect_uri: callbackUrl,
        code_challenge: challenge,
        code_challenge_method: "S256",
        state: state,
        prompt: "none",
      }).toString();
      return new Promise(function (resolve, reject) {
        var settled = false;
        var frame = null;
        var timer = setTimeout(function () {
          if (!settled) {
            settled = true;
            cleanup();
            reject(new Error("静默续期超时"));
          }
        }, SILENT_TIMEOUT_MS);
        function cleanup() {
          if (timer) { clearTimeout(timer); timer = null; }
          if (frame) { frame.remove(); frame = null; }
          global.removeEventListener("message", onMessage);
        }
        function onMessage(event) {
          if (event.origin !== global.location.origin) return;
          var message = event.data || {};
          if (message.type !== "coifesp:silent") return;
          if (settled) return;
          settled = true;
          cleanup();
          if (!message.ok || !message.code) {
            reject(new Error(message.error ? "静默续期被拒绝：" + message.error : "静默续期失败"));
            return;
          }
          exchangeToken(message.code, oidcConfig, storage, options.currentToken).then(resolve, reject);
        }
        global.addEventListener("message", onMessage);
        frame = document.createElement("iframe");
        frame.setAttribute("aria-hidden", "true");
        frame.style.display = "none";
        frame.src = authUrl.toString();
        document.body.appendChild(frame);
      });
    });
  }

  function exchangeToken(code, oidcConfig, storage, currentToken) {
    var body = new URLSearchParams({
      grant_type: "authorization_code",
      client_id: oidcConfig.client_id,
      code: code,
      redirect_uri: global.location.origin + "/app/silent-callback.html",
      code_verifier: storage.getItem(PKCE_VERIFIER_KEY) || "",
    });
    return fetch(
      oidcConfig.issuer.replace(/\/+$/, "") + "/protocol/openid-connect/token",
      {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body,
      }
    ).then(function (response) {
      if (!response.ok) throw new Error("身份服务拒绝了续期交换");
      return response.json();
    }).then(function (tokens) {
      var idToken = tokens.id_token || null;
      var validation = validateIdToken(idToken, oidcConfig.issuer, oidcConfig.audience, Date.now());
      if (idToken && !validation.ok) throw new Error("续期令牌校验失败：" + validation.reason);
      if (!hasSameSubject(currentToken, idToken)) throw new Error("续期身份与当前标签不一致");
      try { storage.setItem(ID_TOKEN_KEY, idToken); } catch (error) {}
      return {
        access_token: tokens.access_token,
        expires_at: Date.now() + (Number(tokens.expires_in) || 300) * 1000,
      };
    });
  }

  function defaultRenew(options) {
    if (options.authMode === "oidc") return oidcRenew(options);
    if (options.authMode === "local") {
      var headers = { Accept: "application/json" };
      if (options.currentToken) headers.Authorization = "Bearer " + options.currentToken;
      return fetch("/app/local-session:renew", {
        method: "POST",
        headers: headers,
        body: "{}",
      }).then(function (response) {
        if (response.status === 401) throw new Error("会话已失效");
        if (!response.ok) throw new Error("续期失败");
        return response.json();
      });
    }
    if (options.authMode === "builtin") {
      var builtinHeaders = { Accept: "application/json" };
      if (options.currentToken) builtinHeaders.Authorization = "Bearer " + options.currentToken;
      return fetch("/v1/sessions/current:renew", {
        method: "POST",
        headers: builtinHeaders,
        body: "{}",
      }).then(function (response) {
        if (response.status === 401) throw new Error("会话已失效");
        if (!response.ok) throw new Error("续期失败");
        return response.json();
      });
    }
    return Promise.reject(new Error("当前登录模式不支持续期"));
  }

  global.CoifespSession = {
    CHANNEL_NAME: CHANNEL_NAME,
    shouldReplay: shouldReplay,
    validateSilentCallback: validateSilentCallback,
    validateIdToken: validateIdToken,
    hasSameSubject: hasSameSubject,
    saveRoute: saveRoute,
    takeRoute: takeRoute,
    normalizeRoute: normalizeRoute,
    createCoordinator: createCoordinator,
    oidcRenew: oidcRenew,
    defaultRenew: defaultRenew,
  };
})(typeof window !== "undefined" ? window : globalThis);
