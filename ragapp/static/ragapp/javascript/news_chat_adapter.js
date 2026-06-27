// ragapp/static/ragapp/javascript/news_chat_adapter.js
(function () {
    "use strict";

    if (window.__NEWS_CHAT_ADAPTER_INITED__) return;
    window.__NEWS_CHAT_ADAPTER_INITED__ = true;

    window.__NEWS_CHAT_ADAPTER__ = true;

    const $ = (sel, root = document) => root.querySelector(sel);
    const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

    const wrap = $(".page-wrap");
    const thread = $("#chatThread");
    const composer = $("#chatComposer");
    const input = $("#chatInput");
    const attachBtn = $("#chatAttachBtn");
    const fileHint = $("#chatFileHint");
    const logList = $("#chatLogList");
    const newBtn = $("#newQuestionBtn");

    if (!wrap || !thread || !composer || !input) return;

    const sections = {
        rag: $("#modeSectionRag"),
        web: $("#modeSectionWeb"),
        pdf: $("#modeSectionPdf"),
    };

    let stickToBottom = true;
    thread.addEventListener("scroll", () => {
        const gap = thread.scrollHeight - thread.scrollTop - thread.clientHeight;
        stickToBottom = gap < 120;
    });

    function scrollBottom() {
        if (!stickToBottom) return;
        thread.scrollTop = thread.scrollHeight + 9999;
    }

    function nowHM() {
        const d = new Date();
        const hh = String(d.getHours()).padStart(2, "0");
        const mm = String(d.getMinutes()).padStart(2, "0");
        return `${hh}:${mm}`;
    }

    function safeText(s) {
        return String(s || "").replace(/\s+/g, " ").trim();
    }

    function escapeHtml(s) {
        return String(s || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function normalizeOutboundUrlForLinks(u) {
        u = String(u || "").trim();
        if (!u) return "";

        // ✅ 핵심: "/https://..." 또는 "/http://..." 보정
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

    // AFTER
    function sanitizeLinks(root) {
        try {
            $$("a[href]", root).forEach((a) => {
                const hrefRaw = (a.getAttribute("href") || "").trim();

                // 1) 먼저 normalize ("/https://..." -> "https://...")
                const href = normalizeOutboundUrlForLinks(hrefRaw);

                // 2) normalize 결과가 비었으면 링크 제거(텍스트로 치환)
                if (!href) {
                    const span = document.createElement("span");
                    span.textContent = a.textContent || hrefRaw;
                    a.replaceWith(span);
                    return;
                }

                // 3) DOM에 실제 href도 교체(클릭 시 깨짐 방지)
                a.setAttribute("href", href);

                // 4) 내부/외부 판정(내부는 target 제거)
                const isLocal = href[0] === "/" || href[0] === "#";

                a.setAttribute("rel", "noopener noreferrer nofollow");
                a.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");

                if (isLocal) {
                    a.removeAttribute("target");
                    return;
                }

                // 5) 외부는 http/https만 허용 + 외부만 새 탭
                try {
                    const u = new URL(href);
                    const okProto = /^https?:$/i.test(u.protocol);
                    if (!okProto) {
                        const span = document.createElement("span");
                        span.textContent = a.textContent || hrefRaw;
                        a.replaceWith(span);
                        return;
                    }
                    if (u.origin !== location.origin) a.setAttribute("target", "_blank");
                    else a.removeAttribute("target");
                } catch (_) {
                    const span = document.createElement("span");
                    span.textContent = a.textContent || hrefRaw;
                    a.replaceWith(span);
                }
            });
        } catch (_) { }
    }


    function sanitizeHtmlSecondPass(html) {
        const raw = String(html || "");
        const rawHasDetails = /<details\b/i.test(raw) || /<summary\b/i.test(raw);

        let out = raw;
        let usedNewsUtils = false;

        try {
            if (window.NEWS_UTILS && typeof window.NEWS_UTILS.sanitizeHTML === "function") {
                out = window.NEWS_UTILS.sanitizeHTML(raw);
                usedNewsUtils = true;
            }
        } catch (_) {
            out = raw;
            usedNewsUtils = false;
        }

        if (usedNewsUtils && rawHasDetails) {
            const outHasDetails = /<details\b/i.test(out) || /<summary\b/i.test(out);
            if (!outHasDetails) {
                try {
                    if (window.DOMPurify && typeof window.DOMPurify.sanitize === "function") {
                        return window.DOMPurify.sanitize(raw, {
                            ADD_TAGS: ["details", "summary"],
                            ADD_ATTR: ["open"],
                        });
                    }
                } catch (_) { }
            }
            return out;
        }

        try {
            if (!usedNewsUtils && window.DOMPurify && typeof window.DOMPurify.sanitize === "function") {
                return window.DOMPurify.sanitize(raw, {
                    ADD_TAGS: ["details", "summary"],
                    ADD_ATTR: ["open"],
                });
            }
        } catch (_) { }

        return out;
    }

    const FEEDBACK_FROM_UI = "news_chat";

    function feedbackAvailable() {
        try {
            return !!(window.DGFeedbackWidget && typeof window.DGFeedbackWidget.mount === "function");
        } catch (_) {
            return false;
        }
    }

    function ensureFeedbackHost(msgEl) {
        if (!msgEl) return null;
        const bubble = $(".mz-bubble", msgEl) || msgEl;
        let host = $(".fx-fb-host", bubble);
        if (!host) {
            host = document.createElement("div");
            host.className = "fx-fb-host";
            bubble.appendChild(host);
        }
        return host;
    }

    function extractAnswerTextFromBubble(bubbleEl) {
        try {
            if (!bubbleEl) return "";
            const body = bubbleEl.querySelector('[data-bubble-body="1"]') || bubbleEl;
            const clone = body.cloneNode(true);

            $$('[data-msg-meta="1"]', clone).forEach((n) => n.remove());
            $$(".fx-fb-host, .fx-fb", clone).forEach((n) => n.remove());
            $$('[data-evidence="1"]', clone).forEach((n) => n.remove());

            return safeText(clone.textContent || "");
        } catch (_) {
            return "";
        }
    }

    function collectSources(mode) {
        try {
            const sec = sections[mode];
            if (!sec) return [];

            if (mode === "web") {
                try {
                    if (typeof window.collectWebSourcesFromDOM === "function") {
                        const s = window.collectWebSourcesFromDOM();
                        if (Array.isArray(s) && s.length) return s.slice(0, 30);
                    }
                } catch (_) { }
            }

            const out = [];
            const scopeCandidates = [
                ".sources-list",
                ".source-list",
                ".references",
                ".reference",
                ".citations",
                ".citation",
                ".sources",
            ];

            let scopeRoot = null;
            for (const sel of scopeCandidates) {
                const r = $(sel, sec);
                if (r) {
                    scopeRoot = r;
                    break;
                }
            }

            const root = scopeRoot || sec;

            $$('a[href]', root).forEach((a) => {
                const hrefRaw = (a.getAttribute("href") || "").trim();
                const url = normalizeOutboundUrlForLinks(hrefRaw);

                // ✅ 외부 출처만 수집(https?만)
                if (!/^https?:\/\//i.test(url)) return;

                const title = safeText(a.textContent || url).slice(0, 200);
                out.push({ title, url });
            });

            const seen = new Set();
            const uniq = [];
            for (const s of out) {
                const k = (s.url || "").toLowerCase();
                if (!k || seen.has(k)) continue;
                seen.add(k);
                uniq.push(s);
                if (uniq.length >= 30) break;
            }
            return uniq;
        } catch (_) {
            return [];
        }
    }

    function feedbackEligible(mode, answerText) {
        if (mode !== "web" && mode !== "rag") return false;
        const t = safeText(answerText);
        if (!t || t.length < 30) return false;
        if (/요청을 실행하지 못|폼\/버튼|PDF 파일을 먼저/i.test(t)) return false;
        if (/생각중|정리하는 중|처리 중|만드는 중|채워 넣는 중/i.test(t)) return false;
        return true;
    }

    function mountFeedbackIfPossible(mode, p) {
        try {
            if (!feedbackAvailable()) return false;
            if (!p || !p.msgEl || !p.bubbleEl) return false;

            const answerText = extractAnswerTextFromBubble(p.bubbleEl);
            if (!feedbackEligible(mode, answerText)) return false;

            const host = ensureFeedbackHost(p.msgEl);
            if (!host) return false;

            const payload = {
                answer_type: mode,
                from_ui: FEEDBACK_FROM_UI,
                question: p.question || "",
                answer: answerText,
                sources: collectSources(mode),
            };

            window.DGFeedbackWidget.mount(host, payload);
            return true;
        } catch (_) {
            return false;
        }
    }

    function combineAnswerAndEvidence(mode, answerHtml) {
        return String(answerHtml || "");
    }

    function makeMsg(role, html, meta) {
        const el = document.createElement("div");
        el.className = `mz-msg mz-msg--${role}`;
        el.dataset.role = role;

        const bubble = document.createElement("div");
        bubble.className = "mz-bubble";

        const body = document.createElement("div");
        body.setAttribute("data-bubble-body", "1");
        body.innerHTML = html || "";
        bubble.appendChild(body);

        if (meta) {
            const m = document.createElement("div");
            m.className = "muted";
            m.style.marginTop = "8px";
            m.textContent = meta;
            m.setAttribute("data-msg-meta", "1");
            bubble.appendChild(m);
        }

        el.appendChild(bubble);
        thread.appendChild(el);

        sanitizeLinks(el);
        scrollBottom();
        return { el, bubble, body };
    }

    function clearThread() {
        thread.innerHTML = "";
        if (logList) logList.innerHTML = "";
        input.value = "";
        input.focus();
        if (fileHint) {
            fileHint.hidden = true;
            fileHint.textContent = "";
        }
    }

    function addLogItem(text, targetEl) {
        if (!logList) return;
        const li = document.createElement("li");
        li.className = "mz-log-item";
        const t = safeText(text);
        const title = t.slice(0, 34) + (t.length > 34 ? "…" : "");
        li.innerHTML =
            `<div class="mz-log-title">${escapeHtml(title)}</div>` +
            `<div class="mz-log-meta">${nowHM()}</div>`;
        li.addEventListener("click", () => {
            try {
                targetEl.scrollIntoView({ behavior: "smooth", block: "start" });
            } catch (_) { }
        });
        logList.prepend(li);
    }

    function currentMode() {
        const m = (wrap.dataset.mode || "rag").trim();
        return m === "web" || m === "pdf" || m === "rag" ? m : "rag";
    }

    function findForm(mode) {
        const sec = sections[mode];
        if (!sec) return null;
        return $("form", sec);
    }

    function findQueryInput(mode) {
        const sec = sections[mode];
        if (!sec) return null;
        return (
            $('textarea[name*="query"]', sec) ||
            $('input[name*="query"]', sec) ||
            $("#query_web", sec) ||
            $("#query_rag", sec) ||
            $('input[type="text"]', sec) ||
            $("textarea", sec)
        );
    }

    function findFileInput() {
        const sec = sections.pdf;
        if (!sec) return null;
        return $('input[type="file"]', sec);
    }

    function setLegacyAction(mode, actionValue) {
        const form = findForm(mode);
        if (!form) return;

        let hidden = form.querySelector('input[name="action"]');
        if (!hidden) {
            hidden = document.createElement("input");
            hidden.type = "hidden";
            hidden.name = "action";
            form.appendChild(hidden);
        }
        hidden.value = actionValue;
    }

    function dispatchComposerSubmit() {
        try {
            if (composer && typeof composer.requestSubmit === "function") {
                composer.requestSubmit();
                return;
            }
        } catch (_) { }
        try {
            if (composer) composer.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
        } catch (_) { }
    }

    function clickSubmit(mode) {
        if (mode === "pdf") {
            const sec = sections.pdf;
            const runBtn = (sec && $("#pdf-run-btn", sec)) || $("#pdf-run-btn");
            if (runBtn && typeof runBtn.click === "function") {
                runBtn.click();
                return true;
            }
            return false;
        }

        const sec = sections[mode] || document;
        const form = findForm(mode);

        let btn = null;
        if (mode === "web") btn = sec.querySelector('button[data-action="web_search"]');
        if (mode === "rag") btn = sec.querySelector('button[data-action="rag_search"]');

        if (!btn && form) {
            btn =
                (mode === "web" && form.querySelector('button[data-action="web_search"]')) ||
                (mode === "rag" && form.querySelector('button[data-action="rag_search"]')) ||
                form.querySelector('button[type="submit"][data-action]') ||
                form.querySelector('button[type="submit"]') ||
                form.querySelector('input[type="submit"]');
        }

        try {
            if (btn && typeof btn.click === "function") {
                btn.click();
                return true;
            }
        } catch (_) { }

        try {
            if (!form) return false;
            if (typeof form.requestSubmit === "function") {
                form.requestSubmit();
                return true;
            }
            form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
            return true;
        } catch (_) {
            return false;
        }
    }

    function findAnswerBlock(mode) {
        const sec = sections[mode];
        if (!sec) return null;

        if (mode === "pdf") {
            return (
                $("#pdf-result-text", sec) ||
                $("#pdf-result-text") ||
                $("#pdf-result-box", sec) ||
                $("#pdf-result-box")
            );
        }

        const known =
            (mode === "web" && $("#web-answer-block")) ||
            (mode === "rag" && ($("#rag-answer-block") || $("#ragAnswerBlock")));

        if (known) return known;

        const byId = $$("[id]", sec).find((el) => /answer|result|output/i.test(el.id));
        if (byId) return byId;

        return (
            $(".answer-block", sec) ||
            $(".answer", sec) ||
            $(".result", sec) ||
            $(".output", sec) ||
            null
        );
    }

    const pending = { rag: null, web: null, pdf: null };
    let sendSeq = 0;

    function setPending(mode, msgEl, bubbleEl, questionText) {
        pending[mode] = {
            msgEl,
            bubbleEl,
            question: safeText(questionText || ""),
            seq: ++sendSeq,
            last: "",
            finalizeTimer: null,
        };
        if (mode === "rag") window.__DG_RAG_PENDING__ = pending[mode];
    }

    function splitAnswerAndEvidenceDOM(rawHtml) {
        try {
            const tmp = document.createElement("div");
            tmp.innerHTML = String(rawHtml || "");

            const evParts = [];
            tmp.querySelectorAll('.mz-evidence[data-evidence="1"]').forEach((n) => {
                evParts.push(n.outerHTML || "");
                n.remove();
            });

            return { ansHtml: tmp.innerHTML || "", evParts: evParts };
        } catch (_) {
            return { ansHtml: String(rawHtml || ""), evParts: [] };
        }
    }

    function updatePending(mode, combinedHtml) {
        const p = pending[mode];
        if (!p || !p.bubbleEl) return;

        const raw = String(combinedHtml || "");
        const parts = splitAnswerAndEvidenceDOM(raw);

        const cleanedAns = sanitizeHtmlSecondPass(parts.ansHtml || "");
        const evHtml = (parts.evParts && parts.evParts.length)
            ? parts.evParts.map((h) => sanitizeHtmlSecondPass(h)).join("")
            : "";

        const finalHtml = String(cleanedAns || "") + String(evHtml || "");
        if (finalHtml === p.last) return;
        p.last = finalHtml;

        const body = p.bubbleEl.querySelector('[data-bubble-body="1"]');
        const target = body || p.bubbleEl;

        target.innerHTML = finalHtml || "";
        sanitizeLinks(target);
        scrollBottom();
    }

    function finalizePending(mode) {
        const p = pending[mode];
        if (!p) return;

        try {
            if (p.finalizeTimer) clearTimeout(p.finalizeTimer);
        } catch (_) { }

        try {
            const ok = mountFeedbackIfPossible(mode, p);
            if (!ok) {
                setTimeout(() => mountFeedbackIfPossible(mode, p), 800);
                setTimeout(() => mountFeedbackIfPossible(mode, p), 1600);
            }
        } catch (_) { }

        if (mode === "rag" && window.__DG_RAG_PENDING__ === p) window.__DG_RAG_PENDING__ = null;
        pending[mode] = null;
    }

    function attachObserver(mode) {
        let block = findAnswerBlock(mode);
        if (!block) {
            let tries = 0;
            const t = setInterval(() => {
                tries++;
                block = findAnswerBlock(mode);
                if (block) {
                    clearInterval(t);
                    attachObserver(mode);
                }
                if (tries > 20) clearInterval(t);
            }, 250);
            return;
        }

        let scheduled = false;
        const schedule = () => {
            if (scheduled) return;
            scheduled = true;

            requestAnimationFrame(() => {
                scheduled = false;
                const p = pending[mode];
                if (!p) return;

                try {
                    const answerHtml = block.innerHTML || "";
                    const combined =
                        mode === "rag" &&
                            window.DG_RAG_EVIDENCE &&
                            typeof window.DG_RAG_EVIDENCE.combine === "function"
                            ? window.DG_RAG_EVIDENCE.combine({ mode, answerHtml, pending: p, fallback: combineAnswerAndEvidence })
                            : combineAnswerAndEvidence(mode, answerHtml);

                    updatePending(mode, combined);

                    const txt = safeText(block.textContent || "");
                    if (!txt || /생각중|정리하는 중|만드는 중|채워 넣는 중|처리 중/i.test(txt)) {
                        if (p.finalizeTimer) {
                            clearTimeout(p.finalizeTimer);
                            p.finalizeTimer = null;
                        }
                        return;
                    }

                    if (!p.finalizeTimer) {
                        p.finalizeTimer = setTimeout(() => finalizePending(mode), 250);
                    }
                } catch (_) { }
            });
        };

        const obs = new MutationObserver(schedule);
        try {
            obs.observe(block, { childList: true, subtree: true, characterData: true });
        } catch (_) { }
    }

    ["rag", "web", "pdf"].forEach(attachObserver);

    function syncFileUI() {
        const fi = findFileInput();
        if (!fi || !fileHint) return;

        function renderHint() {
            const f = fi.files && fi.files[0];
            if (!f) {
                fileHint.hidden = true;
                fileHint.textContent = "";
                return;
            }
            fileHint.hidden = false;
            fileHint.textContent = `선택된 파일: ${f.name}`;
        }

        fi.addEventListener("change", renderHint);
        renderHint();
    }

    if (attachBtn) {
        attachBtn.addEventListener("click", () => {
            const mode = currentMode();
            if (mode !== "pdf") {
                const pdfTab = $('.mode-tab[data-mode="pdf"]');
                if (pdfTab) pdfTab.click();
            }
            const fi = findFileInput();
            if (fi) fi.click();
            syncFileUI();
        });
    }
    syncFileUI();

    function autoresize() {
        try {
            input.style.height = "auto";
            input.style.height = Math.min(input.scrollHeight, 160) + "px";
        } catch (_) { }
    }
    input.addEventListener("input", autoresize);
    autoresize();

    input.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        if (e.shiftKey) return;
        e.preventDefault();
        dispatchComposerSubmit();
    });

    composer.addEventListener("submit", (e) => {
        e.preventDefault();

        const mode = currentMode();
        const q = safeText(input.value);
        if (!q) return;

        const user = makeMsg("user", `<div>${escapeHtml(q)}</div>`, nowHM());
        addLogItem(q, user.el);

        const assistant = makeMsg("assistant", `<div class="muted">생각중…</div>`, nowHM());
        setPending(mode, assistant.el, assistant.bubble, q);

        const qInput = findQueryInput(mode);
        if (qInput) qInput.value = q;

        if (mode === "pdf") {
            const fi = findFileInput();
            const hasFile = fi && fi.files && fi.files.length > 0;
            if (!hasFile) {
                updatePending(mode, `<div>PDF 파일을 먼저 선택해줘. (📎 버튼)</div>`);
                finalizePending(mode);
                input.focus();
                return;
            }
        }

        if (mode === "web") setLegacyAction("web", "web_search");
        if (mode === "rag") setLegacyAction("rag", "rag_search");

        const ok = clickSubmit(mode);
        if (!ok) {
            updatePending(mode, `<div>요청을 실행하지 못했어. (폼/버튼 id·data-action을 확인해줘)</div>`);
            finalizePending(mode);
            return;
        }

        input.value = "";
        autoresize();
        input.focus();
    });

    if (newBtn) {
        newBtn.addEventListener("click", (e) => {
            e.preventDefault();
            clearThread();
        });
    }

    try {
        input.focus();
    } catch (_) { }

    const _lastQByArea = { rag: "", web: "", pdf: "" };

    function _unwrapHandle(h) {
        if (!h) return null;
        if (h.nodeType === 1) return h;
        if (h.el && h.el.nodeType === 1) return h.el;
        return null;
    }

    function _renderTextToHtml(text, opts) {
        const s = String(text || "");
        try {
            if (window.NEWS_UTILS && typeof window.NEWS_UTILS.renderAnswerRich === "function") {
                const tmp = document.createElement("div");
                window.NEWS_UTILS.renderAnswerRich(tmp, s, [], { aiBadge: !!(opts && opts.aiBadge) });
                return tmp.innerHTML || "";
            }
        } catch (_) { }
        return `<div>${escapeHtml(s).replace(/\n/g, "<br/>")}</div>`;
    }

    function _msgRole(el) {
        try {
            return el && el.dataset && el.dataset.role ? el.dataset.role : "";
        } catch (_) {
            return "";
        }
    }

    function _msgText(el) {
        try {
            if (!el) return "";
            // ✅ meta(시간) 제외하고 버블 본문만 비교해야 중복 방지가 됨
            const body = el.querySelector('[data-bubble-body="1"]') || el;
            return safeText(body.textContent || "");
        } catch (_) {
            return "";
        }
    }

    function _findExistingPendingPair(area, q) {
        try {
            const kids = thread.children;
            if (!kids || kids.length < 2) return null;

            const last = kids[kids.length - 1];
            const prev = kids[kids.length - 2];

            if (_msgRole(prev) !== "user") return null;
            if (_msgRole(last) !== "assistant") return null;

            const qText = safeText(q || "");
            if (!qText) return null;

            // ✅ 여기서 _msgText(prev)가 이제 버블본문만 보므로 매칭 성공
            const prevText = _msgText(prev);
            if (prevText !== qText) return null;

            // ✅ pending 판정도 버블본문만 기반으로
            const lastBody = last.querySelector('[data-bubble-body="1"]') || last;
            const lastText = safeText(lastBody.textContent || "");
            const looksPending = /생각중|정리하는 중|처리 중|만드는 중|채워 넣는 중/i.test(lastText);
            if (!looksPending) return null;

            return { userEl: prev, assistantEl: last };
        } catch (_) {
            return null;
        }
    }

    function apiAppend(area, role, text, opts) {
        area = String(area || "").trim();
        role = role === "assistant" ? "assistant" : "user";

        const t = String(text || "");

        if (area === "rag" || area === "web" || area === "pdf") {
            if (role === "user") _lastQByArea[area] = safeText(t);
        }

        const isPending = !!(opts && opts.pending);
        const qForDedupe = role === "user" ? safeText(t) : _lastQByArea[area] || "";
        const pair =
            area === "rag" || area === "web" || area === "pdf"
                ? _findExistingPendingPair(area, qForDedupe)
                : null;

        if (role === "user") {
            if (pair) return pair.userEl;
        }

        if (role === "assistant" && isPending) {
            if (pair) {
                const bubble = $(".mz-bubble", pair.assistantEl) || pair.assistantEl;
                setPending(area, pair.assistantEl, bubble, qForDedupe);
                return pair.assistantEl;
            }
        }

        const html =
            role === "assistant" && isPending
                ? `<div class="muted">${escapeHtml(t)}</div>`
                : _renderTextToHtml(t, opts);

        const msg = makeMsg(role, html, nowHM());
        try {
            msg.el.dataset.area = area || "";
        } catch (_) { }

        if ((area === "rag" || area === "web" || area === "pdf") && role === "assistant" && isPending) {
            const q = _lastQByArea[area] || "";
            setPending(area, msg.el, msg.bubble, q);
        }

        return msg.el;
    }

    function apiUpdate(handle, text, opts) {
        const el = _unwrapHandle(handle);
        if (!el) return false;

        const bubble = $(".mz-bubble", el) || el;
        const body = bubble.querySelector('[data-bubble-body="1"]') || bubble;

        const html = _renderTextToHtml(text, opts);
        body.innerHTML = sanitizeHtmlSecondPass(html);

        sanitizeLinks(body);
        scrollBottom();
        return true;
    }

    function apiError(handle, text, opts) {
        const o = Object.assign({ aiBadge: false, error: true, pending: false }, opts || {});
        return apiUpdate(handle, "❌ " + String(text || ""), o);
    }

    window.NEWS_CHAT_ADAPTER = window.NEWS_CHAT_ADAPTER || {};
    window.NEWS_CHAT_ADAPTER.append = apiAppend;
    window.NEWS_CHAT_ADAPTER.update = apiUpdate;
    window.NEWS_CHAT_ADAPTER.error = apiError;
    window.NEWS_CHAT_ADAPTER.clear = clearThread;
})();
