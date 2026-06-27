// ragapp/static/ragapp/javascript/pdf_thread_ext.js
// 메인 채팅(#chatThread)에서 PDF 업로드 + 요약/표 응답을 바로 출력
// - marked/DOMPurify 없이 동작
// - 안전상 HTML(answer_html)은 텍스트로만 출력
// - answer_text에 포함된 "마크다운 표"만 <table>로 안전 변환(셀 escape)

(function () {
    "use strict";
    if (window.__PDF_THREAD_EXT_INITED__) return;
    window.__PDF_THREAD_EXT_INITED__ = true;

    function esc(s) {
        return String(s || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
    }

    function dglog(tag, obj) {
        try { if (typeof window.dglog === "function") window.dglog(tag, obj); } catch (_) { }
    }

    function getCookie(name) {
        try {
            var cookieValue = null;
            if (document.cookie && document.cookie !== "") {
                var cookies = document.cookie.split(";");
                for (var i = 0; i < cookies.length; i++) {
                    var c = cookies[i].trim();
                    if (c.substring(0, name.length + 1) === name + "=") {
                        cookieValue = decodeURIComponent(c.substring(name.length + 1));
                        break;
                    }
                }
            }
            return cookieValue;
        } catch (_) { return null; }
    }

    // 마크다운 표만 아주 단순 변환(셀은 esc)
    function markdownTableToHTML(md) {
        var text = String(md || "").trim();
        if (!text) return "";

        var lines = text.split(/\r?\n/).map(function (l) { return l.trim(); }).filter(Boolean);
        if (lines.length < 2) return "";

        var headerIdx = -1;
        for (var i = 0; i < lines.length - 1; i++) {
            var hasPipe = lines[i].indexOf("|") !== -1;
            var sep = lines[i + 1].replace(/\s+/g, "");
            var looksSep =
                /^(\|?[-:]+)+\|?$/.test(sep) ||
                /^(\|?[-:]+\|)+[-:]+\|?$/.test(sep);
            if (hasPipe && looksSep) { headerIdx = i; break; }
        }
        if (headerIdx === -1) return "";

        function splitRow(line) {
            var s = line.trim();
            if (s.startsWith("|")) s = s.slice(1);
            if (s.endsWith("|")) s = s.slice(0, -1);
            return s.split("|").map(function (c) { return esc(c.trim()); });
        }

        var headers = splitRow(lines[headerIdx]);
        if (headers.length < 2) return "";

        var body = lines.slice(headerIdx + 2)
            .filter(function (l) { return l.indexOf("|") !== -1; })
            .map(splitRow);

        var colN = headers.length;

        var out = '<table><thead><tr>' +
            headers.map(function (h) { return "<th>" + h + "</th>"; }).join("") +
            "</tr></thead><tbody>";

        for (var r = 0; r < body.length; r++) {
            var row = body[r];
            while (row.length < colN) row.push("");
            row = row.slice(0, colN);
            out += "<tr>" + row.map(function (c) { return "<td>" + c + "</td>"; }).join("") + "</tr>";
        }
        out += "</tbody></table>";
        return out;
    }

    function inferPdfModeFromText(q, fallbackMode) {
        var s = String(q || "");
        if (/(표|테이블|table|행|열)/i.test(s)) return "table";
        if (/(요약|핵심|정리|요점|간단)/i.test(s)) return "summary";
        return fallbackMode || "summary";
    }

    function createMsg(role) {
        var wrap = document.createElement("div");
        wrap.className = "chat-msg chat-msg--" + role;
        var bubble = document.createElement("div");
        bubble.className = "chat-bubble";
        wrap.appendChild(bubble);
        return { wrap: wrap, bubble: bubble };
    }

    function appendMsg(thread, msg) {
        thread.appendChild(msg.wrap);
        try {
            requestAnimationFrame(function () { thread.scrollTop = thread.scrollHeight; });
        } catch (_) { }
    }

    function setBusy(b) {
        var sendBtn = document.getElementById("chatSendBtn");
        if (!sendBtn) return;
        try {
            if (b) {
                sendBtn.disabled = true;
                sendBtn.dataset.origText = sendBtn.innerText || "";
                sendBtn.innerText = "생각중…";
            } else {
                sendBtn.disabled = false;
                if (sendBtn.dataset.origText) {
                    sendBtn.innerText = sendBtn.dataset.origText;
                    delete sendBtn.dataset.origText;
                }
            }
        } catch (_) { }
    }

    function bumpUsage(kind) {
        try {
            if (window.QARAG_USAGE && typeof window.QARAG_USAGE.bump === "function") {
                window.QARAG_USAGE.bump(kind);
            }
        } catch (_) { }
    }

    // ── DOM 확보
    var wrap = document.querySelector(".page-wrap");
    var thread = document.getElementById("chatThread");
    var composer = document.getElementById("chatComposer");
    var input = document.getElementById("chatInput");
    var attachBtn = document.getElementById("chatAttachBtn");
    var fileHint = document.getElementById("chatFileHint");
    if (!wrap || !thread || !composer || !input) return;

    function isPdfMode() { return (wrap.dataset && wrap.dataset.mode === "pdf"); }
    function getPdfForm() { return document.getElementById("pdf-form"); }

    // hidden 제약 회피용 파일피커
    var picker = document.createElement("input");
    picker.type = "file";
    picker.accept = "application/pdf";
    picker.style.position = "fixed";
    picker.style.left = "-9999px";
    picker.style.width = "1px";
    picker.style.height = "1px";
    picker.style.opacity = "0";
    document.body.appendChild(picker);

    var selectedFile = null;
    var inFlight = false;

    function showFileHint() {
        if (!fileHint) return;
        if (selectedFile) {
            fileHint.hidden = false;
            fileHint.textContent = "첨부됨: " + selectedFile.name;
        } else {
            fileHint.hidden = true;
            fileHint.textContent = "";
        }
    }

    picker.addEventListener("change", function () {
        selectedFile = (picker.files && picker.files[0]) ? picker.files[0] : null;
        showFileHint();
    });

    if (attachBtn) {
        attachBtn.addEventListener("click", function (ev) {
            if (!isPdfMode()) return;
            ev.preventDefault();
            ev.stopImmediatePropagation();
            try { picker.click(); } catch (_) { }
        }, true);
    }

    composer.addEventListener("submit", function (ev) {
        if (!isPdfMode()) return;

        ev.preventDefault();
        ev.stopImmediatePropagation();
        if (inFlight) return;

        var q = String(input.value || "").trim();
        if (!q) q = "이 PDF를 핵심만 요약해줘.";

        if (!selectedFile) {
            var a0 = createMsg("assistant");
            a0.bubble.textContent = "PDF 모드에서는 먼저 📎 버튼으로 PDF 파일을 첨부해 주세요.";
            appendMsg(thread, a0);
            return;
        }

        var pdfForm = getPdfForm();
        if (!pdfForm) {
            var a1 = createMsg("assistant");
            a1.bubble.textContent = "내부 오류: #pdf-form 을 찾지 못했습니다.";
            appendMsg(thread, a1);
            return;
        }

        var apiUrl = pdfForm.getAttribute("data-api-url");
        if (!apiUrl) {
            var a2 = createMsg("assistant");
            a2.bubble.textContent = "내부 오류: PDF API URL(data-api-url)이 비어 있습니다.";
            appendMsg(thread, a2);
            return;
        }

        // fallback: 레거시 라디오값(있으면)
        var checked = pdfForm.querySelector("input[name='mode']:checked");
        var fallbackMode = checked ? (checked.value || "summary") : "summary";
        var mode = inferPdfModeFromText(q, fallbackMode);

        // user bubble
        var u = createMsg("user");
        u.bubble.innerHTML =
            '<div class="answer-meta-row">' +
            '<span class="ai-generated-badge">PDF: ' + esc(selectedFile.name) + "</span>" +
            '<span class="ai-generated-badge" style="margin-left:8px">mode: ' + esc(mode) + "</span>" +
            "</div>" +
            '<div style="white-space:pre-wrap">' + esc(q) + "</div>";
        appendMsg(thread, u);

        // assistant placeholder
        var a = createMsg("assistant");
        a.wrap.classList.add("is-thinking");
        a.bubble.textContent = "생각중… (PDF 분석 중)";
        appendMsg(thread, a);

        inFlight = true;
        setBusy(true);

        var fd = new FormData();
        try {
            var csrfEl = pdfForm.querySelector("input[name='csrfmiddlewaretoken']");
            if (csrfEl && csrfEl.value) fd.append("csrfmiddlewaretoken", csrfEl.value);
        } catch (_) { }

        fd.append("question", q);
        fd.append("mode", mode);
        fd.append("pdf", selectedFile, selectedFile.name);

        var headers = { "X-Requested-With": "XMLHttpRequest" };
        var csrftoken = getCookie("csrftoken");
        if (csrftoken) headers["X-CSRFToken"] = csrftoken;

        fetch(apiUrl, {
            method: "POST",
            body: fd,
            credentials: "same-origin",
            headers: headers
        })
            .then(function (resp) {
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
                dglog("PDF_THREAD_JSON", data);

                a.wrap.classList.remove("is-thinking");

                if (!data || !data.ok) {
                    a.bubble.textContent = (data && data.error) ? data.error : "알 수 없는 오류가 발생했습니다.";
                    return;
                }

                // 안전: answer_html은 텍스트로만
                if (data.answer_html) {
                    a.bubble.textContent = String(data.answer_text || data.answer_html || "");
                    bumpUsage("pdf");
                    return;
                }

                var t = String(data.answer_text || "");
                var tableHtml = markdownTableToHTML(t);

                if (tableHtml) {
                    a.bubble.innerHTML =
                        '<div class="answer-meta-row"><span class="ai-generated-badge">검색 결과 기반 답변</span></div>' +
                        '<div class="answer-body">' + tableHtml + "</div>";
                } else {
                    a.bubble.textContent = t;
                }

                bumpUsage("pdf");
            })
            .catch(function (err) {
                a.wrap.classList.remove("is-thinking");
                a.bubble.textContent = "요청 처리 중 오류: " + (err && err.message ? err.message : err);
            })
            .finally(function () {
                inFlight = false;
                setBusy(false);
                try { input.value = ""; } catch (_) { }
            });

    }, true);

})();
