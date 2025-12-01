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
            return (
                (window.newReqId && window.newReqId("qarag")) ||
                "qarag-" + Date.now().toString(36)
            );
        } catch (_) {
            return "qarag-" + Date.now().toString(36);
        }
    }

    // QARAG 세션 id (탭마다 하나)
    const QARAG_SESSION_ID = (window.QARAG_SESSION_ID =
        window.QARAG_SESSION_ID ||
        "qarag-" + Math.random().toString(36).slice(2) + Date.now().toString(36));

    // ─────────────────────────────────────
    // 말풍선 렌더링 (순수 RAG/FAQ 전용)
    // ─────────────────────────────────────
    const lastMsg = { role: "", text: "", at: 0 };

    function addBubble(role, text) {
        const box = document.getElementById("qaragMessages");
        if (!box) return null;

        const now = Date.now();
        const s = String(text || "");

        if (
            role === lastMsg.role &&
            s === lastMsg.text &&
            now - lastMsg.at < 1500
        ) {
            return null;
        }
        lastMsg.role = role;
        lastMsg.text = s;
        lastMsg.at = now;

        const isUser = role === "user" || role === "me" || role === "operator-me";

        const wrap = document.createElement("div");
        wrap.className = "qarag-msgwrap " + (isUser ? "user" : "bot");

        const div = document.createElement("div");
        div.className = "qarag-msg " + (isUser ? "user" : "bot");
        div.textContent = s;

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
        row.dataset.question = question || "";
        row.dataset.answer = answer || "";
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
                const text = await res.text();

                const t0 = (text || "").trim().slice(0, 20).toLowerCase();
                if (t0.startsWith("<!doctype") || t0.startsWith("<html")) {
                    lastErr = new Error("JSON이 아닌 응답(로그인/에러 페이지) @ " + c.url);
                    continue;
                }

                let j = null;
                try {
                    j = JSON.parse(text);
                } catch (_) { }

                if (!res.ok) {
                    lastErr = new Error("HTTP " + res.status + " @ " + c.url);
                    continue;
                }
                if (j && j.ok === false) {
                    lastErr = new Error(
                        (j.error || j.message || "API 실패") + " @ " + c.url
                    );
                    continue;
                }

                if (j) {
                    const ans =
                        j.answer_text ||
                        j.answer ||
                        j.text ||
                        j.reply ||
                        j.result ||
                        j.a ||
                        j.data;
                    if (ans && String(ans).trim()) return String(ans);
                }

                const fallback = (text || "").trim();
                return fallback || "응답이 없습니다.";
            } catch (e) {
                if (e && e.name === "AbortError") throw e;
                lastErr = e;
            }
        }

        throw lastErr || new Error("RAG API 호출 실패");
    }

    // ─────────────────────────────────────
    // QARAG 전송 상태 (RAG/FAQ 전용)
    // ─────────────────────────────────────
    const BUSY = { inflight: false, lastQ: "", lastAt: 0, ctrl: null };

    function abortInflight() {
        try {
            BUSY.ctrl && BUSY.ctrl.abort();
        } catch (_) { }
    }

    async function sendQarag(ev) {
        if (ev && ev.preventDefault) ev.preventDefault();

        const input = document.getElementById("qaragInput");
        const btn = document.getElementById("qaragSendBtn");
        const q = (input?.value || "").trim();
        if (!q) return false;

        const now = Date.now();
        if (
            BUSY.inflight ||
            (BUSY.lastQ === q && now - BUSY.lastAt < 1200)
        ) {
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
                addBubble(
                    "bot",
                    "❌ 오류: " + (e && e.message ? e.message : "요청 실패")
                );
            }
        } finally {
            BUSY.inflight = false;
            if (input) {
                input.disabled = false;
                input.placeholder = "메시지 보내기…";
                input.focus();
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

        // 이미 사용자가 한번이라도 움직였으면 자동 중앙정렬 안 함
        if (panel.dataset.userMoved === "1") return;
        if (panel.dataset.centered === "1") return;

        const rect = panel.getBoundingClientRect();
        const vw =
            window.innerWidth || document.documentElement.clientWidth || 0;
        const vh =
            window.innerHeight || document.documentElement.clientHeight || 0;

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
            if (
                target &&
                target.closest &&
                target.closest("button, a, input, textarea, [data-no-drag]")
            ) {
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
            panel.hidden = false;
            panel.classList.add("show");
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

            const input = document.getElementById("qaragInput");
            if (input) setTimeout(() => input.focus(), 0);
        }

        function closeFn() {
            // RAG 요청 중단
            abortInflight();

            if (panel.contains(document.activeElement)) launch.focus();

            panel.setAttribute("inert", "");
            panel.setAttribute("aria-hidden", "true");
            panel.classList.remove("show");
            launch.setAttribute("aria-expanded", "false");
            document.body.classList.remove("qarag-open");

            if (backdrop) {
                backdrop.classList.remove("show");
                backdrop.setAttribute("aria-hidden", "true");
            }

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
                    input.style.overflowY =
                        input.scrollHeight > maxHeight ? "auto" : "hidden";
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
            if (Array.isArray(opts.reasons))
                reasons = opts.reasons.map((x) => String(x));
            if (typeof opts.comment === "string") comment = opts.comment;
        } else if (typeof opts === "string") comment = opts;

        const djangoSessionId = getCookie("sessionid") || null;

        const payload = {
            answer_type: "qa",
            from_ui: "qarag_main",
            helpful,
            question,
            answer,
            reasons,
            comment,
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
            },
            credentials: "same-origin",
            body: JSON.stringify(payload),
        })
            .then(async (res) => {
                const text = await res.text();
                let j = {};
                try {
                    j = JSON.parse(text);
                } catch (_) { }
                if (!res.ok || (j && j.ok === false)) {
                    const msg =
                        (j && (j.error || j.message || j.detail)) ||
                        "HTTP " + res.status;
                    throw new Error(msg);
                }
                return j;
            })
            .catch((e) => {
                log(
                    "QARAG_FEEDBACK_ERR",
                    e && e.message ? e.message : e
                );
                throw e;
            });
    }

    function showBadFeedbackForm(row) {
        if (
            row.nextElementSibling &&
            row.nextElementSibling.classList?.contains("qarag-feedback-form")
        )
            return;

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

        chips.forEach((chip) =>
            chip.addEventListener("click", () =>
                chip.classList.toggle("is-active")
            )
        );

        function collect() {
            const commentRaw = (textarea && textarea.value) || "";
            const selected = Array.from(chips)
                .filter((c) => c.classList.contains("is-active"))
                .map(
                    (c) =>
                        c.getAttribute("data-reason") ||
                        c.textContent.trim()
                );
            return { selected, comment: commentRaw.trim() };
        }

        async function submitDetail() {
            const { selected, comment } = collect();
            try {
                await sendFeedback(
                    "bad",
                    { reasons: selected, comment },
                    row
                );
                addBubble(
                    "bot",
                    "말씀해 주셔서 정말 감사합니다. 더 나은 답변을 위해 바로 반영해 볼게요. 🙏"
                );
            } catch (_) {
                addBubble(
                    "bot",
                    "피드백 전송에 문제가 있었어요. 잠시 후 다시 시도해 주세요."
                );
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
                sendFeedback(
                    "bad-skip",
                    { reasons: selected, comment: "" },
                    row
                ).catch(() => { });
                addBubble(
                    "bot",
                    "알려주셔서 감사합니다. 더 좋은 답을 준비해 볼게요. 🙏"
                );
                card.remove();
            });
        }
    }

    // 클릭 이벤트 위임 (👍/👎)
    document.addEventListener("click", function (e) {
        const btn =
            e.target.closest && e.target.closest(".qarag-thumb-btn");
        if (!btn) return;

        const row = btn.closest(".qarag-feedback-row");
        if (!row) return;
        if (btn.disabled) return;

        const kind = btn.dataset.kind || "";

        row
            .querySelectorAll(".qarag-thumb-btn")
            .forEach((b) => (b.disabled = true));

        if (kind === "good") {
            sendFeedback("good", null, row).catch(() => { });
            addBubble(
                "bot",
                "도움이 되었다니 기뻐요! 다음에도 더 좋은 답변으로 도와 드릴게요. 😊"
            );
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
