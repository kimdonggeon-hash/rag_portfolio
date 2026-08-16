# ragapp/apps.py
from django.apps import AppConfig


class RagappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ragapp"
    verbose_name = "RAG App"

    def ready(self):
        import ragapp.signals_faq  # noqa: F401

        # ✅ 외부 호출(HTTP) Span 자동 계측 설치 (requests/httpx)
        # - Admin OBS가 enabled일 때만 기록됨 (contextvar로 제어)
        try:
            from ragapp.obs.http_spans import install_http_spans
            install_http_spans()
        except Exception:
            pass

        # ✅ 어드민 삭제(휴지통) — 모든 ModelAdmin의 삭제 동작을 가로채서
        # 즉시 삭제 대신 TrashedRecord 스냅샷을 남긴 뒤 삭제한다.
        # (관리 명령/자동 보존기간 삭제 등 admin을 거치지 않는 삭제에는 영향 없음)
        try:
            _install_admin_trash_hook()
        except Exception:
            pass


def _install_admin_trash_hook() -> None:
    from django.contrib.admin import ModelAdmin

    if getattr(ModelAdmin, "_ragapp_trash_patched", False):
        return

    def _actor(request) -> str:
        try:
            return str(getattr(request.user, "username", "") or "")
        except Exception:
            return ""

    def delete_model(self, request, obj):
        from ragapp.services.trash_service import move_to_trash
        move_to_trash([obj], actor=_actor(request))

    def delete_queryset(self, request, queryset):
        from ragapp.services.trash_service import move_to_trash
        move_to_trash(list(queryset), actor=_actor(request))

    ModelAdmin.delete_model = delete_model
    ModelAdmin.delete_queryset = delete_queryset
    ModelAdmin._ragapp_trash_patched = True