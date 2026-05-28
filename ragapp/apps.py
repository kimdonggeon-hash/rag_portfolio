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