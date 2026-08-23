// ragapp/static/ragapp/javascript/rag_evidence_bridge.js
(function () {
    "use strict";

    if (window.__DG_RAG_EVIDENCE_INITED__) return;
    window.__DG_RAG_EVIDENCE_INITED__ = true;

    var DEFAULT_CFG = {
        max_sources: 30,
        snippet_max: 180,

        close_on_outside_click: true,
        close_on_esc: true,

        prefer_time_side_dock: true,
        redock_retries: 10,
        redock_retry_delay_ms: 120,

        debug: false
    };

    var CFG = (function mergeCfg() {
        var u = window.DG_RAG_EVIDENCE_CFG || {};
        var out = {};
        Object.keys(DEFAULT_CFG).forEach(function (k) { out[k] = DEFAULT_CFG[k]; });
        try { Object.keys(u || {}).forEach(function (k2) { out[k2] = u[k2]; }); } catch (_) { }
        return out;
    })();

    function dbg() {
        if (!CFG.debug) return;
        try {
            var args = Array.prototype.slice.call(arguments);
            args.unshift("[DG_RAG_EVIDENCE]");
            console.log.apply(console, args);
        } catch (_) { }
    }

    /* ------------------------- utils ------------------------- */
    function safeText(s) { return String(s || "").replace(/\s+/g, " ").trim(); }

    function esc(s) {
        return String(s || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function normalizeOutboundUrl(u) {
        u = String(u || "").trim();
        if (!u) return "";
        if (/^\/https?:\/\//i.test(u)) u = u.slice(1);
        if (u[0] === "/" || u[0] === "#") return u;
        if (u.indexOf("//") === 0) return "https:" + u;
        if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(u)) return /^https?:\/\//i.test(u) ? u : "";
        return "https://" + u;
    }

    function safeHost(href) {
        try {
            var u = new URL(href, location.origin);
            return (u.hostname || "").replace(/^www\./i, "");
        } catch (_) { return ""; }
    }

    function pickSourcesAny(obj) {
        var j = obj || {};

        // ✅ 백엔드 응답 키가 여러 형태로 올 수 있으므로 안전하게 선택
        // 우선순위:
        // 1) sources_norm: citation_idx/chunk 형태로 정규화된 근거
        // 2) hits: 실제 검색 hit
        // 3) sources: 일반 근거
        var candidates = [
            j.sources_norm,
            j.hits,
            j.sources,
            j.used_sources,
            j.rag_sources,
            j.ragSources,
            j.docs,
            j.documents,
            j.contexts,
            j.context
        ];

        for (var i = 0; i < candidates.length; i++) {
            if (Array.isArray(candidates[i]) && candidates[i].length > 0) {
                return candidates[i];
            }
        }

        return [];
    }

    function pickAnswerAny(obj) {
        try {
            var j = obj || {};
            return String(
                j.answer_text ||
                j.answer ||
                j.text ||
                j.reply ||
                j.result ||
                ""
            );
        } catch (_) {
            return "";
        }
    }


    function filterSourcesByAnswerCitations(answerText, sources) {
        try {
            if (!Array.isArray(sources) || sources.length === 0) return [];

            var answer = String(answerText || "");
            var used = [];
            var re = /\[\s*(\d{1,2}(?:\s*,\s*\d{1,2})*)\s*\]/g;
            var m;

            while ((m = re.exec(answer)) !== null) {
                m[1].split(",").forEach(function (rawNum) {
                    var n = parseInt(String(rawNum || "").trim(), 10);

                    // [2024] 같은 숫자 오인 방지
                    if (n >= 1 && n <= 99 && used.indexOf(n) < 0) {
                        used.push(n);
                    }
                });
            }

            // ✅ 답변에 인용번호가 하나도 없을 때:
            // - 모델이 "자료 내 직접 근거 부족"처럼 명시적으로 근거 없음을 밝힌 경우엔
            //   백엔드가 후보 근거를 같이 보냈더라도 "근거를 썼다"고 오해하지 않게 숨긴다.
            // - 그 외(단순히 [N] 인용 표기를 깜빡한 경우)엔 기존처럼 후보 근거를 보여준다.
            if (!used.length) {
                // "근거가 부족합니다", "직접 근거 부족", "근거는 부족하다는" 등
                // 조사/어미가 붙어도 잡히도록 "근거"~"부족" 사이 몇 글자만 허용
                if (/근거[^.!?\n]{0,8}부족/.test(answer)) {
                    return [];
                }
                return sources;
            }

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

            // 답변에 [1], [2] 같은 인용이 있으면, 실제로 그 번호에 해당하는
            // 근거만 보여준다. 번호가 하나도 안 맞아서 selected가 비면
            // (백엔드 idx와 어긋난 경우) "일단 다 보여주기"로 되돌아가지 않는다 —
            // 그러면 답변에 안 쓰인 근거까지 "사용됨"으로 오해하게 만들기 때문.
            return selected;
        } catch (_) {
            return Array.isArray(sources) ? sources : [];
        }
    }

    function normalizeSources(arr) {
        var out = [];
        var seen = new Set();

        (arr || []).forEach(function (s, idx) {
            if (!s) return;

            if (typeof s === "string") {
                var urlS = normalizeOutboundUrl(safeText(s));
                var keyS = (urlS || safeText(s) || String(idx)).toLowerCase();
                if (seen.has(keyS)) return;
                seen.add(keyS);
                out.push({
                    idx: idx + 1,
                    citation_idx: idx + 1,
                    title: safeText(s) || urlS || ("근거 " + (idx + 1)),
                    url: urlS || "",
                    snippet: "",
                    score: null
                });
                return;
            }

            var title = safeText(s.title || s.name || s.filename || s.id || "");
            var url = safeText(s.url || s.link || s.href || "");
            var snippet = safeText(s.chunk || s.snippet || s.text || s.content || "");
            var score = (typeof s.score === "number") ? s.score : ((typeof s.similarity === "number") ? s.similarity : null);

            var citationIdx = null;

            try {
                citationIdx =
                    s.citation_idx ||
                    s.idx ||
                    (s.meta && (s.meta.citation_idx || s.meta.idx)) ||
                    (idx + 1);
            } catch (_) {
                citationIdx = idx + 1;
            }

            citationIdx = parseInt(citationIdx, 10);
            if (!(citationIdx >= 1 && citationIdx <= 99)) citationIdx = idx + 1;

            var urlNorm = normalizeOutboundUrl(url);
            // 같은 문서의 서로 다른 인용 번호는 각각 답변 배지와 연결되어야 한다.
            var key = (String(citationIdx) + "|" + (urlNorm || url || title || String(idx))).toLowerCase();
            if (seen.has(key)) return;
            seen.add(key);

            var displayIdx = idx + 1;

            out.push({
                // 답변의 인용 배지 번호와 근거 카드 번호를 동일하게 유지
                idx: citationIdx,
                citation_idx: citationIdx,
                display_idx: displayIdx,

                // ✅ 원래 백엔드 번호는 디버깅용으로 보존
                raw_idx: citationIdx,
                raw_citation_idx: citationIdx,

                title: title || (urlNorm ? urlNorm : (url ? url : ("근거 " + displayIdx))),
                url: urlNorm || "",
                snippet: snippet || "",
                score: score
            });
        });

        return out.slice(0, CFG.max_sources);
    }

    function getRagQaPathname() {
        var url = (window.DG_ENDPOINTS && window.DG_ENDPOINTS.ragQa) || window.RAG_QA_MAIN || "/api/rag_qa";
        try { return new URL(url, location.origin).pathname; } catch (_) { return "/api/rag_qa"; }
    }

    function isRagQaUrl(urlLike) {
        try {
            var u = new URL(String(urlLike || ""), location.origin);
            return u.pathname === getRagQaPathname();
        } catch (_) {
            return String(urlLike || "").indexOf("/api/rag_qa") >= 0;
        }
    }

    function isEvNode(n) {
        try {
            if (!n || n.nodeType !== 1) return false;
            var id = n.id || "";
            if (id === "dgRagEvidenceDock" || id === "dgRagEvidencePanel" || id === "dgRagEvidenceWrap") return true;
            var cls = String(n.className || "");
            if (cls.indexOf("dg-ev-") >= 0) return true;
            if (n.closest && n.closest("#dgRagEvidenceDock, #dgRagEvidencePanel, .dg-ev-row")) return true;
        } catch (_) { }
        return false;
    }

    /* ------------------ pointer-events guard ------------------ */
    function pe(el) { try { return getComputedStyle(el).pointerEvents || ""; } catch (_) { return ""; } }

    function isBlockedByPointerEvents(el, stopAt) {
        var cur = el;
        while (cur && cur !== document.documentElement) {
            if (cur.nodeType !== 1) break;
            if (pe(cur) === "none") return true;
            if (stopAt && cur === stopAt) break;
            cur = cur.parentElement;
        }
        return false;
    }

    /* ---------------- Scaffold ---------------- */
    function ensureDockRoot() {
        var dock = document.getElementById("dgRagEvidenceDock");
        if (dock) return dock;

        dock = document.createElement("div");
        dock.id = "dgRagEvidenceDock";
        try { document.body.appendChild(dock); } catch (_) { }
        return dock;
    }

    function ensureDockScaffold() {
        var dock = ensureDockRoot();

        var wrap = null;

        // 1) 먼저 dock 안에서 찾기
        try {
            wrap = dock.querySelector('details.mz-evidence--rag, details.mz-evidence[data-evidence="rag"], details.mz-evidence');
        } catch (_) {
            wrap = null;
        }

        // 2) dock 밖에 이미 템플릿이 만든 dgRagEvidenceWrap이 있으면 재사용
        //    → 같은 id 중복 생성 방지
        if (!wrap) {
            try {
                wrap = document.getElementById("dgRagEvidenceWrap") || null;
                if (wrap && wrap.parentNode !== dock) {
                    dock.appendChild(wrap);
                }
            } catch (_) {
                wrap = null;
            }
        }

        // 3) 그래도 없을 때만 새로 생성
        if (!wrap) {
            dock.innerHTML =
                '' +
                '<details id="dgRagEvidenceWrap" class="mz-evidence mz-evidence--rag dg-ev-details" data-evidence="rag">' +
                '  <summary class="mz-evidence-summary" role="button" aria-controls="dgRagEvidencePanel" aria-expanded="false">' +
                '    <span class="mz-evidence-title">📎 사용된 근거 보기</span>' +
                '    <span class="mz-evidence-actions">' +
                '      <span class="mz-evidence-count" id="dgRagSourcesCount">0개</span>' +
                '      <span class="mz-evidence-caret" aria-hidden="true">⌄</span>' +
                "    </span>" +
                "  </summary>" +
                "</details>";

            wrap = dock.querySelector("#dgRagEvidenceWrap") || null;
        }

        if (!wrap) return dock;

        try { wrap.id = "dgRagEvidenceWrap"; } catch (_) { }
        try { wrap.setAttribute("data-evidence", "rag"); } catch (_) { }

        var summary = null;
        try { summary = wrap.querySelector("summary"); } catch (_) { summary = null; }

        if (!summary) {
            summary = document.createElement("summary");
            summary.className = "mz-evidence-summary";
            summary.setAttribute("role", "button");
            try { wrap.insertBefore(summary, wrap.firstChild); } catch (_) { }
        }

        summary.setAttribute("aria-controls", "dgRagEvidencePanel");
        summary.setAttribute("aria-expanded", "false");

        try {
            var titleEl = summary.querySelector(".mz-evidence-title");
            if (!titleEl) {
                titleEl = document.createElement("span");
                titleEl.className = "mz-evidence-title";
                summary.appendChild(titleEl);
            }

            // ✅ 예전 문구면 새 문구로 교체
            var titleTxt = safeText(titleEl.textContent || "");
            if (!titleTxt || titleTxt === "📎 근거 보기" || titleTxt === "근거 보기") {
                titleEl.textContent = "📎 사용된 근거 보기";
            }

            var act = summary.querySelector(".mz-evidence-actions");
            if (!act) {
                act = document.createElement("span");
                act.className = "mz-evidence-actions";
                summary.appendChild(act);
            }

            var cnt = act.querySelector(".mz-evidence-count") || summary.querySelector(".mz-evidence-count");
            if (!cnt) {
                cnt = document.createElement("span");
                cnt.className = "mz-evidence-count";
                act.appendChild(cnt);
            }

            cnt.id = "dgRagSourcesCount";
            if (!cnt.textContent) cnt.textContent = "0개";

            if (!act.querySelector(".mz-evidence-caret")) {
                var c = document.createElement("span");
                c.className = "mz-evidence-caret";
                c.setAttribute("aria-hidden", "true");
                c.textContent = "⌄";
                act.appendChild(c);
            }
        } catch (_) { }

        return dock;
    }

    function getDockEls() {
        var dock = document.getElementById("dgRagEvidenceDock") || ensureDockScaffold();
        ensureDockScaffold();

        var wrap = document.getElementById("dgRagEvidenceWrap") || (dock ? dock.querySelector('details[data-evidence="rag"]') : null);
        var summary = wrap ? wrap.querySelector("summary") : null;
        var countEl = document.getElementById("dgRagSourcesCount") || (wrap ? wrap.querySelector(".mz-evidence-count") : null);
        return { dock: dock, wrap: wrap, summary: summary, countEl: countEl };
    }

    function getChatThread() { return document.getElementById("chatThread"); }

    function looksAssistant(el) {
        if (!el || !el.getAttribute) return false;
        var role = String(el.getAttribute("data-role") || el.getAttribute("data-msg-role") || "").toLowerCase();
        if (role === "assistant" || role === "ai") return true;

        var cls = String(el.className || "").toLowerCase();
        if (cls.indexOf("mz-msg--assistant") >= 0) return true;
        if (cls.indexOf("assistant") >= 0 || cls.indexOf("ai") >= 0) return true;
        if (cls.indexOf("user") >= 0) return false;
        return false;
    }

    function findLastAssistantNode() {
        var th = getChatThread();
        if (!th) return null;

        try {
            var direct = th.querySelectorAll(".mz-msg.mz-msg--assistant, .mz-msg--assistant");
            if (direct && direct.length) return direct[direct.length - 1];
        } catch (_) { }

        var kids = Array.from(th.children || []);
        for (var i = kids.length - 1; i >= 0; i--) {
            var el = kids[i];
            if (looksAssistant(el)) return el;
        }

        return th.lastElementChild || null;
    }

    function resolveBubble(el) {
        if (!el) return null;

        var SEL = ".mz-bubble, .msg-bubble, .bubble, .mz-msg-body, .msg-body, .answer-body, .answer";

        function accept(b) {
            if (!b) return null;

            // ✅ 숨김 레거시 호스트(#dgLegacyHost) 안이면 제외
            try {
                if (b.closest && b.closest("#dgLegacyHost")) return null;
            } catch (_) { }

            // ✅ 실제 채팅 스레드(#chatThread) 안에서만 인정
            try {
                if (b.closest && !b.closest("#chatThread")) return null;
            } catch (_) { }

            return b;
        }

        try {
            // 1) 자기 자신이 bubble이면 그대로
            if (el.matches && el.matches(SEL)) return accept(el);

            // 2) ✅ 가장 중요: 부모 방향(ancestor)에서 bubble 찾기
            if (el.closest) {
                var up = el.closest(SEL);
                var okUp = accept(up);
                if (okUp) return okUp;
            }

            // 3) 마지막 fallback: 자식 방향에서 bubble 찾기
            if (el.querySelector) {
                var down = el.querySelector(SEL);
                var okDown = accept(down);
                if (okDown) return okDown;
            }
        } catch (_) { }

        return null;
    }

    function findFeedbackAnchor(bubble) {
        if (!bubble || !bubble.querySelector) return null;
        try {
            return bubble.querySelector(
                [
                    ".feedback",
                    ".feedback-widget",
                    ".answer-feedback",
                    ".mz-feedback",
                    "[data-feedback]",
                    ".msg-actions",
                    ".bubble-actions",
                    ".mz-actions"
                ].join(",")
            );
        } catch (_) { return null; }
    }

    function findTimeElInBubble(bubble) {
        if (!bubble || !bubble.querySelector) return null;

        var selectors = [
            "time",
            ".mz-time",
            ".mz-msg-time",
            ".msg-time",
            ".bubble-time",
            ".chat-time",
            "[data-msg-time]",
            "[data-time]",
            ".timestamp"
        ];

        for (var i = 0; i < selectors.length; i++) {
            try {
                var el = bubble.querySelector(selectors[i]);
                if (el) {
                    var t = String(el.textContent || "").trim();
                    if (/^\d{1,2}:\d{2}$/.test(t)) return el;
                }
            } catch (_) { }
        }

        try {
            var nodes = bubble.querySelectorAll("time, span, div, p, small");
            var last = null;
            for (var j = 0; j < nodes.length; j++) {
                var n = nodes[j];
                if (!n) continue;
                var tx = String(n.textContent || "").trim();
                if (tx.length <= 8 && /^\d{1,2}:\d{2}$/.test(tx)) last = n;
            }
            return last;
        } catch (_) { }

        return null;
    }

    function ensureEvRow(bubble) {
        if (!bubble) return null;

        try {
            var existing = bubble.querySelector(".dg-ev-row");
            if (existing) return existing;
        } catch (_) { }

        var row = document.createElement("div");
        row.className = "dg-ev-row dg-ev-meta";

        var left = document.createElement("div");
        left.className = "dg-ev-left";

        var timeHost = document.createElement("span");
        timeHost.className = "dg-ev-timehost";

        var timeEl = findTimeElInBubble(bubble);
        if (timeEl) {
            // ✅ 이미 우리 host 안이면 또 이동하지 않음
            try {
                if (!(timeEl.parentElement && timeEl.parentElement.classList && timeEl.parentElement.classList.contains("dg-ev-timehost"))) {
                    timeHost.appendChild(timeEl);
                } else {
                    // 이미 host에 들어있으면 그대로 두고 host만 연결
                    timeHost = timeEl.parentElement;
                }
            } catch (_) { }
        }

        left.appendChild(timeHost);

        var right = document.createElement("div");
        right.className = "dg-ev-right";

        var hostSpan = document.createElement("span");
        hostSpan.className = "dg-ev-host";
        right.appendChild(hostSpan);

        row.appendChild(left);
        row.appendChild(right);

        var anchor = findFeedbackAnchor(bubble);

        try {
            if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(row, anchor);
            else bubble.appendChild(row);
        } catch (_) { }

        return row;
    }

    var _ACTIVE_HOST = null;

    function resolveHostFromDock() {
        var dock = document.getElementById("dgRagEvidenceDock");
        if (dock && dock.closest) {
            var h = dock.closest(".mz-msg.mz-msg--assistant, .mz-msg--assistant, [data-role='assistant'], [data-msg-role='assistant']");
            if (h) return h;
        }
        return _ACTIVE_HOST || findLastAssistantNode();
    }

    function ensurePanelForBubble(bubble, afterRow) {
        if (!bubble || !bubble.querySelector) return null;

        var panel = document.getElementById("dgRagEvidencePanel");

        // move panel if it belongs elsewhere
        if (panel && panel.parentNode !== bubble) {
            try { panel.parentNode.removeChild(panel); } catch (_) { }
            panel = null;
        }
        if (!panel) panel = bubble.querySelector("#dgRagEvidencePanel");
        if (panel) return panel;

        panel = document.createElement("div");
        panel.id = "dgRagEvidencePanel";
        panel.className = "mz-evidence-body dg-ev-panel";
        panel.hidden = true;

        panel.innerHTML =
            '' +
            '<div class="sources-block answer-evidence" data-sanitize-links="1">' +
            '  <div class="sources-title-row">' +
            '    <h3 class="sources-title">📎 답변에 사용된 근거</h3>' +
            '    <span class="sources-count" id="dgRagSourcesCount2">0개</span>' +
            "  </div>" +
            '  <ul class="sources-list" id="dgRagSourcesList"></ul>' +
            '  <p class="muted" id="dgRagSourcesEmpty">검색을 실행하면 답변에 실제 사용된 근거만 표시됩니다.</p>' +
            "</div>";

        try {
            if (afterRow && afterRow.parentNode) {
                if (afterRow.nextSibling) afterRow.parentNode.insertBefore(panel, afterRow.nextSibling);
                else afterRow.parentNode.appendChild(panel);
            } else {
                bubble.appendChild(panel);
            }
        } catch (_) { }

        return panel;
    }

    // ✅ 무한 루프 방지용
    var _DOCKING = false;

    function dockIntoLastAssistant() {
        var els = getDockEls();
        if (!els || !els.dock || !els.wrap || !els.summary) return false;

        var host = findLastAssistantNode();
        if (!host) return false;

        var bubble = resolveBubble(host);
        if (!bubble) return false;

        var row = ensureEvRow(bubble);
        if (!row) return false;

        var hostSpan = null;
        try { hostSpan = row.querySelector(".dg-ev-host"); } catch (_) { hostSpan = null; }
        if (!hostSpan) return false;

        // ✅ 이미 올바른 parent면 다시 appendChild 하지 않음
        if (els.dock.parentNode !== hostSpan) {
            _DOCKING = true;
            try {
                els.dock.classList.add("dg-ev-in-bubble");
                hostSpan.appendChild(els.dock);
            } catch (_) {
                _DOCKING = false;
                return false;
            }
            _DOCKING = false;
        } else {
            try { els.dock.classList.add("dg-ev-in-bubble"); } catch (_) { }
        }

        _ACTIVE_HOST = host;

        var panel = ensurePanelForBubble(bubble, row);
        if (!panel) return false;

        try { els.dock.style.pointerEvents = "auto"; } catch (_) { }
        try { els.summary.style.pointerEvents = "auto"; } catch (_) { }
        try {
            els.dock.style.position = "relative";
            els.dock.style.zIndex = "6";
            els.summary.style.position = "relative";
            els.summary.style.zIndex = "7";
        } catch (_) { }

        try {
            if (isBlockedByPointerEvents(els.summary, bubble)) {
                els.summary.style.pointerEvents = "auto";
            }
        } catch (_) { }

        return true;
    }

    // ✅ scheduleRedock 디바운스(중복 폭주 방지)
    var _redockTimer = 0;
    function scheduleRedock() {
        if (_redockTimer) return;
        _redockTimer = window.setTimeout(function () {
            _redockTimer = 0;

            var tries = Math.max(1, CFG.redock_retries | 0);
            var delay = Math.max(0, CFG.redock_retry_delay_ms | 0);

            function attempt(n) {
                var ok = false;
                try { ok = dockIntoLastAssistant(); } catch (_) { ok = false; }
                if (ok) return;
                if (n <= 0) return;
                setTimeout(function () { attempt(n - 1); }, delay);
            }

            try { requestAnimationFrame(function () { attempt(tries); }); }
            catch (_) { attempt(tries); }
        }, 40);
    }

    /* -------------------- toggle open/close ------------------- */
    function isOpenState() {
        var els = getDockEls();
        if (!els || !els.wrap) return false;
        return String(els.wrap.getAttribute("data-dg-open") || "") === "1";
    }

    function setOpenState(open) {
        var els = getDockEls();
        if (!els || !els.wrap || !els.summary) return;

        try { els.wrap.removeAttribute("open"); } catch (_) { }

        // ✅ bubble은 "host 추정"보다 "dock이 실제 붙은 위치"가 1순위
        var dock = els.dock || document.getElementById("dgRagEvidenceDock");
        var host = resolveHostFromDock();

        var bubble =
            resolveBubble(dock) ||
            (host ? resolveBubble(host) : null) ||
            resolveBubble(findLastAssistantNode());

        if (!bubble) {
            bubble = resolveBubble(findLastAssistantNode());
        }

        var row = bubble ? bubble.querySelector(".dg-ev-row") : null;
        var panel = bubble ? ensurePanelForBubble(bubble, row) : document.getElementById("dgRagEvidencePanel");
        if (!panel) return;

        try {
            els.wrap.setAttribute("data-dg-open", open ? "1" : "0");
            els.summary.setAttribute("aria-expanded", open ? "true" : "false");
            panel.hidden = !open;
        } catch (_) { }

        // ✅ 열 때는 redock 금지 (열린 상태에서 DOM이 움직이면 안 보일 수 있음)
        if (!open) {
            try { scheduleRedock(); } catch (_) { }
        }
    }

    function toggleOpen() { setOpenState(!isOpenState()); }

    function installSummaryHandlerOnce() {
        var els = getDockEls();
        if (!els || !els.summary) return;

        if (els.summary.__dg_ev_bound) return;
        els.summary.__dg_ev_bound = true;

        var lastToggleAt = 0;

        function onToggle(e) {
            try { e.preventDefault(); } catch (_) { }
            try { e.stopPropagation(); } catch (_) { }

            var now = Date.now();
            // ✅ pointerup + click이 연달아 들어오는 환경에서 "두 번 토글" 방지
            if (now - lastToggleAt < 250) return;
            lastToggleAt = now;

            toggleOpen();
        }

        // ✅ 가장 안정적인 트리거 (네 케이스에서 이게 필요)
        els.summary.addEventListener("pointerup", onToggle, true);

        // ✅ click도 백업으로 두되, 더블토글은 위 가드가 막아줌
        els.summary.addEventListener("click", onToggle, true);

        // ✅ 키보드 접근성
        els.summary.addEventListener("keydown", function (e) {
            var k = e && (e.key || e.code);
            if (k === "Enter" || k === " " || k === "Spacebar") {
                onToggle(e);
            }
        }, true);
    }
    function installGlobalCloseHandlersOnce() {
        if (window.__DG_RAG_EVIDENCE_CLOSE_HANDLERS__) return;
        window.__DG_RAG_EVIDENCE_CLOSE_HANDLERS__ = true;

        if (CFG.close_on_outside_click) {
            document.addEventListener("click", function (e) {
                try {
                    if (!isOpenState()) return;

                    var dock = document.getElementById("dgRagEvidenceDock");
                    var panel = document.getElementById("dgRagEvidencePanel");
                    var t = e && e.target;

                    if (dock && dock.contains(t)) return;
                    if (panel && panel.contains(t)) return;

                    setOpenState(false);
                } catch (_) { }
            }, true);
        }

        if (CFG.close_on_esc) {
            document.addEventListener("keydown", function (e) {
                try {
                    if (!isOpenState()) return;
                    if ((e && e.key) === "Escape") setOpenState(false);
                } catch (_) { }
            }, true);
        }
    }

    /* ------------------------- render ------------------------- */
    function renderDockEvidence(sources) {
        var src = normalizeSources(sources || []);
        var n = src.length;

        var els = getDockEls();
        if (!els || !els.wrap || !els.countEl) return;

        if (n <= 0) {
            try { els.wrap.hidden = true; } catch (_) { }
            try { els.countEl.textContent = "0개"; } catch (_) { }

            var panel0 = document.getElementById("dgRagEvidencePanel");
            try { if (panel0) panel0.hidden = true; } catch (_) { }
            try { setOpenState(false); } catch (_) { }
            return;
        }

        try { els.wrap.hidden = false; } catch (_) { }
        try { els.countEl.textContent = n + "개"; } catch (_) { }

        var host = resolveHostFromDock();
        var bubble = host ? resolveBubble(host) : null;
        if (!bubble) bubble = resolveBubble(findLastAssistantNode());
        if (!bubble) return;

        var row = bubble.querySelector(".dg-ev-row");
        var panel = ensurePanelForBubble(bubble, row);
        if (!panel) return;

        var list = null, count2 = null, emptyEl = null;
        try {
            list = panel.querySelector("#dgRagSourcesList");
            count2 = panel.querySelector("#dgRagSourcesCount2");
            emptyEl = panel.querySelector("#dgRagSourcesEmpty");
        } catch (_) { }

        try { if (count2) count2.textContent = n + "개"; } catch (_) { }
        try { if (emptyEl) emptyEl.hidden = true; } catch (_) { }

        if (!list) return;

        list.innerHTML = "";
        src.forEach(function (s, i) {
            var li = document.createElement("li");
            li.className = "src-item";

            var href = s.url ? normalizeOutboundUrl(s.url) : "";
            var hostName = href ? safeHost(href) : "";

            if (href) {
                var title = s.title || href;

                li.innerHTML =
                    '' +
                    '<a href="' + esc(href) + '" class="src-title" rel="noopener noreferrer nofollow" referrerpolicy="strict-origin-when-cross-origin">' +
                    esc(title) +
                    "</a>" +
                    (s.snippet ? '<span class="src-snippet">' + esc(s.snippet.length > CFG.snippet_max ? (s.snippet.slice(0, CFG.snippet_max) + "…") : s.snippet) + "</span>" : "") +
                    '<span class="src-meta">' + esc(hostName || href) + " · #" + esc(s.citation_idx || s.idx || (i + 1)) + "</span>" +
                    (typeof s.score === "number" ? '<span class="src-meta">유사도 점수: ' + s.score.toFixed(3) + "</span>" : "");

                try {
                    var u = new URL(href, location.origin);
                    var a = li.querySelector("a");
                    if (a) {
                        if (/^https?:$/i.test(u.protocol) && u.origin !== location.origin) a.target = "_blank";
                        else a.removeAttribute("target");
                    }
                } catch (_) { }
            } else {
                li.innerHTML =
                    '' +
                    '<span class="src-title">' + esc(s.title || ("근거 " + (s.citation_idx || s.idx || (i + 1)))) + "</span>" +
                    (s.snippet ? '<span class="src-snippet">' + esc(s.snippet.length > CFG.snippet_max ? (s.snippet.slice(0, CFG.snippet_max) + "…") : s.snippet) + "</span>" : "") +
                    '<span class="src-meta">#' + esc(s.citation_idx || s.idx || (i + 1)) + "</span>" +
                    (typeof s.score === "number" ? '<span class="src-meta">유사도 점수: ' + s.score.toFixed(3) + "</span>" : "");
            }

            list.appendChild(li);
        });
    }

    /* ---------------------- update pipeline -------------------- */
    function cacheLast(sources, logId) {
        try {
            if (window.__DG_RAG_LAST_SOURCES__ && typeof window.__DG_RAG_LAST_SOURCES__ === "object" && !Array.isArray(window.__DG_RAG_LAST_SOURCES__)) {
                window.__DG_RAG_LAST_SOURCES__.sources = sources;
            } else {
                window.__DG_RAG_LAST_SOURCES__ = { sources: sources };
            }
            window.__DG_RAG_LAST_LOG_ID__ = logId || "";
        } catch (_) { }
    }

    function handleRagResponseJson(j) {
        try {
            var rawSources = pickSourcesAny(j);
            var answerText = pickAnswerAny(j);

            // ✅ 답변에 실제 인용된 [1], [2], [3] 근거만 남김
            rawSources = filterSourcesByAnswerCitations(answerText, rawSources);

            var sources = normalizeSources(rawSources);
            var logId = (j && (j.log_id || j.req_id || j.request_id || "")) ? String(j.log_id || j.req_id || j.request_id || "") : "";

            cacheLast(sources, logId);

            scheduleRedock();
            renderDockEvidence(sources);

            setTimeout(function () {
                try {
                    scheduleRedock();
                    renderDockEvidence(sources);
                } catch (_) { }
            }, 0);

            // 근거는 기본으로 접어두고, 사용자가 "사용된 근거 보기"를 눌렀을 때만 펼친다
            try { setOpenState(false); } catch (_) { }

            try {
                var ev = new CustomEvent("dg:rag_sources_updated", { detail: { sources: sources, log_id: logId } });
                window.dispatchEvent(ev);
            } catch (_) { }
        } catch (_) { }
    }

    /* --------------------- public API -------------------------- */
    window.DG_RAG_EVIDENCE = window.DG_RAG_EVIDENCE || {};

    window.DG_RAG_EVIDENCE.handleRagResponseJson = function (j) {
        try { handleRagResponseJson(j); } catch (_) { }
    };

    window.DG_RAG_EVIDENCE.syncRagEvidenceWrap = function (sources, logId, answerText) {
        try {
            var raw = sources || [];

            // ✅ answerText가 넘어온 경우에는 답변 인용번호 기준으로 한 번 더 필터링
            if (answerText !== undefined && answerText !== null) {
                raw = filterSourcesByAnswerCitations(answerText, raw);
            }

            var src = normalizeSources(raw);
            var lid = (logId != null) ? String(logId) : (window.__DG_RAG_LAST_LOG_ID__ || "");
            cacheLast(src, lid);

            scheduleRedock();
            renderDockEvidence(src);
            // 근거는 기본으로 접어두고, 사용자가 "사용된 근거 보기"를 눌렀을 때만 펼친다
            try { setOpenState(false); } catch (_) { }

            try {
                var ev = new CustomEvent("dg:rag_sources_updated", { detail: { sources: src, log_id: lid } });
                window.dispatchEvent(ev);
            } catch (_) { }
        } catch (_) { }
    };

    window.DG_RAG_EVIDENCE.setOpen = function (open) {
        try { setOpenState(!!open); } catch (_) { }
    };

    /* ----------------------- hooks (opt-in) --------------------- */
    function installFetchHook() {
        if (!window.fetch) return;
        if (window.__DG_RAG_FETCH_HOOKED__) return;
        window.__DG_RAG_FETCH_HOOKED__ = true;

        var orig = window.fetch;

        window.fetch = function (input, init) {
            var url = (typeof input === "string") ? input : (input && input.url) ? input.url : "";
            var p = orig.apply(this, arguments);

            try { if (!isRagQaUrl(url)) return p; } catch (_) { return p; }

            return p.then(function (res) {
                try {
                    if (!res || !res.ok) return res;

                    var ctype = (res.headers && res.headers.get("content-type")) || "";
                    if (ctype.indexOf("application/json") < 0 && ctype.indexOf("json") < 0) return res;

                    var clone = res.clone();
                    clone.json().then(handleRagResponseJson).catch(function () { });
                } catch (_) { }
                return res;
            });
        };
    }

    function installXhrHook() {
        var XHR = window.XMLHttpRequest;
        if (!XHR) return;
        if (window.__DG_RAG_XHR_HOOKED__) return;
        window.__DG_RAG_XHR_HOOKED__ = true;

        var origOpen = XHR.prototype.open;
        var origSend = XHR.prototype.send;

        XHR.prototype.open = function (method, url) {
            try { this.__dg_url = url; } catch (_) { }
            return origOpen.apply(this, arguments);
        };

        XHR.prototype.send = function () {
            try {
                var url = this.__dg_url || "";
                if (isRagQaUrl(url)) {
                    this.addEventListener("load", function () {
                        try {
                            var ctype = this.getResponseHeader("content-type") || "";
                            if (ctype.indexOf("json") < 0) return;

                            var txt = this.responseText || "";
                            if (!txt) return;

                            var j = JSON.parse(txt);
                            handleRagResponseJson(j);
                        } catch (_) { }
                    });
                }
            } catch (_) { }
            return origSend.apply(this, arguments);
        };
    }

    /* ------------------------- boot ---------------------------- */
    function onReady(fn) {
        if (document.readyState === "complete" || document.readyState === "interactive") {
            try { fn(); } catch (_) { }
            return;
        }
        document.addEventListener("DOMContentLoaded", function () {
            try { fn(); } catch (_) { }
        });
    }

    function installMutationObserverOnce() {
        if (window.__DG_RAG_EVIDENCE_OBS__) return;
        window.__DG_RAG_EVIDENCE_OBS__ = true;

        var th = getChatThread();
        if (!th || !window.MutationObserver) return;

        var moTimer = 0;

        try {
            var mo = new MutationObserver(function (muts) {
                try {
                    if (_DOCKING) return;

                    // ✅ evidence 관련 변경만 발생했으면 무시
                    var relevant = false;

                    for (var i = 0; i < muts.length; i++) {
                        var m = muts[i];
                        if (!m) continue;

                        var adds = m.addedNodes || [];
                        var rms = m.removedNodes || [];

                        for (var a = 0; a < adds.length; a++) {
                            if (adds[a] && !isEvNode(adds[a])) { relevant = true; break; }
                        }
                        if (relevant) break;

                        for (var r = 0; r < rms.length; r++) {
                            if (rms[r] && !isEvNode(rms[r])) { relevant = true; break; }
                        }
                        if (relevant) break;
                    }

                    if (!relevant) return;

                    if (moTimer) clearTimeout(moTimer);
                    moTimer = setTimeout(function () {
                        moTimer = 0;
                        scheduleRedock();
                    }, 80);
                } catch (_) { }
            });

            mo.observe(th, { childList: true, subtree: true });
        } catch (_) { }
    }

    onReady(function () {
        ensureDockScaffold();
        installSummaryHandlerOnce();
        installGlobalCloseHandlersOnce();

        try {
            if (!!window.DG_RAG_EVIDENCE_USE_HOOKS__) {
                installFetchHook();
                installXhrHook();
            }
        } catch (_) { }

        scheduleRedock();

        try {
            var s0 = [];
            if (window.__DG_RAG_LAST_SOURCES__ && typeof window.__DG_RAG_LAST_SOURCES__ === "object" && !Array.isArray(window.__DG_RAG_LAST_SOURCES__)) {
                s0 = window.__DG_RAG_LAST_SOURCES__.sources || [];
            } else if (Array.isArray(window.__DG_RAG_LAST_SOURCES__)) {
                s0 = window.__DG_RAG_LAST_SOURCES__;
            }

            renderDockEvidence(s0 || []);
            setOpenState(false);
        } catch (_) { }

        installMutationObserverOnce();
        dbg("boot ok", CFG);
    });
})();
