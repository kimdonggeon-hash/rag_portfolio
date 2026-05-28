// ragapp/static/ragapp/javascript/legal-bundle.js
(function () {
    "use strict";

    const $qa = (s, r) => Array.from((r || document).querySelectorAll(s));
    const $q = (s, r) => (r || document).querySelector(s);

    function onReady(fn) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", fn, { once: true });
        } else {
            fn();
        }
    }

    /* =========================
       UI Enhancements (existing)
       ========================= */

    function addRipple(el) {
        // ✅ 중복 바인딩 방지
        if (el.dataset.rippleBound === "1") return;
        el.dataset.rippleBound = "1";

        el.addEventListener("click", function (e) {
            const rect = this.getBoundingClientRect();
            const size = Math.max(rect.width, rect.height);
            const span = document.createElement("span");
            span.className = "ripple";
            span.style.width = span.style.height = size + "px";
            span.style.left = (e.clientX - rect.left - size / 2) + "px";
            span.style.top = (e.clientY - rect.top - size / 2) + "px";
            this.appendChild(span);
            setTimeout(() => span.remove(), 500);
        });
    }

    function guardSubmits(root) {
        // 폼 안의 submit 버튼 클릭 시 더블클릭 방지
        $qa('form .btn[type="submit"], form button.btn', root).forEach((btn) => {
            // ✅ 중복 바인딩 방지
            if (btn.dataset.submitGuardBound === "1") return;
            btn.dataset.submitGuardBound = "1";

            btn.addEventListener(
                "click",
                function () {
                    const form = this.closest("form");
                    if (!form) return;

                    this.setAttribute("aria-busy", "true");
                    this.setAttribute("data-loading", "");
                    this.disabled = true;

                    setTimeout(() => {
                        this.removeAttribute("data-loading");
                        this.removeAttribute("aria-busy");
                        this.disabled = false;
                    }, 6000);
                },
                { once: false }
            );
        });
    }

    function thumbToggle(root) {
        const rows = $qa(".main-feedback-row", root);
        rows.forEach((row) => {
            // ✅ 중복 바인딩 방지(행 단위)
            if (row.dataset.thumbBound === "1") return;
            row.dataset.thumbBound = "1";

            const ups = $qa('.main-thumb-btn[data-helpful="true"]', row);
            const downs = $qa('.main-thumb-btn[data-helpful="false"]', row);

            function activate(btn) {
                $qa(".main-thumb-btn", row).forEach((b) => b.classList.remove("is-active"));
                btn.classList.add("is-active");
            }

            [...ups, ...downs].forEach((btn) => {
                btn.addEventListener("click", () => activate(btn));
            });
        });
    }

    function enhance(root) {
        // 리플(기존 요소 + 나중에 들어온 모달 탭도 처리)
        $qa(".btn, .main-thumb-btn, .legal-tab", root).forEach(addRipple);

        // 제출 가드
        guardSubmits(root);

        // 좋아요/별로예요 토글
        thumbToggle(root);
    }

    function initEnhanceOnce() {
        enhance(document);
    }

    onReady(initEnhanceOnce);

    // ✅ Lazy-load로 모달이 DOM에 붙은 뒤 다시 강화
    document.addEventListener("legal:modal:loaded", function () {
        const modal = document.getElementById("legalBundleModal");
        if (modal) enhance(modal);
    });

    /* =========================
       Consent Gate (moved from news.html)
       Policy: always show on load (first visit + every refresh)
       ========================= */

    (function ConsentGateModule() {
        if (window.__DG_CONSENT_GATE_INIT__ === true) return;
        window.__DG_CONSENT_GATE_INIT__ = true;

        let _queued = null;
        let _lastFocus = null;

        // ✅ 정책상: 새로고침마다 다시 뜨므로 "항상 false로 시작"
        window.__SESSION_CONSENT__ = { ok: false };

        function safeLog(tag, obj) {
            try {
                window.dglog && window.dglog(tag, obj);
            } catch (_) { }
        }

        function openConsentOverlay(showError) {
            try {
                const ov = document.getElementById("consentOverlay");
                const err = document.getElementById("consentErr");
                const chk = document.getElementById("consent_required");
                if (!ov) return;

                // ✅ 포커스 백업
                try {
                    _lastFocus = document.activeElement;
                } catch (_) {
                    _lastFocus = null;
                }

                // ✅ 체크박스는 매번 초기화(원하면 이 줄 제거 가능)
                try {
                    if (chk) chk.checked = false;
                } catch (_) { }

                // ✅ hidden/aria-hidden 확실히 해제
                ov.hidden = false;
                ov.removeAttribute("hidden");
                ov.setAttribute("aria-hidden", "false");

                // ✅ 에러 토글
                if (err) {
                    if (showError) {
                        err.hidden = false;
                        err.removeAttribute("hidden");
                    } else {
                        err.hidden = true;
                        err.setAttribute("hidden", "hidden");
                    }
                }

                // ✅ 바디 상태 토글(스크롤 잠금 등)
                try {
                    document.body.classList.add("legal-open");
                } catch (_) { }

                // ✅ 모달 안으로 포커스 이동(접근성/경고 방지)
                setTimeout(() => {
                    try {
                        const first =
                            document.getElementById("consent_required") ||
                            document.getElementById("consentConfirmBtn") ||
                            ov.querySelector("button, input, a, [tabindex]:not([tabindex='-1'])");
                        if (first && typeof first.focus === "function") first.focus();
                    } catch (_) { }
                }, 0);
            } catch (e) {
                safeLog("CONSENT_OPEN_ERR", e && (e.message || e));
            }
        }

        function closeConsentOverlay() {
            try {
                const ov = document.getElementById("consentOverlay");
                if (!ov) return;

                // ✅ 모달 내부 포커스면 먼저 blur(aria 경고 방지)
                try {
                    if (ov.contains(document.activeElement)) document.activeElement.blur();
                } catch (_) { }

                ov.hidden = true;
                ov.setAttribute("hidden", "hidden");
                ov.setAttribute("aria-hidden", "true");

                try {
                    document.body.classList.remove("legal-open");
                } catch (_) { }

                // ✅ 포커스 복구
                try {
                    if (_lastFocus && typeof _lastFocus.focus === "function") _lastFocus.focus();
                } catch (_) { }
            } catch (e) {
                safeLog("CONSENT_CLOSE_ERR", e && (e.message || e));
            }
        }

        // 외부에서 호출 가능
        window.__openConsentOverlay = function () {
            openConsentOverlay(false);
        };
        window.__closeConsentOverlay = closeConsentOverlay;

        function sanitizeAnchors(scopeSel) {
            try {
                const root = document.querySelector(scopeSel) || document;
                root.querySelectorAll("a[href]").forEach(function (a) {
                    const href = a.getAttribute("href") || "";
                    try {
                        const u = new URL(href, location.origin);

                        // http(s) 또는 내부경로(/...)만 허용
                        if (!/^https?:$/i.test(u.protocol) && !u.pathname.startsWith("/")) {
                            const span = document.createElement("span");
                            span.className = a.className;
                            span.textContent = a.textContent || href;
                            a.replaceWith(span);
                            return;
                        }

                        a.setAttribute("rel", "noopener noreferrer nofollow");
                        a.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
                    } catch (_) {
                        const s = document.createElement("span");
                        s.className = a.className;
                        s.textContent = a.textContent || href;
                        a.replaceWith(s);
                    }
                });
            } catch (_) { }
        }

        function installForceAction(hiddenId, defaultAction) {
            const hid = document.getElementById(hiddenId);
            const form = hid ? hid.closest("form") : null;
            if (!form) return;

            // 이미 설치됐으면 패스
            if (form.dataset.forceActionBound === "1") return;
            form.dataset.forceActionBound = "1";

            form.addEventListener(
                "click",
                function (e) {
                    const btn = e.target.closest && e.target.closest('button[type="submit"][data-action]');
                    if (!btn || !form.contains(btn)) return;
                    if (hid) hid.value = btn.dataset.action || hid.value || defaultAction;
                },
                true
            );
        }

        function installEnterSubmitFallback(form, defaultAction) {
            if (!form) return;
            if (form.dataset.enterSubmitFallbackBound === "1") return;
            form.dataset.enterSubmitFallbackBound = "1";

            form.addEventListener(
                "submit",
                function () {
                    try {
                        const h = form.querySelector('input[name="action"][type="hidden"]');
                        if (h && !h.value) h.value = defaultAction;
                    } catch (_) { }
                    return true;
                },
                { capture: true }
            );
        }

        // ensureConsentGate 래핑(기존 로직 + 내 로직)
        (function wrapEnsureConsentGate() {
            if (window.__DG_ENSURE_CONSENT_GATE_WRAPPED__) return;
            window.__DG_ENSURE_CONSENT_GATE_WRAPPED__ = true;

            const prev = window.ensureConsentGate;

            function mine(form) {
                try {
                    const requireConsent = !!(form && form.matches && form.matches('[data-require-consent="1"]'));
                    const action = (form && form.querySelector && (form.querySelector('input[name="action"]')?.value || "")).trim();
                    const hasSessionConsent = !!(window.__SESSION_CONSENT__ && window.__SESSION_CONSENT__.ok);

                    // ✅ 동의 필요한 액션(web_ingest) + 세션 동의 없으면 모달
                    const need = requireConsent && action === "web_ingest" && !hasSessionConsent;
                    if (!need) return true;

                    _queued = { form, action };
                    openConsentOverlay(false);
                    return false;
                } catch (_) { }
                return true;
            }

            window.ensureConsentGate = function (form) {
                try {
                    if (typeof prev === "function") {
                        const r = prev(form);
                        if (r === false) return false;
                    }
                } catch (_) { }
                return mine(form);
            };
        })();

        function installQueueApi() {
            window.__CONSENT_QUEUE__ = {
                get queued() {
                    return _queued;
                },
                resume() {
                    const q = _queued;
                    _queued = null;
                    if (!q || !q.form) return;

                    try {
                        if (typeof q.form.requestSubmit === "function") q.form.requestSubmit();
                        else q.form.submit();
                    } catch (_) { }
                },
            };
        }

        function handleConsentConfirm() {
            try {
                const chk = document.getElementById("consent_required");
                const err = document.getElementById("consentErr");

                if (!chk || !chk.checked) {
                    // ✅ 에러 표시 + 오버레이 유지 + 포커스
                    openConsentOverlay(true);
                    try {
                        if (chk && typeof chk.focus === "function") chk.focus();
                    } catch (_) { }
                    return;
                }

                if (err) {
                    err.hidden = true;
                    err.setAttribute("hidden", "hidden");
                }

                // ✅ 이 페이지(세션)에서는 통과
                window.__SESSION_CONSENT__ = window.__SESSION_CONSENT__ || {};
                window.__SESSION_CONSENT__.ok = true;

                closeConsentOverlay();

                // ✅ 큐 재개(저장 액션 등)
                try {
                    if (window.__CONSENT_QUEUE__ && typeof window.__CONSENT_QUEUE__.resume === "function") {
                        window.__CONSENT_QUEUE__.resume();
                    }
                } catch (_) { }
            } catch (e) {
                safeLog("CONSENT_CONFIRM_ERR", e && (e.message || e));
            }
        }

        function bindConsentConfirmClick() {
            // ✅ 중복 바인딩 방지
            if (document.documentElement.dataset.consentConfirmBound === "1") return;
            document.documentElement.dataset.consentConfirmBound = "1";

            document.addEventListener("click", function (ev) {
                const btn = ev.target.closest && ev.target.closest("#consentConfirmBtn");
                if (!btn) return;
                ev.preventDefault();
                handleConsentConfirm();
            });
        }

        function autoOpenEveryLoad() {
            // ✅ 정책: "처음 접속 + 새로고침마다" 무조건 오픈
            openConsentOverlay(false);
        }

        function initConsentGate() {
            // 링크 sanitize
            sanitizeAnchors("[data-sanitize-links]");

            // action 강제 주입(페이지 구조 유지)
            installForceAction("webActionField", "web_search");
            installForceAction("ragActionField", "rag_search");

            const webForm = document.getElementById("webActionField")?.closest("form");
            const ragForm = document.getElementById("ragActionField")?.closest("form");
            if (webForm) installEnterSubmitFallback(webForm, "web_search");
            if (ragForm) installEnterSubmitFallback(ragForm, "rag_search");

            installQueueApi();
            bindConsentConfirmClick();

            // ✅ 여기서 항상 오픈
            autoOpenEveryLoad();
        }

        onReady(initConsentGate);
    })();
})();
