/* ragapp/static/ragapp/javascript/news.js
   2025-11-23 웹 검색 + RAG 검색용 JS (새 화면 구성까지 담당)
   - 동의/화면 흐림(블러) 처리는 news.html 안의 인라인 스크립트에서 다룹니다.
   - 이 파일에서는:
     · 자잘한 도우미 함수
     · 입력 폼(질문창) 처리
     · 웹 / RAG 답변을 화면에 예쁘게 넣어 주기
     · 서버에 검색 요청 보내기(AJAX)
     · 테스터 안내/법무 푸터 DOM 보정(구형 템플릿 호환)
     · Web / RAG 답변에 대한 👍/👎 + 이유 + 코멘트 피드백 전송
     를 담당합니다.

   ✅ QARAG(질문 챗봇)는 여기서 다루지 않습니다. (완전 분리)
*/

/* ---------- 작은 도우미들 ---------- */
function escHtml(s) {
  return (s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function stripTrailingColon(t) {
  return (t || "").replace(/:\s*$/, "").trim();
}

function cleanLeading(id) {
  var el = document.getElementById(id);
  if (!el) return;
  var h = el.innerHTML;
  if (!h) return;
  el.innerHTML = h
    .replace(/^(<br\s*\/?>\s*)+/i, "")
    .replace(/^\s+/, "");
}

function getCookie(name) {
  let cookieValue = null;
  if (document.cookie && document.cookie !== "") {
    const cookies = document.cookie.split(";");
    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();
      if (cookie.substring(0, name.length + 1) === name + "=") {
        cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
        break;
      }
    }
  }
  return cookieValue;
}

function setCookie(name, value, days) {
  try {
    const maxAge = days ? "; max-age=" + days * 24 * 60 * 60 : "";
    document.cookie =
      name + "=" + encodeURIComponent(value) + "; path=/" + maxAge;
  } catch (e) { }
}

/* ---------- HTML 정리 ---------- */
function sanitizeHTML(unsafe) {
  try {
    const ALLOWED = new Set([
      "B",
      "I",
      "STRONG",
      "EM",
      "BR",
      "UL",
      "OL",
      "LI",
      "P",
      "CODE",
      "PRE",
      "A",
    ]);
    const ALLOWED_ATTR = new Set(["href", "target", "rel"]);
    const T = document.createElement("template");
    T.innerHTML = unsafe || "";

    const walk = function (n) {
      var children = Array.from(n.childNodes);
      for (var i = 0; i < children.length; i++) {
        var c = children[i];
        if (c.nodeType === 1) {
          if (!ALLOWED.has(c.tagName)) {
            while (c.firstChild) {
              c.parentNode.insertBefore(c.firstChild, c);
            }
            c.remove();
            continue;
          }
          if (c.tagName === "A") {
            Array.from(c.attributes).forEach(function (a) {
              if (!ALLOWED_ATTR.has(a.name.toLowerCase())) {
                c.removeAttribute(a.name);
              }
            });
            var href = c.getAttribute("href") || "";
            if (!/^https?:\/\//i.test(href)) {
              c.removeAttribute("href");
            }
            c.setAttribute("rel", "noopener noreferrer");
            c.setAttribute("target", "_blank");
          } else {
            Array.from(c.attributes).forEach(function (a) {
              c.removeAttribute(a.name);
            });
          }
          walk(c);
        } else if (c.nodeType === 8) {
          c.remove();
        }
      }
    };

    walk(T.content || T);

    var root = T.content || T;
    return root.firstChild ? T.innerHTML : T.innerHTML || "";
  } catch (e) {
    return escHtml(unsafe || "");
  }
}

/* ---------- 전송 상태 표시 ---------- */
function setLoading(formEl) {
  try {
    var hiddenAction = formEl.querySelector('input[name="action"]');
    var submitter = document.activeElement;

    if (submitter && submitter.tagName === "BUTTON") {
      var a = submitter.getAttribute("data-action");
      if (a && hiddenAction) {
        hiddenAction.value = a;
      }

      submitter.disabled = true;
      submitter.dataset.origText = submitter.innerText || submitter.value || "";

      if (submitter.innerText !== undefined) {
        submitter.innerText = "⏳ 처리 중...";
      } else if (submitter.value !== undefined) {
        submitter.value = "⏳ 처리 중...";
      }
    }
  } catch (e) { }
  return true;
}

if (typeof window !== "undefined") {
  window.setLoading = window.setLoading || setLoading;
}

/* ---------- 웹 요약 블럭 정리 ---------- */
function transformWebAnswerBlock() {
  var el = document.getElementById("web-answer-block");
  if (!el) return;

  var raw = el.innerHTML || "";
  if (!raw || !raw.trim()) return;

  var lines = raw.split(/<br\s*\/?>/i);
  var out = [];

  for (var i = 0; i < lines.length; i++) {
    var t = (lines[i] || "").trim();
    if (!t) {
      out.push("");
      continue;
    }

    if (/^\(https?:\/\/[^\)]+\)\s*$/i.test(t)) continue;

    var mA = t.match(
      /^(\d+\.\s*)?\*\*([^*]+)\*\*\s*([^:]+):\s*\[([^\]]+)\]\((https?:\/\/[^\)]+)\)(.*)$/i
    );
    if (mA) {
      var num = mA[1] || "";
      var label = stripTrailingColon(mA[2].trim()) + ": " + mA[3].trim();
      var url = (mA[5] || mA[4]).trim();
      var tail = mA[6] || "";
      out.push(
        escHtml(num) +
        '<a href="' +
        escHtml(url) +
        '" target="_blank" rel="noopener noreferrer" class="src-title">' +
        escHtml(label) +
        "</a>" +
        (tail ? " " + escHtml(tail) : "")
      );
      continue;
    }

    var mB = t.match(
      /^(\d+\.\s*)?\*\*([^*]+)\*\*\s*\[([^\]]+)\]\((https?:\/\/[^\)]+)\)(.*)$/i
    );
    if (mB) {
      var num2 = mB[1] || "";
      var label2 = mB[2].trim();
      var url2 = (mB[4] || mB[3]).trim();
      var tail2 = mB[5] || "";
      out.push(
        escHtml(num2) +
        '<a href="' +
        escHtml(url2) +
        '" target="_blank" rel="noopener noreferrer" class="src-title">' +
        escHtml(stripTrailingColon(label2)) +
        "</a>" +
        (tail2 ? " " + escHtml(tail2) : "")
      );
      continue;
    }

    var mC = t.match(/^(\d+\.\s*)?\*\*([^*]+)\*\*\s*\[([^\]]+)\]\s*$/i);
    if (mC) {
      var num3 = mC[1] || "";
      var srcOnly = mC[2].trim();
      var urlOnly = mC[3].trim();
      out.push(
        escHtml(num3) +
        '<a href="' +
        escHtml(urlOnly) +
        '" target="_blank" rel="noopener noreferrer" class="src-title">' +
        escHtml(stripTrailingColon(srcOnly)) +
        "</a>"
      );
      continue;
    }

    out.push(escHtml(t));
  }

  el.innerHTML = out.join("<br />");
}

