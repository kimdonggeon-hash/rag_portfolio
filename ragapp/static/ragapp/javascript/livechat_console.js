// ragapp/static/ragapp/javascript/livechat_console.js
(function () {
    "use strict";

    if (!("WebSocket" in window)) {
        console.warn("[livechat] WebSocket not supported");
        return;
    }

    function $(id) {
        return document.getElementById(id);
    }

    const dom = {
        // 기본 콘솔 영역
        sessionList: $("lc-session-list"),
        messages: $("lc-messages"),
        chatForm: $("lc-chat-form"),
        input: $("lc-input"),
        connectBtn: $("lc-connect-btn"),
        nextBtn: $("lc-next-btn"),          // '다음 상담 받기' 버튼 (있으면 사용)
        sendBtn: $("lc-send-btn"),
        endBtn: $("lc-end-btn"),
        refreshBtn: $("lc-refresh-btn"),
        roomTitle: $("lc-room-title"),
        roomSub: $("lc-room-sub"),
        roomMeta: $("lc-room-meta"),
        masterDot: $("lc-master-dot"),
        masterStatus: $("lc-master-status-text"),
        incomingBanner: $("lc-incoming-banner"),
        incomingRoom: $("lc-incoming-room"),
        incomingPage: $("lc-incoming-page"),
        incomingAccept: $("lc-incoming-accept"),
        incomingLater: $("lc-incoming-later"),

        // ✅ 상담 기록 패널 (HTML에 맞게)
        recordType: $("lc-record-type"),
        recordNote: $("lc-record-summary"),        // 한 줄 요약
        recordDetail: $("lc-record-detail"),       // 상세 메모
        recordStatusText: $("lc-record-status-text"),
        recordStatusBadge: $("lc-record-status-badge"),
        saveBtn: $("lc-save-record-btn"),
        clearBtn: $("lc-clear-record-btn"),
    };

    const INITIAL = window.LIVECHAT_INITIAL || { initialRoom: "master", sessions: [] };
    const CONFIG = window.LIVECHAT_CONFIG || {};

    const loc = window.location;
    const wsScheme = loc.protocol === "https:" ? "wss" : "ws";
    const wsBase = wsScheme + "://" + loc.host;

    let masterSocket = null;
    let roomSocket = null;

    // 세션 / 상태 관리
    let sessions = (INITIAL.sessions || []).slice();
    let selectedRoom = null;
    let selectedSessionId = null;
    let connectedRoom = null;
    let connectedSessionId = null;
    let pendingIncoming = null;
    let warnedNotConnected = false;

    // 이미 종료된 방 목록 (재연결 방지)
    const endedRooms = new Set();

    // ✅ 상담사가 연결되었을 때 자동 인사를 이미 보낸 방
    const welcomeSentRooms = new Set();

    // ─────────────────────────────────────
    //  공통 유틸
    // ─────────────────────────────────────
    function setMasterStatus(connected) {
        if (!dom.masterDot || !dom.masterStatus) return;
        if (connected) {
            dom.masterDot.style.background = "#22c55e";
            dom.masterDot.style.boxShadow = "0 0 12px rgba(34,197,94,.7)";
            dom.masterStatus.textContent = "상담사 콘솔 WebSocket 연결됨";
        } else {
            dom.masterDot.style.background = "#6b7280";
            dom.masterDot.style.boxShadow = "none";
            dom.masterStatus.textContent = "상담사 연결 준비 중…";
        }
    }

    function setChatLocked(locked, reason) {
        if (dom.input) {
            dom.input.disabled = locked;
            dom.input.placeholder = locked
                ? (reason || "왼쪽에서 방을 선택하고 '연결하기'를 누르면 메시지를 보낼 수 있습니다.")
                : "사용자에게 보낼 메시지를 입력하세요. (Enter=전송, Shift+Enter=줄바꿈)";
        }
        if (dom.sendBtn) dom.sendBtn.disabled = locked;
        if (dom.endBtn) dom.endBtn.disabled = locked;
    }

    function updateConnectButtonState() {
        if (!dom.connectBtn) return;

        if (!selectedRoom) {
            dom.connectBtn.disabled = true;
            dom.connectBtn.textContent = "연결할 방 선택";
            return;
        }

        if (endedRooms.has(selectedRoom)) {
            dom.connectBtn.disabled = true;
            dom.connectBtn.textContent = "종료된 상담";
            return;
        }

        dom.connectBtn.disabled = false;

        if (
            connectedRoom &&
            connectedRoom === selectedRoom &&
            roomSocket &&
            roomSocket.readyState === WebSocket.OPEN
        ) {
            dom.connectBtn.textContent = "연결됨";
        } else {
            dom.connectBtn.textContent = "연결하기";
        }
    }

    function statusLabel(status) {
        const s = (status || "").toLowerCase();
        if (!s || s === "waiting" || s === "대기" || s === "pending") return "대기중";
        if (s === "active") return "진행중";
        if (s === "ended_need_save" || s === "need_note") return "저장 필요";
        if (s === "saved" || s === "done") return "저장 완료";
        if (
            s === "ended" ||
            s === "종료" ||
            s === "closed" ||
            s === "deleted" ||
            s === "삭제됨"
        ) {
            return "종료됨";
        }
        return status;
    }

    function getCookie(name) {
        if (typeof document === "undefined") return null;
        const value = ("; " + document.cookie).split("; " + name + "=");
        if (value.length === 2) return value.pop().split(";").shift();
        return null;
    }

    function getCsrfToken() {
        if (CONFIG.csrfToken) return CONFIG.csrfToken;
        return getCookie("csrftoken");
    }

    // ─────────────────────────────────────
    //  상담 기록 패널 유틸
    // ─────────────────────────────────────
    function setRecordStatus(type, message) {
        // 상태 설명 텍스트
        if (dom.recordStatusText) {
            dom.recordStatusText.textContent = message || "";
            dom.recordStatusText.className = "lc-record-status";
            if (type === "success") dom.recordStatusText.classList.add("is-success");
            else if (type === "error") dom.recordStatusText.classList.add("is-error");
            else if (type === "info") dom.recordStatusText.classList.add("is-info");
        }

        // 상단 배지 텍스트
        if (dom.recordStatusBadge) {
            if (type === "success") {
                dom.recordStatusBadge.textContent = "저장 완료";
            } else if (type === "error") {
                dom.recordStatusBadge.textContent = "오류";
            } else {
                dom.recordStatusBadge.textContent = "저장 전";
            }
        }
    }

    function resetRecordFormForSession(session) {
        if (dom.recordType) dom.recordType.value = "";
        if (dom.recordNote) dom.recordNote.value = "";
        if (dom.recordDetail) dom.recordDetail.value = "";

        if (!session) {
            setRecordStatus(
                "info",
                "왼쪽에서 상담을 선택하면 이곳에 상담 기록을 남길 수 있습니다."
            );
        } else {
            setRecordStatus(
                "info",
                "상담을 마친 뒤, 요약과 메모를 남기고 '상담기록 저장'을 눌러 주세요."
            );
        }
    }

    // ─────────────────────────────────────
    //  세션 관리
    // ─────────────────────────────────────
    function upsertSessionFromPayload(payload) {
        if (!payload) return;
        const sid = payload.session_id;
        const room = payload.room;

        if (!room && !sid) return;

        let found = null;
        for (let i = 0; i < sessions.length; i++) {
            const s = sessions[i];
            if ((sid && s.id === sid) || (room && s.room === room)) {
                found = s;
                break;
            }
        }

        if (!found) {
            found = {
                id: sid || null,
                room: room || "",
                status: payload.status || "waiting",
                page_title: payload.page && payload.page.title ? payload.page.title : "",
                page_path: payload.page && payload.page.path ? payload.page.path : ""
            };
            sessions.unshift(found);
        } else {
            if (payload.status) found.status = payload.status;
            if (payload.page && payload.page.title) found.page_title = payload.page.title;
            if (payload.page && payload.page.path) found.page_path = payload.page.path;
        }

        const label = statusLabel(found.status);
        if (label === "종료됨" || label === "저장 필요" || label === "저장 완료") {
            endedRooms.add(found.room);
        }

        renderSessionList();
        return found;
    }

    function renderSessionList() {
        if (!dom.sessionList) return;

        dom.sessionList.innerHTML = "";
        if (!sessions.length) {
            const li = document.createElement("li");
            li.className = "lc-session-empty";
            li.textContent = "현재 표시할 상담 요청이 없습니다.";
            dom.sessionList.appendChild(li);
            return;
        }

        sessions.sort(function (a, b) {
            if (!a.id || !b.id) return 0;
            return b.id - a.id;
        });

        sessions.forEach(function (s) {
            const li = document.createElement("li");
            li.className = "lc-session-item";
            if (s.room === selectedRoom) li.classList.add("is-active");
            li.dataset.room = s.room || "";
            li.dataset.sessionId = s.id || "";

            const main = document.createElement("div");
            main.className = "lc-session-main";

            const title = document.createElement("div");
            title.className = "lc-session-title";
            title.textContent = s.page_title || s.page_path || "(페이지 정보 없음)";

            const meta = document.createElement("div");
            meta.className = "lc-session-meta";
            meta.textContent = "room=" + (s.room || "?") + (s.id ? " · #" + s.id : "");

            main.appendChild(title);
            main.appendChild(meta);

            const right = document.createElement("div");
            right.className = "lc-session-right";

            const chip = document.createElement("button");
            chip.type = "button";
            chip.className = "lc-chip-btn";
            chip.textContent = statusLabel(s.status);
            right.appendChild(chip);

            li.appendChild(main);
            li.appendChild(right);

            dom.sessionList.appendChild(li);
        });
    }

    function findSession(room, sessionId) {
        let sid = sessionId;
        if (!sid && room) {
            for (let i = 0; i < sessions.length; i++) {
                if (sessions[i].room === room) return sessions[i];
            }
        }
        if (sid) {
            for (let i = 0; i < sessions.length; i++) {
                if (sessions[i].id === sid) return sessions[i];
            }
        }
        return null;
    }

    function updateRoomHeaderForSession(session) {
        if (!dom.roomTitle || !dom.roomSub || !dom.roomMeta) return;

        if (!session) {
            dom.roomTitle.textContent = "선택된 방 없음";
            dom.roomSub.textContent =
                "왼쪽에서 세션을 선택하고, 위 인입 배너나 아래 '연결하기' 버튼을 눌러 상담을 시작하세요.";
            dom.roomMeta.textContent = "";
            return;
        }

        dom.roomTitle.textContent =
            session.page_title || session.page_path || ("room " + (session.room || "?"));
        dom.roomSub.textContent = "room=" + (session.room || "?") + " · 세션 #" + (session.id || "?");
        dom.roomMeta.textContent = "상태: " + statusLabel(session.status || "");
    }

    // ─────────────────────────────────────
    //  메시지 렌더
    // ─────────────────────────────────────
    function appendChatMessage(src) {
        if (!dom.messages) return;
        const role = (src.sender || src.role || "system").toLowerCase();
        const text = src.text || src.content || "";

        const wrap = document.createElement("div");
        wrap.classList.add("lc-msg");

        if (role === "user") wrap.classList.add("lc-msg-user");
        else if (role === "operator") wrap.classList.add("lc-msg-operator");
        else wrap.classList.add("lc-msg-system");

        const bubble = document.createElement("div");
        bubble.className = "lc-msg-bubble";
        bubble.textContent = text;

        wrap.appendChild(bubble);
        dom.messages.appendChild(wrap);
        dom.messages.scrollTop = dom.messages.scrollHeight;
    }

    function clearMessages() {
        if (dom.messages) dom.messages.innerHTML = "";
    }

    // 인입 배너
    function showIncomingBanner(session) {
        if (!dom.incomingBanner || !dom.incomingRoom || !dom.incomingPage) return;
        pendingIncoming = session;

        dom.incomingRoom.textContent = "room " + (session.room || "?");
        const desc = session.page_title || session.page_path || "";
        dom.incomingPage.textContent = desc ? " · " + desc : "";
        dom.incomingBanner.classList.add("is-visible");
    }

    function hideIncomingBanner() {
        pendingIncoming = null;
        if (dom.incomingBanner) {
            dom.incomingBanner.classList.remove("is-visible");
        }
    }

    function markRoomEnded(room) {
        if (!room) return;
        endedRooms.add(room);
        setChatLocked(true, "이 방은 상담이 종료되었습니다.");
        if (dom.roomMeta && selectedRoom === room) {
            dom.roomMeta.textContent =
                "종료됨 · room=" + room + (selectedSessionId ? " · #" + selectedSessionId : "");
        }
        const s = findSession(room, null);
        if (s) s.status = "ended_need_save";
        renderSessionList();
        updateConnectButtonState();
    }

    // ─────────────────────────────────────
    //  히스토리 로드
    // ─────────────────────────────────────
    function loadHistory(room, sessionId) {
        if (!CONFIG.historyUrl) return;
        const params = new URLSearchParams();
        if (sessionId) params.set("session_id", String(sessionId));
        else if (room) params.set("room", room);
        params.set("limit", "200");

        const url = CONFIG.historyUrl + "?" + params.toString();

        fetch(url, {
            method: "GET",
            headers: { Accept: "application/json" }
        })
            .then(function (res) { return res.json(); })
            .then(function (data) {
                clearMessages();
                if (!data || !Array.isArray(data.messages)) return;
                data.messages.forEach(function (m) {
                    appendChatMessage({
                        role: m.role,
                        content: m.content,
                        created_at: m.created_at
                    });
                });
            })
            .catch(function (err) {
                console.warn("[livechat] history load error", err);
            });
    }

    // ─────────────────────────────────────
    //  master WebSocket
    // ─────────────────────────────────────
    function connectMaster() {
        const url = wsBase + "/ws/chat/master";
        masterSocket = new WebSocket(url);

        masterSocket.onopen = function () {
            setMasterStatus(true);
        };

        masterSocket.onclose = function () {
            setMasterStatus(false);
        };

        masterSocket.onerror = function () {
            console.warn("[livechat] master socket error");
        };

        masterSocket.onmessage = function (event) {
            let payload;
            try {
                payload = JSON.parse(event.data || "{}");
            } catch (e) {
                console.warn("[livechat] invalid master payload", e);
                return;
            }
            handleMasterPayload(payload);
        };
    }

    function handleMasterPayload(payload) {
        if (!payload || !payload.type) return;
        const t = String(payload.type).toLowerCase();

        if (t === "session_created") {
            const s = upsertSessionFromPayload(payload);
            if (s) showIncomingBanner(s);
            return;
        }

        if (t === "session_started" || t === "session_ended" || t === "session_closed") {
            const s = upsertSessionFromPayload(payload);
            if (s && t !== "session_started") {
                markRoomEnded(s.room);
            }
            return;
        }

        if (t === "session_assigned") {
            upsertSessionFromPayload(payload);
            return;
        }

        if (t === "session_saved") {
            const sid = payload.session_id || null;
            const room = payload.room || null;
            const s = findSession(room, sid);
            if (s) {
                s.status = payload.status || "saved";
                const label = statusLabel(s.status);
                if (label === "종료됨" || label === "저장 필요" || label === "저장 완료") {
                    endedRooms.add(s.room);
                }
                renderSessionList();
                if (selectedRoom === s.room &&
                    (!selectedSessionId || selectedSessionId === s.id)) {
                    updateRoomHeaderForSession(s);
                }
            }
            return;
        }
    }

    // ─────────────────────────────────────
    //  room WebSocket
    // ─────────────────────────────────────

    // ✅ 상담사가 방에 연결되었을 때 자동 인사 메세지 전송
    function sendOperatorConnectedMessage() {
        if (!roomSocket || roomSocket.readyState !== WebSocket.OPEN || !connectedRoom) {
            return;
        }

        // 같은 방에는 한 번만 자동 전송
        if (welcomeSentRooms.has(connectedRoom)) return;
        welcomeSentRooms.add(connectedRoom);

        const text =
            "상담사가 연결되었습니다. 안녕하세요, 김동건 포트폴리오 실시간 상담입니다. 무엇을 도와드릴까요?";

        const payload = {
            sender: "operator",
            type: "message",
            text: text,
            room: connectedRoom,
            session_id: connectedSessionId
        };

        try {
            roomSocket.send(JSON.stringify(payload));
        } catch (e) {
            console.warn("[livechat] send welcome error", e);
        }
    }

    function disconnectRoomSocket() {
        if (roomSocket) {
            try { roomSocket.close(1000, "operator switch room"); } catch (e) { }
        }
        roomSocket = null;
        connectedRoom = null;
        connectedSessionId = null;
        setChatLocked(true, "이 방과 아직 연결되지 않았습니다. '연결하기'를 눌러주세요.");
        updateConnectButtonState();
    }

    function connectSelectedRoom() {
        if (!selectedRoom) {
            alert("먼저 왼쪽에서 방을 선택해 주세요.");
            return;
        }
        if (endedRooms.has(selectedRoom)) {
            alert("이미 종료된 상담입니다.");
            return;
        }

        if (
            roomSocket &&
            roomSocket.readyState === WebSocket.OPEN &&
            connectedRoom === selectedRoom
        ) {
            return;
        }

        disconnectRoomSocket();

        const url = wsBase + "/ws/chat/" + encodeURIComponent(selectedRoom);
        roomSocket = new WebSocket(url);

        roomSocket.onopen = function () {
            connectedRoom = selectedRoom;
            connectedSessionId = selectedSessionId || null;
            warnedNotConnected = false;
            setChatLocked(false);
            updateConnectButtonState();
            if (dom.roomMeta) {
                dom.roomMeta.textContent =
                    "연결됨 · room=" + connectedRoom + (connectedSessionId ? " · #" + connectedSessionId : "");
            }

            // ✅ 상담사가 연결된 직후 자동 안내 메시지 전송
            sendOperatorConnectedMessage();
        };

        roomSocket.onclose = function () {
            if (connectedRoom === selectedRoom) {
                connectedRoom = null;
                connectedSessionId = null;
            }
            if (!endedRooms.has(selectedRoom)) {
                setChatLocked(true, "이 방과의 연결이 끊어졌습니다. 다시 연결하려면 '연결하기'를 눌러 주세요.");
            }
            updateConnectButtonState();
        };

        roomSocket.onerror = function () {
            console.warn("[livechat] room socket error");
        };

        roomSocket.onmessage = function (event) {
            let payload;
            try {
                payload = JSON.parse(event.data || "{}");
            } catch (e) {
                console.warn("[livechat] invalid room payload", e);
                return;
            }
            handleRoomPayload(payload);
        };
    }

    function handleRoomPayload(payload) {
        if (!payload) return;

        const pType = (payload.type || "").toLowerCase();
        const txt = payload.text || payload.content || "";

        appendChatMessage(payload);

        const isEndType = pType === "end" || pType === "closed" || pType === "close";
        const isEndText = txt.indexOf("상담을 종료했습니다.") === 0;

        if (isEndType || isEndText) {
            const room = payload.room || connectedRoom || selectedRoom;
            markRoomEnded(room);
        }
    }

    function canSend() {
        return (
            roomSocket &&
            roomSocket.readyState === WebSocket.OPEN &&
            connectedRoom &&
            selectedRoom &&
            connectedRoom === selectedRoom &&
            !endedRooms.has(connectedRoom)
        );
    }

    // ─────────────────────────────────────
    //  '다음 상담 받기'
    // ─────────────────────────────────────
    function takeNextSession() {
        if (!dom.nextBtn || !CONFIG.nextUrl) {
            // 버튼이 없거나 API 경로가 없으면 기능 비활성
            return;
        }

        const csrf = getCsrfToken();
        const headers = {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest"
        };
        if (csrf) headers["X-CSRFToken"] = csrf;

        fetch(CONFIG.nextUrl, {
            method: "POST",
            headers: headers,
            body: JSON.stringify({})
        })
            .then(function (res) {
                return res
                    .json()
                    .catch(function () { return {}; })
                    .then(function (data) {
                        return { httpOk: res.ok, status: res.status, data: data || {} };
                    });
            })
            .then(function (resObj) {
                const data = resObj.data || {};

                if (!resObj.httpOk || data.ok === false) {
                    const reason = data.reason || "";
                    if (reason === "NEED_NOTE") {
                        alert(data.message || "이전 상담에 대한 상담 기록을 먼저 저장해 주세요.");
                        return;
                    }
                    if (reason === "NO_WAITING") {
                        alert(data.message || "현재 대기 중인 상담이 없습니다.");
                        return;
                    }
                    alert(data.message || "다음 상담을 가져오는 중 오류가 발생했습니다.");
                    return;
                }

                const room = data.room || null;
                const sid = data.session_id || null;
                if (!room) {
                    alert("다음 상담 정보를 불러오지 못했습니다.");
                    return;
                }

                let s = findSession(room, sid);
                if (!s) {
                    s = {
                        id: sid,
                        room: room,
                        status: "active",
                        page_title: "",
                        page_path: ""
                    };
                    sessions.unshift(s);
                } else {
                    s.status = "active";
                    endedRooms.delete(room);
                }

                selectedRoom = room;
                selectedSessionId = sid || null;

                renderSessionList();

                if (dom.sessionList && selectedRoom) {
                    const items = dom.sessionList.querySelectorAll(".lc-session-item");
                    items.forEach(function (li) {
                        if (li.dataset.room === selectedRoom) li.classList.add("is-active");
                        else li.classList.remove("is-active");
                    });
                }

                updateRoomHeaderForSession(s);
                loadHistory(selectedRoom, selectedSessionId);
                resetRecordFormForSession(s);
                connectSelectedRoom();
            })
            .catch(function (err) {
                console.warn("[livechat] next-session api error", err);
                alert("다음 상담을 가져오는 중 오류가 발생했습니다.");
            });
    }

    // ─────────────────────────────────────
    //  이벤트 바인딩
    // ─────────────────────────────────────
    function bindSessionListClick() {
        if (!dom.sessionList) return;

        dom.sessionList.addEventListener("click", function (ev) {
            const li = ev.target.closest(".lc-session-item");
            if (!li) return;
            const room = li.dataset.room || "";
            const sidRaw = li.dataset.sessionId || "";
            const sid = sidRaw ? parseInt(sidRaw, 10) : null;

            selectedRoom = room || null;
            selectedSessionId = sid || null;
            warnedNotConnected = false;

            const all = dom.sessionList.querySelectorAll(".lc-session-item");
            all.forEach(function (el) {
                if (el === li) el.classList.add("is-active");
                else el.classList.remove("is-active");
            });

            const session = findSession(selectedRoom, selectedSessionId);
            updateRoomHeaderForSession(session);
            loadHistory(selectedRoom, selectedSessionId);
            resetRecordFormForSession(session);

            if (
                connectedRoom &&
                connectedRoom === selectedRoom &&
                roomSocket &&
                roomSocket.readyState === WebSocket.OPEN &&
                !endedRooms.has(selectedRoom)
            ) {
                setChatLocked(false);
            } else {
                const msg = endedRooms.has(selectedRoom)
                    ? "이 방은 이미 종료되었습니다."
                    : "이 방과 아직 연결되지 않았습니다. '연결하기' 버튼을 눌러 주세요.";
                setChatLocked(true, msg);
            }
            updateConnectButtonState();
        });
    }

    function bindIncomingBannerButtons() {
        if (dom.incomingAccept) {
            dom.incomingAccept.addEventListener("click", function () {
                if (!pendingIncoming) {
                    hideIncomingBanner();
                    return;
                }
                selectedRoom = pendingIncoming.room || null;
                selectedSessionId = pendingIncoming.id || null;
                hideIncomingBanner();

                renderSessionList();
                if (dom.sessionList && selectedRoom) {
                    const items = dom.sessionList.querySelectorAll(".lc-session-item");
                    items.forEach(function (li) {
                        if (li.dataset.room === selectedRoom) li.classList.add("is-active");
                        else li.classList.remove("is-active");
                    });
                }

                const session = findSession(selectedRoom, selectedSessionId);
                updateRoomHeaderForSession(session);
                loadHistory(selectedRoom, selectedSessionId);
                resetRecordFormForSession(session);

                const msg = endedRooms.has(selectedRoom)
                    ? "이 방은 이미 종료되었습니다."
                    : "이 방과 아직 연결되지 않았습니다. '연결하기' 버튼을 눌러 주세요.";
                setChatLocked(true, msg);
                updateConnectButtonState();
                if (!endedRooms.has(selectedRoom)) {
                    connectSelectedRoom();
                }
            });
        }

        if (dom.incomingLater) {
            dom.incomingLater.addEventListener("click", function () {
                hideIncomingBanner();
            });
        }
    }

    function bindConnectButton() {
        if (!dom.connectBtn) return;
        dom.connectBtn.addEventListener("click", function () {
            connectSelectedRoom();
        });
    }

    function bindNextButton() {
        if (!dom.nextBtn) return;
        dom.nextBtn.addEventListener("click", function () {
            takeNextSession();
        });
    }

    function bindRefreshButton() {
        if (!dom.refreshBtn) return;
        dom.refreshBtn.addEventListener("click", function () {
            window.location.reload();
        });
    }

    function bindQuickTemplates() {
        const container = document.querySelector(".lc-quick-row");
        if (!container || !dom.input) return;

        container.addEventListener("click", function (ev) {
            const btn = ev.target.closest(".lc-template-btn");
            if (!btn) return;
            const tpl = btn.getAttribute("data-template") || "";
            if (!tpl) return;

            const cur = dom.input.value || "";
            dom.input.value = cur ? cur.replace(/\s*$/, "") + "\n" + tpl : tpl;
            dom.input.focus();
        });
    }

    function bindChatForm() {
        if (!dom.chatForm || !dom.input) return;

        dom.chatForm.addEventListener("submit", function (ev) {
            ev.preventDefault();
            if (!canSend()) {
                if (!warnedNotConnected) {
                    appendChatMessage({
                        sender: "system",
                        text: endedRooms.has(selectedRoom || connectedRoom)
                            ? "이미 종료된 상담입니다."
                            : "아직 이 방과 연결되지 않았습니다. 아래 '연결하기' 버튼을 먼저 눌러 주세요."
                    });
                    warnedNotConnected = true;
                }
                return;
            }

            const text = (dom.input.value || "").trim();
            if (!text) return;

            const payload = {
                sender: "operator",
                type: "message",
                text: text,
                room: connectedRoom,
                session_id: connectedSessionId
            };

            try {
                roomSocket.send(JSON.stringify(payload));
            } catch (e) {
                console.warn("[livechat] send error", e);
            }

            dom.input.value = "";
            warnedNotConnected = false;
        });

        dom.input.addEventListener("keydown", function (ev) {
            if (ev.key === "Enter" && !ev.shiftKey) {
                ev.preventDefault();
                dom.chatForm.dispatchEvent(new Event("submit", { cancelable: true }));
            }
        });
    }

    // ─────────────────────────────────────
    //  상담 종료 버튼
    // ─────────────────────────────────────
    function bindEndButton() {
        if (!dom.endBtn) return;

        dom.endBtn.addEventListener("click", function () {
            if (!connectedRoom) {
                alert("연결된 상담 방이 없습니다.");
                return;
            }
            if (!CONFIG.endUrl) {
                alert("종료 API 경로가 설정되지 않았습니다.");
                return;
            }

            const reason =
                "상담을 종료했습니다. 추가로 궁금한 점이 생기면 언제든지 질문 챗봇이나 실시간 상담을 이용해 주세요.";

            const csrf = getCsrfToken();
            const headers = {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            };
            if (csrf) headers["X-CSRFToken"] = csrf;

            // ✅ 1) 고객 화면으로 실시간 종료 이벤트 전송
            if (roomSocket && roomSocket.readyState === WebSocket.OPEN) {
                try {
                    roomSocket.send(JSON.stringify({
                        sender: "operator",
                        type: "end",
                        text: reason,
                        room: connectedRoom,
                        session_id: connectedSessionId
                    }));
                } catch (e) {
                    console.warn("[livechat] end socket send error", e);
                }
            }

            // ✅ 2) 서버 DB 상태 종료 처리
            fetch(CONFIG.endUrl, {
                method: "POST",
                headers: headers,
                body: JSON.stringify({
                    session_id: connectedSessionId,
                    room: connectedRoom,
                    text: reason,
                }),
            }).catch(function (err) {
                console.warn("[livechat] end api error", err);
            });

            // ✅ 3) 상담사 화면 종료 처리
            markRoomEnded(connectedRoom);
        });
    }

    // ─────────────────────────────────────
    //  상담 기록 저장 (버튼 + 내용 초기화)
    // ─────────────────────────────────────
    function bindSaveButton() {
        if (!dom.saveBtn) {
            console.warn("[livechat] save button not found (lc-save-record-btn)");
            return;
        }

        function doSave() {
            if (!CONFIG.saveUrl) {
                alert("상담 기록 저장 API 경로가 설정되지 않았습니다.");
                return;
            }

            const room = selectedRoom || connectedRoom;
            const sid = selectedSessionId || connectedSessionId;

            if (!room && !sid) {
                alert("먼저 왼쪽에서 상담 세션을 선택해 주세요.");
                return;
            }

            const type = dom.recordType ? (dom.recordType.value || "") : "";
            const note = dom.recordNote ? (dom.recordNote.value || "") : "";
            const detail = dom.recordDetail ? (dom.recordDetail.value || "") : "";

            if (!type && !note && !detail) {
                const ok = window.confirm("입력된 내용이 없습니다. 그래도 저장하시겠습니까?");
                if (!ok) return;
            }

            const payload = {
                session_id: sid,
                room: room,
                session_type: type,
                session_note: note,
                session_detail: detail
            };

            const csrf = getCsrfToken();
            const headers = {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest"
            };
            if (csrf) headers["X-CSRFToken"] = csrf;

            dom.saveBtn.disabled = true;
            setRecordStatus("info", "저장 중입니다…");

            fetch(CONFIG.saveUrl, {
                method: "POST",
                headers: headers,
                body: JSON.stringify(payload)
            })
                .then(function (res) {
                    return res
                        .json()
                        .catch(function () { return {}; })
                        .then(function (data) {
                            return { httpOk: res.ok, status: res.status, data: data || {} };
                        });
                })
                .then(function (resObj) {
                    const data = resObj.data || {};

                    if (!resObj.httpOk || data.ok === false) {
                        const msg = data.message || data.error || "저장에 실패했습니다.";
                        setRecordStatus("error", msg);
                        return;
                    }

                    const s = room ? findSession(room, sid) : null;
                    if (s) {
                        s.status = data.session_status || data.status_after || "saved";
                        const label = statusLabel(s.status);
                        if (label === "종료됨" || label === "저장 필요" || label === "저장 완료") {
                            endedRooms.add(s.room);
                        }
                        renderSessionList();
                        updateRoomHeaderForSession(s);
                    }

                    setRecordStatus("success", data.message || "상담 기록을 저장했습니다.");
                })
                .catch(function (err) {
                    console.warn("[livechat] save api error", err);
                    setRecordStatus("error", "저장 중 오류가 발생했습니다.");
                })
                .finally(function () {
                    dom.saveBtn.disabled = false;
                });
        }

        dom.saveBtn.addEventListener("click", function (ev) {
            ev.preventDefault();
            doSave();
        });

        if (dom.clearBtn) {
            dom.clearBtn.addEventListener("click", function () {
                if (dom.recordType) dom.recordType.value = "";
                if (dom.recordNote) dom.recordNote.value = "";
                if (dom.recordDetail) dom.recordDetail.value = "";
                setRecordStatus("info", "입력 내용을 모두 지웠습니다.");
            });
        }
    }

    // ─────────────────────────────────────
    //  초기화
    // ─────────────────────────────────────
    function init() {
        renderSessionList();
        connectMaster();

        setChatLocked(true, "왼쪽에서 방을 선택하고, '연결하기'를 눌러 주세요.");
        updateConnectButtonState();
        resetRecordFormForSession(null);

        bindSessionListClick();
        bindIncomingBannerButtons();
        bindConnectButton();
        bindNextButton();
        bindRefreshButton();
        bindQuickTemplates();
        bindChatForm();
        bindEndButton();
        bindSaveButton();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
