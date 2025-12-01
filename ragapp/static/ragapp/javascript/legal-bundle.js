// ui-enhance.js
(function () {
    const $qa = (s, r) => Array.from((r || document).querySelectorAll(s));

    function addRipple(el) {
        el.addEventListener('click', function (e) {
            const rect = this.getBoundingClientRect();
            const size = Math.max(rect.width, rect.height);
            const span = document.createElement('span');
            span.className = 'ripple';
            span.style.width = span.style.height = size + 'px';
            span.style.left = (e.clientX - rect.left - size / 2) + 'px';
            span.style.top = (e.clientY - rect.top - size / 2) + 'px';
            this.appendChild(span);
            setTimeout(() => span.remove(), 500);
        });
    }

    function guardSubmits() {
        // 폼 안의 submit 버튼 클릭 시 더블클릭 방지
        $qa('form .btn[type="submit"], form button.btn').forEach(btn => {
            btn.addEventListener('click', function () {
                const form = this.closest('form');
                if (!form) return;
                // 서버 렌더(Post)라서 제출 직후 비활성만 해도 충분
                this.setAttribute('aria-busy', 'true');
                this.setAttribute('data-loading', '');
                this.disabled = true;
                // 혹시 JS가 막히면 6초 뒤 자동 해제 (안전장치)
                setTimeout(() => {
                    this.removeAttribute('data-loading');
                    this.removeAttribute('aria-busy');
                    this.disabled = false;
                }, 6000);
            }, { once: false });
        });
    }

    function thumbToggle() {
        // 👍/👎 버튼 UI 토글 (실제 API 호출은 기존 스크립트에 맡김)
        const rows = $qa('.main-feedback-row');
        rows.forEach(row => {
            const ups = $qa('.main-thumb-btn[data-helpful="true"]', row);
            const downs = $qa('.main-thumb-btn[data-helpful="false"]', row);
            function activate(btn) {
                $qa('.main-thumb-btn', row).forEach(b => b.classList.remove('is-active'));
                btn.classList.add('is-active');
            }
            [...ups, ...downs].forEach(btn => {
                btn.addEventListener('click', () => activate(btn));
            });
        });
    }

    function init() {
        // 리플
        $qa('.btn, .main-thumb-btn, .legal-tab').forEach(addRipple);
        // 제출 가드
        guardSubmits();
        // 좋아요/별로예요 토글
        thumbToggle();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