/* ---------- DOM 준비 시 : 처음 텍스트 정리 ---------- */
document.addEventListener("DOMContentLoaded", function () {
  cleanLeading("rag-answer-block");
  cleanLeading("web-answer-block");
  transformWebAnswerBlock();
});

/* ============================================================
 *  아래부터는 "웹 검색 / RAG 검색" AJAX + 피드백
 * ============================================================ */
(function () {
  "use strict";

  const log = function (tag, data) {
    try {
      if (typeof window !== "undefined" && typeof window.dglog === "function") {
        window.dglog("NEWS_AJAX " + tag, data);
      } else {
        const ts = new Date().toISOString().slice(11, 23);
        console.log("[news-ajax " + ts + "] " + tag, data ?? "");
      }
    } catch (_) { }
  };

  const $ = (s, r = document) => r.querySelector(s);

  // ---- 공통 POST(JSON) 도우미 ----
  function apiPostJSON(url, payload) {
    const headers = {
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest",
    };
    try {
      const csrftoken =
        typeof getCookie === "function" ? getCookie("csrftoken") : null;
      if (csrftoken) headers["X-CSRFToken"] = csrftoken;
    } catch (_) { }

    try {
      if (
        typeof window !== "undefined" &&
        typeof window.newReqId === "function"
      ) {
        const reqId = window.newReqId("ui");
        if (reqId) headers["X-Request-Id"] = reqId;
      }
    } catch (_) { }

    return fetch(url, {
      method: "POST",
      headers,
      credentials: "same-origin",
      body: JSON.stringify(payload || {}),
    }).then(async (res) => {
      const text = await res.text();
      let json = null;
      try {
        json = JSON.parse(text);
      } catch (_) { }

      if (!res.ok || (json && json.ok === false)) {
        const msg =
          (json && (json.error || json.message || json.detail)) ||
          "HTTP " + res.status;
        const err = new Error(msg);
        err.response = json || text;
        throw err;
      }
      return json || {};
    });
  }

  // ---- 응답에서 자주 쓰는 필드 꺼내기 ----
  function pickAnswer(j) {
    try {
      if (!j) return "";
      const keys = [
        "answer_text",
        "answer",
        "text",
        "reply",
        "result",
        "a",
        "data",
      ];
      for (const k of keys) {
        if (typeof j[k] === "string" && j[k].trim()) return j[k];
      }
    } catch (_) { }
    return "";
  }

  function pickSources(j) {
    try {
      if (!j) return [];
      const cand =
        j.sources || j.web_sources || j.hits || j.docs || j.references || [];
      return Array.isArray(cand) ? cand : [];
    } catch (_) { }
    return [];
  }

  function pickLogId(j) {
    try {
      if (!j) return "";
      const keys = ["log_id", "id", "logId"];
      for (const k of keys) {
        if (j[k] !== undefined && j[k] !== null) return String(j[k]);
      }
    } catch (_) { }
    return "";
  }

  function pickMsg(j) {
    try {
      if (!j) return "";
      return j.msg || j.message || "";
    } catch (_) { }
    return "";
  }

  // ---- 화면에 텍스트 / 출처 목록 넣기 ----
  function renderTextWithBR(target, text) {
    if (!target) return;
    try {
      const html = escHtml(String(text || "")).replace(
        /\r\n|\r|\n/g,
        "<br/>"
      );
      target.innerHTML = html;
    } catch (e) {
      target.textContent = String(text || "");
    }
  }

  function renderSourcesList(containerUl, sources) {
    try {
      if (!containerUl) return;
      const block = containerUl.closest(".sources-block");
      containerUl.innerHTML = "";

      if (!Array.isArray(sources) || sources.length === 0) {
        if (block) block.setAttribute("hidden", "hidden");
        return;
      }
      if (block) block.removeAttribute("hidden");

      sources.forEach((src) => {
        try {
          const li = document.createElement("li");
          const title =
            (src && (src.title || src.name || src.url)) || "(제목 없음)";
          const url = src && src.url;

          if (url) {
            const a = document.createElement("a");
            a.className = "src-title";
            a.href = url;
            a.target = "_blank";
            a.rel = "noopener noreferrer nofollow";
            a.referrerPolicy = "strict-origin-when-cross-origin";
            a.textContent = title || url;
            li.appendChild(a);
          } else {
            const span = document.createElement("span");
            span.className = "src-title";
            span.textContent = title;
            li.appendChild(span);
          }
          containerUl.appendChild(li);
        } catch (_) { }
      });
    } catch (e) {
      log("RENDER_SOURCES_ERR", e && e.message ? e.message : e);
    }
  }

  function restoreSubmitter(ev, form) {
    try {
      const submitter =
        (ev && ev.submitter) ||
        form.querySelector("button[disabled][data-orig-text]");
      if (!submitter) return;
      submitter.disabled = false;
      if (submitter.dataset && submitter.dataset.origText) {
        if (submitter.innerText !== undefined) {
          submitter.innerText = submitter.dataset.origText;
        } else if (submitter.value !== undefined) {
          submitter.value = submitter.dataset.origText;
        }
        delete submitter.dataset.origText;
      }
    } catch (_) { }
  }

  /* ============================================================
   *  Web / RAG 패널 피드백 (애플 스타일 카드 + 이유칩 + 코멘트)
   * ============================================================ */

  function ensureFeedbackShell(area) {
    const blockId = area === "web" ? "web-answer-block" : "rag-answer-block";
    const ansBlock = document.getElementById(blockId);
    if (!ansBlock) return;

    const shellClass = area === "web" ? "fb-shell-web" : "fb-shell-rag";
    let shell = ansBlock.parentNode.querySelector("." + shellClass);

    const titleText =
      area === "web"
        ? "웹에서 정리한 이 답변은 어땠나요?"
        : "자료를 모아 만든 이 답변은 어땠나요?";
    const subText =
      area === "web"
        ? "도움이 되었는지, 어떤 점을 고치면 좋을지 간단히 알려 주세요."
        : "정확도·설명 방식·빠르기 등 어떤 부분이 아쉬웠는지 남겨 주시면 개선에 큰 도움이 됩니다.";

    if (!shell) {
      shell = document.createElement("section");
      shell.className = "fb-shell " + shellClass;
      shell.dataset.area = area;
      shell.dataset.state = "idle";
      shell.dataset.mode = "collapsed";

      shell.innerHTML =
        '<div class="fb-card">' +
        '  <div class="fb-title-row">' +
        '    <span class="fb-dot" aria-hidden="true"></span>' +
        '    <span class="fb-title-text">' +
        escHtml(titleText) +
        "</span>" +
        "  </div>" +
        '  <p class="fb-sub">' +
        escHtml(subText) +
        "</p>" +
        '  <div class="fb-btn-row" role="radiogroup" aria-label="답변 피드백">' +
        '    <button type="button" class="fb-thumb-btn" data-area="' +
        area +
        '" data-kind="good">' +
        '      <span class="emoji">👍</span><span>도움이 되었어요</span>' +
        "    </button>" +
        '    <button type="button" class="fb-thumb-btn" data-area="' +
        area +
        '" data-kind="bad">' +
        '      <span class="emoji">👎</span><span>조금 아쉬웠어요</span>' +
        "    </button>" +
        "  </div>" +
        '  <div class="fb-reason-row">' +
        '    <div class="fb-reason-hint">어떤 점이 아쉬웠나요? (복수 선택 가능)</div>' +
        '    <div class="fb-reason-chips">' +
        '      <button type="button" class="fb-reason-chip" data-reason="wrong">내용이 사실과 달라요</button>' +
        '      <button type="button" class="fb-reason-chip" data-reason="missing">원하는 내용이 빠졌어요</button>' +
        '      <button type="button" class="fb-reason-chip" data-reason="hard">표현이 어렵게 느껴져요</button>' +
        '      <button type="button" class="fb-reason-chip" data-reason="slow">응답이 너무 느렸어요</button>' +
        '      <button type="button" class="fb-reason-chip" data-reason="other">기타</button>' +
        "    </div>" +
        "  </div>" +
        '  <div class="fb-comment-row">' +
        '    <textarea class="fb-comment-input" rows="2" ' +
        '      id="fb-comment-' +
        area +
        '" ' +
        '      name="fb_comment_' +
        area +
        '" ' +
        '      placeholder="편하게 한두 줄만 남겨 주세요. (선택 사항)"></textarea>' +
        '    <div class="fb-comment-actions">' +
        '      <button type="button" class="fb-comment-skip" data-area="' +
        area +
        '">그냥 닫기</button>' +
        '      <button type="button" class="fb-comment-send" data-area="' +
        area +
        '">보내기</button>' +
        "    </div>" +
        "  </div>" +
        '  <div class="fb-thanks" aria-live="polite"></div>' +
        "</div>";

      ansBlock.insertAdjacentElement("afterend", shell);
    }

    resetFeedbackShell(shell);
  }

  function resetFeedbackShell(shell) {
    try {
      shell.dataset.state = "idle";
      shell.dataset.mode = "collapsed";
      shell.dataset.disabled = "";

      // 👍/👎 버튼 다시 보이게
      const btnRow = shell.querySelector(".fb-btn-row");
      if (btnRow) btnRow.style.display = "";

      shell.querySelectorAll(".fb-thumb-btn").forEach(function (btn) {
        btn.disabled = false;
        btn.classList.remove("is-active");
      });

      shell.querySelectorAll(".fb-reason-chip").forEach(function (chip) {
        chip.classList.remove("is-active");
      });

      const reasonRow = shell.querySelector(".fb-reason-row");
      const commentRow = shell.querySelector(".fb-comment-row");
      if (reasonRow) reasonRow.style.display = "none"; // 기본은 숨김
      if (commentRow) commentRow.style.display = "none";

      const ta = shell.querySelector(".fb-comment-input");
      if (ta) ta.value = "";

      const thanks = shell.querySelector(".fb-thanks");
      if (thanks) thanks.textContent = "";
    } catch (e) {
      log("FB_RESET_ERR", e && e.message ? e.message : e);
    }
  }

  function ensureWebFeedbackRow() {
    ensureFeedbackShell("web");
  }

  function ensureRagFeedbackRow() {
    ensureFeedbackShell("rag");
  }

  function collectReasons(shell) {
    const reasons = [];
    shell
      .querySelectorAll(".fb-reason-chip.is-active")
      .forEach(function (chip) {
        const r = chip.getAttribute("data-reason") || chip.textContent.trim();
        if (r) reasons.push(r);
      });
    return reasons;
  }

  function sendFeedbackToApi(area, helpful, reasons, comment, stage, shell) {
    try {
      const blockId = area === "web" ? "web-answer-block" : "rag-answer-block";
      const block = document.getElementById(blockId);
      if (!block) return;

      const question = block.dataset.feedbackQuestion || "";
      const answer = block.dataset.feedbackAnswer || (block.textContent || "");
      let sources = [];
      try {
        if (block.dataset.feedbackSources) {
          const parsed = JSON.parse(block.dataset.feedbackSources);
          if (Array.isArray(parsed)) sources = parsed;
        }
      } catch (_) { }

      const payload = {
        question,
        answer,
        sources,
        answer_type: area === "web" ? "web" : "rag",
        from_ui: area === "web" ? "news_web_panel" : "news_rag_panel",
        helpful: !!helpful,
        reasons: Array.isArray(reasons) ? reasons : [],
        comment: comment || "",
        stage: stage || "thumb",
      };

      const thanks = shell && shell.querySelector(".fb-thanks");
      if (shell) {
        shell.dataset.disabled = "1";
      }

      apiPostJSON("/api/feedback", payload)
        .then(function (j) {
          log("FEEDBACK_OK", j);
          if (!shell) return;
          shell.dataset.state = "sent";
          shell.dataset.disabled = "1";
          if (thanks) {
            thanks.textContent = helpful
              ? "도움이 되었다고 남겨 주셔서 감사합니다."
              : "어떤 점이 아쉬웠는지 알려 주셔서 감사합니다. 다음 답변에 바로 반영해 볼게요.";
          }

          // 전송 후에는 버튼/이유/코멘트는 접어 두기
          const btnRow = shell.querySelector(".fb-btn-row");
          const reasonRow = shell.querySelector(".fb-reason-row");
          const commentRow = shell.querySelector(".fb-comment-row");
          if (btnRow) btnRow.style.display = "none";
          if (reasonRow) reasonRow.style.display = "none";
          if (commentRow) commentRow.style.display = "none";
        })
        .catch(function (err) {
          log("FEEDBACK_SEND_ERR", err && err.message ? err.message : err);
          if (!shell) return;
          shell.dataset.disabled = "";
          shell.dataset.state = "error";
          if (thanks) {
            thanks.textContent =
              "전송 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.";
          }
        });
    } catch (e) {
      log("FEEDBACK_SEND_ERR2", e && e.message ? e.message : e);
      if (shell) shell.dataset.disabled = "";
    }
  }

  function handleFeedbackThumb(btn) {
    const shell = btn.closest(".fb-shell");
    if (!shell || shell.dataset.disabled === "1") return;

    const area = shell.dataset.area || btn.getAttribute("data-area") || "web";
    const kind = btn.getAttribute("data-kind") || "good";
    const helpful = kind === "good";

    shell.querySelectorAll(".fb-thumb-btn").forEach(function (b) {
      b.classList.toggle("is-active", b === btn);
    });

    const reasonRow = shell.querySelector(".fb-reason-row");
    const commentRow = shell.querySelector(".fb-comment-row");

    if (kind === "good") {
      // 👍 일 때는 이유/코멘트 카드는 안 보이게
      shell.dataset.mode = "collapsed";
      if (reasonRow) reasonRow.style.display = "none";
      if (commentRow) commentRow.style.display = "none";

      shell.querySelectorAll(".fb-reason-chip").forEach(function (chip) {
        chip.classList.remove("is-active");
      });
      const ta = shell.querySelector(".fb-comment-input");
      if (ta) ta.value = "";

      sendFeedbackToApi(area, true, [], "", "thumb", shell);
    } else {
      // 👎 일 때만 이유/코멘트 카드 펼치기
      shell.dataset.mode = "detail";
      if (reasonRow) reasonRow.style.display = "";
      if (commentRow) commentRow.style.display = "";
      const ta = shell.querySelector(".fb-comment-input");
      if (ta) {
        setTimeout(function () {
          ta.focus();
        }, 10);
      }
    }
  }

  function toggleReasonChip(chip) {
    const shell = chip.closest(".fb-shell");
    if (!shell || shell.dataset.disabled === "1") return;
    chip.classList.toggle("is-active");
  }

  function handleFeedbackSend(btn) {
    const shell = btn.closest(".fb-shell");
    if (!shell || shell.dataset.disabled === "1") return;

    const area = shell.dataset.area || btn.getAttribute("data-area") || "web";
    const reasons = collectReasons(shell);
    const ta = shell.querySelector(".fb-comment-input");
    const comment = ta ? ta.value.trim() : "";

    sendFeedbackToApi(area, false, reasons, comment, "detail", shell);
  }

  function handleFeedbackSkip(btn) {
    const shell = btn.closest(".fb-shell");
    if (!shell || shell.dataset.disabled === "1") return;

    const area = shell.dataset.area || btn.getAttribute("data-area") || "web";
    const reasons = collectReasons(shell);

    sendFeedbackToApi(area, false, reasons, "", "skip", shell);
  }

  // ---- 피드백 버튼 클릭 핸들러 (이벤트 위임) ----
  document.addEventListener("click", function (ev) {
    const thumb = ev.target.closest(".fb-thumb-btn");
    if (thumb) {
      ev.preventDefault();
      handleFeedbackThumb(thumb);
      return;
    }

    const chip = ev.target.closest(".fb-reason-chip");
    if (chip) {
      ev.preventDefault();
      toggleReasonChip(chip);
      return;
    }

    const sendBtn = ev.target.closest(".fb-comment-send");
    if (sendBtn) {
      ev.preventDefault();
      handleFeedbackSend(sendBtn);
      return;
    }

    const skipBtn = ev.target.closest(".fb-comment-skip");
    if (skipBtn) {
      ev.preventDefault();
      handleFeedbackSkip(skipBtn);
      return;
    }
  });

  // ---- 웹 검색 폼: /api/web_qa ----
  function setupWebForm() {
    try {
      const input = $("#query_web");
      if (!input) return;
      const form = input.closest("form");
      if (!form) return;

      const searchBtn = form.querySelector('button[data-action="web_search"]');
      const ingestBtn = form.querySelector('button[data-action="web_ingest"]');

      if (searchBtn) {
        try {
          searchBtn.onclick = null;
          searchBtn.removeAttribute("onclick");
        } catch (_) { }
      }
      if (ingestBtn) {
        try {
          ingestBtn.onclick = null;
          ingestBtn.removeAttribute("onclick");
        } catch (_) { }
      }

      function runWeb(ev) {
        try {
          if (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
          }
          const query = String(input.value || "").trim();
          if (!query) return;

          const ansBlock = document.getElementById("web-answer-block");
          const card = ansBlock && ansBlock.closest(".card");
          const msgRow = card && card.querySelector(".msg-row");
          const srcUl = document.getElementById("webSourcesList");

          if (msgRow) msgRow.innerHTML = "";
          if (ansBlock)
            renderTextWithBR(
              ansBlock,
              "웹에서 내용을 정리하는 중입니다…"
            );
          if (srcUl) {
            srcUl.innerHTML = "";
            const srcBlock = srcUl.closest(".sources-block");
            if (srcBlock) srcBlock.setAttribute("hidden", "hidden");
          }

          apiPostJSON("/api/web_qa", {
            q: query,
            query: query,
            question: query,
          })
            .then(function (j) {
              const ans = pickAnswer(j) || "(받아온 답이 없습니다.)";
              const srcs = pickSources(j);
              const msg = pickMsg(j);

              if (msgRow) {
                msgRow.innerHTML = msg
                  ? '<div class="msg-ok" role="status">✅ ' +
                  escHtml(msg) +
                  "</div>"
                  : "";
              }
              if (ansBlock) {
                renderTextWithBR(ansBlock, ans);
                ansBlock.dataset.feedbackQuestion = query;
                ansBlock.dataset.feedbackAnswer = ans;
                try {
                  ansBlock.dataset.feedbackSources = JSON.stringify(srcs || []);
                } catch (_) {
                  ansBlock.dataset.feedbackSources = "[]";
                }
              }
              renderSourcesList(srcUl, srcs);
              ensureWebFeedbackRow();
            })
            .catch(function (err) {
              const m =
                (err && err.message) ||
                "웹에서 답을 만드는 동안 문제가 발생했습니다.";
              log("WEB_QA_ERR", m);
              const card2 =
                document.getElementById("web-answer-block")?.closest(".card");
              const msgRow2 = card2 && card2.querySelector(".msg-row");
              if (msgRow2) {
                msgRow2.innerHTML =
                  '<div class="msg-err" role="alert">❌ ' +
                  escHtml(m) +
                  "</div>";
              }
            })
            .finally(function () {
              if (ev) restoreSubmitter(ev, form);
            });
        } catch (e) {
          log("WEB_RUN_ERR", e && e.message ? e.message : e);
        }
      }

      function runWebIngest(ev) {
        try {
          if (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
          }

          const query = String(input.value || "").trim();
          if (!query) {
            alert("먼저 궁금한 내용을 적어 주세요.");
            return;
          }

          const ansBlock = document.getElementById("web-answer-block");
          const answer = ansBlock
            ? String(ansBlock.textContent || "").trim()
            : "";

          if (!answer) {
            alert(
              '먼저 "웹에서 검색"을 눌러 답변을 만든 다음, 저장 버튼을 눌러 주세요.'
            );
            return;
          }

          const srcUl = document.getElementById("webSourcesList");
          const sources = [];
          if (srcUl) {
            srcUl.querySelectorAll("li").forEach(function (li) {
              try {
                const a = li.querySelector("a");
                if (!a) return;
                const url = a.getAttribute("href") || "";
                const title = a.textContent || url;
                if (!url) return;
                sources.push({ url: url, title: title });
              } catch (_) { }
            });
          }

          const card = ansBlock && ansBlock.closest(".card");
          const msgRow = card && card.querySelector(".msg-row");
          if (msgRow) {
            msgRow.innerHTML =
              '<div class="msg-ok" role="status">⏳ 웹에서 찾은 내용을 나중에 다시 쓸 수 있도록 저장하는 중입니다…</div>';
          }

          const payload = {
            question: query,
            answer: answer,
            sources: sources,
            answer_type: "web",
            from_ui: "news_web_panel",
          };

          apiPostJSON("/api/rag/upsert", payload)
            .then(function (j) {
              const msg =
                (j && (j.msg || j.message)) ||
                "웹에서 찾은 내용을 잘 저장해 두었습니다.";
              if (msgRow) {
                msgRow.innerHTML =
                  '<div class="msg-ok" role="status">✅ ' +
                  escHtml(msg) +
                  "</div>";
              }
            })
            .catch(function (err) {
              const m =
                (err && err.message) ||
                "웹에서 찾은 내용을 저장하는 동안 문제가 발생했습니다.";
              log("WEB_INGEST_ERR", m);
              if (msgRow) {
                msgRow.innerHTML =
                  '<div class="msg-err" role="alert">❌ ' +
                  escHtml(m) +
                  "</div>";
              }
            })
            .finally(function () {
              if (ev) restoreSubmitter(ev, form);
            });
        } catch (e) {
          log("WEB_INGEST_RUN_ERR", e && e.message ? e.message : e);
        }
      }

      if (searchBtn) {
        searchBtn.addEventListener("click", function (ev) {
          try {
            const hidden = form.querySelector('input[name="action"]');
            if (hidden) hidden.value = "web_search";
          } catch (_) { }
          try {
            if (typeof setLoading === "function") setLoading(form);
          } catch (_) { }
          runWeb(ev);
        });
      }

      if (ingestBtn) {
        ingestBtn.addEventListener("click", function (ev) {
          try {
            const hidden = form.querySelector('input[name="action"]');
            if (hidden) hidden.value = "web_ingest";
          } catch (_) { }
          try {
            if (typeof setLoading === "function") setLoading(form);
          } catch (_) { }
          runWebIngest(ev);
        });
      }

      form.addEventListener("submit", function (ev) {
        try {
          const hidden = form.querySelector('input[name="action"]');
          const action = (hidden && hidden.value) || "web_search";
          const query = String(input.value || "").trim();

          if (action === "web_search" && query) {
            runWeb(ev);
          }
        } catch (e) {
          log("WEB_FORM_HANDLER_ERR", e && e.message ? e.message : e);
        }
      });
    } catch (e) {
      log("WEB_FORM_SETUP_ERR", e && e.message ? e.message : e);
    }
  }

  // ---- RAG 검색 폼: /api/rag_qa ----
  function setupRagForm() {
    try {
      const input = document.querySelector('input[name="query_rag"]');
      if (!input) return;
      const form = input.closest("form");
      if (!form) return;

      const ragBtn = form.querySelector('button[data-action="rag_search"]');
      const seedBtn = form.querySelector('button[data-action="rag_seed"]');
      const resetBtn = form.querySelector('button[data-action="rag_reset"]');

      [ragBtn, seedBtn, resetBtn].forEach(function (btn) {
        if (!btn) return;
        try {
          btn.onclick = null;
          btn.removeAttribute("onclick");
        } catch (_) { }
      });

      function runRag(ev) {
        try {
          if (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
          }
          const query = String(input.value || "").trim();
          if (!query) return;

          const msgRow = document.getElementById("rag-msg-block");
          const ansBlock = document.getElementById("rag-answer-block");

          if (msgRow) msgRow.innerHTML = "";
          if (ansBlock)
            renderTextWithBR(
              ansBlock,
              "자료를 모아서 답을 만드는 중입니다…"
            );

          apiPostJSON("/api/rag_qa", {
            q: query,
            query: query,
            question: query,
          })
            .then(function (j) {
              const ans = pickAnswer(j) || "(받아온 답이 없습니다.)";
              const srcs = pickSources(j);
              const msg = pickMsg(j);

              if (msgRow) {
                msgRow.innerHTML = msg
                  ? '<div class="msg-ok" role="status">✅ ' +
                  escHtml(msg) +
                  "</div>"
                  : "";
              }
              if (ansBlock) {
                renderTextWithBR(ansBlock, ans);
                ansBlock.dataset.feedbackQuestion = query;
                ansBlock.dataset.feedbackAnswer = ans;
                try {
                  ansBlock.dataset.feedbackSources = JSON.stringify(srcs || []);
                } catch (_) {
                  ansBlock.dataset.feedbackSources = "[]";
                }
              }

              ensureRagFeedbackRow();
            })
            .catch(function (err) {
              const m =
                (err && err.message) || "답을 만드는 동안 문제가 발생했습니다.";
              log("RAG_QA_ERR", m);
              const msgRow2 = document.getElementById("rag-msg-block");
              if (msgRow2) {
                msgRow2.innerHTML =
                  '<div class="msg-err" role="alert">❌ ' +
                  escHtml(m) +
                  "</div>";
              }
            })
            .finally(function () {
              if (ev) restoreSubmitter(ev, form);
            });
        } catch (e) {
          log("RAG_RUN_ERR", e && e.message ? e.message : e);
        }
      }

      function runRagSeed(ev) {
        try {
          if (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
          }

          const query = String(input.value || "").trim();
          const msgRow = document.getElementById("rag-msg-block");
          const ansBlock = document.getElementById("rag-answer-block");

          if (msgRow) {
            msgRow.innerHTML =
              '<div class="msg-ok" role="status">⏳ 기본 자료를 채워 넣는 중입니다…</div>';
          }
          if (ansBlock) {
            renderTextWithBR(
              ansBlock,
              "기본 자료를 채워 넣고 있습니다. 잠시만 기다려 주세요…"
            );
          }

          const params = new URLSearchParams();
          params.set("from_ui", "news_rag_panel");
          if (query) params.set("last_query", query);

          const url = "/api/rag/seed?" + params.toString();

          fetch(url, {
            method: "GET",
            credentials: "same-origin",
            headers: { "X-Requested-With": "XMLHttpRequest" },
          })
            .then(async (res) => {
              const text = await res.text();
              let j = null;
              try {
                j = JSON.parse(text);
              } catch (_) { }

              if (!res.ok || (j && j.ok === false)) {
                const msgErr =
                  (j && (j.error || j.message || j.detail)) ||
                  text ||
                  "기본 자료를 채우는 중에 문제가 발생했습니다.";
                const err = new Error(msgErr);
                err.response = j || text;
                throw err;
              }

              const msg =
                (j && (j.msg || j.message)) || "기본 자료 채우기가 끝났습니다.";
              if (msgRow) {
                msgRow.innerHTML =
                  '<div class="msg-ok" role="status">✅ ' +
                  escHtml(msg) +
                  "</div>";
              }
              if (ansBlock) {
                renderTextWithBR(
                  ansBlock,
                  "기본 자료 채우기가 끝났습니다. 이제 검색 창에서 잘 나오는지 시험해 보세요!"
                );
              }
            })
            .catch(function (err) {
              const m =
                (err && err.message) ||
                "기본 자료를 채우는 중에 문제가 발생했습니다.";
              log("RAG_SEED_ERR", m);
              if (msgRow) {
                msgRow.innerHTML =
                  '<div class="msg-err" role="alert">❌ ' +
                  escHtml(m) +
                  "</div>";
              }
            })
            .finally(function () {
              if (ev) restoreSubmitter(ev, form);
            });
        } catch (e) {
          log("RAG_SEED_RUN_ERR", e && e.message ? e.message : e);
        }
      }

      if (seedBtn) {
        seedBtn.addEventListener("click", function (ev) {
          try {
            const hidden = form.querySelector('input[name="action"]');
            if (hidden) hidden.value = "rag_seed";
          } catch (_) { }
          try {
            if (typeof setLoading === "function") setLoading(form);
          } catch (_) { }
          runRagSeed(ev);
        });
      }

      if (ragBtn) {
        ragBtn.addEventListener("click", function (ev) {
          try {
            const hidden = form.querySelector('input[name="action"]');
            if (hidden) hidden.value = "rag_search";
          } catch (_) { }
          try {
            if (typeof setLoading === "function") setLoading(form);
          } catch (_) { }
          runRag(ev);
        });
      }

      if (resetBtn) {
        resetBtn.addEventListener("click", function (ev) {
          try {
            ev.preventDefault();
            ev.stopPropagation();
            if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
          } catch (_) { }

          try {
            const hidden = form.querySelector('input[name="action"]');
            if (hidden) hidden.value = "rag_reset";
          } catch (_) { }

          try {
            if (typeof setLoading === "function") setLoading(form);
          } catch (_) { }

          try {
            form.submit();
          } catch (e) {
            log("RAG_RESET_SUBMIT_ERR", e && e.message ? e.message : e);
          }
        });
      }

      form.addEventListener("submit", function (ev) {
        try {
          const hidden = form.querySelector('input[name="action"]');
          const action = (hidden && hidden.value) || "rag_search";
          const query = String(input.value || "").trim();

          if (action === "rag_search" && query) {
            runRag(ev);
          }
        } catch (e) {
          log("RAG_FORM_HANDLER_ERR", e && e.message ? e.message : e);
        }
      });
    } catch (e) {
      log("RAG_FORM_SETUP_ERR", e && e.message ? e.message : e);
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    try {
      setupWebForm();
      setupRagForm();
      log("INIT_DONE", {});
    } catch (e) {
      log("DOM_READY_ERR", e && e.message ? e.message : e);
    }
  });
})();

