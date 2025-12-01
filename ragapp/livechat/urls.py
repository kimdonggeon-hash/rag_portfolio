# ragapp/livechat/urls.py
from django.urls import path
from ragapp.livechat import agent_api
from . import views

app_name = "livechat"

urlpatterns = [
    # 🔹 상담 전용 클라이언트 페이지 (사용자용)
    #    /c/<room>/ → client_room.html 렌더링
    path("c/<slug:room>/", views.livechat_client_room_view, name="client_page"),

    # 🔹 운영자 콘솔 화면
    path("ragadmin/live-chat/", views.live_chat_view, name="console"),

    # 🔹 운영자 AJAX (최근 세션 / 정리)
    path(
        "ragadmin/live-chat/recent-sessions/",
        views.api_livechat_recent_sessions,
        name="admin_recent_sessions",
    ),
   
    # 🔹 상담 한 건 전체 로그 보기
    path(
        "ragadmin/live-chat/session/<int:session_id>/",
        views.livechat_session_log_view,
        name="session_log",
    ),

    path(
        "ragadmin/live-chat/cleanup/",
        views.live_chat_cleanup_view,
        name="admin_cleanup",
    ),
    path(
        "ragadmin/live-chat/save-session/",
        views.live_chat_save_session_view,
        name="legacy_admin_save_session",
    ),

    # 🔹 Canonical API (엔드유저/콘솔 공통)
    path(
        "api/livechat/request/",
        views.api_livechat_request,
        name="api_livechat_request",
    ),
    path(
        "api/livechat/history/",
        views.api_livechat_history,
        name="api_livechat_history",
    ),
    path(
        "api/livechat/end/",
        views.api_livechat_end,
        name="api_livechat_end",
    ),
    path(
        "api/livechat/availability/",
        views.livechat_availability_api,
        name="api_livechat_availability",
    ),
    path(
        "api/livechat/save-session/",
        views.live_chat_save_session_view,
        name="api_livechat_save_session",
    ),
    path(
        "api/livechat/recent-sessions/",
        views.api_livechat_recent_sessions,
        name="api_livechat_recent_sessions",
    ),
    path(
        "api/livechat/next/",
        views.api_livechat_next,          # ✅ JS에서 쓰는 nextUrl
        name="api_livechat_next",
    ),

    # 🔹 새 상담 기록 저장 API (콘솔 우측 “상담 기록 저장” 버튼)
    path(
        "api/save/",
        views.api_livechat_save,
        name="api_livechat_save",
    ),

    # 🔹 Legacy 최소 호환
    path(
        "api/request/",
        views.api_livechat_request,
        name="legacy_api_request",
    ),
    path(
        "api/end/",
        views.api_livechat_end,
        name="legacy_api_end",
    ),
    path(
        "ragadmin/live-chat/recent/",
        views.api_livechat_recent_sessions,
        name="legacy_admin_recent",
    ),

    # 🔹 상담 가능 여부 (news.html 에서 data-livechat-availability-url 로 사용)
    path(
        "api/livechat/status/",
        agent_api.livechat_status_api,
        name="livechat_status_api",
    ),
]
