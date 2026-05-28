// ragapp/static/ragapp/javascript/media_search_img_loader.js
(() => {
    "use strict";

    // =========================================================
    // [A] Search submit lock (연타 방지 + 검색중 표시)
    // - 템플릿에 인라인 JS 넣지 않기 위해 여기서 처리
    // - GET submit이라 보통 페이지 이동 -> unlock 불필요
    // =========================================================
    function attachSubmitLock() {
        const form = document.getElementById("mediaSearchForm");
        if (!form) return;

        // 버튼: 템플릿을 "가능하면" 이렇게 바꿔주면 UX가 더 좋아짐
        // <button id="msSubmit" ... data-busy-text="검색 중…">🔎 검색하기</button>
        const btn = document.getElementById("msSubmit") || form.querySelector('button[type="submit"]');

        // 검색 중 안내 문구(있으면 표시)
        // <div id="msBusy" ... hidden>⏳ 검색 중...</div>
        const busy = document.getElementById("msBusy");

        let locked = false;
        const origText = btn ? btn.textContent : "";
        const busyText = (btn && (btn.getAttribute("data-busy-text") || btn.dataset.busyText)) || "검색 중…";

        form.addEventListener("submit", (e) => {
            if (locked) {
                e.preventDefault();
                return;
            }
            locked = true;

            if (btn) {
                btn.disabled = true;
                btn.textContent = busyText;
            }
            if (busy) busy.hidden = false;

            // ✅ validation 등에 걸려서 실제 이동이 안 되는 브라우저 케이스 대비
            // (대부분은 페이지가 이동하니 체감상 영향 거의 없음)
            window.setTimeout(() => {
                locked = false;
                try {
                    if (btn) {
                        btn.disabled = false;
                        btn.textContent = origText;
                    }
                    if (busy) busy.hidden = true;
                } catch (_) { }
            }, 8000);
        });

        // submit 직후 Enter 연타 방지
        form.addEventListener("keydown", (e) => {
            if (!locked) return;
            if (e.key === "Enter") {
                e.preventDefault();
                e.stopPropagation();
            }
        });
    }

    // =========================================================
    // [B] Existing image lazy loader (너가 준 코드 그대로)
    // =========================================================

    // ----------------------------
    // Tunables (서버 보호/체감)
    // ----------------------------
    let MAX_INFLIGHT = 2;        // 동시 로드 제한
    let MIN_GAP_MS = 140;        // ✅ "요청 시작" 간격(버스트 방지)
    const TIMEOUT_MS = 15000;    // ✅ 로딩 타임아웃(무한 대기 방지)
    const RETRY_MAX = 0;         // ✅ 기본 0(재시도는 트래픽 증가). 필요하면 1로만.
    const RETRY_DELAY_MS = 900;  // 재시도 딜레이(쓸 경우)

    // Save-Data / 느린망이면 더 보수적으로
    try {
        const c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
        const saveData = !!(c && c.saveData);
        const et = (c && c.effectiveType) ? String(c.effectiveType) : "";
        const slow = saveData || /(^|-)2g$/.test(et) || et === "slow-2g";
        if (slow) {
            MAX_INFLIGHT = 1;
            MIN_GAP_MS = 260;
        }
    } catch (_) { }

    // ----------------------------
    // State
    // ----------------------------
    const queue = [];
    let inflight = 0;
    let paused = false;

    // ✅ 너무 촘촘히 시작하지 않게 하는 간단한 rate-limit
    let nextStartAt = 0;

    // ✅ 같은 URL 중복 요청 방지(안전장치)
    const startedUrl = new Set();

    function nowMs() {
        return Date.now();
    }

    function jitter(ms) {
        // 0~40ms 정도 랜덤 지터로 "동시 폭주" 패턴 완화
        return ms + Math.floor(Math.random() * 41);
    }

    function markLoaded(img) {
        img.dataset.loaded = "1";
        img.dataset.loading = "0";
        const wrap = img.closest(".ms-thumb-wrap");
        if (wrap) wrap.classList.add("is-loaded");
    }

    function markError(img) {
        img.dataset.error = "1";
        img.dataset.loading = "0";
        const wrap = img.closest(".ms-thumb-wrap");
        if (wrap) wrap.classList.add("is-error");
    }

    function finishOne() {
        inflight = Math.max(0, inflight - 1);
        pump();
    }

    function startLoad(img, url, attempt) {
        inflight += 1;
        img.dataset.loading = "1";
        img.dataset.error = img.dataset.error || "0";

        // 로딩 시작 간격(버스트 방지)
        nextStartAt = nowMs() + jitter(MIN_GAP_MS);

        // 타임아웃(무한대기 방지)
        let done = false;
        const t = setTimeout(() => {
            if (done) return;
            done = true;
            try {
                // 진행 중이던 로드를 끊고 에러 처리
                img.src = "";
            } catch (_) { }
            markError(img);
            finishOne();
        }, TIMEOUT_MS);

        const cleanup = () => {
            clearTimeout(t);
            img.removeEventListener("load", onLoad);
            img.removeEventListener("error", onError);
        };

        const onLoad = () => {
            if (done) return;
            done = true;
            cleanup();
            markLoaded(img);
            finishOne();
        };

        const onError = () => {
            if (done) return;
            done = true;
            cleanup();

            // 재시도는 기본 OFF(서버 보호). 필요하면 RETRY_MAX=1로.
            if (attempt < RETRY_MAX && document.visibilityState === "visible" && navigator.onLine) {
                // 너무 공격적으로 바로 재시도하지 않게 딜레이
                setTimeout(() => {
                    // 재시도는 startedUrl 디듀프에 걸리지 않게 예외 처리(같은 img 한정)
                    // -> img.dataset.retrying를 이용
                    img.dataset.retrying = "1";
                    inflight = Math.max(0, inflight - 1); // 현재 시도는 종료 처리
                    queue.unshift(img);                  // 우선순위로 다시 넣기
                    pump();
                }, jitter(RETRY_DELAY_MS));
                return;
            }

            markError(img);
            finishOne();
        };

        img.addEventListener("load", onLoad, { once: false });
        img.addEventListener("error", onError, { once: false });

        // ✅ 실제 요청 시작
        try {
            img.src = url;
        } catch (_) {
            // src 세팅 자체가 실패하면 즉시 에러 처리
            clearTimeout(t);
            markError(img);
            finishOne();
        }
    }

    function pump() {
        if (paused) return;

        // 탭이 숨김이면 불필요하게 펌프 돌지 않게
        if (document.visibilityState === "hidden") return;

        while (inflight < MAX_INFLIGHT && queue.length) {
            // rate-limit: 아직 시작하면 안 되는 시간이라면 멈춤
            if (nowMs() < nextStartAt) return;

            const img = queue.shift();
            if (!img) continue;

            // 이미 로드/에러 처리된 건 패스
            if (img.dataset.loaded === "1") continue;
            if (img.dataset.error === "1") continue;
            if (img.dataset.loading === "1") continue;

            const url = (img.dataset.src || "").trim();
            if (!url) continue;

            // ✅ URL 중복 로드 방지(같은 URL이 여러 카드에 반복되는 경우 대비)
            // 단, 같은 img에서 재시도(RETRY)인 경우는 허용
            const isRetryingSameImg = img.dataset.retrying === "1";
            if (!isRetryingSameImg) {
                if (startedUrl.has(url)) {
                    startLoad(img, url, attempt);
                    continue;
                }
                startedUrl.add(url);
            } else {
                img.dataset.retrying = "0";
            }

            // attempt 카운트
            const attempt = parseInt(img.dataset.attempt || "0", 10) || 0;
            img.dataset.attempt = String(attempt + 1);

            startLoad(img, url, attempt);
        }
    }

    function enqueue(img) {
        if (!img) return;
        if (img.dataset.enqueued === "1") return;

        // 기본 상태 초기화
        img.dataset.enqueued = "1";
        img.dataset.loading = img.dataset.loading || "0";
        img.dataset.loaded = img.dataset.loaded || "0";
        img.dataset.error = img.dataset.error || "0";
        img.dataset.attempt = img.dataset.attempt || "0";

        queue.push(img);
        pump();
    }

    function boot() {
        // ✅ submit lock 먼저 붙이기 (템플릿 인라인 JS 금지 대응)
        attachSubmitLock();

        const imgs = Array.from(document.querySelectorAll("img.ms-lazy[data-src]"));
        if (!imgs.length) return;

        // IntersectionObserver로 "화면 근처"부터 enqueue
        if ("IntersectionObserver" in window) {
            const rootMargin = (MAX_INFLIGHT <= 1) ? "120px 0px" : "200px 0px";

            const io = new IntersectionObserver(
                (entries) => {
                    for (const e of entries) {
                        if (!e.isIntersecting) continue;
                        const img = e.target;
                        io.unobserve(img);
                        enqueue(img);
                    }
                },
                { rootMargin }
            );

            imgs.forEach((img) => io.observe(img));
        } else {
            // 미지원 브라우저: 그래도 큐로 로드(동시 제한+rate-limit 적용)
            imgs.forEach(enqueue);
        }

        // 탭 상태 변화 대응(숨김 중 과도한 요청 방지)
        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "visible") pump();
        });

        // 페이지 떠날 때 큐 정리(불필요한 펌프/참조 방지)
        window.addEventListener("pagehide", () => {
            paused = true;
            queue.length = 0;
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
