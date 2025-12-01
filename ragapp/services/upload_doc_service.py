# ragapp/services/upload_doc_service.py
from __future__ import annotations

import os
import io
import logging
from collections import defaultdict
from typing import Any, Dict, List, Tuple

from django.utils import timezone
from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest
from ragapp.services.ragchunk_audit import save_ragchunks_safe

from ragapp.log_utils import log_success, log_error

log = logging.getLogger(__name__)


def _pdf_to_text(fp: io.BufferedIOBase) -> str:
    """
    PDF → 텍스트 추출 (pdfminer 우선, 실패 시 pypdf 폴백)
    """
    try:
        from pdfminer.high_level import extract_text  # pdfminer.six
        return extract_text(fp) or ""
    except Exception as e1:
        try:
            from pypdf import PdfReader
            fp.seek(0)
            reader = PdfReader(fp)
            return "\n".join([(p.extract_text() or "") for p in reader.pages]).strip()
        except Exception as e2:
            raise RuntimeError(f"PDF 추출 실패(pdfminer/pypdf 필요): {e1 or e2}")


def _chunk(text: str, maxlen: int = 1600, overlap: int = 200) -> List[str]:
    """
    긴 텍스트를 maxlen/overlap 기준으로 청킹.
    """
    t = (text or "").strip()
    if not t:
        return []
    out: List[str] = []
    n = len(t)
    i = 0
    while i < n:
        j = min(n, i + maxlen)
        out.append(t[i:j])
        if j >= n:
            break
        i = max(0, j - overlap)
    return out


