(function () {
    "use strict";

    function getCookie(name) {
        const parts = document.cookie.split(";").map(s => s.trim());
        for (const it of parts) {
            if (it.startsWith(name + "=")) return decodeURIComponent(it.slice(name.length + 1));
        }
        return "";
    }
    function $(sel, root) { return (root || document).querySelector(sel); }
    function $all(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

    function toast(msg, kind) {
        const el = document.getElementById("dgToast");
        if (!el) return;
        el.hidden = false;
        el.textContent = msg;
        el.classList.remove("ok", "err");
        el.classList.add(kind || "ok");
        clearTimeout(el._t);
        el._t = setTimeout(() => { el.hidden = true; }, 2200);
    }

    function normalizePreviewUrl(url) {
        const u = (url || "").trim();
        if (!u) return "";
        // /uploads/... 는 CSP가 외부(서명 URL)로 막힐 수 있어서 raw=1을 붙여(선택 적용 시)
        if (u.startsWith("/uploads/")) {
            return u + (u.includes("?") ? "&" : "?") + "raw=1";
        }
        return u;
    }

    async function copyText(v) {
        const text = String(v || "");
        try {
            await navigator.clipboard.writeText(text);
            toast("복사됨", "ok");
        } catch (e) {
            // fallback
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.left = "-9999px";
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand("copy"); toast("복사됨", "ok"); } catch (_) { toast("복사 실패", "err"); }
            ta.remove();
        }
    }

    // --- 로컬 파일 미리보기 ---
    const filesInput = document.getElementById("dgFiles");
    const localBox = document.getElementById("dgLocalPreviews");
    const localGrid = document.getElementById("dgLocalGrid");
    const clearBtn = document.getElementById("dgClearBtn");

    function clearLocalPreviews() {
        if (!localGrid) return;
        $all("img", localGrid).forEach(img => {
            try { URL.revokeObjectURL(img.src); } catch (_) { }
        });
        localGrid.innerHTML = "";
        if (localBox) localBox.hidden = true;
    }

    if (filesInput && localGrid) {
        filesInput.addEventListener("change", () => {
            clearLocalPreviews();
            const files = Array.from(filesInput.files || []);
            if (!files.length) return;

            if (localBox) localBox.hidden = false;
            for (const f of files.slice(0, 30)) {
                const url = URL.createObjectURL(f);
                const wrap = document.createElement("div");
                wrap.className = "dg-prev";
                const img = document.createElement("img");
                img.loading = "lazy";
                img.alt = f.name || "file";
                img.src = url;
                wrap.appendChild(img);
                localGrid.appendChild(wrap);
            }
        });
    }

    if (clearBtn && filesInput) {
        clearBtn.addEventListener("click", () => {
            filesInput.value = "";
            clearLocalPreviews();
            toast("초기화", "ok");
        });
    }

    // --- 결과 필터 ---
    const onlyFail = document.getElementById("dgOnlyFail");
    const onlyOk = document.getElementById("dgOnlyOk");
    const resultsList = document.getElementById("dgResultsList");
    const resultsEmpty = document.getElementById("dgResultsEmpty");

    function applyFilters() {
        if (!resultsList) return;
        const showFailOnly = !!onlyFail?.checked;
        const showOkOnly = !!onlyOk?.checked;

        const items = $all(".dg-item", resultsList);
        let visible = 0;

        for (const it of items) {
            const isFail = it.classList.contains("is-fail");
            let hide = false;

            if (showFailOnly && !isFail) hide = true;
            if (showOkOnly && isFail) hide = true;
            if (showFailOnly && showOkOnly) hide = false; // 둘 다 체크면 전체

            it.hidden = hide;
            if (!hide) visible += 1;
        }

        if (resultsEmpty) resultsEmpty.hidden = (visible > 0);
    }

    onlyFail?.addEventListener("change", applyFilters);
    onlyOk?.addEventListener("change", applyFilters);

    // --- 라이트박스 ---
    const lb = document.getElementById("dgLightbox");
    const lbImg = document.getElementById("dgLbImg");
    const lbMeta = document.getElementById("dgLbMeta");

    function openLightbox(url, metaText) {
        if (!lb || !lbImg) return;
        lb.hidden = false;
        lbImg.src = normalizePreviewUrl(url);
        if (lbMeta) lbMeta.textContent = metaText || "";
    }
    function closeLightbox() {
        if (!lb || !lbImg) return;
        lb.hidden = true;
        lbImg.removeAttribute("src");
        if (lbMeta) lbMeta.textContent = "";
    }

    document.addEventListener("click", (e) => {
        const close = e.target && e.target.closest && e.target.closest("[data-lb-close='1']");
        if (close) return void closeLightbox();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeLightbox();
    });

    // --- 기존 서버 렌더 결과에서: 썸네일 클릭 = 라이트박스, 복사 버튼 ---
    if (resultsList) {
        resultsList.addEventListener("click", (e) => {
            const btn = e.target.closest && e.target.closest("button[data-copy]");
            if (btn) {
                const v = btn.dataset.value || "";
                return void copyText(v);
            }

            const img = e.target.closest && e.target.closest(".dg-thumb img");
            if (img) {
                const card = e.target.closest(".dg-item");
                const sumTitle = $(".dg-sum-title", card)?.textContent?.trim() || "";
                const key = $(".dg-mono.small", card)?.textContent?.trim() || "";
                openLightbox(img.getAttribute("src") || "", `${sumTitle}${key ? " · " + key : ""}`);
            }
        });
    }

    // --- AJAX 업로드(페이지 리로드 없이 결과 갱신) ---
    const form = document.getElementById("dgUploadForm");
    const submitBtn = document.getElementById("dgSubmitBtn");
    const statusEl = document.getElementById("dgFormStatus");

    const okEl = document.getElementById("dgOkCount");
    const failEl = document.getElementById("dgFailCount");
    const totalEl = document.getElementById("dgTotalCount");

    function setBusy(b) {
        if (submitBtn) submitBtn.disabled = !!b;
        if (filesInput) filesInput.disabled = !!b;
    }

    function renderResults(cards) {
        if (!resultsList) return;

        resultsList.innerHTML = "";
        const list = Array.isArray(cards) ? cards : [];
        if (resultsEmpty) resultsEmpty.hidden = (list.length > 0);

        for (const c of list) {
            const status = String(c.status || "");
            const isFail = status === "FAIL";

            const det = document.createElement("details");
            det.className = "dg-item" + (isFail ? " is-fail" : "");
            det.open = true;

            const url = normalizePreviewUrl(c.url || "");
            const caption = String(c.caption || "(캡션 없음)");
            const storageKey = String(c.storage_key || "");
            const pid = String(c.pid || "");
            const sha16 = String(c.sha16 || "");
            const msg = String(c.msg || "");
            const mime = String(c.mime || "");
            const size = String(c.size || "");

            const summary = document.createElement("summary");
            summary.className = "dg-item-sum";
            summary.innerHTML = `
        <div class="dg-thumb">
          ${url ? `<img src="${url}" alt="thumb" loading="lazy">` : `<div class="dg-thumb-fallback">no img</div>`}
        </div>
        <div class="dg-sum-main">
          <div class="dg-sum-top">
            <span class="dg-badge ${isFail ? "bad" : "good"}">${status || (isFail ? "FAIL" : "OK")}</span>
            <span class="dg-sum-title"></span>
          </div>
          <div class="dg-sum-sub">
            <span class="dg-chip">key</span>
            <span class="dg-mono small"></span>
          </div>
        </div>
      `;
            $(".dg-sum-title", summary).textContent = caption;

            const body = document.createElement("div");
            body.className = "dg-item-body";
            body.innerHTML = `
        ${msg ? `<div class="dg-alert ${isFail ? "err" : "ok"}">${escapeHtml(msg)}</div>` : ``}

        <div class="dg-meta">
          <div class="dg-meta-row"><div class="dg-pill">Key</div><div class="dg-mono small">${escapeHtml(storageKey)}</div></div>
          <div class="dg-meta-row"><div class="dg-pill">PID</div><div class="dg-mono">${escapeHtml(pid)}</div></div>
          <div class="dg-meta-row"><div class="dg-pill">SHA</div><div class="dg-mono">${escapeHtml(sha16)}</div></div>
          <div class="dg-meta-row"><div class="dg-pill">MIME</div><div class="dg-text">${escapeHtml(mime)}</div></div>
          <div class="dg-meta-row"><div class="dg-pill">Size</div><div class="dg-text">${escapeHtml(size)}</div></div>
        </div>

        <div class="dg-item-actions">
          ${url ? `<a class="dg-btn ghost" href="${url}" target="_blank" rel="noreferrer">새 탭</a>` : ``}
          <button class="dg-btn ghost" type="button" data-copy="storage_key" data-value="${escapeAttr(storageKey)}">key 복사</button>
          ${pid ? `<button class="dg-btn ghost" type="button" data-copy="pid" data-value="${escapeAttr(pid)}">pid 복사</button>` : ``}
          ${url ? `<button class="dg-btn ghost" type="button" data-copy="url" data-value="${escapeAttr(url)}">url 복사</button>` : ``}
        </div>
      `;

            det.appendChild(summary);
            det.appendChild(body);
            resultsList.appendChild(det);
        }

        applyFilters();
    }

    function escapeHtml(s) {
        return String(s || "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#39;");
    }
    function escapeAttr(s) {
        return escapeHtml(s).replaceAll("\n", " ");
    }

    if (form) {
        form.addEventListener("submit", async (e) => {
            e.preventDefault();

            const action = form.getAttribute("action") || window.location.href;
            const fd = new FormData(form);
            if (statusEl) statusEl.textContent = "업로드 중...";
            setBusy(true);

            try {
                const csrftoken = getCookie("csrftoken");
                const res = await fetch(action, {
                    method: "POST",
                    headers: {
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json",
                        "X-CSRFToken": csrftoken
                    },
                    credentials: "same-origin",
                    body: fd
                });

                const data = await res.json().catch(() => ({}));
                if (!res.ok || !data || !data.ok) {
                    const err = (data && data.error) ? String(data.error) : `HTTP ${res.status}`;
                    if (statusEl) statusEl.textContent = "실패: " + err;
                    toast("업로드 실패", "err");
                    setBusy(false);
                    return;
                }

                // counts
                const okCount = Number(data.ok_count || 0);
                const failCount = Number(data.fail_count || 0);
                const cards = data.cards || [];
                if (okEl) okEl.textContent = String(okCount);
                if (failEl) failEl.textContent = String(failCount);
                if (totalEl) totalEl.textContent = String(Array.isArray(cards) ? cards.length : 0);

                renderResults(cards);

                if (statusEl) statusEl.textContent = `완료: OK ${okCount}, FAIL ${failCount}`;
                toast("업로드 완료", "ok");

                // 업로드 후 파일 선택 초기화(원하면 유지하려면 아래 2줄 주석)
                if (filesInput) filesInput.value = "";
                clearLocalPreviews();

            } catch (err) {
                if (statusEl) statusEl.textContent = "실패: " + String(err);
                toast("업로드 실패", "err");
            } finally {
                setBusy(false);
            }
        });
    }

    // 초기 필터 적용
    applyFilters();

})();
