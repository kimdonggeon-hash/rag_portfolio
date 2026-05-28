// ragapp/static/ragapp/javascript/pdf_tab.js
// PDF 탭: 업로드 + Vertex 호출 + (가능하면) 스레드형 UI로 누적 렌더링
// - #pdfChatMount / #pdfChatStream 존재 시: ChatGPT형 bubble 스레드
// - 없으면: 기존 #pdf-result-text 방식으로 폴백
// - MutationObserver 사용 안 함 (팬 도는 원인 제거)
// - 메시지 상한(MAX_THREAD_ITEMS)으로 DOM 크기 제한

console.log("[pdf_tab] script loaded from file");

(function () {
    "use strict";

    // 중복 초기화 방지
    if (window.__PDF_TAB_INITED__) return;
    window.__PDF_TAB_INITED__ = true;

    function $(id) { return document.getElementById(id); }

    function debug() {
        try {
            if (window.console && console.log) {
                var args = Array.prototype.slice.call(arguments);
                args.unshift("[pdf_tab]");
                console.log.apply(console, args);
            }
        } catch (_) { }
    }

    // ✅ 사용량 위젯 bump 헬퍼
    function bumpUsage(kind) {
        try {
            if (window.QARAG_USAGE && typeof window.QARAG_USAGE.bump === "function") {
                window.QARAG_USAGE.bump(kind);
            } else {
                debug("QARAG_USAGE.bump 사용 불가 (정의 안 됨)", kind);
            }
        } catch (e) {
            debug("QARAG_USAGE.bump 에러", e);
        }
    }

    // ✅ 쿠키(= csrftoken) 읽기
    function getCookie(name) {
        try {
            var cookieValue = null;
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
            return cookieValue;
        } catch (_) {
            return null;
        }
    }

    // ✅ 동의 게이트(있으면) 강제 적용
    function passConsentGate(formEl) {
        try {
            if (!formEl) return true;
            if (typeof window.ensureConsentGate === "function") {
                return window.ensureConsentGate(formEl) !== false;
            }
        } catch (_) { }
        return true;
    }

    // ✅ 텍스트 escape (user bubble 등)
    function escapeText(s) {
        return String(s || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
    }

    // ✅ (NEW) 자연어에서 모드 추론: "표/테이블" 계열이면 table, 아니면 summary
    function inferModeFromQuestion(q) {
        var t = String(q || "").toLowerCase();

        // 표/테이블 의도
        var wantsTable = /표|테이블|table|tabular|정리표|비교표|항목별|열\s*로|행\s*으로|매트릭스/.test(t);

        // 요약 의도(명시)
        var wantsSummary = /요약|핵심|간단히|짧게|summary|tl;dr|한\s*줄/.test(t);

        if (wantsTable && !wantsSummary) return "table";
        if (wantsSummary && !wantsTable) return "summary";

        // 애매하면 summary 기본
        return "summary";
    }

    // ✅ (NEW) UI 모드가 있으면 그걸 우선, 없으면 자연어 추론
    function getModeFromUiOrInfer(form, q) {
        try {
            var checked = form ? form.querySelector("input[name='mode']:checked") : null;
            var v = checked ? String(checked.value || "").trim() : "";
            if (v === "summary" || v === "table") return v;
        } catch (_) { }
        return inferModeFromQuestion(q);
    }

    function prettyModeLabel(mode) {
        return mode === "table" ? "표로 정리" : "일반 요약";
    }

    // ✅ AI HTML(표 포함)을 안전하게: table 태그는 허용, 위험 속성 제거
    function sanitizeHTMLAllowTable(unsafe) {
        try {
            var ALLOWED = new Set([
                "B", "I", "STRONG", "EM", "BR", "UL", "OL", "LI", "P", "CODE", "PRE", "A",
                "TABLE", "THEAD", "TBODY", "TFOOT", "TR", "TH", "TD", "CAPTION",
                "H1", "H2", "H3", "H4", "HR", "BLOCKQUOTE", "SPAN", "DIV"
            ]);

            var ALLOWED_ATTR = new Set(["href", "target", "rel", "colspan", "rowspan"]);

            // DOMPurify 우선
            if (window.DOMPurify) {
                unsafe = window.DOMPurify.sanitize(String(unsafe || ""), {
                    USE_PROFILES: { html: true }
                });
            }

            var T = document.createElement("template");
            T.innerHTML = String(unsafe || "");

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
                            // 허용되지 않은 태그는 unwrap
                            walk(child);
                            while (child.firstChild) node.insertBefore(child.firstChild, child);
                            child.remove();
                            child = next;
                            continue;
                        }

                        // 속성 정리
                        if (tag === "A") {
                            Array.from(child.attributes).forEach(function (a) {
                                if (!ALLOWED_ATTR.has(a.name.toLowerCase())) child.removeAttribute(a.name);
                            });

                            var href = (child.getAttribute("href") || "").trim();
                            if (!/^https?:\/\//i.test(href)) child.removeAttribute("href");

                            child.setAttribute("rel", "noopener noreferrer nofollow");
                            child.setAttribute("target", "_blank");
                            child.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
                        } else {
                            Array.from(child.attributes).forEach(function (a) {
                                var nm = a.name.toLowerCase();
                                if (!ALLOWED_ATTR.has(nm)) child.removeAttribute(a.name);
                            });
                        }

                        walk(child);
                    }

                    child = next;
                }
            };

            walk(T.content);
            return T.innerHTML || "";
        } catch (e) {
            return escapeText(unsafe || "");
        }
    }

    // ✅ marked가 있으면 markdown -> html(표 포함) 변환 후 sanitize
    function renderMarkdownIfPossible(text) {
        try {
            if (window.marked && typeof window.marked.parse === "function") {
                var html = window.marked.parse(String(text || ""));
                return { html: sanitizeHTMLAllowTable(html), isHtml: true };
            }
        } catch (_) { }
        return { html: "", isHtml: false };
    }

    // ─────────────────────────────────────────────
    // 스레드 UI helpers
    // ─────────────────────────────────────────────
    var MAX_THREAD_ITEMS = 24; // DOM 크기 제한 (팬/버벅임 방지)

    function getChatEls() {
        return {
            mount: $("pdfChatMount"),
            stream: $("pdfChatStream")
        };
    }

    // ✅ (NEW) mount만 있고 stream이 없으면 자동 생성(리뉴얼로 id 하나 빠졌을 때도 동작)
    function ensureThreadVisible() {
        var els = getChatEls();
        if (els.mount) {
            els.mount.hidden = false;

            if (!els.stream) {
                try {
                    var s = document.createElement("div");
                    s.id = "pdfChatStream";
                    s.className = "pdf-chat-stream"; // CSS 없으면 무해
                    els.mount.appendChild(s);
                    els.stream = s;
                    debug("pdfChatStream이 없어 자동 생성했습니다.");
                } catch (e) {
                    debug("pdfChatStream 자동 생성 실패", e);
                }
            }

            if (els.stream) return els;
        }
        return null;
    }

    function scheduleScrollToBottom(stream) {
        try {
            if (!stream) return;
            requestAnimationFrame(function () {
                try { stream.scrollTop = stream.scrollHeight; } catch (_) { }
            });
        } catch (_) { }
    }

    function trimThread(stream) {
        try {
            if (!stream) return;
            while (stream.children && stream.children.length > MAX_THREAD_ITEMS) {
                stream.removeChild(stream.firstElementChild);
            }
        } catch (_) { }
    }

    function createChatMsg(role) {
        var wrap = document.createElement("div");
        wrap.className = "chat-msg chat-msg--" + role;

        var bubble = document.createElement("div");
        bubble.className = "chat-bubble";

        wrap.appendChild(bubble);
        return { wrap: wrap, bubble: bubble };
    }

    function pushUserMessage(stream, text, metaText) {
        var m = createChatMsg("user");
        var safe = escapeText(text || "");
        var meta = metaText ? '<div class="answer-meta-row"><span class="ai-generated-badge">' + escapeText(metaText) + "</span></div>" : "";

        // 사용자 bubble: meta + 본문(줄바꿈 유지)
        m.bubble.innerHTML = meta + '<div class="chat-user-text" style="white-space:pre-wrap">' + safe + "</div>";

        stream.appendChild(m.wrap);
        trimThread(stream);
        scheduleScrollToBottom(stream);
        return m;
    }

    function pushAssistantPlaceholder(stream, text) {
        var m = createChatMsg("assistant");
        m.bubble.textContent = text || "처리 중…";
        stream.appendChild(m.wrap);
        trimThread(stream);
        scheduleScrollToBottom(stream);
        return m;
    }

    function setAssistantMessage(m, payload, isHtml) {
        try {
            if (!m || !m.bubble) return;

            if (isHtml) {
                m.bubble.innerHTML = payload || "";
            } else {
                // 줄바꿈 보존을 위해 textContent 대신 pre-wrap 컨테이너로
                var safe = escapeText(payload || "");
                m.bubble.innerHTML = '<div style="white-space:pre-wrap">' + safe + "</div>";
            }
        } catch (_) { }
    }

    // ─────────────────────────────────────────────
    // 기존 결과 박스 폴백(스레드 mount가 없을 때만)
    // ─────────────────────────────────────────────
    function setLegacyResult(text, isHtml) {
        var resultBox = $("pdf-result-box");
        var resultText = $("pdf-result-text");
        var details = $("pdfResultDetails");
        var empty = $("pdfResultEmpty");

        // ✅ (NEW) resultText가 없으면 resultBox 아래에 자동 생성(리뉴얼로 id 빠졌을 때도 동작)
        if (!resultText && resultBox) {
            try {
                resultText = document.createElement("div");
                resultText.id = "pdf-result-text";
                resultBox.appendChild(resultText);
                debug("pdf-result-text가 없어 자동 생성했습니다.");
            } catch (e) {
                debug("pdf-result-text 자동 생성 실패", e);
            }
        }

        var msg = text || "";
        if (!resultText) {
            debug("레거시 결과 영역(#pdf-result-text)을 찾지 못했습니다. 스레드/레거시 DOM id를 확인하세요.");
            return;
        }

        if (isHtml) resultText.innerHTML = sanitizeHTMLAllowTable(msg);
        else resultText.textContent = msg;

        // 기존 UI 토글/빈 상태 처리(감시 없이 즉시)
        var has = (resultText.textContent || "").trim().length > 0;
        try {
            if (has) {
                if (empty) empty.hidden = true;
                if (details) { details.hidden = false; details.open = true; }
                if (resultBox) resultBox.classList.add("has-result");
            } else {
                if (details) details.hidden = true;
                if (empty) empty.hidden = false;
            }
        } catch (_) { }
    }

    // ─────────────────────────────────────────────
    // 메인 핸들러
    // ─────────────────────────────────────────────
    function attachPdfHandler() {
        var form = $("pdf-form");
        if (!form) { debug("폼(#pdf-form)을 찾지 못했습니다."); return; }
        if (form._pdfHandlerAttached) { debug("이미 핸들러가 붙어 있습니다."); return; }
        form._pdfHandlerAttached = true;

        var runBtn = $("pdf-run-btn");
        if (!runBtn) { debug("실행 버튼(#pdf-run-btn)을 찾지 못했습니다."); return; }

        var fileInput = form.querySelector("input[name='pdf']");
        var questionInput = form.querySelector("input[name='question']");
        var inFlight = false;

        // ✅ 버튼 로딩/복구
        function setBtnLoading(isLoading) {
            try {
                if (!runBtn) return;
                if (isLoading) {
                    runBtn.disabled = true;
                    runBtn.dataset.origText = runBtn.innerText || "";
                    runBtn.innerText = "⏳ 처리 중...";
                } else {
                    runBtn.disabled = false;
                    if (runBtn.dataset.origText) {
                        runBtn.innerText = runBtn.dataset.origText;
                        delete runBtn.dataset.origText;
                    }
                }
            } catch (_) { }
        }

        function runRequest() {
            debug("PDF 실행 로직 시작");

            if (inFlight) {
                debug("이미 요청 처리 중 → 무시");
                return;
            }

            // ✅ 동의 게이트(우회 방지)
            if (!passConsentGate(form)) {
                debug("동의 게이트에서 중단됨");
                return;
            }

            var apiUrl = form.getAttribute("data-api-url");
            if (!apiUrl) {
                var els0 = ensureThreadVisible();
                if (els0 && els0.stream) {
                    var a0 = pushAssistantPlaceholder(els0.stream, "내부 설정 오류: data-api-url이 비어 있습니다.");
                    setAssistantMessage(a0, "내부 설정 오류: data-api-url이 비어 있습니다.", false);
                } else {
                    setLegacyResult("내부 설정 오류: data-api-url이 비어 있습니다.", false);
                }
                return;
            }

            if (!fileInput || !fileInput.files || !fileInput.files.length) {
                var els1 = ensureThreadVisible();
                if (els1 && els1.stream) {
                    var a1 = pushAssistantPlaceholder(els1.stream, "PDF 파일을 먼저 선택해 주세요.");
                    setAssistantMessage(a1, "PDF 파일을 먼저 선택해 주세요.", false);
                } else {
                    setLegacyResult("PDF 파일을 먼저 선택해 주세요.", false);
                }
                return;
            }

            var q = (questionInput && questionInput.value ? String(questionInput.value) : "").trim();
            if (!q) q = "이 PDF를 핵심만 요약해줘.";

            // ✅ (CHANGED) 모드: UI가 있으면 우선, 없으면 자연어에서 추론
            var mode = getModeFromUiOrInfer(form, q);

            var fileName = "";
            try { fileName = fileInput.files[0] && fileInput.files[0].name ? fileInput.files[0].name : ""; } catch (_) { }
            var metaLine = (fileName ? ("파일: " + fileName + " · ") : "") + ("모드: " + prettyModeLabel(mode) + " (자동)");

            var formData = new FormData(form);

            // ✅ (CHANGED) 폼에 mode 라디오가 없어도 항상 mode를 보내도록 강제
            try { formData.set("mode", mode); } catch (_) { }
            // ✅ (CHANGED) trim/기본 프롬프트 적용한 q를 항상 반영
            try { formData.set("question", q); } catch (_) { }

            // ✅ 스레드 UI가 있으면 스레드로, 없으면 기존 결과 박스로
            var chat = ensureThreadVisible();

            var assistantMsg;

            // 스레드가 있으면: 기존 result box는 숨겨서 DOM 부담 줄임(있어도 동작엔 영향 없음)
            if (chat && chat.stream) {
                try {
                    var legacyBox = $("pdf-result-box");
                    if (legacyBox) legacyBox.hidden = true;
                } catch (_) { }

                pushUserMessage(chat.stream, q, metaLine);
                assistantMsg = pushAssistantPlaceholder(chat.stream, "PDF 분석 중입니다… (Vertex 모델 호출 중)");
            } else {
                // legacy
                setLegacyResult("PDF 분석 중입니다… (Vertex 모델 호출 중)", false);
            }

            setBtnLoading(true);
            inFlight = true;

            // ✅ CSRF 보강
            var headers = { "X-Requested-With": "XMLHttpRequest" };
            var csrftoken = getCookie("csrftoken");
            if (csrftoken) headers["X-CSRFToken"] = csrftoken;

            fetch(apiUrl, {
                method: "POST",
                body: formData,
                credentials: "same-origin",
                headers: headers
            })
                .then(function (resp) {
                    debug("응답 상태코드:", resp.status);

                    var ct = (resp.headers.get("content-type") || "").toLowerCase();
                    var parse = ct.indexOf("application/json") !== -1
                        ? resp.json()
                        : resp.text().then(function (t) {
                            try { return JSON.parse(t); }
                            catch (_) { return { ok: false, error: t || ("HTTP " + resp.status) }; }
                        });

                    return parse.then(function (data) {
                        if (!resp.ok) {
                            var emsg = (data && (data.error || data.message || data.detail)) || ("서버 오류: " + resp.status);
                            throw new Error(emsg);
                        }
                        return data;
                    });
                })
                .then(function (data) {
                    debug("응답 JSON:", data);

                    if (!data || !data.ok) {
                        var msg0 = (data && data.error) ? data.error : "알 수 없는 오류가 발생했습니다.";
                        if (chat && chat.stream && assistantMsg) {
                            setAssistantMessage(assistantMsg, msg0, false);
                        } else {
                            setLegacyResult(msg0, false);
                        }
                        return;
                    }

                    // ✅ 서버가 answer_html 주면 최우선
                    var outIsHtml = false;
                    var out = "";

                    if (data.answer_html) {
                        outIsHtml = true;
                        out = sanitizeHTMLAllowTable(data.answer_html);
                    } else {
                        var t = data.answer_text || "";
                        // marked가 있으면 markdown -> html로 변환(표 지원)
                        var md = renderMarkdownIfPossible(t);
                        if (md.isHtml) {
                            outIsHtml = true;
                            out = md.html;
                        } else {
                            outIsHtml = false;
                            out = String(t || "");
                        }
                    }

                    // 결과 반영
                    if (chat && chat.stream && assistantMsg) {
                        if (outIsHtml) {
                            var html =
                                '<div class="answer-meta-row"><span class="ai-generated-badge">AI 생성</span></div>' +
                                '<div class="answer-body">' + out + "</div>";
                            setAssistantMessage(assistantMsg, html, true);
                        } else {
                            setAssistantMessage(assistantMsg, out, false);
                        }
                        scheduleScrollToBottom(chat.stream);
                    } else {
                        setLegacyResult(out, outIsHtml);
                    }

                    // ✅ 성공 시 1회 사용 처리
                    bumpUsage("pdf");
                })
                .catch(function (err) {
                    debug("fetch 에러:", err);
                    var em = "요청 처리 중 오류가 발생했습니다: " + (err && err.message ? err.message : err);

                    if (chat && chat.stream && assistantMsg) {
                        setAssistantMessage(assistantMsg, em, false);
                        scheduleScrollToBottom(chat.stream);
                    } else {
                        setLegacyResult(em, false);
                    }
                })
                .finally(function () {
                    inFlight = false;
                    setBtnLoading(false);
                });
        }

        debug("PDF 실행 버튼 클릭 핸들러 연결 완료");

        runBtn.addEventListener("click", function (ev) {
            try { ev.preventDefault(); } catch (_) { }
            debug("PDF 실행 버튼 클릭");
            runRequest();
        });

        // Enter로도 실행(질문 입력창에서만)
        if (questionInput) {
            questionInput.addEventListener("keydown", function (ev) {
                try {
                    if (ev.key === "Enter") {
                        ev.preventDefault();
                        runRequest();
                    }
                } catch (_) { }
            });
        }
    }

    document.addEventListener("DOMContentLoaded", function () {
        debug("DOMContentLoaded 발생");
        attachPdfHandler();
    });

    if (document.readyState === "interactive" || document.readyState === "complete") {
        debug("readyState=" + document.readyState + " → 즉시 attachPdfHandler 호출");
        attachPdfHandler();
    }
})();
