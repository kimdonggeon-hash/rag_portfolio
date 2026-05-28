// ragapp/static/ragapp/javascript/qarag_chat.js
(function () {
    "use strict";

    if (window.__QARAG_INITED__) return;
    window.__QARAG_INITED__ = true;

    // ─────────────────────────────────────
    // 공통 유틸
    // ─────────────────────────────────────
    function log(tag, data) {
        try {
            if (typeof window.dglog === "function") window.dglog(tag, data);
            else console.log("[qarag] " + tag, data || "");
        } catch (_) { }
    }

    function getCookie(name) {
        const v = document.cookie ? document.cookie.split("; ") : [];
        for (const s of v) {
            const [k, ...rest] = s.split("=");
            if (k === name) return decodeURIComponent(rest.join(""));
        }
        return null;
    }

    const csrftoken = getCookie("csrftoken");

    function getReqId() {
        try {
            return (window.newReqId && window.newReqId("qarag")) || "qarag-" + Date.now().toString(36);
        } catch (_) {
            return "qarag-" + Date.now().toString(36);
        }
    }

    // ✅ 사용량 위젯 카운트(있으면 갱신)
    function bumpUsage(kind) {
        try {
            if (window.QARAG_USAGE && typeof window.QARAG_USAGE.bump === "function") {
                window.QARAG_USAGE.bump(kind);
            }
        } catch (_) { }
    }

    // QARAG 세션 id (탭마다 하나)
    const QARAG_SESSION_ID = (window.QARAG_SESSION_ID =
        window.QARAG_SESSION_ID || "qarag-" + Math.random().toString(36).slice(2) + Date.now().toString(36));

    // ─────────────────────────────────────
    // ✅ 동의 게이트(폼이 없어도 강제)
    // ─────────────────────────────────────
    function passConsentGateForQarag() {
        try {
            if (typeof window.ensureConsentGate === "function") {
                const panel = document.getElementById("qaragPanel") || document.body;
                return window.ensureConsentGate(panel) !== false;
            }
        } catch (_) { }
        return true;
    }

    // ─────────────────────────────────────
    // ✅ PII 탐지/마스킹 (질문은 차단, 피드백/코멘트는 마스킹)
    // ─────────────────────────────────────
    const PII_RULES = [
        { key: "email", re: /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi },
        { key: "phone_mobile", re: /\b01[016789][-\s]?\d{3,4}[-\s]?\d{4}\b/g },
        { key: "phone_land", re: /\b0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}\b/g },
        { key: "rrn", re: /\b\d{6}[-\s]?[1-4]\d{6}\b/g },
    ];

    function detectLikelyPII(text) {
        const s = String(text || "");
        if (!s.trim()) return false;
        for (const r of PII_RULES) {
            try {
                if (r.re.test(s)) return true;
            } catch (_) {
            } finally {
                try { r.re.lastIndex = 0; } catch (_) { }
            }
        }
        return false;
    }

    function redactPII(text) {
        let s = String(text || "");
        if (!s.trim()) return s;
        for (const r of PII_RULES) {
            try {
                s = s.replace(r.re, "[REDACTED]");
            } catch (_) {
            } finally {
                try { r.re.lastIndex = 0; } catch (_) { }
            }
        }
        return s;
    }

    // ─────────────────────────────────────
    // ✅ AI 생성 배지 유틸 (봇 말풍선에만)
    // ─────────────────────────────────────
    function _makeAIBadge() {
        const badge = document.createElement("span");
        badge.className = "ai-generated-badge";
        badge.textContent = "AI 생성";
        return badge;
    }

    // ─────────────────────────────────────
    // 말풍선 렌더링 (순수 RAG/FAQ 전용)
    // ─────────────────────────────────────
    const lastMsg = { role: "", text: "", at: 0 };

    function addBubble(role, text) {
        const box = document.getElementById("qaragMessages");
        if (!box) return null;

        const now = Date.now();
        const s = String(text || "");

        if (role === lastMsg.role && s === lastMsg.text && now - lastMsg.at < 1500) {
            return null;
        }
        lastMsg.role = role;
        lastMsg.text = s;
        lastMsg.at = now;

        const isUser = role === "user" || role === "me" || role === "operator-me";
        const isBot = !isUser;

        const wrap = document.createElement("div");
        wrap.className = "qarag-msgwrap " + (isUser ? "user" : "bot");
        if (isBot) wrap.dataset.aiGenerated = "1";

        const div = document.createElement("div");
        div.className = "qarag-msg " + (isUser ? "user" : "bot");
        if (isBot) div.dataset.aiGenerated = "1";

        // ✅ user는 텍스트만
        if (isUser) {
            div.textContent = s;
        } else {
            // ✅ bot은 "AI 생성" 배지 + 텍스트(텍스트 노드로 안전하게)
            div.appendChild(_makeAIBadge());

            const body = document.createElement("span");
            body.className = "qarag-msg-text";
            body.textContent = s;
            div.appendChild(body);
        }

        wrap.appendChild(div);
        box.appendChild(wrap);
        box.scrollTop = box.scrollHeight;
        return wrap;
    }

    // 다른 스크립트(WS 등)에서 필요할 수 있으니 노출
    window.__qaragAddMsg = addBubble;

    // ─────────────────────────────────────
    // 피드백 줄 생성
    // ─────────────────────────────────────
    function makeFeedbackRow(question, answer) {
        const row = document.createElement("div");
        row.className = "qarag-feedback-row";
        // ✅ 저장/전송용 데이터는 마스킹된 값으로 유지
        row.dataset.question = redactPII(question || "");
        row.dataset.answer = redactPII(answer || "");
        row.innerHTML = `
      <div class="qarag-feedback-card">
        <div class="qarag-feedback-head">
          <span class="qarag-feedback-title">이 답변, 괜찮으셨나요?</span>
          <span class="qarag-feedback-sub">한 번만 눌러도 다음 답변이 더 좋아져요.</span>
        </div>
        <div class="qarag-feedback-actions">
          <button type="button" class="qarag-thumb-btn" data-kind="good">👍 유용했어요</button>
          <button type="button" class="qarag-thumb-btn" data-kind="bad">👎 아쉬웠어요</button>
        </div>
      </div>
    `;
        return row;
    }

    // ─────────────────────────────────────
    // 429(사용량/레이트리밋) UX 개선 유틸
    // ─────────────────────────────────────
    function _parseMaybeJson(text) {
        const t = (text || "").trim();
        if (!t) return null;
        try { return JSON.parse(t); } catch (_) { return null; }
    }

    function _isHtmlLike(text) {
        const t0 = (text || "").trim().slice(0, 40).toLowerCase();
        return t0.startsWith("<!doctype") || t0.startsWith("<html");
    }

    function _labelForKind(kind) {
        switch (kind) {
            case "web": return "웹 검색";
            case "rag": return "RAG 질문";
            case "pdf": return "PDF";
            case "image": return "이미지 업로드";
            case "table": return "표 업로드";
            default: return "요청";
        }
    }

    function _guessKindFromUrl(url) {
        const u = String(url || "").toLowerCase();
        if (u.includes("/api/web")) return "web";
        if (u.includes("/api/rag")) return "rag";
        if (u.includes("/api/pdf")) return "pdf";
        if (u.includes("/media/")) return "image";
        if (u.includes("/table/")) return "table";
        return "rag";
    }

    // /api/usage/status/는 위젯 갱신 대응용이니 429 메시지 만들 때 활용
    const _USAGE_CACHE = { at: 0, data: null, inflight: null };

    async function _getUsageStatusSafe(signal) {
        // 너무 자주 안 치게 2초 캐시
        const now = Date.now();
        if (_USAGE_CACHE.data && now - _USAGE_CACHE.at < 2000) return _USAGE_CACHE.data;
        if (_USAGE_CACHE.inflight) return _USAGE_CACHE.inflight;

        const candidates = ["/api/usage/status/", "/api/usage/status"];
        _USAGE_CACHE.inflight = (async () => {
            for (const url of candidates) {
                try {
                    const res = await fetch(url, {
                        method: "GET",
                        credentials: "same-origin",
                        signal,
                        headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
                    });
                    if (!res.ok) continue;
                    const j = await res.json().catch(() => null);
                    if (!j) continue;
                    if (j.ok === false) continue;
                    _USAGE_CACHE.at = Date.now();
                    _USAGE_CACHE.data = j;
                    return j;
                } catch (e) {
                    if (e && e.name === "AbortError") throw e;
                }
            }
            return null;
        })().finally(() => {
            _USAGE_CACHE.inflight = null;
        });

        return _USAGE_CACHE.inflight;
    }

    function _readUsageNumber(usage, pathArr) {
        try {
            let cur = usage;
            for (const k of pathArr) {
                if (!cur || typeof cur !== "object") return null;
                cur = cur[k];
            }
            if (cur === null || cur === undefined) return null;
            const n = Number(cur);
            return Number.isFinite(n) ? n : null;
        } catch (_) {
            return null;
        }
    }

    async function _humanize429(errObj, signal) {
        const code = String(errObj.code || "").toLowerCase();
        const message = String(errObj.message || "").toLowerCase();

        if (code.includes("rate") || message.includes("too many") || message.includes("rate")) {
            const sec = errObj.retryAfter ? `${errObj.retryAfter}초 후 ` : "";
            return `요청이 너무 빠르게 들어왔어요. ${sec}다시 시도해 주세요.`;
        }

        const usage = await _getUsageStatusSafe(signal);
        const kind = errObj.kind || _guessKindFromUrl(errObj.url);
        const label = _labelForKind(kind);

        if (usage) {
            const limit =
                _readUsageNumber(usage, ["limits", kind]) ??
                _readUsageNumber(usage, ["limit", kind]) ??
                _readUsageNumber(usage, ["daily_limits", kind]) ??
                _readUsageNumber(usage, ["limits", kind.toUpperCase()]) ??
                null;

            const used =
                _readUsageNumber(usage, ["used", kind]) ??
                _readUsageNumber(usage, ["usage", kind]) ??
                _readUsageNumber(usage, ["count", kind]) ??
                null;

            const remaining =
                _readUsageNumber(usage, ["remaining", kind]) ??
                _readUsageNumber(usage, ["left", kind]) ??
                null;

            if (typeof limit === "number" && typeof used === "number") {
                return `오늘 ${label} 사용 가능 횟수를 다 썼어요. (사용: ${used}/${limit})\n내일 다시 시도해 주세요.`;
            }
            if (typeof remaining === "number" && remaining <= 0) {
                return `오늘 ${label} 사용 가능 횟수를 다 썼어요.\n내일 다시 시도해 주세요.`;
            }
        }

        if (errObj.message && String(errObj.message).trim().length > 0) {
            return String(errObj.message);
        }

        return `오늘 ${label} 사용 가능 횟수를 다 썼어요.\n내일 다시 시도해 주세요.`;
    }

    function _makeErr(message, props) {
        const e = new Error(message || "요청 실패");
        try {
            if (props && typeof props === "object") {
                Object.keys(props).forEach((k) => { e[k] = props[k]; });
            }
        } catch (_) { }
        return e;
    }

    function _errRank(e) {
        const s = Number(e && e.status) || 0;
        if (s === 429) return 100;
        if (s === 403) return 80;
        if (s === 401) return 70;
        if (s >= 500) return 60;
        if (s === 404) return 10;
        return 30;
    }

    function _pickBetterErr(a, b) {
        if (!a) return b;
        if (!b) return a;
        return _errRank(b) >= _errRank(a) ? b : a;
    }

    // ─────────────────────────────────────
    // RAG API 호출
    // ─────────────────────────────────────
    function getRagCandidates() {
        const arr = [];
        if (typeof window.RAG_QA_MAIN === "string" && window.RAG_QA_MAIN) {
            arr.push({ url: window.RAG_QA_MAIN, method: "POST", json: true });
        }
        arr.push(
            { url: "/api/rag_qa", method: "POST", json: true },
            { url: "/api/rag_qa/", method: "POST", json: true },
            { url: "/api/rag_search", method: "POST", json: true },
            { url: "/api/rag_search/", method: "POST", json: true }
        );
        return arr;
    }

    async function callRagAPI(q, signal) {
        const reqId = getReqId();
        const payload = { query: q, q: q, question: q };

        const baseHeaders = {
            "X-Request-Id": reqId,
            "X-Requested-With": "XMLHttpRequest",
            Accept: "application/json",
        };
        if (csrftoken) baseHeaders["X-CSRFToken"] = csrftoken;

        let lastErr = null;

        for (const c of getRagCandidates()) {
            try {
                const headers = Object.assign({}, baseHeaders);
                const init = {
                    method: c.method || "POST",
                    headers,
                    credentials: "same-origin",
                    signal,
                };
                if (c.json) {
                    headers["Content-Type"] = "application/json";
                    init.body = JSON.stringify(payload);
                }

                log("QARAG_FETCH → " + c.url, { reqId, payload, method: init.method });

                const res = await fetch(c.url, init);
                const retryAfter = parseInt(res.headers.get("Retry-After") || "0", 10) || null;

                const text = await res.text().catch(() => "");
                if (_isHtmlLike(text)) {
                    lastErr = _pickBetterErr(
                        lastErr,
                        _makeErr("JSON이 아닌 응답(로그인/에러 페이지) @ " + c.url, {
                            status: res.status || 0,
                            url: c.url,
                            code: "non_json_response",
                            userMessage: "요청이 차단되었거나 로그인 페이지가 반환됐어요. 다시 시도해 주세요.",
                        })
                    );
                    continue;
                }

                const j = _parseMaybeJson(text);

                const code = (j && (j.error || j.code)) || "";
                const msg =
                    (j && (j.message || j.detail)) ||
                    (text && String(text).trim().slice(0, 300)) ||
                    ("HTTP " + res.status);

                if (!res.ok) {
                    if (res.status === 429) {
                        const kind = _guessKindFromUrl(c.url);
                        const friendly = await _humanize429(
                            { status: 429, url: c.url, code, message: msg, retryAfter, kind },
                            signal
                        );

                        lastErr = _pickBetterErr(
                            lastErr,
                            _makeErr(friendly, {
                                status: 429,
                                url: c.url,
                                code: code || "too_many_requests",
                                retryAfter,
                                userMessage: friendly,
                                kind,
                            })
                        );
                        continue;
                    }

                    const userMessage =
                        res.status === 403
                            ? "요청 권한이 없거나 보안 정책에 의해 차단됐어요. 새로고침 후 다시 시도해 주세요."
                            : res.status === 401
                                ? "로그인이 필요해요. 다시 로그인 후 시도해 주세요."
                                : "요청에 실패했어요. (HTTP " + res.status + ")";

                    lastErr = _pickBetterErr(
                        lastErr,
                        _makeErr(msg + " @ " + c.url, {
                            status: res.status,
                            url: c.url,
                            code: code || "http_error",
                            userMessage: msg && String(msg).trim() ? String(msg) : userMessage,
                        })
                    );
                    continue;
                }

                if (j && j.ok === false) {
                    if (String(code).toLowerCase().includes("rate") || String(code).toLowerCase().includes("quota")) {
                        const kind = _guessKindFromUrl(c.url);
                        const friendly = await _humanize429(
                            { status: 429, url: c.url, code, message: msg, retryAfter: null, kind },
                            signal
                        );
                        lastErr = _pickBetterErr(
                            lastErr,
                            _makeErr(friendly, { status: 429, url: c.url, code, userMessage: friendly, kind })
                        );
                    } else {
                        lastErr = _pickBetterErr(
                            lastErr,
                            _makeErr((code || msg || "API 실패") + " @ " + c.url, {
                                status: 200,
                                url: c.url,
                                code: code || "api_failed",
                                userMessage: msg || "요청에 실패했어요.",
                            })
                        );
                    }
                    continue;
                }

                const codeUp = String((j && (j.code || j.error_code || j.err_code)) || "").toUpperCase();
                const blocked = !!(j && (codeUp === "PII_BLOCKED" || j.mode === "blocked"));

                if (blocked) {
                    const txt =
                        (j && (j.answer_text || j.answer || j.msg || j.message || j.detail || j.error)) ||
                        "개인정보로 보이는 내용이 있어 전송을 중단했어요.";

                    throw _makeErr(txt, {
                        status: 200,
                        url: c.url,
                        code: "PII_BLOCKED",
                        userMessage: txt,
                        blocked: true,
                    });
                }

                if (j) {
                    const ans = j.answer_text || j.answer || j.text || j.reply || j.result || j.a || j.data;
                    if (ans && String(ans).trim()) return String(ans);
                }

                const fallback = (text || "").trim();
                return fallback || "응답이 없습니다.";
            } catch (e) {
                if (e && e.name === "AbortError") throw e;
                lastErr = _pickBetterErr(
                    lastErr,
                    _makeErr(e && e.message ? e.message : "요청 실패", {
                        status: 0,
                        url: (c && c.url) || "",
                        code: "network_error",
                        userMessage: "네트워크 오류가 발생했어요. 잠시 후 다시 시도해 주세요.",
                    })
                );
            }
        }

        throw lastErr || new Error("RAG API 호출 실패");
    }

    // ─────────────────────────────────────
    // QARAG 전송 상태 (RAG/FAQ 전용)
    // ─────────────────────────────────────
    const BUSY = { inflight: false, lastQ: "", lastAt: 0, ctrl: null };

    function abortInflight() {
        try { BUSY.ctrl && BUSY.ctrl.abort(); } catch (_) { }
    }

    async function sendQarag(ev) {
        if (ev && ev.preventDefault) ev.preventDefault();

        const input = document.getElementById("qaragInput");
        const btn = document.getElementById("qaragSendBtn");
        const q = (input?.value || "").trim();
        if (!q) return false;

        if (!passConsentGateForQarag()) {
            addBubble("bot", "먼저 저장 동의가 필요해요. 안내를 확인하고 다시 시도해 주세요.");
            return false;
        }

        if (detectLikelyPII(q)) {
            addBubble("bot", "개인정보로 보이는 내용이 포함되어 전송하지 않았어요. 지우고 다시 시도해 주세요.");
            return false;
        }

        const now = Date.now();
        if (BUSY.inflight || (BUSY.lastQ === q && now - BUSY.lastAt < 1200)) {
            return false;
        }

        addBubble("user", q);

        BUSY.inflight = true;
        BUSY.lastQ = q;
        BUSY.lastAt = now;

        abortInflight();
        BUSY.ctrl = new AbortController();

        if (input) {
            input.value = "";
            input.disabled = true;
            input.placeholder = "답변 생성 중…";
        }
        if (btn) {
            btn.disabled = true;
            btn.dataset.loading = "1";
        }

        try {
            const answer = await callRagAPI(q, BUSY.ctrl.signal);
            addBubble("bot", answer);

            bumpUsage("rag");

            const box = document.getElementById("qaragMessages");
            if (box) {
                box.appendChild(makeFeedbackRow(q, answer));
                box.scrollTop = box.scrollHeight;
            }
        } catch (e) {
            if (e && e.name === "AbortError") {
                log("QARAG_ABORT", e);
            } else {
                log("QARAG_ERR", e);
                const friendly = e && e.userMessage ? e.userMessage : e && e.message ? e.message : "요청 실패";
                addBubble("bot", "⚠️ " + friendly);
            }
        } finally {
            BUSY.inflight = false;
            if (input) {
                input.disabled = false;
                input.placeholder = "메시지 보내기…";
                // ⚠️ 패널이 닫히는 도중(숨김/inert)에는 focus를 주지 않음
                try {
                    const panel = document.getElementById("qaragPanel");
                    if (panel && !panel.hidden && panel.getAttribute("aria-hidden") !== "true" && !panel.hasAttribute("inert")) {
                        input.focus();
                    }
                } catch (_) { }
            }
            if (btn) {
                btn.disabled = false;
                btn.removeAttribute("data-loading");
            }
        }

        return false;
    }

    window.sendQarag = sendQarag;

    // ─────────────────────────────────────
    // 패널 위치: 가운데 정렬 + 드래그
    // ─────────────────────────────────────
    function centerQaragPanel(panel) {
        if (!panel) return;

        if (panel.dataset.userMoved === "1") return;
        if (panel.dataset.centered === "1") return;

        const rect = panel.getBoundingClientRect();
        const vw = window.innerWidth || document.documentElement.clientWidth || 0;
        const vh = window.innerHeight || document.documentElement.clientHeight || 0;

        if (!rect.width || !rect.height || !vw || !vh) return;

        let left = (vw - rect.width) / 2;
        let top = (vh - rect.height) / 2;
        const margin = 12;
        if (left < margin) left = margin;
        if (top < margin) top = margin;

        panel.style.left = left + "px";
        panel.style.top = top + "px";
        panel.style.right = "auto";
        panel.style.bottom = "auto";
        panel.style.transform = "none";

        panel.dataset.centered = "1";
    }

    function makeQaragDraggable(panel, handle) {
        if (!panel || !handle) return;
        if (panel.dataset.draggable === "1") return;
        panel.dataset.draggable = "1";

        let dragging = false;
        let offsetX = 0;
        let offsetY = 0;

        function startDrag(e) {
            const target = e.target;
            if (target && target.closest && target.closest("button, a, input, textarea, [data-no-drag]")) {
                return;
            }

            e.preventDefault();

            dragging = true;
            panel.classList.add("is-dragging");
            panel.dataset.userMoved = "1";

            const evt = e.touches ? e.touches[0] : e;

            const rect = panel.getBoundingClientRect();
            panel.style.left = rect.left + "px";
            panel.style.top = rect.top + "px";
            panel.style.right = "auto";
            panel.style.bottom = "auto";
            panel.style.transform = "none";

            offsetX = evt.clientX - rect.left;
            offsetY = evt.clientY - rect.top;

            document.addEventListener("mousemove", onMove);
            document.addEventListener("mouseup", endDrag);
            document.addEventListener("touchmove", onMove, { passive: false });
            document.addEventListener("touchend", endDrag);
        }

        function onMove(e) {
            if (!dragging) return;
            const evt = e.touches ? e.touches[0] : e;
            e.preventDefault();

            let x = evt.clientX - offsetX;
            let y = evt.clientY - offsetY;

            const maxX = window.innerWidth - panel.offsetWidth;
            const maxY = window.innerHeight - panel.offsetHeight;

            if (x < 0) x = 0;
            if (y < 0) y = 0;
            if (x > maxX) x = maxX;
            if (y > maxY) y = maxY;

            panel.style.left = x + "px";
            panel.style.top = y + "px";
        }

        function endDrag() {
            if (!dragging) return;
            dragging = false;
            panel.classList.remove("is-dragging");
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", endDrag);
            document.removeEventListener("touchmove", onMove);
            document.removeEventListener("touchend", endDrag);
        }

        handle.addEventListener("mousedown", startDrag);
        handle.addEventListener("touchstart", startDrag, { passive: false });
    }

    // ─────────────────────────────────────
    // ✅ 접근성 안전: 패널 열기/닫기 포커스 관리
    // ─────────────────────────────────────
    let _qaragReturnFocusEl = null;

    function _isFocusable(el) {
        if (!el || !(el instanceof HTMLElement)) return false;
        if (el.hasAttribute("disabled")) return false;
        const tab = el.getAttribute("tabindex");
        if (tab === "-1") return false;
        // hidden/aria-hidden/inert는 포커스 대상에서 제외
        if (el.hidden) return false;
        if (el.getAttribute("aria-hidden") === "true") return false;
        if (el.hasAttribute("inert")) return false;
        return true;
    }

    function _safeFocus(el) {
        try {
            if (!_isFocusable(el)) return false;
            el.focus({ preventScroll: true });
            return true;
        } catch (_) {
            return false;
        }
    }

    function _firstFocusable(root) {
        if (!root) return null;
        return root.querySelector(
            'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
    }

    function _moveFocusOutsidePanel(panel, launch) {
        try {
            const ae = document.activeElement;
            if (ae instanceof HTMLElement && panel && panel.contains(ae)) {
                // 1) 저장해둔 복귀 포커스
                if (_qaragReturnFocusEl && document.contains(_qaragReturnFocusEl)) {
                    if (_safeFocus(_qaragReturnFocusEl)) return;
                }
                // 2) launch 버튼
                if (launch && document.contains(launch)) {
                    if (_safeFocus(launch)) return;
                }
                // 3) 최후: body
                if (document.body) {
                    try { document.body.focus({ preventScroll: true }); } catch (_) { }
                }
            }
        } catch (_) { }
    }

    // ─────────────────────────────────────
    // 패널 열기/닫기
    // ─────────────────────────────────────
    function wirePanel() {
        const panel = document.getElementById("qaragPanel");
        const backdrop = document.getElementById("qaragBackdrop");
        const launch = document.getElementById("qaragLaunchBtn");
        const close = document.getElementById("qaragCloseBtn");

        if (!panel || !launch) return;
        if (panel.dataset.wired === "1") return;
        panel.dataset.wired = "1";

        const header = panel.querySelector(".qarag-header");
        if (header) {
            makeQaragDraggable(panel, header);
        }

        function open() {
            // 열기 직전 포커스 저장(닫을 때 복귀)
            try {
                const ae = document.activeElement;
                _qaragReturnFocusEl = (ae instanceof HTMLElement) ? ae : null;
            } catch (_) {
                _qaragReturnFocusEl = null;
            }

            panel.hidden = false;
            panel.classList.add("show");

            // ✅ 먼저 상호작용 허용
            panel.removeAttribute("inert");
            panel.setAttribute("aria-hidden", "false");

            launch.setAttribute("aria-expanded", "true");
            document.body.classList.add("qarag-open");

            if (backdrop) {
                backdrop.hidden = false;
                backdrop.classList.add("show");
                backdrop.setAttribute("aria-hidden", "false");
            }

            centerQaragPanel(panel);

            // ✅ 포커스를 패널 내부로 이동 (닫기 버튼 우선, 없으면 입력)
            requestAnimationFrame(() => {
                const input = document.getElementById("qaragInput");
                const target = close || input || _firstFocusable(panel) || panel;
                if (target && typeof target.focus === "function") {
                    try { target.focus({ preventScroll: true }); } catch (_) { }
                }
            });
        }

        function closeFn() {
            // RAG 요청 중단
            abortInflight();

            // ✅ (핵심) aria-hidden/inert/hidden 적용 전에 포커스를 패널 밖으로 이동
            _moveFocusOutsidePanel(panel, launch);

            // ✅ 상호작용 차단
            panel.setAttribute("inert", "");

            // aria-hidden은 '보이는 동안 페이드아웃'을 위해 유지 가능
            panel.setAttribute("aria-hidden", "true");

            panel.classList.remove("show");
            launch.setAttribute("aria-expanded", "false");
            document.body.classList.remove("qarag-open");

            if (backdrop) {
                backdrop.classList.remove("show");
                backdrop.setAttribute("aria-hidden", "true");
            }

            // 애니메이션 이후 숨김 (hidden은 접근성 트리에서도 제거)
            setTimeout(() => {
                panel.hidden = true;
                if (backdrop) backdrop.hidden = true;
            }, 130);
        }

        window.openQaragPanel = open;
        window.closeQaragPanel = closeFn;

        launch.addEventListener("click", function (e) {
            e.preventDefault();
            if (panel.hidden || panel.getAttribute("aria-hidden") === "true") open();
            else closeFn();
        });

        if (close) {
            close.addEventListener("click", function (e) {
                e.preventDefault();
                closeFn();
            });
        }

        if (backdrop && !backdrop.dataset.wired) {
            backdrop.dataset.wired = "1";
            backdrop.addEventListener("click", function (e) {
                if (e.target === backdrop) closeFn();
            });
        }

        document.addEventListener("keydown", function (ev) {
            if (ev.key === "Escape" && !panel.hidden) closeFn();
        });

        const input = document.getElementById("qaragInput");
        const sendBtn = document.getElementById("qaragSendBtn");

        // QARAG 입력창: Enter = 전송, Shift+Enter = 줄바꿈 (+ 4줄까지 auto-resize)
        if (input && !input.dataset.qaragWired) {
            input.dataset.qaragWired = "1";

            if (input.tagName === "TEXTAREA") {
                const style = window.getComputedStyle(input);
                const lineH = parseFloat(style.lineHeight || "20") || 20;
                const baseHeight = input.scrollHeight || lineH * 1.5;
                const maxHeight = baseHeight * 4;

                function autoResize() {
                    input.style.height = "auto";
                    let h = input.scrollHeight;
                    if (h < baseHeight) h = baseHeight;
                    if (h > maxHeight) h = maxHeight;
                    input.style.height = h + "px";
                    input.style.overflowY = input.scrollHeight > maxHeight ? "auto" : "hidden";
                }

                input.addEventListener("input", autoResize);
                autoResize();
            }

            input.addEventListener(
                "keydown",
                function (e) {
                    if (e.isComposing || e.keyCode === 229) return;
                    if (e.key !== "Enter") return;

                    if (e.shiftKey) {
                        e.stopPropagation();
                        return;
                    }

                    e.preventDefault();
                    e.stopPropagation();
                    sendQarag(e);
                },
                true
            );
        }

        if (sendBtn && !sendBtn.dataset.qaragWired) {
            sendBtn.dataset.qaragWired = "1";
            sendBtn.addEventListener("click", function (e) {
                e.preventDefault();
                sendQarag(e);
            });
        }

        // 질문 챗봇에서는 상담 종료 버튼은 기본적으로 숨김 상태 유지
        try {
            const btnEnd = document.getElementById("btnEndLive");
            if (btnEnd) {
                btnEnd.style.display = "none";
                btnEnd.hidden = true;
            }
        } catch (_) { }
    }

    // ─────────────────────────────────────
    // 피드백 (👍/👎)
    // ─────────────────────────────────────
    function sendFeedback(kind, opts, row) {
        const question = (row && row.dataset.question) || "";
        const answer = (row && row.dataset.answer) || "";

        let helpful = null;
        let stage = "thumb";
        if (kind === "good") {
            helpful = true;
            stage = "thumb";
        } else if (kind === "bad") {
            helpful = false;
            stage = "detail";
        } else if (kind === "bad-skip") {
            helpful = false;
            stage = "skip";
        }

        let reasons = [];
        let comment = "";
        if (opts && typeof opts === "object") {
            if (Array.isArray(opts.reasons)) reasons = opts.reasons.map((x) => String(x));
            if (typeof opts.comment === "string") comment = opts.comment;
        } else if (typeof opts === "string") comment = opts;

        const djangoSessionId = getCookie("sessionid") || null;

        // ✅ 마스킹 적용(피드백/코멘트/answer/question)
        const safeQuestion = redactPII(question);
        const safeAnswer = redactPII(answer);
        const safeComment = redactPII(comment);

        const payload = {
            answer_type: "qa",
            from_ui: "qarag_main",
            helpful,
            question: safeQuestion,
            answer: safeAnswer,
            reasons,
            comment: safeComment,
            stage,
            session_id: QARAG_SESSION_ID,
        };
        if (djangoSessionId) payload.django_session_id = djangoSessionId;

        const reqId = getReqId();
        log("QARAG_FEEDBACK_SEND", { kind, reqId, payload });

        return fetch("/api/feedback", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-Request-Id": reqId,
                "X-CSRFToken": csrftoken || "",
                Accept: "application/json",
            },
            credentials: "same-origin",
            body: JSON.stringify(payload),
        })
            .then(async (res) => {
                const text = await res.text();
                let j = {};
                try { j = JSON.parse(text); } catch (_) { }
                if (!res.ok || (j && j.ok === false)) {
                    const msg = (j && (j.error || j.message || j.detail)) || "HTTP " + res.status;
                    throw new Error(msg);
                }
                return j;
            })
            .catch((e) => {
                log("QARAG_FEEDBACK_ERR", e && e.message ? e.message : e);
                throw e;
            });
    }

    function showBadFeedbackForm(row) {
        if (row.nextElementSibling && row.nextElementSibling.classList?.contains("qarag-feedback-form")) return;

        const card = document.createElement("div");
        card.className = "qarag-feedback-form";
        card.innerHTML = `
      <div class="qarag-feedback-card qarag-feedback-card-detail">
        <p class="qarag-feedback-title">어떤 점이 아쉬웠나요?</p>

        <div class="qarag-feedback-chip-row">
          <button type="button" class="qarag-feedback-chip" data-reason="정확하지 않아요">정확하지 않아요</button>
          <button type="button" class="qarag-feedback-chip" data-reason="답변이 너무 길어요">답변이 너무 길어요</button>
          <button type="button" class="qarag-feedback-chip" data-reason="질문을 잘 이해 못한 것 같아요">질문을 잘 이해 못한 것 같아요</button>
          <button type="button" class="qarag-feedback-chip" data-reason="최신 내용이 아닌 것 같아요">최신 내용이 아닌 것 같아요</button>
        </div>

        <textarea class="qarag-feedback-text"
                  name="qarag_feedback_comment"
                  autocomplete="off"
                  placeholder="추가로 적어 주실 내용이 있다면 편하게 써 주세요."></textarea>

        <div class="qarag-feedback-form-actions">
          <button type="button" class="qarag-feedback-send">보내기</button>
          <button type="button" class="qarag-feedback-skip">건너뛰기</button>
        </div>
      </div>
    `;

        row.insertAdjacentElement("afterend", card);

        const textarea = card.querySelector(".qarag-feedback-text");
        const sendBtn = card.querySelector(".qarag-feedback-send");
        const skipBtn = card.querySelector(".qarag-feedback-skip");
        const chips = card.querySelectorAll(".qarag-feedback-chip");

        chips.forEach((chip) => chip.addEventListener("click", () => chip.classList.toggle("is-active")));

        function collect() {
            const commentRaw = (textarea && textarea.value) || "";
            const selected = Array.from(chips)
                .filter((c) => c.classList.contains("is-active"))
                .map((c) => c.getAttribute("data-reason") || c.textContent.trim());
            return { selected, comment: commentRaw.trim() };
        }

        async function submitDetail() {
            const { selected, comment } = collect();
            try {
                await sendFeedback("bad", { reasons: selected, comment }, row);
                addBubble("bot", "말씀해 주셔서 정말 감사합니다. 더 나은 답변을 위해 바로 반영해 볼게요. 🙏");
            } catch (_) {
                addBubble("bot", "피드백 전송에 문제가 있었어요. 잠시 후 다시 시도해 주세요.");
            } finally {
                card.remove();
            }
        }

        if (textarea) {
            textarea.addEventListener("keydown", function (e) {
                if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    submitDetail();
                }
            });
            textarea.focus();
        }

        if (sendBtn) sendBtn.addEventListener("click", submitDetail);
        if (skipBtn) {
            skipBtn.addEventListener("click", function () {
                const { selected } = collect();
                sendFeedback("bad-skip", { reasons: selected, comment: "" }, row).catch(() => { });
                addBubble("bot", "알려주셔서 감사합니다. 더 좋은 답을 준비해 볼게요. 🙏");
                card.remove();
            });
        }
    }

    // 클릭 이벤트 위임 (👍/👎)
    document.addEventListener("click", function (e) {
        const btn = e.target.closest && e.target.closest(".qarag-thumb-btn");
        if (!btn) return;

        const row = btn.closest(".qarag-feedback-row");
        if (!row) return;
        if (btn.disabled) return;

        const kind = btn.dataset.kind || "";

        row.querySelectorAll(".qarag-thumb-btn").forEach((b) => (b.disabled = true));

        if (kind === "good") {
            sendFeedback("good", null, row).catch(() => { });
            addBubble("bot", "도움이 되었다니 기뻐요! 다음에도 더 좋은 답변으로 도와 드릴게요. 😊");
        } else if (kind === "bad") {
            showBadFeedbackForm(row);
        }
    });

    // ─────────────────────────────────────
    // 초기화
    // ─────────────────────────────────────
    function init() {
        wirePanel();
        // 실시간 상담 모드/WS 연동은 전부 livechat_client.js에서만 처리
        // 여기서는 RAG/FAQ 전용 질문 챗봇 역할만 수행
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
