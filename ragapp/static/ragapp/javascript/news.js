/* ragapp/static/ragapp/javascript/news.js */

(function () {
  "use strict";

  const markChatPage = () => {
    if (document.getElementById("chatThread")) document.body.classList.add("mz-chat-page");
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", markChatPage, { once: true });
  } else {
    markChatPage();
  }

  // ✅ 중복 로드 방지 (다른 기능 영향 없음)
  if (typeof window !== "undefined") {
    if (window.__NEWS_INITED__) return;
    window.__NEWS_INITED__ = true;
  }

  /* ============================================================
   *  0) 작은 도우미들
   * ============================================================ */

  function escHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function stripTrailingColon(t) {
    return String(t || "").replace(/:\s*$/, "").trim();
  }

  function cleanLeading(id) {
    var el = document.getElementById(id);
    if (!el) return;
    var h = el.innerHTML;
    if (!h) return;
    el.innerHTML = h.replace(/^(<br\s*\/?>\s*)+/i, "").replace(/^\s+/, "");
  }

  function getCookie(name) {
    var cookieValue = null;
    try {
      if (document.cookie && document.cookie !== "") {
        var cookies = document.cookie.split(";");
        for (var i = 0; i < cookies.length; i++) {
          var cookie = cookies[i].trim();
          if (cookie.substring(0, name.length + 1) === name + "=") {
            cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
            break;
          }
        }
      }
    } catch (_) { }
    return cookieValue;
  }

  function setCookie(name, value, days) {
    try {
      var maxAge = days ? "; max-age=" + (days * 24 * 60 * 60) : "";
      document.cookie = name + "=" + encodeURIComponent(value) + "; path=/" + maxAge;
    } catch (_) { }
  }

  // ✅ legacy 호환: consent overlay 등에서 전역 getCookie/setCookie를 찾을 수 있음
  if (typeof window !== "undefined") {
    window.getCookie = window.getCookie || getCookie;
    window.setCookie = window.setCookie || setCookie;
  }

  // ✅ 사용량 위젯 카운트 헬퍼
  function bumpUsage(kind) {
    try {
      if (window.QARAG_USAGE && typeof window.QARAG_USAGE.bump === "function") {
        window.QARAG_USAGE.bump(kind);
      }
    } catch (e) {
      try {
        if (window.dglog) window.dglog("USAGE_BUMP_ERR", e && e.message ? e.message : e);
      } catch (_) { }
    }
  }

  function normalizeOutboundUrlForLinks(u) {
    u = String(u || "").trim();
    if (!u) return "";

    // ✅ 핵심: "/https://..." 또는 "/http://..." 오타 보정
    if (/^\/https?:\/\//i.test(u)) u = u.slice(1);

    // 내부 경로/앵커는 그대로
    if (u[0] === "/" || u[0] === "#") return u;

    // protocol-relative
    if (u.indexOf("//") === 0) return "https:" + u;

    // 스킴이 있으면 http/https만 허용
    if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(u)) {
      return /^https?:\/\//i.test(u) ? u : "";
    }

    // 도메인만 온 경우 → https://
    return "https://" + u;
  }

  /* ============================================================
   *  0.5) (선택) Chat UI 어댑터 — “핸들만 받아 업데이트”
   * ============================================================ */

  function getChatAdapter() {
    try {
      if (typeof window === "undefined") return null;
      return window.NEWS_CHAT_ADAPTER || window.NEWS_ADAPTER || window.ChatUIAdapter || null;
    } catch (_) {
      return null;
    }
  }

  function chatBegin(area, query, loadingText) {
    try {
      var ad = getChatAdapter();
      if (!ad) return null;
      if (typeof ad.append !== "function" || typeof ad.update !== "function") return null;

      ad.append(area, "user", query, {});
      var assistantHandle = ad.append(
        area,
        "assistant",
        loadingText || "처리 중입니다…",
        { aiBadge: true, pending: true }
      );

      return { adapter: ad, area: area, assistant: assistantHandle };
    } catch (_) {
      return null;
    }
  }

  function chatAppend(area, role, text, opts) {
    try {
      var ad = getChatAdapter();
      if (!ad || typeof ad.append !== "function") return null;
      return ad.append(area, role, text, opts || {});
    } catch (_) {
      return null;
    }
  }

  function chatUpdate(chatCtx, text, opts) {
    try {
      if (!chatCtx || !chatCtx.adapter || !chatCtx.assistant) return false;
      chatCtx.adapter.update(chatCtx.assistant, String(text || ""), opts || {});
      return true;
    } catch (_) {
      return false;
    }
  }

  function chatError(chatCtx, msg, extraOpts) {
    try {
      if (!chatCtx) return false;

      var text = String(msg || "");
      var opts = Object.assign({ aiBadge: false, error: true, pending: false }, extraOpts || {});

      if (chatCtx.adapter && typeof chatCtx.adapter.error === "function" && chatCtx.assistant) {
        chatCtx.adapter.error(chatCtx.assistant, text, opts);
        return true;
      }
      return chatUpdate(chatCtx, (opts.limit ? "⏳ " : "❌ ") + text, opts);
    } catch (_) {
      return false;
    }
  }

  function _hasChatAdapterPage() {
    try {
      // ✅ DOM 기반 빠른 판별 (타이밍 이슈에 강함)
      if (document.getElementById("chatThread")) return true;
      if (document.getElementById("chatComposer")) return true;

      // ✅ 어댑터 존재 확인
      const a = window && window.NEWS_CHAT_ADAPTER;
      return !!(
        a &&
        typeof a.append === "function" &&
        typeof a.update === "function" &&
        typeof a.error === "function"
      );
    } catch (_) {
      return false;
    }
  }

  function _applyChatAdapterFlag() {
    const on = !!_hasChatAdapterPage();

    document.documentElement.classList.toggle("is-chat-adapter-page", on);
    if (document.body) document.body.classList.toggle("is-chat-adapter-page", on);

    try {
      document.documentElement.dataset.chatAdapter = on ? "1" : "0";
      if (document.body) document.body.dataset.chatAdapter = on ? "1" : "0";
    } catch (_) { }
  }

  // ✅ 한 번만 하지 말고 재시도
  function _applyChatAdapterFlagWithRetry() {
    _applyChatAdapterFlag();
    setTimeout(_applyChatAdapterFlag, 50);
    setTimeout(_applyChatAdapterFlag, 200);
    setTimeout(_applyChatAdapterFlag, 600);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _applyChatAdapterFlagWithRetry);
  } else {
    _applyChatAdapterFlagWithRetry();
  }


  function _safeEndpoint(u, fallback) {
    try {
      u = String(u || "").trim();
      if (!u) return fallback;
      if (u.indexOf("window.DG_ENDPOINTS.") === 0) return fallback; // 실수 방지
      if (u[0] === "/" || /^https?:\/\//i.test(u)) return u;         // 상대경로/https만 허용
      return fallback;
    } catch (_) {
      return fallback;
    }
  }

  function _pickEndpoint(name, fallback) {
    try {
      const ep = (window.DG_ENDPOINTS && window.DG_ENDPOINTS[name]) ? String(window.DG_ENDPOINTS[name]) : "";
      if (ep && ep !== ("window.DG_ENDPOINTS." + name)) return ep;
    } catch (_) { }
    return fallback;
  }

  // ✅ 여기서만 딱 1번 결정
  var webUrl = _safeEndpoint(_pickEndpoint("webQa", ""), "/api/web_qa");
  var ragUrl = _safeEndpoint((_pickEndpoint("ragQa", "") || window.RAG_QA_MAIN), "/api/rag_qa");
  var policyUrl = _safeEndpoint((_pickEndpoint("qaPolicy", "") || window.QA_POLICY_API_PATH), "/api/qa_policy");

  /* ============================================================
   *  1) PII 탐지/마스킹
   * ============================================================ */

  var PII_RULES = [
    { key: "email", label: "이메일", re: /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi },
    { key: "phone_mobile", label: "휴대폰 번호", re: /\b01[016789][-\s]?\d{3,4}[-\s]?\d{4}\b/g },
    { key: "phone_land", label: "전화번호", re: /\b0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}\b/g },
    { key: "rrn", label: "주민등록번호", re: /\b\d{6}[-\s]?[1-4]\d{6}\b/g },
    { key: "kw_rrn", label: "주민번호 키워드", re: /(주민등록번호|주민번호|ssn|social\s*security)/gi },
    { key: "kw_addr", label: "주소 키워드", re: /(주소|우편번호|도로명|지번)/gi },
  ];

  function detectLikelyPII(text) {
    var s = String(text || "");
    if (!s.trim()) return { found: false, labels: [] };

    var labels = [];
    for (var i = 0; i < PII_RULES.length; i++) {
      var r = PII_RULES[i];
      try {
        if (r.re.test(s)) labels.push(r.label);
        r.re.lastIndex = 0;
      } catch (_) { }
    }

    // 숫자 없는 경우: 키워드만으로는 너무 공격적이므로 제외
    var hasDigit = /\d/.test(s);
    if (!hasDigit) {
      var filtered = labels.filter(function (l) { return !/키워드/.test(l); });
      return { found: filtered.length > 0, labels: filtered };
    }
    return { found: labels.length > 0, labels: labels };
  }

  function redactPII(text) {
    var s = String(text || "");
    if (!s.trim()) return s;

    for (var i = 0; i < PII_RULES.length; i++) {
      var r = PII_RULES[i];
      try {
        if (String(r.key || "").indexOf("kw_") === 0) continue;
        s = s.replace(r.re, "[REDACTED]");
        r.re.lastIndex = 0;
      } catch (_) { }
    }
    return s;
  }

  function showInlineMsg(msgRowEl, kind, msg) {
    var safe = escHtml(msg || "");
    if (msgRowEl) {
      msgRowEl.innerHTML =
        (kind === "err")
          ? '<div class="msg-err" role="alert">❌ ' + safe + "</div>"
          : '<div class="msg-ok" role="status">✅ ' + safe + "</div>";
      return;
    }
    try { alert(msg); } catch (_) { }
  }

  /** query 같은 "의미 보존이 중요한 입력"은 차단 */
  function blockIfPII(text, msgRowEl, fieldLabel, answerBlockEl) {
    var r = detectLikelyPII(text);
    if (!r.found) return false;

    var label = fieldLabel ? (fieldLabel + "에 ") : "";
    var detail = r.labels && r.labels.length ? (" (" + r.labels.join(", ") + ")") : "";

    var msg =
      label + "개인정보로 보이는 내용이 포함되어 전송을 중단했어요" + detail + ". " +
      "해당 부분을 삭제하거나 예시처럼 가명/임의값으로 바꾼 뒤 다시 시도해 주세요. " +
      "이 내용은 저장되거나 검색에 사용되지 않았습니다.";

    // ✅ 기존: msgRow에만 표시
    showInlineMsg(msgRowEl, "err", msg);

    // ✅ 추가: answer-block에도 표시 (chat_adapter가 이 변화를 보고 말풍선 업데이트함)
    try {
      if (answerBlockEl) {
        answerBlockEl.innerHTML = '<div class="msg-err" role="alert">❌ ' + escHtml(msg) + "</div>";
        // 저장 버튼/기타 흐름이 최신 답으로 인식하도록(선택)
        try { answerBlockEl.dataset.latestAnswerText = msg; } catch (_) { }
        try { answerBlockEl.dataset.latestQuestionText = String(text || "").trim(); } catch (_) { }
      }
    } catch (_) { }

    return true;
  }

  /* ============================================================
   *  2) HTML sanitize (whitelist)  — (중요) Evidence UI 토글 속성 보존
   * ============================================================ */

  function sanitizeHTML(unsafe) {
    try {
      var ALLOWED = new Set([
        "B", "I", "STRONG", "EM", "BR",
        "UL", "OL", "LI",
        "P", "CODE", "PRE",
        "A", "H1", "H2", "H3", "H4", "BLOCKQUOTE",
        // ✅ Evidence/UI 토글에 필요한 태그
        "DIV", "SPAN", "BUTTON", "DETAILS", "SUMMARY"
      ]);

      function merge(a, b) {
        var out = new Set();
        a.forEach(function (x) { out.add(x); });
        b.forEach(function (x) { out.add(x); });
        return out;
      }

      // 공통 허용
      var COMMON = new Set(["class", "role", "aria-expanded", "aria-hidden", "aria-label"]);
      // ✅ 토글/렌더 동작용 data-*
      var DATA = new Set([
        "data-evidence",
        "data-total",
        "data-evidence-toggle",
        "data-evidence-more",
        "data-evidence-summary",
        "data-evidence-body",
        "data-extra",
        "data-msg-meta",
        "data-bubble-body"
      ]);

      function allowedAttrsFor(tag) {
        tag = String(tag || "").toUpperCase();

        if (tag === "A") return new Set(["href", "target", "rel", "referrerpolicy", "class", "title"]);
        if (tag === "BUTTON") return merge(COMMON, merge(DATA, new Set(["type", "class"])));
        if (tag === "SUMMARY") return merge(COMMON, merge(DATA, new Set(["class"])));

        if (tag === "DIV" || tag === "SPAN" || tag === "LI" || tag === "UL" || tag === "OL" || tag === "PRE" || tag === "CODE") {
          return merge(COMMON, merge(DATA, new Set(["class"])));
        }

        if (tag === "DETAILS") return merge(COMMON, merge(DATA, new Set(["open", "class"])));

        // 나머지 태그는 속성 제거(보수적)
        return new Set([]);
      }

      var T = document.createElement("template");
      T.innerHTML = unsafe || "";

      var walk = function (node) {
        for (var child = node.firstChild; child;) {
          var next = child.nextSibling;

          // comment 제거
          if (child.nodeType === 8) {
            child.remove();
            child = next;
            continue;
          }

          if (child.nodeType === 1) {
            var tag = child.tagName;

            if (!ALLOWED.has(tag)) {
              // 내부 먼저 정리 후 unwrap
              walk(child);
              while (child.firstChild) node.insertBefore(child.firstChild, child);
              child.remove();
              child = next;
              continue;
            }

            // ✅ 허용 태그: 속성 정리
            var allow = allowedAttrsFor(tag);

            Array.from(child.attributes).forEach(function (a) {
              var name = String(a.name || "").toLowerCase();

              // boolean attr: hidden은 "존재"가 의미라 제거하지 않음(단, 허용 목록에는 없으므로 예외 처리)
              if (name === "hidden") return;

              if (!allow.has(name)) child.removeAttribute(a.name);
            });

            // ✅ A 태그 href 안전 처리(https? + / + # 만 허용)
            if (tag === "A") {
              var hrefRaw = child.getAttribute("href") || "";
              var href = normalizeOutboundUrlForLinks(hrefRaw);

              // ✅ 보정 반영 (비면 제거)
              if (!href) {
                child.removeAttribute("href");
              } else if (href !== hrefRaw) {
                child.setAttribute("href", href);
              }

              var ok =
                /^https?:\/\//i.test(href) ||
                href.indexOf("/") === 0 ||
                href.indexOf("#") === 0;

              if (!ok) child.removeAttribute("href");

              child.setAttribute("rel", "noopener noreferrer nofollow");
              child.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");

              try {
                var u = new URL(href, location.origin);
                if (/^https?:$/i.test(u.protocol) && u.origin !== location.origin) {
                  child.setAttribute("target", "_blank");
                } else {
                  child.removeAttribute("target");
                }
              } catch (_) {
                child.removeAttribute("target");
              }
            }

            // ✅ button은 form submit 방지
            if (tag === "BUTTON") {
              child.setAttribute("type", "button");
            }

            walk(child);
          }

          child = next;
        }
      };

      walk(T.content);
      return T.innerHTML || "";
    } catch (e) {
      return escHtml(unsafe || "");
    }
  }

  function hardenLinks(root) {
    try {
      if (!root || !root.querySelectorAll) return;
      var links = root.querySelectorAll("a[href]");
      Array.prototype.forEach.call(links, function (a) {
        try {
          var hrefRaw = a.getAttribute("href") || "";
          var href = normalizeOutboundUrlForLinks(hrefRaw);

          // javascript: 등 + 비정상 URL 제거
          if (!href || /^\s*javascript:/i.test(href)) {
            a.removeAttribute("href");
            return;
          }

          if (href !== hrefRaw) a.setAttribute("href", href);

          a.setAttribute("rel", "noopener noreferrer nofollow");
          a.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");

          // 외부만 새 탭
          try {
            var u = new URL(href, location.origin);
            if (/^https?:$/i.test(u.protocol) && u.origin !== location.origin) {
              a.setAttribute("target", "_blank");
            } else {
              a.removeAttribute("target");
            }
          } catch (_) {
            a.removeAttribute("target");
          }
        } catch (_) { }
      });
    } catch (_) { }
  }

  /* ============================================================
   *  3) Markdown → Safe HTML (렌더)
   * ============================================================ */

  var AI_BADGE_HTML = '<p class="answer-meta-row"><b class="ai-generated-badge">검색 결과 기반 답변</b></p>';

  function wrapWithAIBadge(html) {
    return AI_BADGE_HTML + (html || "");
  }

  function trimReferencesSection(text) {
    try {
      var render = String(text || "");
      if (!render.trim()) return render;

      var patterns = [
        /(\r?\n)\s*\*\*\s*참고\s*링크\s*\*\*\s*:?\s*(\r?\n)/i,
        /(\r?\n)\s*참고\s*링크\s*:?\s*(\r?\n)/i,
        /(\r?\n)\s*\*\*\s*출처\s*\*\*\s*:?\s*(\r?\n)/i,
        /(\r?\n)\s*출처\s*:?\s*(\r?\n)/i,
        /(\r?\n)\s*\*\*\s*References?\s*\*\*\s*:?\s*(\r?\n)/i,
        /(\r?\n)\s*References?\s*:?\s*(\r?\n)/i,
        /(\r?\n)\s*\*\*\s*Sources?\s*\*\*\s*:?\s*(\r?\n)/i,
        /(\r?\n)\s*Sources?\s*:?\s*(\r?\n)/i,
      ];

      for (var i = 0; i < patterns.length; i++) {
        var m = render.match(patterns[i]);
        if (m && m.index !== undefined && m.index >= 0) {
          return render.slice(0, m.index).trim();
        }
      }

      // 꼬리 URL 덩어리 감지
      var lines = render.split(/\r?\n/);
      var tailN = Math.min(10, lines.length);
      var tail = lines.slice(lines.length - tailN);

      var urlish = tail.filter(function (ln) {
        var t = String(ln || "").trim();
        if (!t) return false;
        return (
          /\[[^\]]+\]\((\/?https?:\/\/[^\s)]+)\)/i.test(t) ||
          /^\(\/?https?:\/\/[^\s)]+\)$/i.test(t) ||
          /^\/?https?:\/\/\S+$/i.test(t)
        );
      }).length;

      if (tail.length >= 6 && urlish >= Math.max(4, Math.floor(tail.length * 0.7))) {
        var cutAt = lines.length - tailN;
        return lines.slice(0, cutAt).join("\n").trim();
      }

      return render;
    } catch (_) {
      return String(text || "");
    }
  }

  function markdownToHtmlLite(mdText) {
    var md = String(mdText || "");
    if (!md.trim()) return "";

    var codeBlocks = [];
    md = md.replace(/```([\s\S]*?)```/g, function (_, code) {
      var idx = codeBlocks.length;
      codeBlocks.push(String(code || "").replace(/^\n+|\n+$/g, ""));
      return "@@CODEBLOCK_" + idx + "@@";
    });

    var lines = md.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
    var out = [];
    var inOl = false;
    var inUl = false;

    function closeLists() {
      if (inOl) { out.push("</ol>"); inOl = false; }
      if (inUl) { out.push("</ul>"); inUl = false; }
    }

    function fmtInlineSafe(s) {
      var t = escHtml(String(s || ""));

      t = t.replace(/\[([^\]]+)\]\((\/?https?:\/\/[^\s)]+)\)/gi, function (_, label, url) {
        var safeLabel = label;
        var raw = String(url || "").replace(/&amp;/g, "&");
        var href = normalizeOutboundUrlForLinks(raw);

        // 링크 불가면 라벨만
        if (!href) return safeLabel;

        return '<a href="' + escHtml(href) + '" target="_blank" rel="noopener noreferrer nofollow">' + safeLabel + "</a>";
      });

      t = t.replace(/`([^`]+)`/g, function (_, code) {
        return "<code>" + code + "</code>";
      });

      t = t.replace(/\*\*([^*]+)\*\*/g, function (_, bold) {
        return "<strong>" + bold + "</strong>";
      });

      return t;
    }

    for (var i = 0; i < lines.length; i++) {
      var raw = lines[i];
      var line = String(raw || "").trim();

      var codeTok = line.match(/^@@CODEBLOCK_(\d+)@@$/);
      if (codeTok) {
        closeLists();
        var idx2 = parseInt(codeTok[1], 10);
        var code2 = codeBlocks[idx2] || "";
        out.push("<pre><code>" + escHtml(code2) + "</code></pre>");
        continue;
      }

      if (!line) {
        closeLists();
        continue;
      }

      if (/^\(\/?https?:\/\/[^\)]+\)\s*$/i.test(line)) continue;

      var mOl = line.match(/^\s*(\d+)\.\s+(.+)$/);
      if (mOl) {
        if (inUl) { out.push("</ul>"); inUl = false; }
        if (!inOl) { out.push("<ol>"); inOl = true; }
        var itemOl = fmtInlineSafe(mOl[2]);
        if (itemOl) out.push("<li>" + itemOl + "</li>");
        continue;
      }

      var mUl = line.match(/^\s*[-*]\s+(.+)$/);
      if (mUl) {
        if (inOl) { out.push("</ol>"); inOl = false; }
        if (!inUl) { out.push("<ul>"); inUl = true; }
        var itemUl = fmtInlineSafe(mUl[1]);
        if (itemUl) out.push("<li>" + itemUl + "</li>");
        continue;
      }

      var mH2 = line.match(/^##\s+(.+)$/);
      if (mH2) {
        closeLists();
        out.push("<h2>" + fmtInlineSafe(mH2[1]) + "</h2>");
        continue;
      }
      var mH3 = line.match(/^###\s+(.+)$/);
      if (mH3) {
        closeLists();
        out.push("<h3>" + fmtInlineSafe(mH3[1]) + "</h3>");
        continue;
      }

      closeLists();
      var p = fmtInlineSafe(line);
      if (p) out.push("<p>" + p + "</p>");
    }

    closeLists();

    var html = out.join("\n");
    html = html.replace(/@@CODEBLOCK_(\d+)@@/g, function (_, idxStr) {
      var idx3 = parseInt(idxStr, 10);
      var code3 = codeBlocks[idx3] || "";
      return "<pre><code>" + escHtml(code3) + "</code></pre>";
    });

    return html;
  }

  function renderMarkdownSafeToHTML(mdText) {
    try {
      var md = String(mdText || "");

      // 외부 라이브러리 우선
      if (typeof window !== "undefined" && window.marked && window.DOMPurify) {
        try {
          if (window.marked && typeof window.marked.use === "function") {
            window.marked.use({
              renderer: {
                link: function (href, title, text) {
                  var safeHref = href || "#";
                  var t = title ? ' title="' + escHtml(title) + '"' : "";
                  return (
                    '<a href="' + escHtml(safeHref) + '"' + t +
                    ' target="_blank" rel="noopener noreferrer nofollow">' +
                    escHtml(text || "") +
                    "</a>"
                  );
                },
              },
            });
          }
        } catch (_) { }

        var rawHtml = window.marked.parse(md);

        // ✅ evidence 토글/aria/data-*가 잘 보존되도록 옵션을 보수적으로 추가
        var purified = window.DOMPurify.sanitize(rawHtml, {
          USE_PROFILES: { html: true },
          ADD_TAGS: ["details", "summary", "button", "div", "span"],
          ADD_ATTR: [
            "open", "class", "role",
            "aria-expanded", "aria-hidden", "aria-label",
            "data-evidence", "data-total", "data-evidence-toggle", "data-evidence-more",
            "data-evidence-summary", "data-evidence-body",
            "data-extra", "data-msg-meta", "data-bubble-body"
          ]
        });

        return sanitizeHTML(purified);
      }

      var lite = markdownToHtmlLite(md);
      return sanitizeHTML(lite);
    } catch (e) {
      return sanitizeHTML(escHtml(String(mdText || "")).replace(/\r\n|\r|\n/g, "<br/>"));
    }
  }

  function renderAnswerRich(target, text, maybeSources, opts) {
    if (!target) return;

    try {
      var s = String(text || "");
      s = trimReferencesSection(s);
      s = s.replace(/\n{3,}/g, "\n\n").trim();

      var html = renderMarkdownSafeToHTML(s);
      var outHtml = html || "";
      if (opts && opts.aiBadge) outHtml = wrapWithAIBadge(outHtml);

      target.innerHTML = outHtml;
      hardenLinks(target);
    } catch (e) {
      try {
        target.innerHTML = escHtml(String(text || "")).replace(/\r\n|\r|\n/g, "<br/>");
      } catch (_) {
        target.textContent = String(text || "");
      }
    }

    try { target.classList.add("answer-prose"); } catch (_) { }
  }



  /* ============================================================
   *  5) 구형 SSR 텍스트 정리(web-answer)
   * ============================================================ */

  function transformWebAnswerBlock() {
    var el = document.getElementById("web-answer-block");
    if (!el) return;

    var raw = el.innerHTML || "";
    if (!raw || !raw.trim()) return;

    if (/<p[\s>]|<ul[\s>]|<ol[\s>]|<pre[\s>]/i.test(raw)) return;

    var lines = raw.split(/<br\s*\/?>/i);
    var out = [];

    for (var i = 0; i < lines.length; i++) {
      var t = String(lines[i] || "").trim();
      if (!t) { out.push(""); continue; }

      if (/^\(\/?https?:\/\/[^\)]+\)\s*$/i.test(t)) continue;

      var mA = t.match(/^(\d+\.\s*)?\*\*([^*]+)\*\*\s*([^:]+):\s*\[([^\]]+)\]\((\/?https?:\/\/[^\)]+)\)(.*)$/i);
      if (mA) {
        var num = mA[1] || "";
        var label = stripTrailingColon(String(mA[2] || "").trim()) + ": " + String(mA[3] || "").trim();
        var urlRaw = String((mA[5] || mA[4]) || "").trim();
        var href = normalizeOutboundUrlForLinks(urlRaw);
        var tail = mA[6] || "";
        if (!href) {
          out.push(escHtml(num) + escHtml(label) + (tail ? " " + escHtml(tail) : ""));
        } else {
          out.push(
            escHtml(num) +
            '<a href="' + escHtml(href) + '" target="_blank" rel="noopener noreferrer nofollow" class="src-title">' +
            escHtml(label) +
            "</a>" +
            (tail ? " " + escHtml(tail) : "")
          );
        }
        continue;
      }

      var mB = t.match(/^(\d+\.\s*)?\*\*([^*]+)\*\*\s*\[([^\]]+)\]\((\/?https?:\/\/[^\)]+)\)(.*)$/i);
      if (mB) {
        var num2 = mB[1] || "";
        var label2 = String(mB[2] || "").trim();

        // mB[4]가 URL, (fallback로 mB[3]를 두지만 사실상 4가 맞음)
        var url2Raw = String((mB[4] || mB[3]) || "").trim();
        var href2 = normalizeOutboundUrlForLinks(url2Raw);

        var tail2 = mB[5] || "";

        if (!href2) {
          // 링크 불가 → 텍스트로만
          out.push(
            escHtml(num2) +
            escHtml(stripTrailingColon(label2)) +
            (tail2 ? " " + escHtml(tail2) : "")
          );
        } else {
          // 링크 가능 → 보정된 href2로 a 태그 생성
          out.push(
            escHtml(num2) +
            '<a href="' + escHtml(href2) + '" target="_blank" rel="noopener noreferrer nofollow" class="src-title">' +
            escHtml(stripTrailingColon(label2)) +
            "</a>" +
            (tail2 ? " " + escHtml(tail2) : "")
          );
        }
        continue;
      }

      var mC = t.match(/^(\d+\.\s*)?\*\*([^*]+)\*\*\s*\[(\/?https?:\/\/[^\]]+)\]\s*$/i);
      if (mC) {
        var num3 = mC[1] || "";
        var srcOnly = String(mC[2] || "").trim();
        var urlOnlyRaw = String(mC[3] || "").trim();
        var hrefOnly = normalizeOutboundUrlForLinks(urlOnlyRaw);

        if (!hrefOnly) {
          out.push(escHtml(num3) + escHtml(stripTrailingColon(srcOnly)));
        } else {
          out.push(
            escHtml(num3) +
            '<a href="' + escHtml(hrefOnly) + '" target="_blank" rel="noopener noreferrer nofollow" class="src-title">' +
            escHtml(stripTrailingColon(srcOnly)) +
            "</a>"
          );
        }
        continue;
      }

      out.push(escHtml(t));
    }

    el.innerHTML = out.join("<br />");
    hardenLinks(el);
  }

  // ✅ (추가) 혹시 템플릿/이전 렌더링으로 남아있는 피드백 UI가 있다면 제거(구형만)
  function removeFeedbackUI() {
    try {
      document.querySelectorAll(".fb-shell, .fb-shell-web, .fb-shell-rag").forEach(function (el) {
        try { el.remove(); } catch (_) { }
      });
    } catch (_) { }
  }

  function _newsReady(fn) {
    try {
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", fn, { once: true });
      } else {
        fn();
      }
    } catch (_) { }
  }

  _newsReady(function () {
    cleanLeading("rag-answer-block");
    cleanLeading("web-answer-block");
    transformWebAnswerBlock();
    removeFeedbackUI();
  });

  /* ============================================================
  *  6) AJAX + Web/RAG 실행 (견고화)
  *   - 중복 요청은 AbortController로 취소
  *   - setLoading 자체 구현(없어도 안전)
  * ============================================================ */

  (function () {
    var log = function (tag, data) {
      try {
        if (typeof window !== "undefined" && typeof window.dglog === "function") {
          window.dglog("NEWS_AJAX " + tag, data);
        } else {
          var ts = new Date().toISOString().slice(11, 23);
          console.log("[news-ajax " + ts + "] " + tag, data || "");
        }
      } catch (_) { }
    };

    var $ = function (s, r) { return (r || document).querySelector(s); };

    // ✅ in-flight 제어(중복 요청 취소)
    var inflight = { web: null, rag: null };

    try { window.__DG_CLARIFY__ = window.__DG_CLARIFY__ || { web: null, rag: null }; } catch (_) { }

    function abortInFlight(kind) {
      try {
        if (inflight[kind] && typeof inflight[kind].abort === "function") {
          inflight[kind].abort();
        }
      } catch (_) { }
      inflight[kind] = null;
    }

    function setInFlight(kind, controller) {
      abortInFlight(kind);
      inflight[kind] = controller;
    }

    function clearInFlight(kind, controller) {
      if (inflight[kind] === controller) inflight[kind] = null;
    }

    function pickSubmitterFromEvent(ev, form) {
      try {
        if (!ev) return null;
        if (ev.submitter) return ev.submitter;
        if (ev.target && ev.target.tagName) {
          var t = ev.target;
          if (t.tagName === "BUTTON" || t.tagName === "INPUT") return t;
        }
      } catch (_) { }
      try {
        return form ? form.querySelector("button[type='submit'], button[data-action]") : null;
      } catch (_) { }
      return null;
    }

    function setLoading(form, submitter, loadingText) {
      // ✅ 외부 setLoading이 있으면 우선 사용(호환)
      try {
        if (typeof window !== "undefined" && typeof window.setLoading === "function") {
          window.setLoading(form);
          return;
        }
      } catch (_) { }

      try {
        var btn = submitter || (form ? form.querySelector("button[data-action], button[type='submit'], input[type='submit']") : null);
        if (!btn) return;

        // 텍스트 백업
        if (btn.dataset) {
          if (!btn.dataset.origText) {
            btn.dataset.origText = (btn.innerText !== undefined ? btn.innerText : (btn.value !== undefined ? btn.value : ""));
          }
          btn.dataset.origText = btn.dataset.origText || "";
          btn.setAttribute("data-orig-text", btn.dataset.origText);
        }

        // disable + loading text
        btn.disabled = true;
        if (loadingText) {
          if (btn.innerText !== undefined) btn.innerText = loadingText;
          else if (btn.value !== undefined) btn.value = loadingText;
        }
      } catch (_) { }
    }

    function restoreSubmitter(ev, form) {
      try {
        var submitter = (ev && ev.submitter) || pickSubmitterFromEvent(ev, form) || (form ? form.querySelector("button[disabled][data-orig-text]") : null);
        if (!submitter) return;

        submitter.disabled = false;

        var orig = null;
        try { orig = submitter.dataset && (submitter.dataset.origText || submitter.getAttribute("data-orig-text")); } catch (_) { orig = null; }
        if (orig !== null && orig !== undefined) {
          if (submitter.innerText !== undefined) submitter.innerText = orig;
          else if (submitter.value !== undefined) submitter.value = orig;
        }

        try {
          if (submitter.dataset) {
            delete submitter.dataset.origText;
          }
          submitter.removeAttribute("data-orig-text");
        } catch (_) { }
      } catch (_) { }
    }

    function apiPostJSON(url, payload, opts) {
      opts = opts || {};
      var headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      };

      try {
        var csrftoken = getCookie("csrftoken");
        if (csrftoken) headers["X-CSRFToken"] = csrftoken;
      } catch (_) { }

      try {
        if (typeof window !== "undefined" && typeof window.newReqId === "function") {
          var reqId = window.newReqId("ui");
          if (reqId) headers["X-Request-Id"] = reqId;
        }
      } catch (_) { }

      var controller = opts.controller || null;
      var signal = controller ? controller.signal : (opts.signal || undefined);
      var timeoutMs = typeof opts.timeoutMs === "number" ? opts.timeoutMs : 90000;

      var timeoutId = null;
      try {
        if (controller && timeoutMs > 0) {
          timeoutId = setTimeout(function () {
            try { controller.abort(); } catch (_) { }
          }, timeoutMs);
        }
      } catch (_) { }

      return fetch(url, {
        method: "POST",
        headers: headers,
        credentials: "same-origin",
        body: JSON.stringify(payload || {}),
        signal: signal
      }).then(function (res) {
        return res.text().then(function (text) {
          if (timeoutId) { try { clearTimeout(timeoutId); } catch (_) { } }

          var json = null;
          try { json = JSON.parse(text); } catch (_) { }

          if (!res.ok || (json && json.ok === false)) {
            var msg = (json && (json.error || json.message || json.detail)) || ("HTTP " + res.status);

            // ✅ (NEW) content guard 정보 보존
            var blocked = !!(json && json.blocked === true);
            var blockCode = "";
            try { blockCode = String((json && json.code) || "").toLowerCase(); } catch (_) { blockCode = ""; }

            // 메시지가 비어있을 때도 "강한 문구" 기본값 보장(서버가 msg를 안 준 케이스 대비)
            if (blocked && (!msg || !String(msg).trim())) {
              msg = (blockCode === "sexual")
                ? "🚫 성희롱/음란/성적 모욕 표현은 허용되지 않습니다.\n정중한 표현으로 다시 작성해 주세요.\n반복될 경우 이용이 제한될 수 있습니다."
                : "🚫 욕설/모욕 표현은 허용되지 않습니다.\n정중한 표현으로 다시 작성해 주세요.\n반복될 경우 이용이 제한될 수 있습니다.";
            }

            var err = new Error(msg);
            err.response = json || text;
            err.status = res.status;

            // ✅ (NEW) catch에서 바로 분기 가능
            err.isBlocked = blocked;
            err.blockCode = blockCode;

            throw err;
          }
          return json || {};
        });
      }).catch(function (err) {
        if (timeoutId) { try { clearTimeout(timeoutId); } catch (_) { } }
        throw err;
      });
    }

    function apiPostQAWithInsurance(kind, sendQ, opts) {
      opts = opts || {};

      // ✅ web은 policy를 타지 말고 무조건 직행
      if (kind !== "rag") {
        return apiPostJSON(
          webUrl, // 이미 위에서 결정해둔 "/api/web_qa" 안전 엔드포인트
          { q: sendQ, query: sendQ, question: sendQ, from_ui: "news_web_panel" },
          opts
        );
      }

      // ✅ rag만 policy → 기술적 실패 시 fallback 유지
      var prefer = "rag";
      var fromUi = "news_rag_panel";

      // 1) 정책 API (권장)
      var policyPayload = { q: sendQ, prefer: prefer, from_ui: fromUi };

      // 2) 보험용: 기존 직행 API payload
      function directPayload() {
        return { q: sendQ, query: sendQ, question: sendQ, prefer: prefer, from_ui: fromUi };
      }

      // 3) 보험용: rag 직행 URL만 필요
      function pickDirectUrl() {
        return (window.DG_ENDPOINTS && window.DG_ENDPOINTS.ragQa) || window.RAG_QA_MAIN || "/api/rag_qa";
      }

      // ✅ “기술적 실패”일 때만 fallback
      return apiPostJSON(policyUrl, policyPayload, opts).catch(function (err) {
        if (err && err.name === "AbortError") throw err;

        var st = (err && typeof err.status === "number") ? err.status : null;
        var shouldFallback =
          (!st) ||             // 네트워크/파싱 등
          (st === 404) ||       // 정책 URL 미존재
          (st === 405) ||       // 메서드 불일치
          (st >= 500 && st <= 599); // 서버 장애

        if (!shouldFallback) throw err;

        try { log("POLICY_FALLBACK", { kind: "rag", status: st }); } catch (_) { }
        return apiPostJSON(pickDirectUrl(), directPayload(), opts);
      });
    }

    function pickAnswer(j) {
      try {
        if (!j) return "";
        var keys = ["answer_text", "answer", "text", "reply", "result", "a", "data"];
        for (var i = 0; i < keys.length; i++) {
          var k = keys[i];
          if (typeof j[k] === "string" && j[k].trim()) return j[k];
        }
      } catch (_) { }
      return "";
    }

    function pickSources(j) {
      try {
        if (!j) return [];

        var candidates = [
          j.sources_norm,   // ✅ 정규화된 근거 우선
          j.hits,           // ✅ 실제 검색 hit
          j.sources,
          j.used_sources,
          j.rag_sources,
          j.ragSources,
          j.web_sources,
          j.refs,
          j.citations,
          j.evidence,
          j.contexts,
          j.results,
          j.items,
          j.docs,
          j.references
        ];

        for (var i = 0; i < candidates.length; i++) {
          if (Array.isArray(candidates[i]) && candidates[i].length > 0) {
            return candidates[i];
          }
        }

        return [];
      } catch (_) { }
      return [];
    }

    function filterSourcesByAnswerCitations(answerText, sources) {
      try {
        if (!Array.isArray(sources) || sources.length === 0) return [];

        var answer = String(answerText || "");
        var used = [];
        var re = /\[(\d{1,2})\]/g;
        var m;

        while ((m = re.exec(answer)) !== null) {
          var n = parseInt(m[1], 10);

          // [2024] 같은 숫자 오인 방지
          if (n >= 1 && n <= 99 && used.indexOf(n) < 0) {
            used.push(n);
          }
        }

        // ✅ 답변에 인용번호가 없어도 백엔드가 근거를 줬으면 보여준다.
        if (!used.length) return sources;

        var selected = sources.filter(function (src, pos) {
          try {
            var idx = parseInt(
              src && (src.citation_idx || src.idx)
                ? (src.citation_idx || src.idx)
                : (pos + 1),
              10
            );

            return used.indexOf(idx) >= 0;
          } catch (_) {
            return false;
          }
        });

        // ✅ 답변 번호와 근거 번호가 어긋난 경우에도 근거를 0개로 만들지 않는다.
        return selected.length ? selected : sources;
      } catch (_) {
        return Array.isArray(sources) ? sources : [];
      }
    }

    // ✅ (추가) "RSS 근거 없음 → 직답" 판정 + UI placeholder 렌더
    function isWebNoSources(j, srcs, msg) {
      try {
        var code = String((j && (j.code || j.error_code || j.err_code)) || "").toUpperCase();
        var mode = String((j && j.mode) || "").toLowerCase();
        var m = String(msg || (j && (j.msg || j.message)) || "");

        // 서버가 명시적으로 주는 케이스
        if (code === "NO_SOURCES" || code === "NO_SOURCE" || mode === "no_sources") return true;

        // 소스가 비어있으면 대부분 "직답"으로 간주
        if (!Array.isArray(srcs) || srcs.length === 0) return true;

        // 메시지에 힌트가 있으면 보조 판정
        if (m.indexOf("참고자료 없음") >= 0 || m.indexOf("RSS") >= 0 && m.indexOf("없") >= 0) return true;
      } catch (_) { }
      return false;
    }

    function renderNoSourcesPlaceholder(container, hintText) {
      try {
        if (!container) return false;

        // 기존 렌더 흔적 제거
        try { container.innerHTML = ""; } catch (_) { }

        // sources-block은 기본적으로 숨김 처리되므로, 여기서는 강제로 보이게
        var block = container.closest ? container.closest(".sources-block") : null;
        if (block) block.removeAttribute("hidden");

        // UL/OL이면 LI로, 아니면 DIV로
        var isList = (container.tagName === "UL" || container.tagName === "OL" || container.tagName === "MENU");
        var item = document.createElement(isList ? "li" : "div");
        item.className = "src-card src-empty";

        var txt = String(hintText || "").trim();
        if (!txt) txt = "참고자료가 없어 직답으로 응답했어요. (RSS 결과 없음)";

        item.innerHTML =
          '<div class="src-head">' +
          '  <div class="src-title">직답 (참고자료 없음)</div>' +
          '  <div class="src-meta"><span class="src-url">' + escHtml(txt) + '</span></div>' +
          '</div>';

        container.appendChild(item);

        // 카운트는 0개로
        try { setSourcesCountForContainer(container, 0); } catch (_) { }

        return true;
      } catch (_) {
        return false;
      }
    }


    // ✅ (추가) sources 컨테이너를 “해당 카드 내부”에서 찾는 헬퍼
    function findSourcesContainer(kind, ansBlock) {
      // kind: "web" | "rag"
      try {
        // 1) 레거시 id 우선
        var id = (kind === "web") ? "webSourcesList" : "ragSourcesList";
        var el = document.getElementById(id);
        if (el) return el;
      } catch (_) { }

      // 2) 답변 블록이 속한 카드 내부에서 탐색
      try {
        var card = ansBlock && ansBlock.closest ? ansBlock.closest(".card") : null;
        if (!card) return null;

        // sources-block 안에서 list 계열/대체 컨테이너 탐색
        return (
          card.querySelector(".sources-block ul") ||
          card.querySelector(".sources-block ol") ||
          card.querySelector(".sources-block [data-sources-list]") ||
          card.querySelector(".sources-list") ||
          null
        );
      } catch (_) {
        return null;
      }
    }

    function pickMsg(j) {
      try {
        if (!j) return "";
        return j.msg || j.message || "";
      } catch (_) { }
      return "";
    }

    function setSourcesCountForContainer(container, n) {
      try {
        // 1) 가까운 sources-block 내 .sources-count
        var block = container && container.closest ? container.closest(".sources-block") : null;
        var countEl = null;
        try { countEl = (block && block.querySelector(".sources-count")) || null; } catch (_) { countEl = null; }
        if (countEl) countEl.textContent = String(n) + "개";
      } catch (_) { }

      try {
        // 2) id 기반 (레거시): <ul id="webSourcesList"> => webSourcesListCount 같은 패턴
        var byId = null;
        try {
          byId = document.getElementById(container && container.id ? (container.id + "Count") : "") || null;
        } catch (_) { byId = null; }
        if (byId) byId.textContent = String(n) + "개";
      } catch (_) { }

      // 3) rag/web 고정 id(가능하면)
      try {
        var ragC = document.getElementById("ragSourcesCount");
        if (ragC && container && container.id === "ragSourcesList") ragC.textContent = String(n) + "개";
      } catch (_) { }

      try {
        var webC = document.getElementById("webSourcesCount");
        if (webC && container && container.id === "webSourcesList") webC.textContent = String(n) + "개";
      } catch (_) { }
    }

    function renderSourcesList(container, sources, opts) {
      opts = opts || {};

      try {
        if (!container) return false;

        var origContainer = container;
        var block = container.closest ? container.closest(".sources-block") : null;

        // 초기화
        try { origContainer.innerHTML = ""; } catch (_) { }

        if (!Array.isArray(sources) || sources.length === 0) {
          if (block) block.setAttribute("hidden", "hidden");
          setSourcesCountForContainer(origContainer, 0);
          return false;
        }

        var isList = (container.tagName === "UL" || container.tagName === "OL" || container.tagName === "MENU");

        // src 표준화
        var normalize = function (src, pos) {
          if (!src) return { title: "", url: "", snippet: "", citation_idx: pos + 1 };
          if (typeof src === "string") {
            return {
              title: src,
              url: src,
              snippet: "",
              citation_idx: pos + 1
            };
          }

          var url = src.url || src.link || src.href || "";
          var title = src.title || src.name || src.site_name || url || "(제목 없음)";
          var snippet = src.chunk || src.snippet || src.summary || src.desc || src.description || src.text || src.content || "";

          // ✅ [META ONLY] 표시는 숨김 처리하지 말고 화면에서만 제거
          title = String(title || "").replace(/\[META ONLY\]\s*/gi, "").trim();
          snippet = String(snippet || "").replace(/\[META ONLY\]\s*/gi, "").trim();

          var citationIdx = null;
          try {
            citationIdx =
              src.citation_idx ||
              src.idx ||
              (src.meta && (src.meta.citation_idx || src.meta.idx)) ||
              (pos + 1);
          } catch (_) {
            citationIdx = pos + 1;
          }

          return {
            title: String(title || ""),
            url: String(url || ""),
            snippet: String(snippet || ""),
            citation_idx: citationIdx
          };
        };

        function safeHost(u) {
          try {
            var urlObj = new URL(u, location.origin);
            return (urlObj.hostname || "").replace(/^www\./i, "");
          } catch (_) {
            return "";
          }
        }

        function normalizeOutboundUrl(u) {
          u = String(u || "").trim();
          if (!u) return "";

          // ✅ 핵심: "/https://..." 또는 "/http://..." 실수 케이스 보정
          if (/^\/https?:\/\//i.test(u)) u = u.slice(1);

          // 내부 경로/앵커는 그대로
          if (u[0] === "/" || u[0] === "#") return u;

          // protocol-relative
          if (u.indexOf("//") === 0) return "https:" + u;

          // 스킴이 있으면 http/https만 허용 (javascript:, data: 등 차단)
          if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(u)) {
            return /^https?:\/\//i.test(u) ? u : "";
          }

          // 도메인만 온 경우 → https:// 부여
          return "https://" + u;
        }

        function shouldHide(src, pos) {
          var it = normalize(src, pos);
          var title = (it.title || "").trim();
          var rawUrl = (it.url || "").trim();
          var snippet = (it.snippet || "").trim();

          var tUp = title.toUpperCase();
          var sUp = snippet.toUpperCase();

          // (아주 보수적으로) rag 소개 페이지 계열만 숨김
          var tLow = title.toLowerCase();
          var hrefForCheck = normalizeOutboundUrl(rawUrl) || rawUrl;
          var uLow = String(hrefForCheck || "").toLowerCase();

          if (tLow === "rag 소개" || tLow.indexOf("rag 소개") >= 0) return true;
          if (uLow.indexOf("rag-intro") >= 0) return true;

          return false;
        }

        var filtered = sources.filter(function (s, idx) {
          try { return !shouldHide(s, idx); } catch (_) { return true; }
        });

        if (!filtered.length) {
          if (block) block.setAttribute("hidden", "hidden");
          setSourcesCountForContainer(origContainer, 0);
          return false;
        }

        if (block) block.removeAttribute("hidden");
        setSourcesCountForContainer(origContainer, filtered.length);

        // 렌더
        filtered.forEach(function (src, idx) {
          try {
            var it = normalize(src, idx);
            var title = it.title || it.url || "(제목 없음)";

            var rawUrl = (it.url || "").trim();
            var href = normalizeOutboundUrl(rawUrl);
            var host = href ? safeHost(href) : "";

            var item = document.createElement(isList ? "li" : "div");
            item.className = "src-card";

            var head = document.createElement(href ? "a" : "div");
            head.className = "src-head";

            if (href) {
              head.href = href;

              // 외부만 새 탭(내부 경로면 target 제거)
              try {
                var u = new URL(href, location.origin);
                if (/^https?:$/i.test(u.protocol) && u.origin !== location.origin) {
                  head.target = "_blank";
                } else {
                  head.removeAttribute("target");
                }
              } catch (_) {
                head.removeAttribute("target");
              }

              head.rel = "noopener noreferrer nofollow";
              head.referrerPolicy = "strict-origin-when-cross-origin";
            }

            var t = document.createElement("div");
            t.className = "src-title";
            t.textContent = title;

            var meta = document.createElement("div");
            meta.className = "src-meta";

            var badge = document.createElement("span");
            badge.className = "src-badge";
            badge.textContent = "#" + (it.citation_idx || idx + 1);
            meta.appendChild(badge);

            var hostEl = document.createElement("span");
            hostEl.className = "src-url";
            hostEl.textContent = host || href || rawUrl || "";
            meta.appendChild(hostEl);

            head.appendChild(t);
            head.appendChild(meta);
            item.appendChild(head);

            if (it.snippet) {
              var p = document.createElement("p");
              p.className = "src-snippet";
              p.textContent = it.snippet;
              item.appendChild(p);
            }

            container.appendChild(item);
          } catch (_) { }
        });

        return true;
      } catch (e) {
        try { log("RENDER_SOURCES_ERR", e && e.message ? e.message : e); } catch (_) { }
        return false;
      }
    }

    /* ---------------- Web Form ---------------- */

    function setupWebForm() {
      try {
        var form = null;
        try {
          var forms = document.querySelectorAll("form");
          for (var i = 0; i < forms.length; i++) {
            if (forms[i].querySelector('button[data-action="web_search"]')) {
              form = forms[i];
              break;
            }
          }
        } catch (_) { }

        if (!form) return;

        var input =
          form.querySelector("#query_web") ||
          form.querySelector('textarea[name="query_web"]') ||
          form.querySelector('input[name="query_web"]');

        if (!input) return;

        var searchBtn = form.querySelector('button[data-action="web_search"]');
        var ingestBtn = form.querySelector('button[data-action="web_ingest"]');

        [searchBtn, ingestBtn].forEach(function (btn) {
          if (!btn) return;
          try { btn.onclick = null; btn.removeAttribute("onclick"); } catch (_) { }
        });

        // ✅ (추가) 웹 저장 버튼(ingestBtn) 활성/비활성 헬퍼
        function setWebIngestEnabled(on, title) {
          try {
            if (!ingestBtn) return;
            ingestBtn.disabled = !on;
            ingestBtn.classList.toggle("is-disabled", !on);
            ingestBtn.title = (!on && title) ? String(title) : "";
          } catch (_) { }
        }

        // ✅ (추가) “웹 추가질문 대기” 상태 저장 + 버튼 비활성화/복구
        function setWebClarifyPending(on, info) {
          try {
            window.__DG_CLARIFY__ = window.__DG_CLARIFY__ || {};

            if (on) {
              var q0 = String((info && (info.q0 || info.originalQ)) || "").trim();
              var ask = String((info && info.ask) || "").trim();

              window.__DG_CLARIFY__.web = { q0: q0, ask: ask };

              // 저장 버튼 잠금
              setWebIngestEnabled(false, "추가 질문에 먼저 답한 뒤 저장할 수 있어요.");

              // UX: placeholder 유도
              try { input.placeholder = "추가 질문에 답해 주세요 (1회)…"; } catch (_) { }
            } else {
              window.__DG_CLARIFY__.web = null;

              // 저장 버튼 해제
              setWebIngestEnabled(true, "");

              // UX: placeholder 원복
              try { input.placeholder = "검색어를 입력하세요…"; } catch (_) { }
            }
          } catch (_) { }
        }

        function runWeb(ev) {
          try {
            if (ev) {
              ev.preventDefault();
              ev.stopPropagation();
              if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
            }

            var query = String(input.value || "").trim();
            if (!query) {
              if (ev) restoreSubmitter(ev, form);
              return;
            }

            try { setWebIngestEnabled(false, "답변을 만든 뒤 저장할 수 있어요."); } catch (_) { }

            // ✅ clarify 최소 상태: { q0, ask }만 사용
            var st = null;
            try { st = window.__DG_CLARIFY__ && window.__DG_CLARIFY__.web; } catch (_) { st = null; }

            var isFollowUp = !!(st && st.q0);

            var displayQ = query;                 // 화면(채팅)에 보여줄 사용자 입력(=이번 입력)
            var storeQ = isFollowUp ? st.q0 : query; // 저장/기록용 질문(=원질문 유지)
            var sendQ = query;                 // ✅ 서버로는 "이번 입력(추가답변)"만 전송

            var ansBlock = document.getElementById("web-answer-block");
            var card = ansBlock ? ansBlock.closest(".card") : null;
            var msgRow = card ? card.querySelector(".msg-row") : null;

            // ✅ PII 차단(검색 질문도 차단)
            if (blockIfPII(query, msgRow, "질문", ansBlock)) {
              try { if (ansBlock) ansBlock.dataset.latestQuestionText = String(storeQ || query || "").trim(); } catch (_) { }
              if (ev) restoreSubmitter(ev, form);
              return;
            }

            var srcUl = findSourcesContainer("web", ansBlock);

            if (ansBlock) {
              try { ansBlock.dataset.latestQuestionText = storeQ; } catch (_) { }   // ✅ storeQ
              try { ansBlock.dataset.latestAnswerText = ""; } catch (_) { }
            }

            if (msgRow) msgRow.innerHTML = "";

            if (srcUl) {
              srcUl.innerHTML = "";
              var srcBlock = srcUl.closest ? srcUl.closest(".sources-block") : null;
              if (srcBlock) srcBlock.setAttribute("hidden", "hidden");
              setSourcesCountForContainer(srcUl, 0);
            }

            // ✅ Chat UI 어댑터(있으면) 시작
            var chatCtx = chatBegin("web", displayQ, "웹에서 내용을 정리하는 중입니다…"); // ✅ displayQ

            // ✅ 중복 요청 취소
            var controller = null;
            try { controller = new AbortController(); } catch (_) { controller = null; }
            if (controller) setInFlight("web", controller);

            apiPostJSON(webUrl, { q: sendQ, query: sendQ, question: sendQ, from_ui: "news_web_panel" }, { controller: controller, timeoutMs: 90000 })
              .then(function (j) {

                // ✅ [추가/강화] 서버가 PII 차단을 200(ok)로 내려주는 경우: 여기서 UI 안내하고 종료
                var code = String((j && (j.code || j.error_code || j.err_code)) || "").toUpperCase();
                var guardHit = !!(j && (j.guard_hit || j.guardHit));
                var blocked = !!(j && (guardHit || code === "PII_BLOCKED" || j.mode === "blocked"));

                if (blocked) {
                  var txt =
                    pickAnswer(j) ||
                    pickMsg(j) ||
                    (j && (j.error || j.message || j.detail)) ||
                    "개인정보로 보이는 내용이 있어 전송을 중단했어요.";

                  if (msgRow) {
                    msgRow.innerHTML = '<div class="msg-err" role="alert">❌ ' + escHtml(txt) + "</div>";
                  }

                  // 채팅 말풍선도 로딩 해제 + 안내로 업데이트
                  chatUpdate(chatCtx, txt, { aiBadge: false, pending: false, error: true });

                  if (ansBlock) {
                    ansBlock.innerHTML = '<div class="msg-err" role="alert">❌ ' + escHtml(txt) + "</div>";
                    try { ansBlock.dataset.latestAnswerText = String(txt || "").trim(); } catch (_) { }
                    try { ansBlock.dataset.latestQuestionText = String(storeQ || query || "").trim(); } catch (_) { }
                  }
                  renderSourcesList(srcUl, []);
                  return; // ✅ 여기서 끝
                }

                // ✅ [추가] 하드 정책 차단(모욕/루머/사생활/개인정보 등): "한 문장"만 보여주고 끝
                var codeS = String((j && (j.code || j.error_code || j.err_code)) || "").toUpperCase();
                var safetyBlocked = !!(
                  j && (
                    j.mode === "safety_blocked" ||
                    codeS === "SAFETY_BLOCKED" ||
                    codeS === "POLICY_BLOCKED" ||
                    codeS === "DISALLOWED"
                  )
                );

                if (safetyBlocked) {
                  var txtS = "이 요청에 대해서는 제공 할 수 가 없습니다";

                  if (msgRow) {
                    msgRow.innerHTML = '<div class="msg-err" role="alert">' + escHtml(txtS) + "</div>";
                  }

                  // 말풍선도 같은 한 문장
                  chatUpdate(chatCtx, txtS, { aiBadge: false, pending: false, error: true });

                  // 답변 블록도 같은 한 문장 + 저장/근거 여지 제거
                  if (ansBlock) {
                    ansBlock.innerHTML = '<div class="msg-err" role="alert">' + escHtml(txtS) + "</div>";
                    try { ansBlock.dataset.latestAnswerText = ""; } catch (_) { }   // ✅ 저장 방지
                    try { ansBlock.dataset.latestBlocked = "1"; } catch (_) { }     // ✅ blocked 플래그
                    try { ansBlock.dataset.latestQuestionText = String(storeQ || query || "").trim(); } catch (_) { }
                  }

                  // 근거/링크는 완전 제거 + 숨김
                  try { renderSourcesList(srcUl, []); } catch (_) { }
                  try {
                    var b = srcUl && srcUl.closest ? srcUl.closest(".sources-block") : null;
                    if (b) b.setAttribute("hidden", "hidden");
                  } catch (_) { }

                  // 저장 버튼 잠금(여지 제거)
                  try { setWebIngestEnabled(false, ""); } catch (_) { }

                  return;
                }

                // ✅ 정상 응답이면 blocked 해제(선택이지만 추천)
                try { if (ansBlock && ansBlock.dataset) ansBlock.dataset.latestBlocked = "0"; } catch (_) { }

                var ansRaw = pickAnswer(j) || "(받아온 답이 없습니다.)";

                var isClarify = !!(j && (j.mode === "clarify" || String(j.code || "").toUpperCase() === "NEED_CLARIFY"));
                if (isClarify) {
                  var ask = String((j && j.ask) || ansRaw || "").trim() || "(추가 질문이 비어있습니다.)";

                  // ✅ 최소 상태로 저장: q0(원질문) + ask(표시용)
                  try { setWebClarifyPending(true, { q0: storeQ, ask: ask }); } catch (_) { }

                  var show = "🔁 추가 질문(1회): " + ask;

                  if (msgRow) {
                    msgRow.innerHTML = '<div class="msg-ok" role="status">✅ 추가 질문에 답해 주세요.</div>';
                  }

                  chatUpdate(chatCtx, show, { aiBadge: true, pending: false });

                  if (ansBlock) {
                    renderAnswerRich(ansBlock, show, [], { aiBadge: true });
                    try { ansBlock.dataset.latestAnswerText = trimReferencesSection(String(show || "")); } catch (_) { }
                  }

                  renderSourcesList(srcUl, []);

                  // UX: 입력 유도
                  try {
                    input.value = "";
                    input.placeholder = "추가 질문에 답해 주세요 (1회)…";
                    input.focus();
                  } catch (_) { }

                  return; // ✅ 여기서 끝(정상 답변 플로우로 내려가면 안 됨)
                }

                var srcs = pickSources(j);
                var msg = pickMsg(j);

                // ✅ (추가) "근거 없음(직답)" 판정
                var noSrc = isWebNoSources(j, srcs, msg);

                try { if (ansBlock && ansBlock.dataset) ansBlock.dataset.latestNoSources = noSrc ? "1" : "0"; } catch (_) { }

                // ✅ msgRow: noSrc면 "직답" 라벨을 강제로 보여주기
                if (msgRow) {
                  if (noSrc) {
                    // 서버 msg가 있으면 그걸 보여주되, 라벨은 확실히
                    var m2 = msg ? msg : "참고자료 없음 → 직답으로 응답했어요.";
                    msgRow.innerHTML = '<div class="msg-ok" role="status">✅ 직답(참고자료 없음) · ' + escHtml(m2) + "</div>";
                  } else {
                    msgRow.innerHTML = msg ? '<div class="msg-ok" role="status">✅ ' + escHtml(msg) + "</div>" : "";
                  }
                }

                // ✅ 채팅 말풍선은 기존처럼 "답변 본문" 그대로
                chatUpdate(chatCtx, ansRaw, { aiBadge: true, pending: false });

                // ✅ 답변 블록 렌더
                if (ansBlock) {
                  renderAnswerRich(ansBlock, ansRaw, srcs, { aiBadge: true });
                  var cleanedForStorage = trimReferencesSection(String(ansRaw || ""));
                  try { ansBlock.dataset.latestAnswerText = cleanedForStorage; } catch (_) { }
                }

                // ✅ sources 렌더: noSrc면 placeholder 카드 1개를 보여주고, 아니면 정상 렌더
                if (noSrc) {
                  // 기존 renderSourcesList는 빈배열이면 sources-block을 hidden 처리하므로,
                  // 먼저 비워주고 → placeholder로 "강제로 표시"한다.
                  try { renderSourcesList(srcUl, []); } catch (_) { }
                  renderNoSourcesPlaceholder(srcUl, msg);
                } else {
                  renderSourcesList(srcUl, srcs);
                }

                bumpUsage("web");

                // ✅ clarify 상태 해제(기존 유지)
                try { setWebClarifyPending(false); } catch (_) { }

              })

              .catch(function (err) {
                // ✅ abort도 pending 해제(말풍선 "생각중…" 고정 방지)
                try {
                  if (err && err.name === "AbortError") {
                    var cancelMsg = "⏹️ 이전 요청이 취소되었습니다.";
                    try {
                      if (ansBlock) ansBlock.innerHTML = '<div class="msg-ok" role="status">' + escHtml(cancelMsg) + "</div>";
                    } catch (_) { }
                    chatUpdate(chatCtx, cancelMsg, { aiBadge: false, pending: false, error: false });
                    return;
                  }
                } catch (_) { }

                // ✅✅ (NEW) content guard 차단: 200이어도 강력 경고를 그대로 보여주고 종료
                try {
                  var r0 = (err && err.response && typeof err.response === "object") ? err.response : null;
                  var isBlocked0 = !!((err && err.isBlocked) || (r0 && r0.blocked));

                  if (isBlocked0) {
                    var warn =
                      String((r0 && (r0.error || r0.message || r0.detail)) || (err && err.message) || "").trim() ||
                      "🚫 부적절한 표현은 허용되지 않습니다.\n정중한 표현으로 다시 작성해 주세요.\n반복될 경우 이용이 제한될 수 있습니다.";

                    var warnHtml = escHtml(warn).replace(/\n/g, "<br/>");

                    // 상단 메시지
                    if (msgRow2) msgRow2.innerHTML = '<div class="msg-err" role="alert">' + warnHtml + "</div>";

                    // 말풍선(로딩 종료)
                    chatUpdate(chatCtx, warn, { aiBadge: false, pending: false, error: true });

                    // 답변 영역(저장/근거 여지 제거)
                    if (ansBlock) {
                      ansBlock.innerHTML = '<div class="msg-err" role="alert">' + warnHtml + "</div>";
                      try { ansBlock.dataset.latestAnswerText = ""; } catch (_) { }
                      try { ansBlock.dataset.latestBlocked = "1"; } catch (_) { }
                    }

                    // 출처 완전 제거 + 숨김
                    try { renderSourcesList(srcUl, []); } catch (_) { }
                    try {
                      var sb = srcUl && srcUl.closest ? srcUl.closest(".sources-block") : null;
                      if (sb) sb.setAttribute("hidden", "hidden");
                    } catch (_) { }

                    // ✅ 웹 저장 버튼도 여지 없이 잠금 유지
                    try { setWebIngestEnabled(false, ""); } catch (_) { }

                    return;
                  }
                } catch (_) { }

                var msg = (err && err.message) || "웹에서 답을 만드는 동안 문제가 발생했습니다.";

                try {
                  var st429 = (err && typeof err.status === "number") ? err.status : null;
                  var raw = String(msg || "");
                  var rawUp = raw.toUpperCase();

                  // Vertex/모델 레이트리밋 계열
                  if (st429 === 429 && (rawUp.indexOf("RESOURCE_EXHAUSTED") >= 0 || rawUp.indexOf("RATE") >= 0)) {
                    msg = "AI요약검색에서 웹으로 답변으로 전환해 답변을 시도했지만, 오늘 사용 한도/호출 제한으로 답변이 종료됐어요.";
                  }
                } catch (_) { }

                if (err && err.response && (err.response.code === "PII_BLOCKED" || err.response.code === "pii_blocked")) {
                  try {
                    msg =
                      err.response.answer_text ||
                      err.response.answer ||
                      err.response.error ||
                      err.response.message ||
                      msg;
                  } catch (_) { }
                }

                var hardLimit = false;
                if (err && err.response && err.response.code === "limit_exceeded") {
                  hardLimit = true;
                  var limit = err.response.limit;
                  var used = err.response.used;
                  var extra = "";
                  if (typeof limit === "number" && typeof used === "number") {
                    extra = " (오늘 " + used + " / " + limit + "회 사용)";
                  }
                  msg = "오늘 사용할 수 있는 웹 검색 횟수를 모두 사용했습니다." + extra;
                }

                log("WEB_QA_ERR", msg);
                chatError(chatCtx, msg, { limit: hardLimit }); // chatCtx 없으면 no-op

                var errCls2 = hardLimit ? "msg-limit" : "msg-err";
                var errIcon2 = hardLimit ? "⏳" : "❌";

                try {
                  var ansElFallback = document.getElementById("web-answer-block");
                  if (ansElFallback) {
                    ansElFallback.innerHTML = '<div class="' + errCls2 + '" role="alert">' + errIcon2 + " " + escHtml(msg) + "</div>";
                  }
                } catch (_) { }

                var ansEl = document.getElementById("web-answer-block");
                var card2 = ansEl ? ansEl.closest(".card") : null;
                var msgRow2 = card2 ? card2.querySelector(".msg-row") : null;

                if (msgRow2) {
                  msgRow2.innerHTML = '<div class="' + errCls2 + '" role="alert">' + errIcon2 + " " + escHtml(msg) + "</div>";
                }

                if (hardLimit) {
                  try {
                    var formEl = input ? input.closest("form") : null;
                    if (formEl) {
                      var searchBtn2 = formEl.querySelector('button[data-action="web_search"]');
                      if (searchBtn2) {
                        searchBtn2.disabled = true;
                        searchBtn2.classList.add("is-disabled");
                        searchBtn2.title = "하루 사용 한도를 모두 사용했습니다.";
                      }
                    }
                  } catch (_) { }
                }
              })
              .finally(function () {
                clearInFlight("web", controller);
                if (ev) restoreSubmitter(ev, form);

                // ✅ 웹 저장 버튼: clarify 또는 policy blocked면 계속 잠금
                try {
                  var pend = !!(window.__DG_CLARIFY__ && window.__DG_CLARIFY__.web);

                  var isBlocked = false;
                  try { isBlocked = !!(ansBlock && ansBlock.dataset && ansBlock.dataset.latestBlocked === "1"); } catch (_) { }

                  if (pend) {
                    setWebIngestEnabled(false, "추가 질문에 먼저 답한 뒤 저장할 수 있어요.");
                  } else if (isBlocked) {
                    setWebIngestEnabled(false, ""); // ✅ 여지 없이 잠금 유지
                  } else {
                    setWebIngestEnabled(true, "");
                  }
                } catch (_) { }
              });
          } catch (e) {
            log("WEB_RUN_ERR", e && e.message ? e.message : e);
            if (ev) restoreSubmitter(ev, form);
          }
        }

        function runWebIngest(ev) {
          try {
            if (ev) {
              ev.preventDefault();
              ev.stopPropagation();
              if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
            }

            var query = String(input.value || "").trim();
            if (!query) {
              alert("먼저 궁금한 내용을 적어 주세요.");
              if (ev) restoreSubmitter(ev, form);
              return;
            }

            // ✅ (추가) 추가질문(clarify) 대기 중이면 저장 금지
            var ansBlock0 = document.getElementById("web-answer-block");
            var card0 = ansBlock0 ? ansBlock0.closest(".card") : null;
            var msgRow0 = card0 ? card0.querySelector(".msg-row") : null;

            try {
              if (window.__DG_CLARIFY__ && window.__DG_CLARIFY__.web) {
                var warn = "추가 질문에 먼저 답해 주세요. (추가 질문 단계에서는 저장할 수 없어요.)";
                if (msgRow0) msgRow0.innerHTML = '<div class="msg-err" role="alert">❌ ' + escHtml(warn) + "</div>";
                alert(warn);
                if (ev) restoreSubmitter(ev, form);
                return;
              }
            } catch (_) { }

            var ansBlock = document.getElementById("web-answer-block");

            // ✅ [추가] policy blocked면 저장 금지(여지 없음)
            try {
              if (ansBlock && ansBlock.dataset && ansBlock.dataset.latestBlocked === "1") {
                var warnB = "이 요청에 대해서는 제공 할 수 가 없습니다";
                if (msgRow0) msgRow0.innerHTML = '<div class="msg-err" role="alert">' + escHtml(warnB) + "</div>";
                try { alert(warnB); } catch (_) { }
                if (ev) restoreSubmitter(ev, form);
                return;
              }
            } catch (_) { }

            var latestQ = "";

            var latestQ = "";
            try { latestQ = (ansBlock && ansBlock.dataset && ansBlock.dataset.latestQuestionText) ? ansBlock.dataset.latestQuestionText : ""; } catch (_) { latestQ = ""; }
            var questionToStore = String(latestQ || query || "").trim();

            var latestAnswer = "";
            try {
              latestAnswer = (ansBlock && ansBlock.dataset && ansBlock.dataset.latestAnswerText)
                ? ansBlock.dataset.latestAnswerText
                : "";
            } catch (_) { latestAnswer = ""; }

            var answer = String(latestAnswer || (ansBlock ? (ansBlock.textContent || "") : "")).trim();

            if (!answer) {
              alert('먼저 "웹에서 검색"을 눌러 답변을 만든 다음, 저장 버튼을 눌러 주세요.');
              if (ev) restoreSubmitter(ev, form);
              return;
            }

            var srcUl = findSourcesContainer("web", ansBlock);
            var sources = [];
            if (srcUl) {
              srcUl.querySelectorAll("a[href]").forEach(function (a) {
                try {
                  var url = a.getAttribute("href") || "";
                  if (!url) return;
                  var tEl = a.querySelector ? a.querySelector(".src-title") : null;
                  var title = (tEl && tEl.textContent) ? String(tEl.textContent || "").trim() : String(a.textContent || "").trim();
                  title = title || url;
                  sources.push({ url: url, title: title });
                } catch (_) { }
              });
            }

            var card = ansBlock ? ansBlock.closest(".card") : null;
            var msgRow = card ? card.querySelector(".msg-row") : null;
            if (msgRow) {
              msgRow.innerHTML =
                '<div class="msg-ok" role="status">⏳ 웹에서 찾은 내용을 나중에 다시 쓸 수 있도록 저장하는 중입니다…</div>';
            }

            var noSourcesFlag = false;
            try { noSourcesFlag = (ansBlock && ansBlock.dataset && ansBlock.dataset.latestNoSources === "1"); } catch (_) { }

            var payload = {
              question: questionToStore,
              answer: answer,
              sources: sources,
              answer_type: noSourcesFlag ? "web_direct" : "web",
              no_sources: noSourcesFlag ? true : false,
              from_ui: "news_web_panel",
            };

            // ✅ 저장 전 자동 마스킹(저장은 의미 보존보다 안전 우선)
            var safeQuestion = redactPII(payload.question);
            var safeAnswer = redactPII(payload.answer);

            var changed = (safeQuestion !== payload.question) || (safeAnswer !== payload.answer);
            payload.question = safeQuestion;
            payload.answer = safeAnswer;

            if (changed) {
              showInlineMsg(msgRow, "ok", "개인정보로 보이는 내용은 저장 전에 자동으로 가렸어요.");
            }

            apiPostJSON("/api/rag/upsert", payload, { timeoutMs: 90000 })
              .then(function (j) {
                var msg = (j && (j.msg || j.message)) || "웹에서 찾은 내용을 잘 저장해 두었습니다.";
                if (msgRow) {
                  msgRow.innerHTML = '<div class="msg-ok" role="status">✅ ' + escHtml(msg) + "</div>";
                }
                bumpUsage("rag");
              })
              .catch(function (err) {
                var m = (err && err.message) || "웹에서 찾은 내용을 저장하는 동안 문제가 발생했습니다.";
                log("WEB_INGEST_ERR", m);
                if (msgRow) {
                  msgRow.innerHTML = '<div class="msg-err" role="alert">❌ ' + escHtml(m) + "</div>";
                }
                chatAppend("rag", "assistant", "❌ " + m, { aiBadge: false });
              })
              .finally(function () {
                if (ev) restoreSubmitter(ev, form);
              });
          } catch (e) {
            log("WEB_INGEST_RUN_ERR", e && e.message ? e.message : e);
            if (ev) restoreSubmitter(ev, form);
          }
        }

        if (searchBtn) {
          try { searchBtn.setAttribute("type", "button"); } catch (_) { }

          searchBtn.addEventListener("click", function (ev) {
            try {
              ev.preventDefault();
              ev.stopPropagation();
              if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();

              var hidden = form.querySelector('input[name="action"]');
              if (hidden) hidden.value = "web_search";
            } catch (_) { }

            setLoading(form, searchBtn, "검색 중…");
            runWeb(ev);
          }, true);
        }

        if (ingestBtn) {
          ingestBtn.addEventListener("click", function (ev) {
            try {
              var hidden = form.querySelector('input[name="action"]');
              if (hidden) hidden.value = "web_ingest";
            } catch (_) { }

            setLoading(form, ingestBtn, "저장 중…");
            runWebIngest(ev);
          });
        }

        form.addEventListener("submit", function (ev) {
          try {
            var hidden = form.querySelector('input[name="action"]');
            var action = (hidden && hidden.value) || "web_search";
            var query = String(input.value || "").trim();
            if (action === "web_search" && query) {
              setLoading(form, pickSubmitterFromEvent(ev, form), "검색 중…");
              runWeb(ev);
            }
          } catch (e) {
            log("WEB_FORM_HANDLER_ERR", e && e.message ? e.message : e);
          }
        }, true);
      } catch (e) {
        log("WEB_FORM_SETUP_ERR", e && e.message ? e.message : e);
      }
    }

    /* ---------------- RAG Form ---------------- */

    function setupRagForm() {
      try {
        var form = null;
        try {
          var forms = document.querySelectorAll("form");
          for (var i = 0; i < forms.length; i++) {
            if (forms[i].querySelector('button[data-action="rag_search"]')) {
              form = forms[i];
              break;
            }
          }
        } catch (_) { }

        if (!form) return;

        var input =
          form.querySelector("#query_rag") ||
          form.querySelector('input[name="query_rag"]') ||
          form.querySelector('textarea[name="query_rag"]') ||
          form.querySelector("#ragQueryInput");

        if (!input) return;

        var ragBtn = form.querySelector('button[data-action="rag_search"]');
        var seedBtn = form.querySelector('button[data-action="rag_seed"]');
        var resetBtn = form.querySelector('button[data-action="rag_reset"]');

        [ragBtn, seedBtn, resetBtn].forEach(function (btn) {
          if (!btn) return;
          try { btn.onclick = null; btn.removeAttribute("onclick"); } catch (_) { }
        });

        var ragIngestBtn = form.querySelector('button[data-action="rag_ingest"]'); // (선택) 나중에 생길 수 있음
        var ragLockBtns = [seedBtn, resetBtn, ragIngestBtn].filter(Boolean);

        function setRagLockEnabled(on, title) {
          try {
            ragLockBtns.forEach(function (btn) {
              try {
                btn.disabled = !on;
                btn.classList.toggle("is-disabled", !on);
                btn.title = (!on && title) ? String(title) : "";
              } catch (_) { }
            });
          } catch (_) { }
        }

        function setRagClarifyPending(on, info) {
          try {
            window.__DG_CLARIFY__ = window.__DG_CLARIFY__ || {};

            if (on) {
              var q0 = String((info && (info.q0 || info.originalQ)) || "").trim();
              var ask = String((info && info.ask) || "").trim();

              window.__DG_CLARIFY__.rag = { q0: q0, ask: ask };

              setRagLockEnabled(false, "추가 질문에 먼저 답한 뒤 실행할 수 있어요.");

              try { input.placeholder = "추가 질문에 답해 주세요 (1회)…"; } catch (_) { }
            } else {
              window.__DG_CLARIFY__.rag = null;

              setRagLockEnabled(true, "");

              try { input.placeholder = "질문을 입력하세요…"; } catch (_) { }
            }
          } catch (_) { }
        }

        function runRag(ev) {
          try {
            if (ev) {
              ev.preventDefault();
              ev.stopPropagation();
              if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
            }

            var query = String(input.value || "").trim();
            if (!query) {
              if (ev) restoreSubmitter(ev, form);
              return;
            }

            var st = null;
            try { st = window.__DG_CLARIFY__ && window.__DG_CLARIFY__.rag; } catch (_) { st = null; }

            var isFollowUp = !!(st && st.q0);

            var displayQ = query;
            var storeQ = isFollowUp ? st.q0 : query;
            var sendQ = query;   // ✅ 서버로는 이번 입력만

            var msgRow = document.getElementById("rag-msg-block");
            var ansBlock = document.getElementById("rag-answer-block");

            // ✅ PII 차단이 먼저
            if (blockIfPII(query, msgRow, "질문", ansBlock)) {
              try { if (ansBlock) ansBlock.dataset.latestQuestionText = String(storeQ || query || "").trim(); } catch (_) { }
              if (ev) restoreSubmitter(ev, form);
              return;
            }

            if (msgRow) msgRow.innerHTML = "";

            if (ansBlock) {
              try { ansBlock.dataset.latestQuestionText = storeQ; } catch (_) { } // ✅ storeQ
              try { ansBlock.dataset.latestAnswerText = ""; } catch (_) { }
            }

            var chatCtx = chatBegin("rag", displayQ, "자료를 모아서 답을 만드는 중입니다…"); // ✅ displayQ

            // ✅ 중복 요청 취소
            var controller = null;
            try { controller = new AbortController(); } catch (_) { controller = null; }
            if (controller) setInFlight("rag", controller);

            apiPostQAWithInsurance("rag", sendQ, { controller: controller, timeoutMs: 90000 })
              .then(function (j) {

                window.__DG_RAG_LAST_SOURCES__ = {
                  sources: pickSources(j)
                };

                // ✅ 서버가 PII 차단을 200(ok:true)로 내려주는 경우: 여기서 처리하고 성공 플로우 중단
                var code = String((j && (j.code || j.error_code || j.err_code)) || "").toUpperCase();
                var guardHit = !!(j && (j.guard_hit || j.guardHit));
                var blocked = !!(j && (guardHit || code === "PII_BLOCKED" || j.mode === "blocked"));

                if (blocked) {

                  var txt =
                    pickAnswer(j) ||
                    pickMsg(j) ||
                    (j && (j.error || j.message || j.detail)) ||
                    "개인정보로 보이는 내용이 있어 전송을 중단했어요.";

                  // 1) 상단/상태 메시지
                  if (msgRow) {
                    msgRow.innerHTML = '<div class="msg-err" role="alert">❌ ' + escHtml(txt) + "</div>";
                  }

                  // 2) 채팅 UI (배지 끄고, 에러로 표시, pending 종료)
                  chatUpdate(chatCtx, txt, { aiBadge: false, pending: false, error: true });

                  // 3) 답변 영역 (HTML 직접 세팅 — renderAnswerRich에 HTML 문자열을 넣지 않음)

                  if (ansBlock) {
                    ansBlock.innerHTML = '<div class="msg-err" role="alert">❌ ' + escHtml(txt) + "</div>";
                    try { ansBlock.dataset.latestAnswerText = String(txt || "").trim(); } catch (_) { }
                    try { ansBlock.dataset.latestQuestionText = String(storeQ || query || "").trim(); } catch (_) { }
                  }

                  // 4) 근거/출처 초기화
                  var ragSourcesList0 = document.getElementById("ragSourcesList");
                  renderSourcesList(ragSourcesList0, []);
                  try {
                    if (window.DG_RAG_EVIDENCE && typeof window.DG_RAG_EVIDENCE.syncRagEvidenceWrap === "function") {
                      window.DG_RAG_EVIDENCE.syncRagEvidenceWrap([]);
                    }
                  } catch (_) { }
                  return;
                }

                // ✅ [추가] 최신/실시간 → “웹 검색 권장” 안내만 하고 종료
                var code2 = String((j && (j.code || j.error_code || j.err_code)) || "").toUpperCase();
                if (j && (j.mode === "hint_web" || code2 === "HINT_WEB")) {
                  var txt2 = pickAnswer(j) || pickMsg(j) || "최신/실시간 정보는 위의 있는 웹 검색을 이용해 주세요.";

                  if (msgRow) {
                    msgRow.innerHTML = '<div class="msg-ok" role="status">ℹ️ ' + escHtml(txt2) + "</div>";
                  }

                  chatUpdate(chatCtx, txt2, { aiBadge: false, pending: false, error: false });

                  if (ansBlock) {
                    renderAnswerRich(ansBlock, txt2, [], { aiBadge: false });
                    try { ansBlock.dataset.latestAnswerText = trimReferencesSection(String(txt2 || "")); } catch (_) { }
                    try { ansBlock.dataset.latestQuestionText = String(storeQ || query || "").trim(); } catch (_) { }
                  }

                  // 근거/이비던스 초기화
                  var ragSourcesList0 = findSourcesContainer("rag", ansBlock) || document.getElementById("ragSourcesList");
                  renderSourcesList(ragSourcesList0, []);
                  try {
                    if (window.DG_RAG_EVIDENCE && typeof window.DG_RAG_EVIDENCE.syncRagEvidenceWrap === "function") {
                      window.DG_RAG_EVIDENCE.syncRagEvidenceWrap([]);
                    }
                  } catch (_) { }

                  try { setRagClarifyPending(false); } catch (_) { }
                  return;
                }

                var ansRaw = pickAnswer(j) || "(받아온 답이 없습니다.)";

                var isClarify = !!(j && (j.mode === "clarify" || String(j.code || "").toUpperCase() === "NEED_CLARIFY"));
                if (isClarify) {
                  var ask = String((j && j.ask) || ansRaw || "").trim() || "(추가 질문이 비어있습니다.)";

                  try { setRagClarifyPending(true, { q0: storeQ, ask: ask }); } catch (_) { }

                  var show = "🔁 추가 질문(1회): " + ask;

                  if (msgRow) {
                    msgRow.innerHTML = '<div class="msg-ok" role="status">✅ 추가 질문에 답해 주세요.</div>';
                  }

                  chatUpdate(chatCtx, show, { aiBadge: true, pending: false });

                  if (ansBlock) {
                    renderAnswerRich(ansBlock, show, [], { aiBadge: true });
                    try { ansBlock.dataset.latestAnswerText = trimReferencesSection(String(show || "")); } catch (_) { }
                  }

                  // ✅ 근거/이비던스는 "추가질문 단계"에서는 비움
                  var ragSourcesList0 = findSourcesContainer("rag", ansBlock) || document.getElementById("ragSourcesList");
                  renderSourcesList(ragSourcesList0, []);
                  try {
                    if (window.DG_RAG_EVIDENCE && typeof window.DG_RAG_EVIDENCE.syncRagEvidenceWrap === "function") {
                      window.DG_RAG_EVIDENCE.syncRagEvidenceWrap([]);
                    }
                  } catch (_) { }

                  try {
                    if (isFollowUp) {
                      input.value = "";
                      input.placeholder = "추가 질문에 답해 주세요 (1회)…";
                      input.focus();
                    }
                  } catch (_) { }

                  return;
                }

                var srcs = pickSources(j);

                // ✅ 답변 인용번호와 매칭 시도하되, 실패해도 백엔드 근거는 유지
                srcs = filterSourcesByAnswerCitations(ansRaw, srcs);

                var msg = pickMsg(j);

                if (msgRow) {
                  msgRow.innerHTML = msg ? '<div class="msg-ok" role="status">✅ ' + escHtml(msg) + "</div>" : "";
                }

                // ✅ Chat UI: 답변만 업데이트 (근거는 chat_adapter가 레거시 DOM에서 제품화)
                chatUpdate(chatCtx, ansRaw, { aiBadge: true, pending: false });

                // ✅ 기존 answer-block 업데이트(근거 HTML을 붙이지 않음)
                if (ansBlock) {
                  renderAnswerRich(ansBlock, ansRaw, srcs, { aiBadge: true });

                  var cleanedForStorage = trimReferencesSection(String(ansRaw || ""));
                  try { ansBlock.dataset.latestAnswerText = cleanedForStorage; } catch (_) { }
                }

                var ragSourcesList = findSourcesContainer("rag", ansBlock);
                renderSourcesList(ragSourcesList, srcs);

                try {
                  if (window.DG_RAG_EVIDENCE && typeof window.DG_RAG_EVIDENCE.syncRagEvidenceWrap === "function") {
                    window.DG_RAG_EVIDENCE.syncRagEvidenceWrap(srcs, j && j.log_id, ansRaw);
                  }
                } catch (_) { }

                try { setRagClarifyPending(false); } catch (_) { }

                bumpUsage("rag");
              })
              .catch(function (err) {
                try {
                  if (err && (err.name === "AbortError")) {
                    chatUpdate(chatCtx, "⏹️ 이전 요청이 취소되었습니다.", { aiBadge: false, pending: false, error: false });
                    return;
                  }
                } catch (_) { }

                // ✅✅ (NEW) content guard 차단: 강력 경고만 보여주고 종료
                try {
                  var r0 = (err && err.response && typeof err.response === "object") ? err.response : null;
                  var isBlocked0 = !!((err && err.isBlocked) || (r0 && r0.blocked));

                  if (isBlocked0) {
                    var warn =
                      String((r0 && (r0.error || r0.message || r0.detail)) || (err && err.message) || "").trim() ||
                      "🚫 부적절한 표현은 허용되지 않습니다.\n정중한 표현으로 다시 작성해 주세요.\n반복될 경우 이용이 제한될 수 있습니다.";

                    var warnHtml = escHtml(warn).replace(/\n/g, "<br/>");

                    // 상단 메시지
                    if (msgRow2) msgRow2.innerHTML = '<div class="msg-err" role="alert">' + warnHtml + "</div>";

                    // 말풍선(로딩 종료)
                    chatUpdate(chatCtx, warn, { aiBadge: false, pending: false, error: true });

                    // 답변 영역(저장 여지 제거)
                    try {
                      var ansElB = document.getElementById("rag-answer-block");
                      if (ansElB) {
                        ansElB.innerHTML = '<div class="msg-err" role="alert">' + warnHtml + "</div>";
                        try { ansElB.dataset.latestAnswerText = ""; } catch (_) { }
                        try { ansElB.dataset.latestBlocked = "1"; } catch (_) { }
                      }
                    } catch (_) { }

                    // 근거/이비던스 완전 제거
                    try {
                      var ragSourcesList0 = document.getElementById("ragSourcesList");
                      renderSourcesList(ragSourcesList0, []);
                    } catch (_) { }

                    try {
                      if (window.DG_RAG_EVIDENCE && typeof window.DG_RAG_EVIDENCE.syncRagEvidenceWrap === "function") {
                        window.DG_RAG_EVIDENCE.syncRagEvidenceWrap([]);
                      }
                    } catch (_) { }

                    return;
                  }
                } catch (_) { }

                var msg = (err && err.message) || "답을 만드는 동안 문제가 발생했습니다.";
                var hardLimit = false;

                var r = (err && err.response && typeof err.response === "object") ? err.response : null;
                var code = r ? String((r.code || r.error_code || r.err_code) || "").toUpperCase() : "";

                try {
                  var st429 = (err && typeof err.status === "number") ? err.status : null;

                  // 서버가 JSON으로 내려준 경우에도 메시지 후보를 같이 본다
                  var raw =
                    String(
                      (r && (r.error || r.message || r.detail || r.msg)) ||
                      msg ||
                      ""
                    );

                  var rawUp = raw.toUpperCase();

                  if (st429 === 429 && (
                    rawUp.indexOf("RESOURCE_EXHAUSTED") >= 0 ||
                    rawUp.indexOf("RATE") >= 0 ||
                    rawUp.indexOf("TOO MANY") >= 0 ||
                    rawUp.indexOf("QUOTA") >= 0
                  )) {
                    msg = "AI요약검색에서 답변을 생성하려 했지만, 현재 호출 한도/혼잡으로 답변이 종료됐어요.";
                  }
                } catch (_) { }

                // 1) limit_exceeded (최우선)
                if (code === "LIMIT_EXCEEDED" || code === "limit_exceeded") {
                  hardLimit = true;
                  var limit = r && r.limit;
                  var used = r && r.used;
                  var extra = "";
                  if (typeof limit === "number" && typeof used === "number") {
                    extra = " (오늘 " + used + " / " + limit + "회 사용)";
                  }
                  msg = "오늘 사용할 수 있는 RAG 질문 횟수를 모두 사용했습니다." + extra;

                  // 2) PII_BLOCKED (다음 우선)
                } else if (code === "PII_BLOCKED" || (r && r.mode === "blocked")) {
                  msg =
                    (r && (r.answer_text || r.answer)) ||
                    (r && (r.error || r.message || r.msg || r.detail)) ||
                    msg;

                  // 3) 일반 에러(서버 메시지 우선)
                } else if (r) {
                  msg = (r.error || r.message || r.detail || r.msg) || msg;
                }

                log("RAG_QA_ERR", msg);
                chatError(chatCtx, msg, { limit: hardLimit });

                var errCls3 = hardLimit ? "msg-limit" : "msg-err";
                var errIcon3 = hardLimit ? "⏳" : "❌";

                try {
                  var ansElFallback = document.getElementById("rag-answer-block");
                  if (ansElFallback) {
                    ansElFallback.innerHTML = '<div class="' + errCls3 + '" role="alert">' + errIcon3 + " " + escHtml(msg) + "</div>";
                    try { ansElFallback.dataset.latestAnswerText = String(msg || "").trim(); } catch (_) { }
                  }
                } catch (_) { }

                var msgRow2 = document.getElementById("rag-msg-block");
                if (msgRow2) {
                  msgRow2.innerHTML = '<div class="' + errCls3 + '" role="alert">' + errIcon3 + " " + escHtml(msg) + "</div>";
                }

                try {
                  if (window.DG_RAG_EVIDENCE && typeof window.DG_RAG_EVIDENCE.syncRagEvidenceWrap === "function") {
                    window.DG_RAG_EVIDENCE.syncRagEvidenceWrap([]);
                  }
                } catch (_) { }

                if (hardLimit) {
                  try {
                    var formEl = input ? input.closest("form") : null;
                    if (formEl) {
                      var ragBtn2 = formEl.querySelector('button[data-action="rag_search"]');
                      if (ragBtn2) {
                        ragBtn2.disabled = true;
                        ragBtn2.classList.add("is-disabled");
                        ragBtn2.title = "하루 사용 한도를 모두 사용했습니다.";
                      }
                    }
                  } catch (_) { }
                }
              }).finally(function () {
                clearInFlight("rag", controller);
                if (ev) restoreSubmitter(ev, form);

                // ✅ (추가) RAG 보조 버튼: clarify 대기면 계속 잠금, 아니면 해제
                try {
                  var pend = !!(window.__DG_CLARIFY__ && window.__DG_CLARIFY__.rag);
                  if (pend) setRagLockEnabled(false, "추가 질문에 먼저 답한 뒤 실행할 수 있어요.");
                  else setRagLockEnabled(true, "");
                } catch (_) { }
              });
          } catch (e) {
            log("RAG_RUN_ERR", e && e.message ? e.message : e);
            if (ev) restoreSubmitter(ev, form);
          }
        }

        function guardRagClarify(msgRow) {
          try {
            if (window.__DG_CLARIFY__ && window.__DG_CLARIFY__.rag) {
              var warn = "추가 질문에 먼저 답해 주세요. (추가 질문 단계에서는 실행할 수 없어요.)";
              if (msgRow) msgRow.innerHTML = '<div class="msg-err" role="alert">❌ ' + escHtml(warn) + "</div>";
              try { alert(warn); } catch (_) { }
              return true;
            }
          } catch (_) { }
          return false;
        }

        function runRagSeed(ev) {
          try {
            if (ev) {
              ev.preventDefault();
              ev.stopPropagation();
              if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
            }

            var query = String(input.value || "").trim();
            var msgRow = document.getElementById("rag-msg-block");
            if (guardRagClarify(msgRow)) { if (ev) restoreSubmitter(ev, form); return; }

            if (msgRow) {
              msgRow.innerHTML = '<div class="msg-ok" role="status">⏳ 기본 자료를 채워 넣는 중입니다…</div>';
              chatAppend("rag", "assistant", "기본 자료를 채워 넣는 중입니다…", { aiBadge: true });
            }

            var params = new URLSearchParams();
            params.set("from_ui", "news_rag_panel");
            if (query) params.set("last_query", query);

            var url = "/api/rag/seed?" + params.toString();

            fetch(url, {
              method: "GET",
              credentials: "same-origin",
              headers: { "X-Requested-With": "XMLHttpRequest" },
            })
              .then(function (res) {
                return res.text().then(function (text) {
                  var j = null;
                  try { j = JSON.parse(text); } catch (_) { }

                  if (!res.ok || (j && j.ok === false)) {
                    var msgErr = (j && (j.error || j.message || j.detail)) || text || "기본 자료를 채우는 중에 문제가 발생했습니다.";
                    var err = new Error(msgErr);
                    err.response = j || text;
                    throw err;
                  }

                  var msg = (j && (j.msg || j.message)) || "기본 자료 채우기가 끝났습니다.";
                  if (msgRow) {
                    msgRow.innerHTML = '<div class="msg-ok" role="status">✅ ' + escHtml(msg) + "</div>";
                  }
                  chatAppend("rag", "assistant", "✅ " + msg, { aiBadge: true });
                });
              })
              .catch(function (err) {
                var m = (err && err.message) || "기본 자료를 채우는 중에 문제가 발생했습니다.";
                log("RAG_SEED_ERR", m);
                if (msgRow) {
                  msgRow.innerHTML = '<div class="msg-err" role="alert">❌ ' + escHtml(m) + "</div>";
                }
                chatAppend("rag", "assistant", "❌ " + m, { aiBadge: false });
              })
              .finally(function () {
                if (ev) restoreSubmitter(ev, form);
              });
          } catch (e) {
            log("RAG_SEED_RUN_ERR", e && e.message ? e.message : e);
            if (ev) restoreSubmitter(ev, form);
          }
        }

        if (seedBtn) {
          seedBtn.addEventListener("click", function (ev) {
            try {
              var hidden = form.querySelector('input[name="action"]');
              if (hidden) hidden.value = "rag_seed";
            } catch (_) { }

            setLoading(form, seedBtn, "처리 중…");
            runRagSeed(ev);
          });
        }

        if (ragBtn) {
          try { ragBtn.setAttribute("type", "button"); } catch (_) { }

          ragBtn.addEventListener("click", function (ev) {
            try {
              ev.preventDefault();
              ev.stopPropagation();
              if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();

              var hidden = form.querySelector('input[name="action"]');
              if (hidden) hidden.value = "rag_search";
            } catch (_) { }

            setLoading(form, ragBtn, "검색 중…");
            runRag(ev);
          }, true);
        }

        if (resetBtn) {
          resetBtn.addEventListener("click", function (ev) {
            try {
              ev.preventDefault();
              ev.stopPropagation();
              if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
            } catch (_) { }

            // ✅ 추가질문(clarify) 대기 중이면 reset 금지
            var msgRow = document.getElementById("rag-msg-block");
            if (guardRagClarify(msgRow)) {
              try { restoreSubmitter(ev, form); } catch (_) { }
              return;
            }

            try {
              var hidden = form.querySelector('input[name="action"]');
              if (hidden) hidden.value = "rag_reset";
            } catch (_) { }

            setLoading(form, resetBtn, "초기화 중…");

            try {
              if (typeof form.requestSubmit === "function") form.requestSubmit();
              else form.submit();
            } catch (e) {
              log("RAG_RESET_SUBMIT_ERR", e && e.message ? e.message : e);
              restoreSubmitter(ev, form);
            }
          });
        }

        form.addEventListener("submit", function (ev) {
          try {
            var hidden = form.querySelector('input[name="action"]');
            var action = (hidden && hidden.value) || "rag_search";
            var query = String(input.value || "").trim();
            if (action === "rag_search" && query) {
              setLoading(form, pickSubmitterFromEvent(ev, form), "검색 중…");
              runRag(ev);
            }
          } catch (e) {
            log("RAG_FORM_HANDLER_ERR", e && e.message ? e.message : e);
          }
        }, true);
      } catch (e) {
        log("RAG_FORM_SETUP_ERR", e && e.message ? e.message : e);
      }
    }

    _newsReady(function () {
      try {
        setupWebForm();
        setupRagForm();

        log("INIT_DONE", { readyState: document.readyState });
      } catch (e) {
        log("DOM_READY_ERR", e && e.message ? e.message : e);
      }
    });
  })();

  /* ============================================================
   *  7) 새 화면 전용: 테스터/법무 푸터 보정(구형 템플릿 호환)
   * ============================================================ */

  (function () {
    function prettifyTesterAndLegal() {
      if (document.querySelector(".page-footer-wrap") || document.querySelector(".legal-footer-bar")) return;

      var allPs = document.querySelectorAll("body > p, body > div > p, body > section > p");
      var testerP = null;
      var legalP = null;

      allPs.forEach(function (p) {
        var text = String(p.textContent || "").trim();
        if (!testerP && text.indexOf("테스터 고지") === 0) testerP = p;
        if (!legalP && text.indexOf("개인정보처리방침") !== -1) legalP = p;
      });

      if (!testerP && !legalP) return;

      var wrap = document.createElement("section");
      wrap.className = "page-footer-wrap";

      if (testerP) {
        var notice = document.createElement("p");
        notice.className = "tester-notice";
        var html = String(testerP.innerHTML || "").replace(/^\s*테스터 고지\s*/i, "");
        notice.innerHTML = html;
        wrap.appendChild(notice);
      }

      if (legalP) {
        var bar = document.createElement("div");
        bar.className = "legal-footer-bar";
        bar.innerHTML = legalP.innerHTML;
        wrap.appendChild(bar);
      }

      if (testerP) testerP.replaceWith(wrap);
      else if (legalP) legalP.replaceWith(wrap);

      if (testerP) { try { testerP.remove(); } catch (_) { } }
      if (legalP) { try { legalP.remove(); } catch (_) { } }
    }

    function ready(fn) {
      if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
      else fn();
    }

    ready(function () {
      try { prettifyTesterAndLegal(); }
      catch (e) { console.error("[footer layout]", e && e.message ? e.message : e); }
    });
  })();

  /* ============================================================
   *  전역 접근용: chat_adapter.js가 sanitize 2nd-pass에서 사용
   * ============================================================ */

  window.NEWS_UTILS = window.NEWS_UTILS || {};
  Object.assign(window.NEWS_UTILS, {
    escHtml: escHtml,
    stripTrailingColon: stripTrailingColon,
    getCookie: getCookie,
    setCookie: setCookie,
    detectLikelyPII: detectLikelyPII,
    redactPII: redactPII,
    sanitizeHTML: sanitizeHTML,
    trimReferencesSection: trimReferencesSection,
    renderAnswerRich: renderAnswerRich,
    bumpUsage: bumpUsage,
  });
})();