def handle_upload_doc(request: HttpRequest) -> Dict[str, Any]:
    """
    업로드/텍스트 추출/청킹/임베딩/업서트 전체 담당.
    - 템플릿에서 바로 쓸 수 있는 dict를 반환한다.

    반환 예:
      {
        "error_msg": str | None,
        "file_errors": [str, ...],
        "result": {
          "inserted": int,
          "total_chunks": int,
          "duplicated": int,
          "failed": int,
          "files": [...],
          "file_rows": [
             {
               "file_name": "...",
               "status": "ok|empty|error",
               "chunk_count": 10,
               "message": "...",
               ...
             },
             ...
          ],
        } | None
      }
    """
    # ── 1) 입력 수집 ────────────────────────────────────────
    common_title = (request.POST.get("common_title") or request.POST.get("title") or "").strip()
    source_label = (request.POST.get("source_label") or request.POST.get("source_name") or "").strip()
    pasted_text = (
        (request.POST.get("direct_text") or "")
        or (request.POST.get("pasted_text") or "")
        or (request.POST.get("rawtext") or "")
    ).strip()

    files: List[Any] = []
    files += list(request.FILES.getlist("files"))
    files += list(request.FILES.getlist("docfiles"))
    if request.FILES.get("file"):
        files.append(request.FILES["file"])

    extracted: List[Tuple[str, str]] = []          # (name, text)
    extracted_infos: List[Dict[str, Any]] = []     # 성공/부분성공 파일 정보
    error_infos: List[Dict[str, Any]] = []         # 추출단계에서 실패한 파일 정보
    file_errors: List[str] = []                    # 템플릿용 에러 메시지 리스트

    # 붙여넣기 텍스트를 가상 파일로 취급
    if pasted_text:
        name_key = "__pasted__.txt"
        extracted.append((name_key, pasted_text))
        extracted_infos.append(
            {
                "name_key": name_key,
                "file_name": "(직접 입력 텍스트)",
                "is_pasted": True,
                "size_bytes": len(pasted_text.encode("utf-8", errors="ignore")),
                "text": pasted_text,
            }
        )

    # 실제 업로드 파일 처리
    for f in files:
        name = getattr(f, "name", "uploaded")
        try:
            ext = os.path.splitext(name.lower())[1]
            buf = io.BytesIO(f.read())
            if ext == ".pdf":
                text = _pdf_to_text(buf)
            else:
                try:
                    text = buf.getvalue().decode("utf-8", errors="ignore")
                except Exception:
                    text = buf.getvalue().decode("cp949", errors="ignore")
            text = (text or "").strip()
            if not text:
                msg = "추출된 텍스트가 없습니다."
                file_errors.append(f"{name}: {msg}")
                error_infos.append({"file_name": name, "message": msg})
            else:
                extracted.append((name, text))
                extracted_infos.append(
                    {
                        "name_key": name,
                        "file_name": name,
                        "is_pasted": False,
                        "size_bytes": getattr(f, "size", 0),
                        "text": text,
                    }
                )
        except Exception as e:
            log.exception("파일 처리 실패: %s", name)
            msg = str(e)
            file_errors.append(f"{name}: {msg}")
            error_infos.append({"file_name": name, "message": msg})

    if not extracted:
        msg = "유효한 텍스트가 없어 인덱싱을 진행하지 않았습니다."
        messages.error(request, msg)
        log_error(
            mode_label="upload_doc",
            query_text=common_title or "(upload_doc)",
            err_msg=msg,
            request=request,
            extra={
                "where": "upload_doc_view",
                "stage": "no_text",
                "file_count": len(files),
            },
        )
        return {
            "error_msg": msg,
            "file_errors": file_errors,
            "result": None,
        }

    # ── 2) 청킹 + 메타 생성 ─────────────────────────────────
    size = int(getattr(settings, "EMBED_CHUNK_SIZE", 1600))
    overlap = int(getattr(settings, "EMBED_CHUNK_OVERLAP", 200))
    now_iso = timezone.now().strftime("%Y-%m-%d %H:%M:%S")

    all_ids: List[str] = []
    all_docs: List[str] = []
    all_metas: List[Dict[str, Any]] = []
    per_file_cnt: defaultdict[str, int] = defaultdict(int)

    from ragapp.services.vector_store import _sha as _sha_vs  # 안전 해시

    for name, text in extracted:
        chunks = _chunk(text, maxlen=size, overlap=overlap)
        doc_id = _sha_vs(f"{name}::{now_iso}")[:20]
        for i, ch in enumerate(chunks):
            meta = {
                "title": common_title or name,
                "file_name": name,
                "source": source_label or "upload",
                "doc_id": doc_id,
                "chunk_index": i,
                "ingested_at": now_iso,
            }
            all_docs.append(ch)
            all_metas.append(meta)
            all_ids.append(_sha_vs(f"{doc_id}::{i}")[:64])
            per_file_cnt[name] += 1

    if not all_docs:
        msg = "청킹 결과가 비어 업서트를 진행하지 않았습니다."
        messages.warning(request, msg)
        log_error(
            mode_label="upload_doc",
            query_text=common_title or "(upload_doc)",
            err_msg=msg,
            request=request,
            extra={
                "where": "upload_doc_view",
                "stage": "no_chunks",
                "file_count": len(files),
            },
        )
        return {
            "error_msg": msg,
            "file_errors": file_errors,
            "result": None,
        }

    # ── 3) 전체 청크 수 제한 (.env) ─────────────────────────
    max_chunks_env = os.getenv("UPLOAD_MAX_EMBED_CHUNKS", "") or os.getenv(
        "RAG_UPLOAD_MAX_CHUNKS", ""
    )
    try:
        max_chunks = int(max_chunks_env) if (max_chunks_env or "").strip() else 0
    except Exception:
        max_chunks = 0

    if max_chunks > 0 and len(all_docs) > max_chunks:
        trimmed = len(all_docs) - max_chunks
        all_docs = all_docs[:max_chunks]
        all_metas = all_metas[:max_chunks]
        all_ids = all_ids[:max_chunks]

        from collections import defaultdict as _dd
        new_cnt: _dd[str, int] = _dd(int)
        for m in all_metas:
            fname = m.get("file_name") or "(unknown)"
            new_cnt[fname] += 1
        per_file_cnt = new_cnt

        messages.warning(
            request,
            f"문서가 매우 커서 앞쪽 {max_chunks}개 청크만 임베딩했습니다. "
            f"(잘린 청크 수: {trimmed})",
        )

    # ── 4) 임베딩 + 업서트 ──────────────────────────────────
    try:
        try:
            from ragapp.services.vertex_embed import embed_texts as _embed_texts  # Vertex 우선
        except Exception:
            from ragapp.services.news_services import _embed_texts  # 폴백

        batch_env = os.getenv("UPLOAD_EMBED_BATCH_SIZE", "") or os.getenv(
            "EMBED_BATCH_SIZE", ""
        )
        try:
            batch_size = int(batch_env) if (batch_env or "").strip() else 0
        except Exception:
            batch_size = 0

        def _embed_in_batches(texts: List[str]) -> List[List[float]]:
            if not texts:
                return []
            if batch_size <= 0:
                return _embed_texts(texts)

            out: List[List[float]] = []
            n = len(texts)
            for i in range(0, n, batch_size):
                part = texts[i : i + batch_size]
                embs_part = _embed_texts(part)
                if not isinstance(embs_part, list) or len(embs_part) != len(part):
                    raise RuntimeError("임베딩 결과 개수가 청크 수와 다릅니다.")
                out.extend(embs_part)
            return out

        embs = _embed_in_batches(all_docs)

        try:
            from ragapp.services.vdb_store import vdb_upsert as _vup
        except Exception:
            from ragapp.services.vector_store import vdb_upsert as _vup
        _vup(all_ids, all_docs, all_metas, embs)

        saved_total = 0
        by_file_docs: defaultdict[str, List[str]] = defaultdict(list)
        by_file_meta: dict[str, Dict[str, Any]] = {}

        for d, m in zip(all_docs, all_metas):
            fname = (m.get("file_name") or "").strip() or "(unknown)"
            by_file_docs[fname].append(d)

            # 파일별로 대표 메타 1개만 잡아두기
            if fname not in by_file_meta:
                by_file_meta[fname] = {
                    "kind": "upload",
                    "file_name": fname,
                    "source": m.get("source"),
                    "doc_id": m.get("doc_id"),
                    "ingested_at": m.get("ingested_at"),
                    "title": m.get("title"),
                }

        for fname, docs in by_file_docs.items():
            saved_total += save_ragchunks_safe(
                texts=docs,
                title=(common_title or fname),   # ✅ original_filename 대신 이걸 사용
                url="",
                source="upload-doc",
                base_meta=by_file_meta.get(fname) or {"kind": "upload", "file_name": fname},
            )
            messages.info(request, f"RagChunk 저장(감사용): {saved_total} 청크")

        # ──────────────────────────
        # 5) 파일별 요약 정보 생성
        # ──────────────────────────
        file_rows: List[Dict[str, Any]] = []

        # 정상/부분정상
        for info in extracted_infos:
            name_key = info.get("name_key") or info.get("file_name") or "(unknown)"
            display_name = info.get("file_name") or name_key
            chunk_cnt = int(per_file_cnt.get(name_key, 0))
            raw_text = info.get("text") or ""
            status = "ok" if chunk_cnt > 0 else "empty"
            msg = f"{chunk_cnt}개 청크 생성" if status == "ok" else "청크가 생성되지 않았습니다."

            file_rows.append(
                {
                    "file_name": display_name,
                    "title": common_title or display_name,
                    "is_pasted": bool(info.get("is_pasted")),
                    "size_bytes": int(info.get("size_bytes") or 0),
                    "raw_chars": len(raw_text),
                    "chunk_count": chunk_cnt,
                    "inserted_chunks": chunk_cnt,
                    "status": status,
                    "message": msg,
                }
            )

        # 추출 단계에서 바로 실패한 파일들
        for einfo in error_infos:
            fname = (einfo.get("file_name") or "").strip() or "(알 수 없는 파일)"
            msg = einfo.get("message") or ""
            target = None
            for r in file_rows:
                if r.get("file_name") == fname:
                    target = r
                    break
            if target:
                target["status"] = "error"
                if target.get("message"):
                    target["message"] = target["message"] + " / " + msg
                else:
                    target["message"] = msg
            else:
                file_rows.append(
                    {
                        "file_name": fname,
                        "title": fname,
                        "is_pasted": False,
                        "size_bytes": 0,
                        "raw_chars": 0,
                        "chunk_count": 0,
                        "inserted_chunks": 0,
                        "status": "error",
                        "message": msg,
                    }
                )

        total_chunks = len(all_ids)
        failed_files = len([r for r in file_rows if r.get("status") == "error"])

        result = {
            "inserted": total_chunks,
            "total_chunks": total_chunks,
            "duplicated": 0,
            "failed": failed_files,
            "files": [r.get("file_name") for r in file_rows],
            "file_rows": file_rows,
            "ragchunk_saved": saved_total,
        }

        messages.success(request, f"인덱싱 완료: 총 {total_chunks} 청크 업서트")

        # AppLog에 한 줄 요약 찍기
        log_success(
            mode_label="upload_doc",
            query_text=common_title or "(upload_doc)",
            preview=f"{total_chunks} chunks, files={len(file_rows)}, failed_files={failed_files}",
            request=request,
            extra={
                "where": "upload_doc_view",
                "stage": "done",
                "result": "success",
                "total_chunks": total_chunks,
                "failed_files": failed_files,
                "file_summaries": [
                    {
                        "file_name": r["file_name"],
                        "chunk_count": r["chunk_count"],
                        "status": r["status"],
                    }
                    for r in file_rows
                ],
            },
        )

        return {
            "error_msg": None,
            "file_errors": file_errors,
            "result": result,
        }

    except Exception as e:
        log.exception("업서트 실패")
        messages.error(request, f"업서트 실패: {e}")
        log_error(
            mode_label="upload_doc",
            query_text=common_title or "(upload_doc)",
            err_msg=str(e),
            request=request,
            extra={
                "where": "upload_doc_view",
                "stage": "exception",
            },
        )
        return {
            "error_msg": f"업서트 실패: {e}",
            "file_errors": file_errors,
            "result": None,
        }
