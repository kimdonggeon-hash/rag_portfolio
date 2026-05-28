/* ragapp/static/ragapp/javascript/livechat_client.js */
/* QARAG ↔ 실시간 상담 콘솔 WebSocket 클라이언트 (브라우저 측)
   리뉴얼 버전:
   - 뉴스/QARAG 페이지: 상담은 "세션 생성 + /c/<token> 리다이렉트"만 담당 (WS X)
   - /c/<token>/ 상담 전용 페이지: 여기에서만 WebSocket 사용
*/

(function () {
    "use strict";

    try {
        // ─────────────────────────────────────────────────────────────
        // 공통 유틸
        // ─────────────────────────────────────────────────────────────
        const log = (tag, data) => {
            try {
                if (typeof window.dglog === "function") {
                    window.dglog(tag, data);
                } else {
                    const ts = new Date().toISOString().slice(11, 23);
                    console.log(`[livechat ${ts}] ${tag}`, data ?? "");
                }
            } catch (_) { }
        };

        const $ = (sel, root = document) => root.querySelector(sel);

        function getCookie(name) {
            let cookieValue = null;
            if (document.cookie && document.cookie !== "") {
                const cookies = document.cookie.split(";");
                for (let i = 0; i < cookies.length; i++) {
                    const cookie = cookies[i].trim();
                    if (cookie.substring(0, name.length + 1) === name + "=") {
                        cookieValue = decodeURIComponent(
                            cookie.substring(name.length + 1)
                        );
                        break;
                    }
                }
            }
            return cookieValue;
        }

        // WS payload 안전 파서 (/c/<token>/ 용)
        function safeParse(raw) {
            try {
                let j = typeof raw === "string" ? JSON.parse(raw) : raw || {};
                if (j && typeof j === "object") {
                    for (const k of ["message", "msg", "data", "text"]) {
                        if (typeof j[k] === "string") {
                            try {
                                const jj = JSON.parse(j[k]);
                                if (jj && typeof jj === "object") {
                                    j = Object.assign({}, j, jj);
                                }
                            } catch (_) { }
                        }
                    }
                }
                return j;
            } catch (_) {
                return { sender: "system", text: String(raw ?? "") };
            }
        }

        // 공용 말풍선 렌더러 (QARAG / /c/<token>/ 둘 다 지원)
        function pushMsg(role, text) {
            try {
                if (typeof window.__qaragAddMsg === "function") {
                    return window.__qaragAddMsg(role, text);
                }
            } catch (_) { }

            let box =
                document.querySelector("#qaragMessages") ||
                document.querySelector("#lc-messages");
            if (!box) return;

            const isUser = role === "user";
            const wrap = document.createElement("div");

            if (box.id === "qaragMessages") {
                wrap.className =
                    "qarag-msgwrap " + (isUser ? "user" : "bot");
                const div = document.createElement("div");
                div.className = "qarag-msg " + (isUser ? "user" : "bot");
                div.textContent = String(text || "");
                wrap.appendChild(div);
            } else {
                wrap.className =
                    "lc-line " + (isUser ? "lc-user" : "lc-bot");
                wrap.textContent = String(text || "");
            }

            box.appendChild(wrap);
            box.scrollTop = box.scrollHeight;
        }

        // 상담 가능 여부 체크
        function _getAvailabilityUrl() {
            try {
                const ds =
                    document.body && document.body.dataset
                        ? document.body.dataset
                        : {};
                // 우선순위: data-* 속성 → 기본값은 status API
                return (
                    ds.availabilityUrl ||
                    ds.livechatAvailabilityUrl ||
                    "/api/livechat/status/"
                );
            } catch (_) {
                return "/api/livechat/status/";
            }
        }
        async function checkLivechatAvailability(options) {
            const opts = options || {};
            const url = _getAvailabilityUrl();

            try {
                const res = await fetch(url, {
                    method: "GET",
                    headers: { "X-Requested-With": "XMLHttpRequest" },
                    credentials: "same-origin",
                });

                if (!res.ok) {
                    return {
                        ok: false,
                        available: true,
                        status: res.status,
                        message: "availability_http_" + res.status,
                    };
                }

                const ct = (res.headers.get("Content-Type") || "").toLowerCase();

                if (ct.includes("application/json")) {
                    const data = await res.json().catch(() => null);
                    if (!data) {
                        return {
                            ok: false,
                            available: true,
                            message: "availability_bad_json",
                        };
                    }
                    const available =
                        typeof data.available === "boolean"
                            ? data.available
                            : typeof data.is_available === "boolean"
                                ? data.is_available
                                : true;
                    return {
                        ok: data.ok !== false,
                        available,
                        message: data.message || data.reason || "",
                        raw: data,
                    };
                }

                // json이 아니면 문자열 기준으로 아주 보수적으로만 막기
                const text = (await res.text().catch(() => "")).toLowerCase();
                const blocked =
                    text.includes("false") ||
                    text.includes("off") ||
                    text.includes("0") ||
                    text.includes("unavailable");
                return { ok: true, available: !blocked, message: "" };
            } catch (e) {
                if (!opts.silent) {
                    console.warn(
                        "[livechat_client] availability check error:",
                        e
                    );
                }
                return {
                    ok: false,
                    available: true,
                    message: "availability_fetch_error",
                };
            }
        }

        // QARAG 페이지에서만 사용하는 세션 생성 API
        async function requestLiveChatSessionFromQarag() {
            const url = "/api/livechat/request/";

            const payload = {
                from: "qarag",
                page: { title: document.title, path: location.pathname },
            };

            const headers = { "Content-Type": "application/json" };
            const csrftoken = getCookie("csrftoken");
            if (csrftoken) headers["X-CSRFToken"] = csrftoken;

            try {
                const resp = await fetch(url, {
                    method: "POST",
                    headers,
                    body: JSON.stringify(payload),
                    credentials: "same-origin", // ✅ 쿠키/CSRF 흐름 안정화
                });

                if (!resp.ok) {
                    // ✅ PII 차단(400) 등: 서버 JSON 메시지를 우선 보여주기
                    let data = null;
                    try { data = await resp.json(); } catch (_) { }

                    const serverMsg =
                        (data && (data.error || data.message)) ||
                        "";

                    const msg =
                        (resp.status === 400 && serverMsg)
                            ? serverMsg
                            : "상담사 연결 요청 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.";

                    log("REQ_HTTP_ERR", { status: resp.status, msg });
                    pushMsg("bot", msg);
                    return null;
                }

                const data = await resp.json().catch(() => null);
                if (!data || data.ok === false) {
                    const msg = (data && (data.error || data.message)) ||
                        "상담사 연결 요청에 실패했습니다. 잠시 후 다시 시도해 주세요.";
                    pushMsg("bot", msg);
                    return null;
                }

                const sessionId =
                    typeof data.session_id !== "undefined"
                        ? data.session_id
                        : null;
                const room = (data && data.room) || null;
                const redirectUrl =
                    (data && (data.redirect_url || data.redirectUrl)) || null;

                return { sessionId, room, redirectUrl };
            } catch (err) {
                log("REQ_FETCH_ERR", err);
                pushMsg(
                    "bot",
                    "상담사 연결 요청 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
                );
                return null;
            }
        }

        // ─────────────────────────────────────────────────────────────
        // 1) 뉴스/QARAG 페이지 모드
        //    - QARAG는 RAG/FAQ만 담당, 상담은 /c/<token>/로 넘김
        // ─────────────────────────────────────────────────────────────
        function initQaragMode() {
            const btnConnectLive = $("#btnConnectLive");
            const btnEndLive = $("#btnEndLive"); // 이 페이지에서는 안 씀
            const overlay = $("#livechatOverlay");
            const agreeBtn = $("#livechatAgreeBtn");
            const cancelBtn = $("#livechatCancelBtn");

            if (!btnConnectLive) {
                log("QARAG_MODE_NO_BUTTON", null);
                return;
            }

            // QARAG 페이지에서는 "상담 종료" 버튼은 숨겨두기만 함
            if (btnEndLive) {
                btnEndLive.hidden = true;
                btnEndLive.disabled = true;
                btnEndLive.style.display = "none";
            }

            btnConnectLive.disabled = false;

            // 상담사 연결 버튼 클릭 → 동의 모달만 띄움
            btnConnectLive.addEventListener("click", function () {
                if (overlay) {
                    overlay.removeAttribute("hidden");
                } else {
                    const ok = window.confirm(
                        "욕설·폭언·성희롱 등은 상담 중단 및 서비스 제한 사유가 될 수 있습니다.\n\n위 안내를 읽고 동의하시면 [확인]을 눌러 주세요."
                    );
                    if (ok) handleAgreeFlow();
                }
            });

            // 동의 모달: 취소 → 그냥 QARAG 계속 사용
            if (cancelBtn) {
                cancelBtn.addEventListener("click", function () {
                    if (overlay) overlay.setAttribute("hidden", "true");

                    try {
                        pushMsg(
                            "bot",
                            "실시간 상담 연결 요청을 취소했어요.\n\n질문 챗봇을 계속 이용하실 수 있습니다."
                        );
                    } catch (_) { }
                });
            }

            // 동의 모달: 동의 버튼 → 실제 세션 생성 + 리다이렉트
            if (agreeBtn) {
                agreeBtn.addEventListener("click", function () {
                    handleAgreeFlow();
                });
            }

            async function handleAgreeFlow() {
                if (overlay) overlay.setAttribute("hidden", "true");

                // 상담 가능 여부 먼저 확인
                const avail = await checkLivechatAvailability({ silent: true });
                const isAvailable =
                    typeof avail === "boolean"
                        ? avail
                        : (avail && typeof avail.available === "boolean"
                            ? avail.available
                            : true);

                if (!isAvailable) {
                    const msg = (avail && avail.message
                        ? String(avail.message).trim()
                        : "") || "현재 상담이 어려운 상태입니다. 잠시 후 다시 시도해 주세요.";
                    pushMsg("bot", msg);
                    if (btnConnectLive) btnConnectLive.disabled = false;
                    return;
                }

                if (btnConnectLive) btnConnectLive.disabled = true;

                const result = await requestLiveChatSessionFromQarag();
                if (!result) {
                    if (btnConnectLive) btnConnectLive.disabled = false;
                    return;
                }

                const { room, redirectUrl } = result;

                let url = redirectUrl;
                if (!url) {
                    const token = room || "";
                    url = `/c/${encodeURIComponent(token)}/`;
                }

                window.location.href = url;
            }

            log("QARAG_MODE_READY", null);
        }

        // ─────────────────────────────────────────────────────────────
        // 2) /c/<token>/ 상담 전용 클라이언트 페이지 모드
        //    - 여기서만 WebSocket 열어서 상담사와 대화
        // ─────────────────────────────────────────────────────────────
        function initClientPageMode() {
            const msgBox = $("#lc-messages");
            const inputBox = $("#lc-input");
            const sendBtn = $("#lc-sendBtn") || $("#lc-send");
            const endBtn = $("#lc-endBtn") || $("#lc-end");

            if (!msgBox) {
                log("CLIENT_MODE_NO_MESSAGES_BOX", null);
                return;
            }

            const LC = window.__LIVECHAT_CONFIG__ || {};
            const room = LC.roomToken || LC.room || null;
            const sessionId = LC.sessionId || null;

            if (!room) {
                log("CLIENT_MODE_NO_ROOM_TOKEN", LC);
                pushMsg(
                    "bot",
                    "상담 세션 정보가 올바르지 않습니다. 다시 접속해 주세요."
                );
                return;
            }

            const scheme = location.protocol === "https:" ? "wss" : "ws";
            const wsUrl = `${scheme}://${location.host}/ws/chat/${encodeURIComponent(
                room
            )}`;

            let ws = null;
            let manuallyClosed = false;
            let ended = false; // ✅ 한번 종료되면 true

            // ✅ blocked(PII/욕설/성희롱) 되면 입력 복구용
            let lastDraft = "";
            let lastDraftAt = 0;

            function lockEnded(reasonText, opts) {
                ended = true;
                manuallyClosed = true; // 종료 후에는 재접속 금지

                const reason =
                    reasonText ||
                    "상담이 종료되었습니다. 새 상담이 필요하면 다시 요청해 주세요.";

                if (inputBox) {
                    inputBox.disabled = true;
                    inputBox.placeholder = reason;
                }
                if (sendBtn) sendBtn.disabled = true;
                if (endBtn) endBtn.disabled = true;

                // ✅ 이미 end 메시지를 화면에 그린 뒤라면 silent로 중복 출력 방지
                if (!(opts && opts.silent)) {
                    pushMsg("bot", reason);
                }
            }

            function connect() {
                try {
                    ws = new WebSocket(wsUrl);
                } catch (e) {
                    log("CLIENT_WS_NEW_ERR", e);
                    pushMsg(
                        "bot",
                        "상담 연결에 실패했습니다. 잠시 후 다시 접속해 주세요."
                    );
                    return;
                }

                ws.onopen = function () {
                    log("CLIENT_WS_OPEN", { wsUrl, room, sessionId });
                };

                ws.onmessage = function (ev) {
                    try {
                        const data = safeParse(ev.data);

                        const pType = String(data.type || "message").toLowerCase();
                        const sender = String(data.role || data.sender || "").toLowerCase();
                        const text = String(data.text || data.message || data.msg || "");

                        // ✅ 1) blocked 먼저 처리 (서버가 user 메시지 브로드캐스트를 안 하는 케이스)
                        if (pType === "blocked") {
                            // 입력값 복구(최근에 보낸 문장이라면)
                            if (inputBox && lastDraft && Date.now() - lastDraftAt < 8000) {
                                inputBox.value = lastDraft;
                                try { inputBox.focus(); } catch (_) { }
                            }
                            lastDraft = "";
                            lastDraftAt = 0;

                            if (text) pushMsg("bot", text);
                            return;
                        }

                        // ✅ 2) end 타입이면(텍스트 없어도) 종료 처리
                        const isEndType = (pType === "end" || pType === "close" || pType === "closed");
                        if (ended && isEndType) {
                            // 이미 종료 잠금 상태면 중복 출력/처리 방지
                            return;
                        }

                        // 텍스트 없으면 일반 메시지는 무시(단, end는 위에서 처리)
                        if (!text && !isEndType) return;

                        // 일반 렌더
                        if (text) {
                            if (sender === "user") pushMsg("user", text);
                            else pushMsg("bot", text);
                        }

                        // ✅ 3) 종료 텍스트 휴리스틱 (서버 end_text 기반)
                        const isEndText =
                            text.startsWith("상담을 종료했습니다.") ||
                            text.includes("상담이 종료되었습니다") ||
                            text.includes("정책 위반") ||
                            text.includes("[사용자]가 상담을 종료했습니다") ||
                            text.includes("[상담사]가 상담을 종료했습니다");

                        if (!ended && (isEndType || isEndText)) {
                            // 방금 end 메시지를 이미 그렸으니 silent로 잠금만
                            lockEnded(
                                "상담이 종료되었습니다. 새 상담이 필요하면 다시 요청해 주세요.",
                                { silent: true }
                            );
                            try {
                                if (ws && ws.readyState === WebSocket.OPEN) {
                                    ws.close(1000, "ended");
                                }
                            } catch (_) { }
                        }
                    } catch (e) {
                        log("CLIENT_WS_MSG_ERR", e);
                    }
                };

                ws.onclose = function (ev) {
                    log("CLIENT_WS_CLOSE", {
                        code: ev.code,
                        clean: ev.wasClean,
                    });
                    // 종료 후에는 자동 재연결 안 함
                    if (!manuallyClosed && !ended && ev.code !== 1000) {
                        setTimeout(connect, 1500);
                    }
                };

                ws.onerror = function (err) {
                    log("CLIENT_WS_ERR", err);
                };
            }

            function sendUserText(text) {
                const msg = String(text || "").trim();
                if (!msg) return false;

                if (ended) {
                    pushMsg("bot", "이미 종료된 상담입니다. 새 상담이 필요하면 다시 요청해 주세요.");
                    return false;
                }

                if (!ws || ws.readyState !== WebSocket.OPEN) {
                    pushMsg("bot", "연결이 아직 준비되지 않았습니다. 잠시 후 다시 시도해 주세요.");
                    return false;
                }

                const payload = {
                    sender: "user",
                    text: msg,
                    ts: Date.now(),
                    session_id: sessionId || null,
                };

                try {
                    // ✅ blocked되면 복구할 수 있게 draft 저장
                    lastDraft = msg;
                    lastDraftAt = Date.now();

                    ws.send(JSON.stringify(payload));
                    return true;
                } catch (e) {
                    log("CLIENT_WS_SEND_ERR", e);
                    return false;
                }
            }

            connect();

            if (sendBtn && inputBox) {
                sendBtn.addEventListener("click", function () {
                    if (sendUserText(inputBox.value)) {
                        inputBox.value = "";
                    }
                });
                inputBox.addEventListener("keydown", function (e) {
                    if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        if (sendUserText(inputBox.value)) {
                            inputBox.value = "";
                        }
                    }
                });
            }

            if (endBtn) {
                endBtn.addEventListener("click", async function () {
                    if (ended) return; // 이미 종료 처리됨

                    if (!window.confirm("상담을 종료하시겠습니까?")) return;

                    try {
                        manuallyClosed = true;
                        if (ws && ws.readyState === WebSocket.OPEN) {
                            ws.send(
                                JSON.stringify({
                                    sender: "user",
                                    type: "end",
                                    text: "[사용자]가 상담을 종료했습니다.",
                                    ts: Date.now(),
                                    session_id: sessionId || null,
                                })
                            );
                            ws.close(1000, "user_end");
                        }
                    } catch (e) {
                        log("CLIENT_END_SEND_ERR", e);
                    }

                    // ✅ 백엔드 종료 API도 호출 (고객 전용 endpoint 기본값)
                    const endUrl =
                        LC.apiEnd ||
                        `/api/livechat/client/${encodeURIComponent(room)}/end/`;

                    try {
                        await fetch(endUrl, {
                            method: "POST",
                            credentials: "same-origin", // ✅ 쿠키/CSRF 흐름 안정화
                            headers: {
                                "Content-Type": "application/json",
                                "X-CSRFToken": getCookie("csrftoken") || "",
                            },
                            body: JSON.stringify({
                                session_id: sessionId,
                                room: room,
                            }),
                        });
                    } catch (_) { }

                    // ✅ 로컬에서도 바로 잠그기
                    lockEnded("상담이 종료되었습니다. 이용해 주셔서 감사합니다.", { silent: false });
                });
            }

            // 클라이언트 페이지에서 쓸 수 있도록 브리지 제공
            window.LiveChatClient = {
                sendToOperator: sendUserText,
                endFromUser: function (reasonText) {
                    try {
                        if (ended) return;
                        const text =
                            reasonText ||
                            "[사용자]가 상담을 종료했습니다.";

                        if (ws && ws.readyState === WebSocket.OPEN) {
                            ws.send(
                                JSON.stringify({
                                    sender: "user",
                                    type: "end",
                                    text: text,
                                    ts: Date.now(),
                                    session_id: sessionId || null,
                                })
                            );
                            manuallyClosed = true;
                            ws.close(1000, "user_end_bridge");
                        }
                    } catch (e) {
                        log("CLIENT_END_BRIDGE_ERR", e);
                    }
                    lockEnded(
                        "상담이 종료되었습니다. 이용해 주셔서 감사합니다."
                    );
                },
            };

            // sendLiveChatText 호환 (혹시 기존 코드가 쓰고 있으면)
            if (typeof window !== "undefined") {
                window.sendLiveChatText =
                    window.sendLiveChatText ||
                    function (txt) {
                        sendUserText(txt);
                    };
            }

            log("CLIENT_MODE_READY", { room, sessionId });
        }

        // ─────────────────────────────────────────────────────────────
        // 초기화: 페이지 상황에 따라 모드 분기
        // ─────────────────────────────────────────────────────────────
        document.addEventListener("DOMContentLoaded", function () {
            const hasQarag = !!document.getElementById("qaragPanel");
            const hasClientBox = !!document.getElementById("lc-messages");

            log("LIVECHAT_BOOT", {
                hasQarag,
                hasClientBox,
            });

            if (hasClientBox) {
                // /c/<token>/ 상담 전용 페이지
                initClientPageMode();
            } else if (hasQarag) {
                // 뉴스/QARAG 메인 페이지
                initQaragMode();

                // QARAG 모드에서는 LiveChatClient가 필요 없지만
                // 혹시 참조해도 에러 안 나게 더미 제공
                if (!window.LiveChatClient) {
                    window.LiveChatClient = {
                        sendToOperator: function () { },
                        endFromUser: function () { },
                    };
                }
                if (typeof window !== "undefined") {
                    window.sendLiveChatText =
                        window.sendLiveChatText ||
                        function () {
                            /* no-op */
                        };
                }
            } else {
                log("LIVECHAT_NO_UI", null);
            }
        });
    } catch (e) {
        try {
            if (typeof window.dglog === "function") {
                window.dglog("LIVECHAT_FATAL", e);
            } else {
                console.error("[livechat fatal]", e);
            }
        } catch (_) { }
    }
})();
