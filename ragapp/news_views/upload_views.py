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
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache

from ragapp.services.upload_doc_service import handle_upload_doc

try:
    from ragapp.models import AppLog
except Exception:  # pragma: no cover
    AppLog = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


def _new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def _applog(
    component: str,
    message: str,
    *,
    level: str = "INFO",
    trace_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
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
        return


def _vector_db_path() -> str:
    """
    vdb_store의 단일 SoT를 우선 사용.
    실패하면 BASE_DIR/sqlite3/vector_store.sqlite3 폴백.
    """
    try:
        from ragapp.services.vdb_store import vdb_db_path
        return str(vdb_db_path())
    except Exception:
        base = getattr(settings, "BASE_DIR", Path.cwd())
        return str(Path(base) / "sqlite3" / "vector_store.sqlite3")


@csrf_protect
@staff_member_required
@require_http_methods(["GET", "POST"])
def upload_doc_view(request: HttpRequest) -> HttpResponse:
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
        ctx: dict[str, Any] = {
            "error_msg": None,
            "file_errors": [],
            "result": None,
        }
    else:
        trace_id = _new_trace_id()
        request.META["X_TRACE_ID"] = trace_id

        # ✅ 템플릿 name="docfiles"가 정식 (구버전 호환: files)
        files = []
        if hasattr(request, "FILES"):
            files = request.FILES.getlist("docfiles") or request.FILES.getlist("files") or []

        rawtext = (request.POST.get("rawtext") or "").strip()
        has_text = len(rawtext) > 0

        # ✅ handle_upload_doc 내부 파일 key 차이 방지용 보조 속성
        request._upload_doc_files = files  # type: ignore[attr-defined]
        request._upload_doc_rawtext = rawtext  # type: ignore[attr-defined]

        _applog(
            component="upload",
            message=f"업로드 요청 수신(files={len(files)}, rawtext={'Y' if has_text else 'N'})",
            trace_id=trace_id,
            meta={
                "file_names": [getattr(f, "name", "") for f in files],
                "rawtext_chars": len(rawtext),
                "media_root": str(media_root),
                "vector_db_path": vector_db,
            },
        )

        # ─────────────────────────────────────────────
        # 사전 검증: 개수 / 용량 제한
        # ─────────────────────────────────────────────
        max_files = getattr(settings, "UPLOADDOC_MAX_FILES", 10)
        try:
            max_files_int = int(max_files)
        except Exception:
            max_files_int = 10

        max_per_mb = getattr(settings, "UPLOADDOC_MAX_SIZE_MB_PER_FILE", 10)
        try:
            max_per_mb_int = int(max_per_mb)
        except Exception:
            max_per_mb_int = 10

        max_total_mb = getattr(settings, "UPLOADDOC_MAX_TOTAL_MB", 50)
        try:
            max_total_mb_int = int(max_total_mb)
        except Exception:
            max_total_mb_int = 50

        per_file_limit_bytes = max_per_mb_int * 1024 * 1024 if max_per_mb_int > 0 else 0
        total_limit_bytes = max_total_mb_int * 1024 * 1024 if max_total_mb_int > 0 else 0

        validation_error_msg: str | None = None
        validation_file_errors: list[str] = []

        # 1) 파일도 없고 텍스트도 없으면
        if (not files) and (not has_text):
            validation_error_msg = "업로드할 파일을 하나 이상 선택하거나 텍스트를 입력해 주세요."

        # 2) 파일 개수 제한(파일이 있을 때만)
        elif files and max_files_int > 0 and len(files) > max_files_int:
            validation_error_msg = f"한 번에 업로드할 수 있는 파일은 최대 {max_files_int}개까지입니다."

        # 3) 파일 타입/용량 제한(파일이 있을 때만)
        if files and not validation_error_msg:
            total_size = 0
            for f in files:
                name = getattr(f, "name", "(이름 없음)")
                lname = str(name).lower()
                if not (lname.endswith(".pdf") or lname.endswith(".txt")):
                    validation_file_errors.append(f"{name}: PDF/TXT 파일만 업로드할 수 있어요.")
                    continue

                size = getattr(f, "size", 0) or 0
                total_size += size

                if per_file_limit_bytes and size > per_file_limit_bytes:
                    validation_file_errors.append(f"{name}: 파일 크기가 {max_per_mb_int}MB를 초과했습니다.")

                if total_limit_bytes and total_size > total_limit_bytes:
                    validation_file_errors.append(f"전체 업로드 용량이 {max_total_mb_int}MB를 초과했습니다.")
                    break

            if validation_file_errors and not validation_error_msg:
                validation_error_msg = "일부 파일이 제한 조건을 만족하지 않습니다."

        if validation_error_msg or validation_file_errors:
            ctx = {
                "error_msg": validation_error_msg,
                "file_errors": validation_file_errors,
                "result": None,
            }
            _applog(
                component="upload",
                message="업로드 사전 검증 실패",
                level="WARNING",
                trace_id=trace_id,
                meta={
                    "error_msg": validation_error_msg or "",
                    "file_errors": validation_file_errors,
                    "file_count": len(files),
                    "rawtext_chars": len(rawtext),
                    "max_files": max_files_int,
                    "max_per_mb": max_per_mb_int,
                    "max_total_mb": max_total_mb_int,
                },
            )
        else:
            ctx_any = handle_upload_doc(request)
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

            error_msg = ctx.get("error_msg")
            file_errors = ctx.get("file_errors") or []
            has_error = bool(error_msg) or bool(file_errors)

            # ✅ 업로드/인덱싱 성공 시 RAG 관련 캐시 무효화
            if not has_error:
                for ck in [
                    "rag_recent_results",
                    "rag_sources",
                    "rag_chunks",
                    "admin_rag_stats",
                    "upload_doc_stats",
                ]:
                    try:
                        cache.delete(ck)
                    except Exception:
                        pass

            _applog(
                component="upload",
                message="업로드/인덱싱 완료" if not has_error else "업로드/인덱싱 에러",
                level="ERROR" if has_error else "INFO",
                trace_id=trace_id,
                meta={
                    "error_msg": str(error_msg) if error_msg else "",
                    "file_errors_count": len(file_errors),
                    "result": ctx.get("result"),
                    "vector_db_path": vector_db,
                    "cache_invalidated": not has_error,
                },
            )

    base_ctx = {
        "MEDIA_URL": str(media_url),
        "MEDIA_ROOT": str(media_root),
        "VECTOR_DB_PATH": vector_db,
        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        "RAG_SOURCES_FILTER": os.environ.get("RAG_SOURCES_FILTER") or getattr(settings, "RAG_SOURCES_FILTER", ""),
        "TRACE_ID": trace_id or "",
    }
    ctx.update(base_ctx)
    return render(request, "ragadmin/upload_doc.html", ctx)
