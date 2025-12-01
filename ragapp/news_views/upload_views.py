# ragapp/news_views/upload_views.py
from __future__ import annotations

import os
import uuid
import logging
from pathlib import Path
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from ragapp.services.upload_doc_service import handle_upload_doc

# AppLog 모델이 있으면 가져오고, 없으면 None 처리
try:
    from ragapp.models import AppLog
except Exception:  # pragma: no cover
    AppLog = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


def _new_trace_id() -> str:
    """업로드 1회당 묶어서 추적할 수 있는 trace_id 생성."""
    return uuid.uuid4().hex[:16]


def _applog(
    component: str,
    message: str,
    *,
    level: str = "INFO",
    trace_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """
    AppLog + python logging 동시 기록.
    - AppLog 모델이 없으면 DB 기록은 건너뜀.
    - component: 기능 이름(예: upload, ingest 등)
    - trace_id : 한 번의 업로드/요청을 묶는 ID
    """
    # python logging 에도 남겨두기
    extra = {"component": component, "trace_id": trace_id or ""}
    level_lower = (level or "INFO").lower()
    logger_fn = getattr(log, level_lower, log.info)
    logger_fn(message, extra=extra)

    if AppLog is None:
        return

    try:
        AppLog.objects.create(  # type: ignore[call-arg]
            component=component,
            message=message,
            level=level,
            trace_id=trace_id or "",
            meta=meta or {},
        )
    except Exception:
        # 로깅 때문에 실제 업로드가 깨지면 안 되므로 예외는 무시
        return


def _vector_db_path() -> str:
    """
    현재 사용하는 벡터 SQLite DB 경로를 문자열로 반환.

    우선순위:
      1) settings.VECTOR_DB_PATH
      2) 환경변수 VECTOR_DB_PATH
      3) BASE_DIR/sqlite3/vector_store.sqlite3
    """
    try:
        p = getattr(settings, "VECTOR_DB_PATH", None)
    except Exception:
        p = None

    p = p or os.environ.get("VECTOR_DB_PATH")
    if p:
        return str(p)

    base = getattr(settings, "BASE_DIR", Path.cwd())
    return str(Path(base) / "sqlite3" / "vector_store.sqlite3")


@csrf_protect
@require_http_methods(["GET", "POST"])
def upload_doc_view(request: HttpRequest) -> HttpResponse:
    """
    /ragadmin/upload-doc/ HTML 화면 전용 뷰.

    - GET  : 업로드 폼 렌더링
    - POST : ragapp.services.upload_doc_service.handle_upload_doc(...) 에
             실제 업로드/인덱싱 로직을 위임하고,
             그 결과 컨텍스트(error_msg / file_errors / result)를 템플릿에 전달한다.

    추가:
    - POST 요청마다 trace_id를 생성해 AppLog에 기록하고,
      템플릿 컨텍스트에 TRACE_ID로 내려준다.
      → /admin/ragapp/applog/ 에서 trace_id로 해당 업로드를 추적 가능.
    """
    # 업로드/템플릿용 공통 경로 정보
    media_root = getattr(settings, "MEDIA_ROOT", None) or os.path.join(
        str(getattr(settings, "BASE_DIR", ".")),
        "uploads",
    )
    media_url = getattr(settings, "MEDIA_URL", "/uploads/")
    if not str(media_url).endswith("/"):
        media_url = str(media_url) + "/"

    vector_db = _vector_db_path()
    trace_id: str | None = None

    if request.method == "GET":
        # 초기 진입: 에러/결과 없음
        ctx: dict[str, Any] = {
            "error_msg": None,   # 상단 에러 메시지(문자열)
            "file_errors": [],   # 파일별 텍스트 추출 실패 메시지 리스트
            "result": None,      # 인덱싱 결과(dict) – handle_upload_doc 쪽에서 세팅
        }
    else:
        # POST: 실제 업로드/인덱싱 작업
        trace_id = _new_trace_id()
        # 서비스 레이어에서 필요하면 꺼내 쓸 수 있도록 META에 심어두기 (옵션)
        request.META["X_TRACE_ID"] = trace_id

        files = request.FILES.getlist("files") if hasattr(request, "FILES") else []

        # 업로드 요청 시작 로그
        _applog(
            component="upload",
            message=f"{len(files)}개 파일 업로드 요청 수신",
            trace_id=trace_id,
            meta={
                "file_names": [f.name for f in files],
                "media_root": str(media_root),
                "vector_db_path": vector_db,
            },
        )

        ctx_any = handle_upload_doc(request)

        # 혹시라도 서비스에서 dict 외 타입을 주면 방어
        if not isinstance(ctx_any, dict):
            _applog(
                component="upload",
                message="handle_upload_doc가 dict를 반환하지 않았습니다.",
                level="ERROR",
                trace_id=trace_id,
                meta={"returned_type": str(type(ctx_any))},
            )
            ctx = {
                "error_msg": "내부 오류: 업로드 결과 형식이 올바르지 않습니다.",
                "file_errors": [],
                "result": None,
            }
        else:
            ctx = ctx_any

        # 결과 요약 로그 (성공/실패 모두)
        error_msg = ctx.get("error_msg")
        file_errors = ctx.get("file_errors") or []
        has_error = bool(error_msg) or bool(file_errors)

        _applog(
            component="upload",
            message="업로드/인덱싱 완료" if not has_error else "업로드/인덱싱 중 에러 발생",
            level="ERROR" if has_error else "INFO",
            trace_id=trace_id,
            meta={
                "error_msg": str(error_msg) if error_msg else "",
                "file_errors_count": len(file_errors),
            },
        )

    base_ctx = {
        "MEDIA_URL": str(media_url),
        "MEDIA_ROOT": str(media_root),
        "VECTOR_DB_PATH": vector_db,
        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        # 템플릿에서 이 업로드를 찾을 수 있게 trace_id 내려주기 (GET일 땐 빈 문자열)
        "TRACE_ID": trace_id or "",
    }

    ctx.update(base_ctx)
    return render(request, "ragadmin/upload_doc.html", ctx)
