// ragapp/static/ragapp/javascript/crawl_news.js
(function () {
    "use strict";

    const root = document.getElementById("crawlPage");
    const form = document.getElementById("crawlForm");

    if (!root || !form) {
        return;
    }

    const DEFAULT_COLL = root.dataset.defaultColl || "";
    const DEFAULT_DIR = root.dataset.defaultDir || "";
    const VDB_PATH = root.dataset.vdbPath || "(미설정)";
    const API_WEB_QA = root.dataset.apiWebqa || "/api/web_qa";
    const API_INGEST = root.dataset.apiIngest || "/api/news_ingest/";

    // ✅ 법적 리스크 완화용 프론트 정책
    // 기사 전문 저장/표시 금지, 짧은 발췌만 표시
    const LEGAL_SNIPPET_MAX = 300;
    const FORCE_META_ONLY = true;

    const input = document.getElementById("keyword");
    const errBox = document.getElementById("errorBox");
    const errText = document.getElementById("errorText");
    const answerBox = document.getElementById("answerBox");
    const newsBox = document.getElementById("newsBox");
    const ingestBox = document.getElementById("ingestBox");
    const runBtn = form.querySelector(".search-btn-inbar");

    function escHtml(s) {
        return (s ?? "")
            .toString()
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    // ✅ 기사 발췌문이 길어지는 것 방지
    function clampText(s, maxLen) {
        const t = (s ?? "").toString().replace(/\s+/g, " ").trim();
        const n = Number(maxLen || 0);

        if (!t) {
            return "";
        }

        if (!n || t.length <= n) {
            return t;
        }

        return t.slice(0, n).trim() + "…";
    }

    function safeHref(u) {
        let s = (u ?? "").toString().trim();

        if (!s) {
            return "";
        }

        // ✅ "/https://..." 형태 보정
        if (/^\/https?:\/\//i.test(s)) {
            s = s.slice(1);
        }

        // ✅ "//example.com/..." 형태 보정
        if (s.indexOf("//") === 0) {
            s = "https:" + s;
        }

        try {
            const url = new URL(s, window.location.origin);

            if (url.protocol === "http:" || url.protocol === "https:") {
                return url.href;
            }

            return "";
        } catch {
            return "";
        }
    }

    function showError(msg) {
        if (!errBox || !errText) {
            return;
        }

        errText.textContent = msg;
        errBox.style.display = "flex";
    }

    function clearError() {
        if (!errBox || !errText) {
            return;
        }

        errText.textContent = "";
        errBox.style.display = "none";
    }

    function getCookie(name) {
        const m = document.cookie.match("(^|;)\\s*" + name + "\\s*=\\s*([^;]+)");
        return m ? decodeURIComponent(m.pop()) : "";
    }

    function setRunning(on) {
        if (!runBtn) {
            return;
        }

        runBtn.disabled = !!on;

        if (on) {
            if (!runBtn.dataset.prev) {
                runBtn.dataset.prev = runBtn.innerHTML;
            }
            runBtn.innerHTML = "실행 중…";
        } else {
            runBtn.innerHTML =
                runBtn.dataset.prev ||
                '<span class="search-btn-emoji">🔍</span><span>뉴스 수집 &amp; 인덱싱 실행</span>';

            try {
                delete runBtn.dataset.prev;
            } catch (_) { }
        }
    }

    function renderAnswer(text) {
        if (!answerBox) {
            return;
        }

        const t = (text || "").toString().trim();
        answerBox.textContent = t || "(API가 답변을 반환하지 않았습니다.)";
    }

    function renderNews(list) {
        if (!newsBox) {
            return;
        }

        const items = Array.isArray(list) ? list : [];

        if (!items.length) {
            newsBox.textContent = "아직 뉴스 데이터가 없습니다.";
            return;
        }

        newsBox.innerHTML = items
            .map((n) => {
                const t = escHtml(n.title || "(제목 없음)");
                const source = escHtml(n.source || "");
                const published = escHtml(n.published_at || "");
                const snippet = escHtml(clampText(n.snippet || "", LEGAL_SNIPPET_MAX));
                const href = safeHref(n.url);

                return `
          <div class="news-item">
            <div class="news-title-row">${t}</div>

            <div class="news-meta">
              ${source ? `<span>출처: ${source}</span>` : ""}
              ${published ? `<span>게시: ${published}</span>` : ""}
              ${href
                        ? `<span><a class="news-url" href="${escHtml(href)}" target="_blank" rel="noopener noreferrer nofollow" referrerpolicy="strict-origin-when-cross-origin">${escHtml(href)}</a></span>`
                        : ""
                    }
            </div>

            ${snippet ? `<div class="news-snippet">${snippet}</div>` : ""}

            <div class="news-policy-note">
              기사 전문은 저장하지 않으며, 자세한 내용은 원문 링크에서 확인해 주세요.
            </div>
          </div>
        `;
            })
            .join("");
    }

    function renderIngest(sum) {
        if (!ingestBox) {
            return;
        }

        const s = sum || {};

        const inserted = s.inserted ?? s.indexed_count ?? 0;
        const aChunks = s.answer_chunks ?? 0;

        const newsTotalChunks = s.news_total_chunks ?? s.news_indexed_chunks ?? 0;
        const newsMetaChunks = s.news_meta_chunks ?? 0;
        const newsBodyChunks = s.news_body_chunks ?? 0;

        const ragSaved = s.ragchunk_saved ?? 0;
        const metaOnly = !!s.meta_only;
        const allowBody = !!s.allow_body;

        const legalWarning = allowBody || newsBodyChunks > 0;

        const at = escHtml(s.ingested_at || "-");
        const coll = escHtml(s.collection || DEFAULT_COLL);
        const dir = escHtml(s.dir || DEFAULT_DIR);
        const vdb = escHtml(VDB_PATH);
        const source = escHtml(s.source || s.audit_source || "news");

        let itemsHtml = "";

        if (Array.isArray(s.news_items) && s.news_items.length) {
            itemsHtml = `
        <div class="ingest-items">
          ${s.news_items
                    .map((it) => {
                        const title = escHtml(it.title || "(제목 없음)");
                        const url = escHtml(it.url || "(없음)");
                        const chunks = escHtml(it.chunks ?? "-");
                        const metaLabel = it.meta_only ? " (메타 전용)" : "";
                        const bodyChunks = it.body_chunks
                            ? ` / 본문 청크 ${escHtml(it.body_chunks)}개`
                            : "";

                        return `
                <div class="ingest-item">
                  <strong>${title}</strong><br />
                  URL: ${url}<br />
                  청크 수: ${chunks}${metaLabel}${bodyChunks}
                </div>
              `;
                    })
                    .join("")}
        </div>
      `;
        }

        ingestBox.innerHTML = `
      <div class="ingest-summary-line">
        저장된 전체 청크 수:
        <strong>${inserted}</strong>개
        (답변 청크 ${aChunks}개 / 뉴스 전체 청크 ${newsTotalChunks}개)
        <br />
        뉴스 메타 청크: <code>${newsMetaChunks}</code>개 /
        뉴스 본문 청크: <code>${newsBodyChunks}</code>개
      </div>

      <div class="ingest-summary-line">
        수집 정책: <code>${metaOnly ? "메타 전용" : "본문 허용"}</code><br />
        본문 저장: <code>${allowBody ? "허용" : "차단"}</code><br />
        RagChunk 저장: <code>${ragSaved}</code>개<br />
        source: <code>${source}</code>
      </div>

      ${legalWarning ? `
        <div class="ingest-summary-line" style="color:#b42318;">
          ⚠️ 법적 리스크 주의: 뉴스 본문 청크가 저장된 것으로 보입니다.
          포트폴리오/테스트 운영에서는 <code>WEB_INGEST_META_ONLY=True</code>,
          <code>ALLOW_STORE_NEWS_BODY=False</code>를 권장합니다.
        </div>
      ` : `
        <div class="ingest-summary-line">
          ✅ 안전 모드: 기사 전문은 저장하지 않고 제목·출처·URL·짧은 발췌문 중심으로만 인덱싱합니다.
        </div>
      `}

      <div class="ingest-summary-line">
        엔진: <code>현재 벡터 DB</code><br />
        저장 경로: <code>${vdb}</code><br />
        (호환) 컬렉션: <code>${coll}</code><br />
        (호환) 저장 경로: <code>${dir}</code><br />
        시각: <code>${at}</code>
      </div>

      ${itemsHtml}
    `;
    }

    async function postJSON(url, payload) {
        const csrf =
            getCookie("csrftoken") ||
            document.querySelector("input[name=csrfmiddlewaretoken]")?.value ||
            "";

        const resp = await fetch(url, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrf,
                "X-Requested-With": "XMLHttpRequest",
            },
            credentials: "same-origin",
            body: JSON.stringify(payload || {}),
        });

        const ct = (resp.headers.get("content-type") || "").toLowerCase();

        if (!ct.includes("application/json")) {
            throw new Error(`JSON이 아닌 응답을 받았습니다. (status=${resp.status})`);
        }

        const data = await resp.json().catch(() => ({}));

        if (!resp.ok || data.ok === false) {
            const msg =
                (data && (data.error || data.detail || data.message || data.msg)) ||
                `${resp.status} ${resp.statusText}`;
            throw new Error(msg);
        }

        return data;
    }

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        clearError();

        const q = (input && input.value ? input.value : "").trim();

        if (!q) {
            showError("키워드를 입력해 주세요.");
            return;
        }

        setRunning(true);

        if (answerBox) answerBox.textContent = "실행 중…";
        if (newsBox) newsBox.textContent = "실행 중…";
        if (ingestBox) ingestBox.textContent = "실행 중…";

        try {
            // 모델 답변은 선택 기능이다. 로컬 모드에서는 OAuth 호출을 건너뛴다.
            let qa = {};
            const modelEnabled = (root.dataset.modelEnabled || "1") === "1";

            if (modelEnabled) {
                try {
                    qa = await postJSON(API_WEB_QA, { q });
                } catch {
                    // 웹 QA 실패해도 뉴스 수집/인덱싱은 계속 진행
                    qa = {};
                }
            }

            const ans = (qa && (qa.answer_text || qa.answer || qa.model_answer)) || "";
            renderAnswer(
                ans || (modelEnabled
                    ? ""
                    : "로컬 모드: 모델 호출 없이 뉴스 수집과 인덱싱을 진행합니다.")
            );

            // 2) 뉴스 수집 & 안전 인덱싱
            const ing = await postJSON(API_INGEST, {
                q,
                answer: ans,

                // ✅ 법적 리스크 완화:
                // 기사 전문이 아니라 제목/URL/출처/짧은 snippet 중심으로만 저장
                meta_only: FORCE_META_ONLY,
                snippet_max: LEGAL_SNIPPET_MAX,
            });

            renderNews(ing.news || ing.headlines || []);
            renderIngest(ing.ingest_summary || ing.indexto_chroma || ing.ingest || {});
        } catch (err) {
            showError(`요청 실패: ${err && err.message ? err.message : err}`);

            if (answerBox) answerBox.textContent = "—";
            if (newsBox) newsBox.textContent = "—";
            if (ingestBox) ingestBox.textContent = "—";
        } finally {
            setRunning(false);
        }
    });
})();
