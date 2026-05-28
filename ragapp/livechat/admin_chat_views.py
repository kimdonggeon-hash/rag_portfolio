# ragapp/livechat/admin_chat_views.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from django.apps import apps
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Max
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone


def _model_fields(model) -> set[str]:
    out: set[str] = set()
    try:
        for f in model._meta.get_fields():
            n = getattr(f, "name", None)
            a = getattr(f, "attname", None)
            if n:
                out.add(str(n))
            if a:
                out.add(str(a))
    except Exception:
        pass
    return out


def _order_messages(qs, M) -> Any:
    mf = _model_fields(M)
    if "created_at" in mf:
        return qs.order_by("created_at", "id")
    return qs.order_by("id")


def _get_models():
    S = apps.get_model("ragapp", "LiveChatSession")
    # message model: retention 모델이 있으면 우선
    try:
        M = apps.get_model("ragapp", "LiveChatMessage")
    except Exception:
        # 혹시 app label 다르면 여기만 맞춰주면 됨
        M = apps.get_model("ragapp", "LiveChatMessage")
    return S, M


@staff_member_required
def admin_livechat_rooms(request: HttpRequest) -> HttpResponse:
    """
    ✅ Room 테이블을 보지 않고 '세션'에서 room_id를 뽑아 목록을 만든다.
    """
    S, _M = _get_models()

    sf = _model_fields(S)
    room_field = "room" if "room" in sf else None
    if not room_field:
        raise Http404("LiveChatSession.room field not found")

    # 최근 room 목록 (세션 id 기준)
    rows = (
        S.objects.values(room_field)
        .annotate(last_sid=Max("id"))
        .order_by("-last_sid")[:300]
    )

    rooms: List[Dict[str, Any]] = []
    for r in rows:
        rooms.append({
            "room_id": r.get(room_field) or "",
            "last_sid": r.get("last_sid"),
        })

    return render(request, "ragapp/ragadmin/livechat_rooms.html", {
        "rooms": rooms,
        "now": timezone.now(),
    })


@staff_member_required
def admin_livechat_room_detail(request: HttpRequest, room_id: str) -> HttpResponse:
    """
    ✅ '대화보기'는 무조건 Session+Message로 표시한다.
    - LiveChatRoom이 없어도 된다.
    - sid 파라미터가 있으면 그 세션, 없으면 최신 세션.
    """
    S, M = _get_models()

    sf = _model_fields(S)
    if "room" not in sf:
        raise Http404("LiveChatSession.room field not found")

    sessions = list(S.objects.filter(room=room_id).order_by("-id")[:50])
    if not sessions:
        # 세션이 없으면 메시지를 보여줄 수가 없음(네 구조상 message가 session_id FK)
        return render(request, "ragapp/ragadmin/livechat_room_detail.html", {
            "room_id": room_id,
            "session": None,
            "sessions": [],
            "messages": [],
            "note": "세션이 없어 대화를 표시할 수 없습니다.",
        })

    sid_q = request.GET.get("sid")
    session: Any = None
    if sid_q and sid_q.isdigit():
        for s in sessions:
            if int(getattr(s, "id", 0)) == int(sid_q):
                session = s
                break
    if session is None:
        session = sessions[0]  # 최신

    mf = _model_fields(M)
    qs = M.objects.all()

    # message가 session FK로 연결되는 방식에 맞춰 필터
    if "session_id" in mf:
        qs = qs.filter(session_id=getattr(session, "id"))
    elif "session" in mf:
        qs = qs.filter(session=session)
    elif "room" in mf:
        # 혹시 room 필드가 있는 스키마면 이것도 fallback
        qs = qs.filter(room=room_id)
    else:
        qs = qs.none()

    qs = _order_messages(qs, M)

    # 어드민 표시에 필요한 최소 형태로 변환 (PII 로그 금지지만, 화면에는 원문 표시가 목적)
    messages: List[Dict[str, Any]] = []
    for m in qs[:2000]:
        d = {"id": getattr(m, "id", None)}
        if "role" in mf:
            d["role"] = getattr(m, "role", "")
        elif "sender" in mf:
            d["role"] = getattr(m, "sender", "")
        else:
            d["role"] = ""

        if "content" in mf:
            d["text"] = getattr(m, "content", "")
        elif "text" in mf:
            d["text"] = getattr(m, "text", "")
        elif "body" in mf:
            d["text"] = getattr(m, "body", "")
        elif "message" in mf:
            d["text"] = getattr(m, "message", "")
        else:
            d["text"] = ""

        if "created_at" in mf:
            d["created_at"] = getattr(m, "created_at", None)
        messages.append(d)

    return render(request, "ragapp/ragadmin/livechat_room_detail.html", {
        "room_id": room_id,
        "session": session,
        "sessions": sessions,
        "messages": messages,
        "note": "",
    })
