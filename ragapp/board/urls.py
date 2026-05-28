# ragapp/board/urls.py
from django.urls import path

from .views import (
    PostListView, PostDetailView, PostCreateView, PostUpdateView,
    post_delete, post_auth, post_moderate,
    comment_create, comment_auth, comment_delete, comment_moderate,
    report_post, report_comment, comment_update,
)

from .views_extra import (
    staff_mine, staff_mine_redirect,
    staff_reports, staff_report_action, staff_reports_bulk_action,
    staff_admin_logs,
    staff_bans, staff_unban,
    staff_manual_ban,
    staff_manual_linkblock, staff_clear_linkblock,
    staff_mine_summary_api,
)

app_name = "board"

post_detail_view = PostDetailView.as_view()

urlpatterns = [
    # ── public board ──────────────────────────────────────────
    path("", PostListView.as_view(), name="list"),
    path("new/", PostCreateView.as_view(), name="create"),

    # ── staff "mine" ──────────────────────────────────────────
    path("mine/", staff_mine, name="mine"),
    path("mine/posts/", staff_mine_redirect, name="mine_posts"),
    path("mine/comments/", staff_mine_redirect, name="mine_comments"),
    path("api/mine/summary/", staff_mine_summary_api, name="mine_summary_api"),

    # ── post detail / edit / delete ───────────────────────────
    path("<int:pk>/", post_detail_view, name="detail"),
    path("<int:pk>/", post_detail_view, name="post_detail"),  # ✅ 과거 템플릿 호환용 alias
    path("<int:pk>/edit/", PostUpdateView.as_view(), name="edit"),
    path("<int:pk>/delete/", post_delete, name="delete"),
    path("<int:pk>/auth/", post_auth, name="post_auth"),
    path("<int:pk>/moderate/", post_moderate, name="post_moderate"),

    # ── comments ──────────────────────────────────────────────
    path("<int:pk>/comment/", comment_create, name="comment_create"),
    path("comment/<int:comment_id>/auth/", comment_auth, name="comment_auth"),
    path("comment/<int:comment_id>/delete/", comment_delete, name="comment_delete"),
    path("comment/<int:comment_id>/moderate/", comment_moderate, name="comment_moderate"),
    path("comment/<int:comment_id>/edit/", comment_update, name="comment_edit"),

    # ── reports (user) ─────────────────────────────────────────
    path("<int:pk>/report/", report_post, name="report_post"),
    path("comment/<int:comment_id>/report/", report_comment, name="report_comment"),

    # ── staff console: reports ────────────────────────────────
    path("mod/reports/", staff_reports, name="reports"),
    path("mod/reports/<int:report_id>/action/", staff_report_action, name="report_action"),

    # ✅ bulk endpoint (1개 URL에 name 2개로 reverse 호환)
    path("mod/reports/bulk/", staff_reports_bulk_action, name="reports_bulk_action"),
    path("mod/reports/bulk/", staff_reports_bulk_action, name="reports_bulk"),  # ✅ NoReverseMatch 해결

    # ── staff console: logs ───────────────────────────────────
    path("mod/logs/", staff_admin_logs, name="admin_logs"),

    # ── staff console: bans ───────────────────────────────────
    path("mod/bans/", staff_bans, name="bans"),
    path("mod/bans/<str:fp>/unban/", staff_unban, name="unban"),
    path("mod/bans/ban/", staff_manual_ban, name="manual_ban"),
    path("mod/bans/linkblock/", staff_manual_linkblock, name="manual_linkblock"),
    path("mod/bans/<str:fp>/unlink/", staff_clear_linkblock, name="clear_linkblock"),
]