/* ============================================================
 *  새 화면 전용 JS: 테스터/법무 푸터 보정 (구형 템플릿 호환)
 * ============================================================ */
(function () {
  "use strict";

  function prettifyTesterAndLegal() {
    // 이미 새 푸터 구조를 쓰는 템플릿이면 건드리지 않음
    if (
      document.querySelector(".page-footer-wrap") ||
      document.querySelector(".legal-footer-bar")
    )
      return;

    // 구형 템플릿 호환: "테스터 고지"로 시작하는 <p>, 그리고 "개인정보처리방침" 포함하는 <p> 찾기
    var allPs = document.querySelectorAll(
      "body > p, body > div > p, body > section > p"
    );
    var testerP = null;
    var legalP = null;

    allPs.forEach(function (p) {
      var text = (p.textContent || "").trim();
      if (!testerP && text.indexOf("테스터 고지") === 0) testerP = p;
      if (!legalP && text.indexOf("개인정보처리방침") !== -1) legalP = p;
    });

    if (!testerP && !legalP) return;

    var wrap = document.createElement("section");
    wrap.className = "page-footer-wrap";

    if (testerP) {
      var notice = document.createElement("p");
      notice.className = "tester-notice";
      var html = testerP.innerHTML.replace(/^\s*테스터 고지\s*/i, "");
      notice.innerHTML = html;
      wrap.appendChild(notice);
    }

    if (legalP) {
      var bar = document.createElement("div");
      bar.className = "legal-footer-bar";
      // 템플릿에서 a 태그들이 들어있다고 가정 (구형은 단순 텍스트+링크 mix)
      bar.innerHTML = legalP.innerHTML;
      wrap.appendChild(bar);
    }

    // 배치: testerP 위치에 wrap을 꽂고, 원본 p는 제거
    if (testerP) testerP.replaceWith(wrap);
    else if (legalP) legalP.replaceWith(wrap);

    if (testerP) {
      try {
        testerP.remove();
      } catch (_) { }
    }
    if (legalP) {
      try {
        legalP.remove();
      } catch (_) { }
    }
  }

  function ready(fn) {
    if (document.readyState === "loading")
      document.addEventListener("DOMContentLoaded", fn);
    else fn();
  }

  ready(function () {
    try {
      prettifyTesterAndLegal();
    } catch (e) {
      console.error("[footer layout]", e && e.message ? e.message : e);
    }
  });
})();
