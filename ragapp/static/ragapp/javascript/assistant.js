/* ragapp/static/ragapp/javascript/assistant.js */
(() => {
    "use strict";

    // endpoints from <body data-...>
    const rawChat = (document.body?.dataset?.chatEndpoint || "/api/rag_qa/").trim();
    const rawFb = (document.body?.dataset?.feedbackEndpoint || "/api/feedback/").trim();

    // ✅ same-origin path만 쓰도록 정규화 (안전)
    function normalizeSameOriginPath(u, fallback) {
        try {
            const url = new URL(u, location.origin);
            if (url.origin !== location.origin) return fallback;
            return url.pathname + url.search;
        } catch (_) {
            return fallback;
        }
    }

    const CHAT_ENDPOINT = normalizeSameOriginPath(rawChat, "/api/rag_qa/");
    const FEEDBACK_ENDPOINT = normalizeSameOriginPath(rawFb, "/api/feedback/");

    // ✅ 피드백 위젯 endpoint 주입 (위젯 내부도 same-origin 강제하지만, 여기서도 안전 경로만 넣음)
    window.DG_ENDPOINTS = window.DG_ENDPOINTS || {};
    window.DG_ENDPOINTS.feedback = FEEDBACK_ENDPOINT;
    window.DG_FEEDBACK_ENDPOINT = FEEDBACK_ENDPOINT;

    function getCookie(name) {
        const v = document.cookie ? document.cookie.split("; ") : [];
        for (const s of v) {
            const [k, ...rest] = s.split("=");
            if (k === name) return decodeURIComponent(rest.join("="));
        }
        return null;
    }

    function appendMessage(role, text, isHtml) {
        const msgs = document.getElementById("chatMessages");
        if (!msgs) return;

        const div = document.createElement("div");
        div.classList.add("msg");
        if (role === "user") div.classList.add("user");
        else if (role === "bot") div.classList.add("bot");
        else if (role === "error") div.classList.add("error");

        if (isHtml) div.innerHTML = text;
        else div.textContent = text;

        msgs.appendChild(div);
        msgs.scrollTop = msgs.scrollHeight;
    }

    function mountFeedbackWidget(container, payload) {
        try {
            const w = window.DGFeedbackWidget;
            if (w && typeof w.mount === "function") {
                w.mount(container, payload);
                return;
            }
        } catch (_) { }

        container.textContent = "피드백 위젯을 불러오지 못했어요.";
    }

    function appendBotAnswer(data) {
        const msgs = document.getElementById("chatMessages");
        if (!msgs) return;

        const mode = data.mode || "";
        const answerText = data.answer || "(빈 응답)";
        const hits = Array.isArray(data.hits) ? data.hits : [];
        const faqHtml = data.answer_html || "";
        const userQuestion = data._user_question || "";

        const outer = document.createElement("div");
        outer.classList.add("msg", "bot");
        if (mode === "blocked") outer.classList.add("blocked");

        let contentEl;
        if (mode === "faq" && faqHtml) {
            contentEl = document.createElement("div");
            contentEl.innerHTML = faqHtml; // 서버에서 safe html로 내려오는 걸 전제로
        } else if (mode === "blocked") {
            contentEl = document.createElement("div");
            contentEl.classList.add("blocked-card");
            contentEl.innerHTML =
                '<div class="blocked-head">❌ 민감한 질문으로 분류되었어요</div>' +
                '<div class="blocked-body"></div>' +
                '<div class="blocked-hint">개인정보(연락처 등)나 보안에 민감한 요청은 답변할 수 없어요.</div>';
            const bodyEl = contentEl.querySelector(".blocked-body");
            if (bodyEl) bodyEl.textContent = answerText;
        } else {
            contentEl = document.createElement("div");
            contentEl.classList.add("assistant-answer-text");
            contentEl.textContent = answerText;
        }

        outer.appendChild(contentEl);

        // Evidence (RAG)
        if (mode === "rag" && hits.length > 0) {
            const evWrap = document.createElement("div");
            evWrap.classList.add("evidence-wrap");

            const toggleBtn = document.createElement("button");
            toggleBtn.type = "button";
            toggleBtn.classList.add("evidence-btn", "evidence-toggle-btn");
            toggleBtn.textContent = "📎 근거 보기 ▾";

            const evBox = document.createElement("div");
            evBox.classList.add("evidence-box");
            evBox.style.display = "none";

            const ul = document.createElement("ul");
            ul.classList.add("evidence-list");

            hits.forEach((h) => {
                const li = document.createElement("li");

                const titleDiv = document.createElement("div");
                titleDiv.classList.add("ev-title");

                let label = (h && h.title) ? String(h.title) : "문서";
                if (h && h.source) label += " · " + String(h.source);

                if (h && h.url) {
                    const a = document.createElement("a");
                    a.href = String(h.url);
                    a.target = "_blank";
                    a.rel = "noopener noreferrer";
                    a.textContent = label;
                    titleDiv.appendChild(a);
                } else {
                    titleDiv.textContent = label;
                }

                const snipDiv = document.createElement("div");
                snipDiv.classList.add("ev-snippet");
                snipDiv.textContent = (h && h.snippet) ? String(h.snippet) : "";

                li.appendChild(titleDiv);
                li.appendChild(snipDiv);
                ul.appendChild(li);
            });

            evBox.appendChild(ul);
            evWrap.appendChild(toggleBtn);
            evWrap.appendChild(evBox);
            outer.appendChild(evWrap);
        }

        // ✅ Feedback: fx-fb 위젯만 사용
        const fbHost = document.createElement("div");
        fbHost.className = "fx-fb-host js-feedback-widget";
        outer.appendChild(fbHost);

        const payload = {
            answer_type: (mode === "rag" ? "rag" : "qa"),
            from_ui: "assistant",
            question: userQuestion,
            answer: answerText,
            sources: hits,
            log_id: data.log_id || data.chat_log_id || null,
        };

        mountFeedbackWidget(fbHost, payload);

        msgs.appendChild(outer);
        msgs.scrollTop = msgs.scrollHeight;
    }

    async function sendChat(evt) {
        evt?.preventDefault?.();

        const inputEl = document.getElementById("chatInput");
        const sendBtn = document.getElementById("chatSendBtn");
        if (!inputEl || !sendBtn) return false;

        const q = (inputEl.value || "").trim();
        if (!q) return false;

        appendMessage("user", q, false);

        inputEl.value = "";
        inputEl.disabled = true;
        sendBtn.disabled = true;
        const oldBtnTxt = sendBtn.textContent;
        sendBtn.textContent = "⏳";

        try {
            const csrftoken = getCookie("csrftoken");

            const resp = await fetch(CHAT_ENDPOINT, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-Requested-With": "fetch",
                    ...(csrftoken ? { "X-CSRFToken": csrftoken } : {}),
                },
                credentials: "same-origin",
                body: JSON.stringify({ q }),
            });

            const rawText = await resp.text();
            let data;
            try {
                data = JSON.parse(rawText);
            } catch (parseErr) {
                appendMessage("error", "응답 JSON 파싱 실패: " + parseErr + "\n" + rawText.slice(0, 240), false);
                return false;
            }

            if (data && data.ok !== false) {
                data._user_question = q;
                appendBotAnswer(data);
            } else {
                appendMessage("error", "에러: " + ((data && data.error) || "서버 오류"), false);
            }
        } catch (err) {
            appendMessage("error", "네트워크 오류: " + err, false);
        } finally {
            inputEl.disabled = false;
            sendBtn.disabled = false;
            sendBtn.textContent = oldBtnTxt;
            try { inputEl.focus(); } catch (_) { }
        }

        return false;
    }

    function autosizeTextarea(el) {
        if (!el) return;
        el.style.height = "auto";
        const h = Math.min(el.scrollHeight, 140);
        el.style.height = h + "px";
    }

    document.addEventListener("DOMContentLoaded", () => {
        const form = document.getElementById("chatForm");
        const input = document.getElementById("chatInput");

        if (form) form.addEventListener("submit", sendChat);

        if (input) {
            autosizeTextarea(input);
            input.addEventListener("input", () => autosizeTextarea(input));

            input.addEventListener("keydown", (e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    sendChat(e);
                }
            });
        }

        const welcomeHtml =
            '<div class="faq-card">' +
            '  <div class="faq-head">👋 환영합니다</div>' +
            '  <div class="faq-answer">' +
            "    사내 지식 + 최신 정보 기반으로 답합니다.<br/>" +
            '    예: “휴가 규정 요약”, “업무 규정 핵심”, “최근 AI 이슈 정리”<br/>' +
            "  </div>" +
            "</div>";

        appendMessage("bot", welcomeHtml, true);
    });

    // Evidence toggle (delegation)
    document.addEventListener("click", (e) => {
        const btn = e.target.closest(".evidence-toggle-btn");
        if (!btn) return;

        const wrap = btn.closest(".evidence-wrap");
        if (!wrap) return;

        const box = wrap.querySelector(".evidence-box");
        if (!box) return;

        const opened = box.style.display !== "none";
        box.style.display = opened ? "none" : "block";
        btn.textContent = opened ? "📎 근거 보기 ▾" : "📎 근거 숨기기 ▴";
    });
})();
