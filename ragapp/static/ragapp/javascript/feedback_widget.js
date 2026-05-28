/* ragapp/static/ragapp/javascript/feedback_widget.js*/

(function () {
    "use strict";

    if (typeof window !== "undefined") {
        if (window.__DG_FEEDBACK_INITED__) return;
        window.__DG_FEEDBACK_INITED__ = true;
    }

    const DEFAULT_ENDPOINT = "/api/feedback";
    const MAX_QUESTION_LEN = 2000;
    const MAX_ANSWER_LEN = 12000;
    const MAX_SOURCES = 30;
    const MAX_SOURCE_TITLE = 200;
    const MAX_SOURCE_URL = 1000;
    const REQUEST_TIMEOUT_MS = 12000;

    function getCookie(name) {
        const v = document.cookie ? document.cookie.split("; ") : [];
        for (const s of v) {
            const [k, ...rest] = s.split("=");
            if (k === name) return decodeURIComponent(rest.join("="));
        }
        return null;
    }

    function safeJsonParse(text, fallback) {
        try {
            return JSON.parse(text);
        } catch (_) {
            return fallback;
        }
    }

    function normalizeText(s, maxLen) {
        const t = (s == null ? "" : String(s)).trim();
        if (!maxLen) return t;
        return t.length > maxLen ? t.slice(0, maxLen) : t;
    }

    function normalizeSources(src) {
        if (!Array.isArray(src)) return [];
        const out = [];
        for (const item of src.slice(0, MAX_SOURCES)) {
            if (item == null) continue;

            // 1) {title,url} 형태
            if (typeof item === "object") {
                const title = normalizeText(item.title || item.name || "", MAX_SOURCE_TITLE);
                const url = normalizeText(item.url || item.href || "", MAX_SOURCE_URL);
                if (!title && !url) continue;
                out.push({ title, url });
                continue;
            }

            // 2) 문자열(url) 형태
            if (typeof item === "string") {
                const url = normalizeText(item, MAX_SOURCE_URL);
                if (!url) continue;
                out.push({ title: "", url });
            }
        }
        return out;
    }

    // ✅ endpoint()는 1개만 유지: 템플릿 주입 우선 + same-origin 강제 + path/query만 반환
    function endpoint() {
        let ep = DEFAULT_ENDPOINT;
        try {
            ep =
                (window.DG_ENDPOINTS && window.DG_ENDPOINTS.feedback) ||
                window.DG_FEEDBACK_ENDPOINT ||
                DEFAULT_ENDPOINT;
        } catch (_) {
            ep = DEFAULT_ENDPOINT;
        }

        ep = String(ep || "").trim();
        if (!ep) ep = DEFAULT_ENDPOINT;

        try {
            const u = new URL(ep, location.origin);
            if (u.origin !== location.origin) return DEFAULT_ENDPOINT;
            return u.pathname + u.search;
        } catch (_) {
            return DEFAULT_ENDPOINT;
        }
    }

    async function postFeedback(payload) {
        const csrftoken = getCookie("csrftoken");
        const url = endpoint();

        const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
        const timer = ctrl ? setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS) : null;

        try {
            const res = await fetch(url, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    ...(csrftoken ? { "X-CSRFToken": csrftoken } : {}),
                },
                credentials: "same-origin",
                body: JSON.stringify(payload),
                ...(ctrl ? { signal: ctrl.signal } : {}),
            });

            let data = null;
            try {
                data = await res.json();
            } catch (_) {
                data = null;
            }

            return { ok: !!(res.ok && data && data.ok), status: res.status, data };
        } catch (e) {
            return { ok: false, status: 0, data: { ok: false, error: "network_error", detail: String(e || "") } };
        } finally {
            if (timer) clearTimeout(timer);
        }
    }

    function el(tag, attrs, children) {
        const n = document.createElement(tag);
        if (attrs) {
            for (const [k, v] of Object.entries(attrs)) {
                if (k === "class") n.className = String(v);
                else if (k === "html") n.innerHTML = String(v);
                else if (k.startsWith("data-")) n.setAttribute(k, String(v));
                else if (k === "disabled") n.disabled = !!v;
                else if (k === "hidden") {
                    if (v) n.setAttribute("hidden", "true");
                } else n.setAttribute(k, String(v));
            }
        }
        if (children) {
            for (const c of children) {
                if (c == null) continue;
                if (typeof c === "string") n.appendChild(document.createTextNode(c));
                else n.appendChild(c);
            }
        }
        return n;
    }

    function mount(container, payload) {
        if (!container || container.__dgMounted) return;
        container.__dgMounted = true;

        const state = {
            sent: false,
            sending: false,
            reasons: new Set(),
        };

        const answerType = normalizeText(payload.answer_type || "web", 20).toLowerCase(); // web|rag|qa
        const fromUi = normalizeText(payload.from_ui || "news", 50);
        const question = normalizeText(payload.question || "", MAX_QUESTION_LEN);
        const answer = normalizeText(payload.answer || "", MAX_ANSWER_LEN);
        const sources = normalizeSources(payload.sources);
        const logId = payload.log_id || null;

        const title = el("div", { class: "fx-fb__title" }, [
            "이 답변이 도움이 되었나요?",
            el("span", { class: "fx-fb__badge" }, [String(answerType || "WEB").toUpperCase()]),
        ]);

        const status = el("div", { class: "fx-fb__status", "aria-live": "polite" }, [""]);

        const btnUp = el("button", { type: "button", class: "fx-fb__btn fx-fb__btn--up" }, [
            el("span", { class: "fx-fb__icon", "aria-hidden": "true" }, ["👍"]),
            "도움돼요",
        ]);

        const btnDown = el("button", { type: "button", class: "fx-fb__btn fx-fb__btn--down" }, [
            el("span", { class: "fx-fb__icon", "aria-hidden": "true" }, ["👎"]),
            "별로예요",
        ]);

        const btnRow = el("div", { class: "fx-fb__row" }, [btnUp, btnDown, status]);

        const chips = [
            { key: "grounding", label: "근거 부족" },
            { key: "incorrect", label: "내용이 틀림" },
            { key: "stale", label: "최신 아님" },
            { key: "unclear", label: "설명이 애매함" },
            { key: "format", label: "표현/형식" },
            { key: "other", label: "기타" },
        ];

        const chipWrap = el("div", { class: "fx-fb__chips" }, []);
        chips.forEach((c) => {
            const b = el(
                "button",
                { type: "button", class: "fx-fb__chip", "data-key": c.key, "aria-pressed": "false" },
                [c.label]
            );
            b.addEventListener("click", () => {
                if (state.sent || state.sending) return;
                const key = b.getAttribute("data-key") || "";
                if (!key) return;

                const on = state.reasons.has(key);
                if (on) {
                    state.reasons.delete(key);
                    b.classList.remove("is-on");
                    b.setAttribute("aria-pressed", "false");
                } else {
                    state.reasons.add(key);
                    b.classList.add("is-on");
                    b.setAttribute("aria-pressed", "true");
                }
            });
            chipWrap.appendChild(b);
        });

        const comment = el("textarea", {
            class: "fx-fb__comment",
            name: "comment", // ✅ 추가: DevTools 경고( id 또는 name 없음 ) 해결
            placeholder: "원하시면 자세히 적어주세요. (개인정보 입력 금지)",
            maxlength: "800",
            rows: "3",
        });

        const hint = el("div", { class: "fx-fb__hint" }, [
            "개인정보(전화번호/주소/주민번호 등)는 입력하지 마세요.",
        ]);

        const btnSend = el("button", { type: "button", class: "fx-fb__send" }, ["피드백 보내기"]);
        const btnSkip = el("button", { type: "button", class: "fx-fb__skip" }, ["사유 없이 전송"]);

        const panel = el("div", { class: "fx-fb__panel", hidden: true }, [
            el("div", { class: "fx-fb__panelTitle" }, ["어떤 점이 아쉬웠나요?"]),
            chipWrap,
            comment,
            hint,
            el("div", { class: "fx-fb__actions" }, [btnSend, btnSkip]),
        ]);

        function lockUI(locked) {
            btnUp.disabled = locked;
            btnDown.disabled = locked;
            btnSend.disabled = locked;
            btnSkip.disabled = locked;
        }

        async function send(helpful, stage) {
            if (state.sent || state.sending) return;
            state.sending = true;
            lockUI(true);
            status.textContent = "전송 중…";

            const payloadToSend = {
                answer_type: answerType,
                from_ui: fromUi,
                stage: stage || "thumb",
                helpful: helpful,
                reasons: helpful === false ? Array.from(state.reasons) : [],
                comment: helpful === false ? normalizeText(comment.value || "", 800) : "",
                question,
                answer,
                sources,
                log_id: logId,
            };

            const r = await postFeedback(payloadToSend);

            state.sending = false;

            if (r.ok) {
                state.sent = true;
                status.textContent = "감사합니다. 피드백이 저장되었습니다.";
                container.classList.add("is-sent");
                panel.hidden = true;
            } else {
                status.textContent = "전송 실패 (잠시 후 다시 시도)";
                lockUI(false);
            }
        }

        btnUp.addEventListener("click", () => {
            if (state.sent || state.sending) return;
            send(true, "thumb");
        });

        btnDown.addEventListener("click", () => {
            if (state.sent || state.sending) return;
            panel.hidden = false;
            status.textContent = "사유를 선택하고 전송해 주세요.";
            try { comment.focus(); } catch (_) { }
        });

        btnSend.addEventListener("click", () => {
            if (state.sent || state.sending) return;
            send(false, "detail");
        });

        btnSkip.addEventListener("click", () => {
            if (state.sent || state.sending) return;
            state.reasons.clear();
            comment.value = "";
            send(false, "thumb");
        });

        const root = el("div", { class: "fx-fb" }, [title, btnRow, panel]);
        container.innerHTML = "";
        container.appendChild(root);
    }

    // (옵션) data-payload-id 방식도 유지
    function autoMount() {
        const nodes = document.querySelectorAll(".js-feedback-widget[data-payload-id]");
        nodes.forEach((node) => {
            const id = node.getAttribute("data-payload-id");
            if (!id) return;
            const script = document.getElementById(id);
            if (!script) return;

            const payload = safeJsonParse(script.textContent || "{}", {});
            mount(node, payload);
        });
    }

    window.DGFeedbackWidget = { mount, autoMount };

    document.addEventListener("DOMContentLoaded", autoMount);
})();
