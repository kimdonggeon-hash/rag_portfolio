/* ragapp/static/ragapp/obsbadge/admin_obs_badge.js */
(function () {
    "use strict";
    if (window.__DG_OBS_BADGE__) return;
    window.__DG_OBS_BADGE__ = true;

    // -----------------------------
    // Dedup: 같은 페이지에 중복 주입된 경우 마지막 것만 유지
    // -----------------------------
    var roots = document.querySelectorAll("#dgObsRoot");
    if (!roots || !roots.length) return;
    var root = roots[roots.length - 1];
    for (var i = 0; i < roots.length - 1; i++) {
        try { roots[i].remove(); } catch (_) { }
    }

    // dgObsBoot도 중복되면 마지막만 유지
    var boots = document.querySelectorAll("#dgObsBoot");
    if (boots && boots.length > 1) {
        for (var j = 0; j < boots.length - 1; j++) {
            try { boots[j].remove(); } catch (_) { }
        }
    }

    function $(sel, ctx) { return (ctx || root).querySelector(sel); }
    function $$(sel, ctx) { return Array.prototype.slice.call((ctx || root).querySelectorAll(sel)); }

    function decodeHtml(s) {
        if (s == null) return "";
        var ta = document.createElement("textarea");
        ta.innerHTML = String(s);
        return ta.value;
    }

    function safeJsonParse(s, fallback) {
        try { return JSON.parse(s || ""); } catch (_) { return fallback; }
    }

    function getCookie(name) {
        var m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]+)"));
        return m ? decodeURIComponent(m[1]) : "";
    }
    function csrfToken() { return getCookie("csrftoken"); }

    async function post(url, payload) {
        return fetch(url, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken()
            },
            body: JSON.stringify(payload || {})
        });
    }

    function reload() { location.reload(); }

    function parseBoot() {
        var el = document.getElementById("dgObsBoot");
        if (!el) return null;
        try {
            var txt = el.textContent || el.innerText || "{}";
            return JSON.parse(txt);
        } catch (_) {
            return null;
        }
    }

    // -----------------------------
    // Boot + fallback sources
    // -----------------------------
    var boot = parseBoot() || {};

    // enabled
    var enabled = (typeof boot.enabled === "boolean")
        ? boot.enabled
        : (root.getAttribute("data-enabled") === "true");

    // cfg (boot 우선, 없으면 data-cfg)
    var dataCfg = safeJsonParse(root.getAttribute("data-cfg"), {}) || {};
    var cfg = (boot.cfg && typeof boot.cfg === "object") ? boot.cfg : dataCfg;

    cfg = cfg || {};
    cfg.fields = cfg.fields || {};
    cfg.pos = cfg.pos || boot.pos || dataCfg.pos || "br";
    cfg.compact = (cfg.compact === 1 || cfg.compact === true) ? 1 : 0;

    var hm = parseInt(cfg.history_max || boot.history_max || dataCfg.history_max || 10, 10);
    cfg.history_max = isNaN(hm) ? 10 : Math.max(3, Math.min(30, hm));

    // history (boot 우선, 없으면 data-history)
    var dataHistory = safeJsonParse(root.getAttribute("data-history"), []) || [];
    var history = Array.isArray(boot.history) ? boot.history : dataHistory;

    // is_error (boot 우선, 없으면 root class)
    var is_error = (typeof boot.is_error === "boolean") ? boot.is_error : root.classList.contains("dg-error");

    // rows (boot 우선, 없으면 DOM에서 읽어서 폴백)
    var rows = Array.isArray(boot.rows) ? boot.rows : [];

    function extractRowsFromDom() {
        // panel.html이 이미 rows_html을 넣었을 수도 있으니, 그걸 읽어서 rows로 변환
        var panel = document.getElementById("dgObsPanel");
        if (!panel) return [];
        var domRows = $$(".dg-row", panel);
        if (!domRows.length) return [];

        var out = [];
        domRows.forEach(function (r) {
            var k = r.getAttribute("data-k") || "";
            var v = r.getAttribute("data-v") || "";
            // data-k/v가 없으면 텍스트에서 폴백
            if (!k) {
                var kk = $(".dg-k", r);
                k = kk ? (kk.textContent || "") : "";
            }
            if (!v) {
                var vv = $(".dg-v", r);
                v = vv ? (vv.textContent || "") : "";
            }
            var vvEl = $(".dg-v", r);
            var copy = vvEl ? vvEl.classList.contains("dg-copy") : false;

            out.push({ k: k, v: v, copy: copy });
        });
        return out;
    }

    if (!rows.length) rows = extractRowsFromDom();

    // -----------------------------
    // Root class sync (pos/compact/error)
    // -----------------------------
    function syncRootClasses() {
        root.classList.remove("dg-pos-br", "dg-pos-bl", "dg-pos-tr", "dg-pos-tl");
        root.classList.add("dg-pos-" + (cfg.pos || "br"));

        if (cfg.compact === 1) root.classList.add("dg-compact");
        else root.classList.remove("dg-compact");

        if (is_error) root.classList.add("dg-error");
        else root.classList.remove("dg-error");
    }
    syncRootClasses();

    // 원복용 스냅샷 (취소 시 되돌림)
    var initialCfg = safeJsonParse(JSON.stringify(cfg), cfg);
    var initialIsError = is_error;

    // -----------------------------
    // Launcher
    // -----------------------------
    var launcher = $("#dgObsLauncher");
    var panel = document.getElementById("dgObsPanel"); // enabled일 때만 주입될 수 있음

    if (launcher) {
        launcher.addEventListener("pointerdown", async function (e) {
            e.preventDefault();
            e.stopPropagation();

            try {
                if (!enabled) {
                    await post("/admin/obsbadge/toggle/", { enabled: true });
                    reload();
                } else {
                    if (panel) panel.hidden = !panel.hidden;
                }
            } catch (_) { }
        });
    }

    if (panel) {
        panel.addEventListener("pointerdown", function (e) {
            e.stopPropagation(); // ✅ 패널 클릭이 바깥 닫기 로직으로 튀는 것 방지
        });
    }

    // enabled가 아니면 여기서 끝
    if (!enabled) return;

    // enabled면 기본 open
    if (panel) panel.hidden = false;

    // -----------------------------
    // Panel HTML(panel.html) 기반: 내용은 boot(JSON)/fallback로 재렌더
    // -----------------------------
    function ensureBanner() {
        if (!panel) return;
        var existing = $(".dg-banner", panel);

        if (is_error) {
            if (existing) return;
            var b = document.createElement("div");
            b.className = "dg-banner";
            b.textContent = "오류 응답이 감지되었습니다. ‘요청 ID’로 로그를 추적하세요.";
            var header = $(".dg-h", panel);
            if (header) header.insertAdjacentElement("afterend", b);
        } else {
            if (existing) {
                try { existing.remove(); } catch (_) { }
            }
        }
    }

    function renderRowsIntoBody() {
        if (!panel) return;

        var body = $(".dg-b", panel);
        if (!body) return;

        // 서버가 넣어준 rows_html이 있어도 JS가 일원화해서 다시 그린다
        body.innerHTML = "";

        for (var i = 0; i < rows.length; i++) {
            var it = rows[i] || {};

            // boot가 html.escape 기반이면 decodeHtml로 복원, 아니면 영향 없음
            var k = decodeHtml(it.k || "");
            var v = decodeHtml(it.v || "");

            var row = document.createElement("div");
            row.className = "dg-row";
            row.setAttribute("data-k", k);
            row.setAttribute("data-v", v);

            var kk = document.createElement("div");
            kk.className = "dg-k";
            kk.textContent = k;

            var vv = document.createElement("div");
            vv.className = "dg-v";
            vv.textContent = v;

            var shouldCopy = !!it.copy || (k === "요청 ID");
            if (shouldCopy) {
                vv.classList.add("dg-copy");
                vv.title = "클릭해서 복사";
                vv.addEventListener("click", (function (val, el) {
                    return async function () {
                        try {
                            if (navigator.clipboard && navigator.clipboard.writeText) {
                                await navigator.clipboard.writeText(val || "");
                            } else {
                                var ta = document.createElement("textarea");
                                ta.value = val || "";
                                document.body.appendChild(ta);
                                ta.select();
                                document.execCommand("copy");
                                ta.remove();
                            }
                            el.style.opacity = "0.65";
                            setTimeout(function () { el.style.opacity = "1"; }, 250);
                        } catch (_) { }
                    };
                })(v, vv));
            }

            row.appendChild(kk);
            row.appendChild(vv);
            body.appendChild(row);
        }

        var hint = document.createElement("div");
        hint.className = "dg-hint";
        hint.textContent = "관리자(스태프)에게만 표시됩니다. 설정은 세션에 저장됩니다.";
        body.appendChild(hint);
    }

    ensureBanner();
    renderRowsIntoBody();

    // -----------------------------
    // Buttons
    // -----------------------------
    var btnOff = document.getElementById("dgObsOff");
    var btnCopy = document.getElementById("dgObsCopy");
    var btnSettings = document.getElementById("dgObsSettings");
    var btnHealth = document.getElementById("dgObsHealth");
    var btnHistory = document.getElementById("dgObsHistoryBtn");

    if (btnOff) btnOff.addEventListener("click", async function () {
        try {
            await post("/admin/obsbadge/toggle/", { enabled: false });
            reload();
        } catch (_) { }
    });

    if (btnCopy) btnCopy.addEventListener("click", async function () {
        try {
            var lines = [];
            for (var i = 0; i < rows.length; i++) {
                var it = rows[i] || {};
                lines.push(decodeHtml(it.k || "") + ": " + decodeHtml(it.v || ""));
            }

            if (history && history.length) {
                var errCount = history.filter(function (x) { return (x.status || 0) >= 400; }).length;
                lines.push("");
                lines.push("기록: " + history.length + "건, 오류=" + errCount + "건");
                history.slice(0, 3).forEach(function (x, idx) {
                    lines.push(
                        " - " + (idx + 1) + ") " +
                        (x.status || 0) + " " + (x.ms || 0) + "ms " +
                        "db=" + (x.db_ms || 0) + "ms/" + (x.db_q || 0) + "q " +
                        "ext=" + (x.ext_ms || 0) + "ms/" + (x.ext_n || 0) + "c " +
                        "vx=" + (x.vx_ms || 0) + "ms/" + (x.vx_n || 0) + "c " +
                        (x.path || "") + " rid=" + (x.rid || "")
                    );
                });
            }

            if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(lines.join("\n"));
            } else {
                var ta = document.createElement("textarea");
                ta.value = lines.join("\n");
                document.body.appendChild(ta);
                ta.select();
                document.execCommand("copy");
                ta.remove();
            }

            var old = btnCopy.textContent;
            btnCopy.textContent = "복사됨";
            setTimeout(function () { btnCopy.textContent = old || "복사"; }, 700);
        } catch (_) { }
    });

    // -----------------------------
    // History
    // -----------------------------
    var historyWrap = document.getElementById("dgObsHistory");
    var historyList = document.getElementById("dgObsHistoryList");
    var onlyErrors = document.getElementById("dgObsOnlyErrors");
    var clearBtn = document.getElementById("dgObsClearHistory");

    function clsForStatus(s) {
        if (s >= 500) return "err";
        if (s >= 400) return "warn";
        return "ok";
    }

    function renderHistory() {
        if (!historyList) return;

        var items = Array.isArray(history) ? history.slice() : [];
        var filterErr = onlyErrors ? !!onlyErrors.checked : false;
        if (filterErr) items = items.filter(function (x) { return (x.status || 0) >= 400; });

        historyList.innerHTML = "";
        if (!items.length) {
            var empty = document.createElement("div");
            empty.className = "dg-item";
            empty.textContent = "기록이 없습니다.";
            historyList.appendChild(empty);
            return;
        }

        items.forEach(function (it) {
            var div = document.createElement("div");
            div.className = "dg-item";
            div.title = "클릭하면 요청 ID가 복사됩니다.";

            var top = document.createElement("div");
            top.className = "dg-item-top";

            var p = document.createElement("div");
            p.className = "dg-item-path";
            p.textContent = it.path || "";

            var badges = document.createElement("div");
            function mkBadge(cls, text) {
                var s = document.createElement("span");
                s.className = "dg-mini" + (cls ? (" " + cls) : "");
                s.textContent = text;
                return s;
            }

            badges.appendChild(mkBadge(clsForStatus(it.status || 0), "status " + (it.status || 0)));
            badges.appendChild(mkBadge("", (it.ms || 0) + "ms"));
            badges.appendChild(mkBadge("", "db " + (it.db_ms || 0) + "ms/" + (it.db_q || 0) + "q"));
            badges.appendChild(mkBadge("", "ext " + (it.ext_ms || 0) + "ms/" + (it.ext_n || 0) + "c"));
            badges.appendChild(mkBadge("", "vx " + (it.vx_ms || 0) + "ms/" + (it.vx_n || 0) + "c"));

            top.appendChild(p);
            top.appendChild(badges);

            var meta = document.createElement("div");
            meta.className = "dg-item-meta";
            meta.textContent = "rid: " + (it.rid || "");

            div.appendChild(top);
            div.appendChild(meta);

            div.addEventListener("click", async function () {
                try {
                    if (navigator.clipboard && navigator.clipboard.writeText) {
                        await navigator.clipboard.writeText(it.rid || "");
                    }
                } catch (_) { }
            });

            historyList.appendChild(div);
        });
    }

    if (btnHistory) btnHistory.addEventListener("click", function () {
        if (!historyWrap) return;
        historyWrap.hidden = !historyWrap.hidden;

        // 제품 UX: 히스토리 열면 설정은 닫기
        var settingsPanel = document.getElementById("dgObsSettingsPanel");
        if (settingsPanel && !historyWrap.hidden) settingsPanel.hidden = true;

        if (!historyWrap.hidden) renderHistory();
    });

    if (onlyErrors) onlyErrors.addEventListener("change", renderHistory);

    if (clearBtn) clearBtn.addEventListener("click", async function () {
        try {
            await post("/admin/obsbadge/history/clear/", {});
            reload();
        } catch (_) { }
    });

    // -----------------------------
    // Settings
    // -----------------------------
    var settingsPanel = document.getElementById("dgObsSettingsPanel");
    var grid = document.getElementById("dgObsGrid");
    var btnCancel = document.getElementById("dgObsCancel");
    var btnSave = document.getElementById("dgObsSave");
    var cbCompact = document.getElementById("dgObsCompact");
    var inputHistoryMax = document.getElementById("dgObsHistoryMax");

    var FIELD_LABELS = {
        env: "환경",
        rev: "리비전",
        host: "호스트",
        rid: "요청 ID",
        status: "HTTP 상태",
        latency: "지연",
        db: "DB",
        ext: "외부(HTTP)",
        vertex: "Vertex"
    };

    function renderFields() {
        if (!grid) return;
        grid.innerHTML = "";

        Object.keys(FIELD_LABELS).forEach(function (k) {
            var wrap = document.createElement("label");
            wrap.className = "dg-field";

            var cb = document.createElement("input");
            cb.type = "checkbox";
            cb.checked = !!cfg.fields[k];
            cb.addEventListener("change", function () {
                cfg.fields[k] = cb.checked ? 1 : 0;
            });

            var t = document.createElement("span");
            t.textContent = FIELD_LABELS[k];

            wrap.appendChild(cb);
            wrap.appendChild(t);
            grid.appendChild(wrap);
        });
    }

    function markPosChips() {
        $$("#dgObsSettingsPanel .dg-chip").forEach(function (b) {
            b.classList.toggle("active", b.getAttribute("data-pos") === (cfg.pos || "br"));
        });
    }

    function openSettings(open) {
        if (!settingsPanel || !btnSettings) return;
        settingsPanel.hidden = !open;
        btnSettings.textContent = open ? "닫기" : "설정";
    }

    if (btnSettings) btnSettings.addEventListener("click", function () {
        var open = settingsPanel ? settingsPanel.hidden : true;

        if (open) {
            renderFields();
            if (cbCompact) cbCompact.checked = (cfg.compact === 1);
            if (inputHistoryMax) inputHistoryMax.value = String(cfg.history_max || 10);
            markPosChips();

            // 제품 UX: 설정 열면 히스토리는 닫기
            if (historyWrap) historyWrap.hidden = true;
        }

        openSettings(open);
    });

    // 컴팩트: 즉시 프리뷰
    if (cbCompact) cbCompact.addEventListener("change", function () {
        cfg.compact = cbCompact.checked ? 1 : 0;
        syncRootClasses();
    });

    // 기록 개수: cfg만 업데이트 (저장은 Save)
    if (inputHistoryMax) inputHistoryMax.addEventListener("change", function () {
        var v = parseInt(inputHistoryMax.value || "10", 10);
        if (!isNaN(v)) cfg.history_max = Math.max(3, Math.min(30, v));
        inputHistoryMax.value = String(cfg.history_max);
    });

    // 위치 칩: 즉시 프리뷰
    $$("#dgObsSettingsPanel .dg-chip").forEach(function (b) {
        b.addEventListener("click", function () {
            cfg.pos = b.getAttribute("data-pos") || "br";
            syncRootClasses();
            markPosChips();
        });
    });

    // 취소: “원복 + 닫기”
    if (btnCancel) btnCancel.addEventListener("click", function () {
        try {
            cfg = safeJsonParse(JSON.stringify(initialCfg), initialCfg) || initialCfg;
            is_error = initialIsError;
            syncRootClasses();
            ensureBanner();
            renderRowsIntoBody();
            openSettings(false);
        } catch (_) {
            openSettings(false);
        }
    });

    // 저장: 서버 저장 후 reload
    if (btnSave) btnSave.addEventListener("click", async function () {
        try {
            await post("/admin/obsbadge/config/", { cfg: cfg });
            reload();
        } catch (_) { }
    });

    // -----------------------------
    // Health Modal
    // -----------------------------
    var modal = document.getElementById("dgObsHealthModal");
    var modalPre = document.getElementById("dgObsModalPre");
    var modalClose = document.getElementById("dgObsHealthModalClose");

    function openModal(open) {
        if (!modal) return;
        modal.hidden = !open;
    }

    if (modalClose) modalClose.addEventListener("click", function () { openModal(false); });
    if (modal) modal.addEventListener("click", function (ev) {
        if (ev.target === modal) openModal(false);
    });

    if (btnHealth) btnHealth.addEventListener("click", async function () {
        try {
            var res = await fetch("/admin/obsbadge/health/", { headers: { "Accept": "application/json" } });
            var data = await res.json();
            if (modalPre) modalPre.textContent = JSON.stringify(data, null, 2);
            openModal(true);
        } catch (_) { }
    });

    // ESC로 모달 닫기
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
            try { openModal(false); } catch (_) { }
        }
    });
})();
