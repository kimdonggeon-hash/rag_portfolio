# ragapp/log_utils.py
from django.utils import timezone
from ragapp.models import MyLog

def _remote_ip_from_request(request):
    if not request:
        return ""
    return (
        request.META.get("HTTP_X_FORWARDED_FOR", "")
        or request.META.get("REMOTE_ADDR", "")
        or ""
    )

def log_success(mode_label: str, query_text: str, preview: str, request=None, extra: dict | None = None):
    """
    성공 케이스 로깅 (예: RAG 성공, 크롤 성공 등)

    mode_label 예시: "gemini", "rag", "crawl"
    query_text      : 유저가 검색한 질문/키워드
    preview         : 모델 답변 일부나 "ingest ok" 같은 상태 메시지
    extra           : 디버깅용 추가 정보(dict). DB에는 JSON으로 저장.
    """
    try:
        MyLog.objects.create(
            created_at=timezone.now(),
            mode_text=mode_label or "",
            query=query_text or "",
            ok_flag=True,
            answer_preview=(preview or "")[:500],
            remote_addr_text=_remote_ip_from_request(request),
            extra_json=extra or {},
        )
    except Exception:
        # 로깅하다가 터지면 본 작업이 죽으면 안 되니까 그냥 무시
        pass

def log_error(mode_label: str, query_text: str, err_msg: str, request=None, extra: dict | None = None):
    """
    실패/에러 케이스 로깅
    err_msg: 에러 상세
    """
    try:
        MyLog.objects.create(
            created_at=timezone.now(),
            mode_text=mode_label or "",
            query=query_text or "",
            ok_flag=False,
            answer_preview=(err_msg or "")[:500],
            remote_addr_text=_remote_ip_from_request(request),
            extra_json=extra or {},
        )
    except Exception:
        pass
