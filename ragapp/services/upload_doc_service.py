# ragapp/services/upload_doc_service.py
from __future__ import annotations

import os
import io
import re
import hashlib
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

# ✅ RAG 검색 필터에 걸리는 source
# PDF 업로드가 RAG 검색에서 잡히려면 source="pdf"가 가장 안전하다.
PDF_SOURCE = "pdf"

# TXT/직접 입력 텍스트용
TEXT_SOURCE = "upload_doc"

# 업로드 문서라는 성격은 kind로 따로 보관
UPLOAD_KIND = "upload_doc"


def _source_for_file(name: str, *, is_pasted: bool = False) -> str:
    if is_pasted:
        return TEXT_SOURCE

    ext = os.path.splitext(str(name).lower())[1]

    if ext == ".pdf":
        return PDF_SOURCE

    return TEXT_SOURCE


def _sha(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8", "ignore")).hexdigest()


def _safe_label(s: str, *, max_len: int = 48) -> str:
    """
    표시용 라벨(source_name) 정리:
    - 너무 길거나 이상한 문자는 제거
    - 빈 값이면 "" 반환
    """
    t = (s or "").strip()
    if not t:
        return ""
    # 한글/영문/숫자/공백/일부 기호만
    t = re.sub(r"[^0-9A-Za-z가-힣 _\-\.\(\)\[\]]+", " ", t)
    t = " ".join(t.split()).strip()
    return t[:max_len]


def _pdf_to_text(fp: io.BufferedIOBase) -> str:
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
    # ─────────────────────────────────────────────
    # 1) 입력 수집 (✅ 확정 키: common_title/source_label/docfiles/rawtext)
    # ─────────────────────────────────────────────
    common_title = (request.POST.get("common_title") or "").strip()
    source_label_raw = (request.POST.get("source_label") or "").strip()
    pasted_text = (request.POST.get("rawtext") or "").strip()

    # (호환) 예전 키도 허용
    if not common_title:
        common_title = (request.POST.get("title") or "").strip()
    if not source_label_raw:
        source_label_raw = (request.POST.get("source_name") or "").strip()
    if not pasted_text:
        pasted_text = (
            (request.POST.get("direct_text") or "")
            or (request.POST.get("pasted_text") or "")
        ).strip()

    # ✅ 표시용(source_name)만 정리해서 사용
    # 검색 필터용 source는 파일 확장자에 따라 pdf/upload_doc로 따로 결정한다.
    source_name = _safe_label(source_label_raw) or UPLOAD_KIND

    files: List[Any] = []

    # ✅ upload_views.py에서 보조 속성으로 넘긴 파일 목록 우선 사용
    preset_files = getattr(request, "_upload_doc_files", None)

    if preset_files:
        files = list(preset_files)
    elif hasattr(request, "FILES"):
        files += list(request.FILES.getlist("docfiles"))  # ✅ 정식
        files += list(request.FILES.getlist("files"))     # (호환)
        if request.FILES.get("file"):
            files.append(request.FILES["file"])

    extracted: List[Tuple[str, str, str]] = []
    extracted_infos: List[Dict[str, Any]] = []
    error_infos: List[Dict[str, Any]] = []
    file_errors: List[str] = []

    # 붙여넣기 텍스트
    if pasted_text:
        name_key = "__pasted__.txt"
        file_source = _source_for_file(name_key, is_pasted=True)

        extracted.append((name_key, pasted_text, file_source))
        extracted_infos.append(
            {
                "name_key": name_key,
                "file_name": "(직접 입력 텍스트)",
                "is_pasted": True,
                "size_bytes": len(pasted_text.encode("utf-8", errors="ignore")),
                "text": pasted_text,
                "source": file_source,
            }
        )

    # 업로드 파일 처리
    for f in files:
        name = getattr(f, "name", "uploaded")
        try:
            ext = os.path.splitext(str(name).lower())[1]
            if ext not in (".pdf", ".txt"):
                msg = "지원하지 않는 확장자입니다(PDF/TXT만 가능)."
                file_errors.append(f"{name}: {msg}")
                error_infos.append({"file_name": name, "message": msg})
                continue

            buf = io.BytesIO(f.read())
            if ext == ".pdf":
                text = _pdf_to_text(buf)
            else:
                raw = buf.getvalue()
                try:
                    text = raw.decode("utf-8", errors="ignore")
                except Exception:
                    text = raw.decode("cp949", errors="ignore")

            text = (text or "").strip()
            if not text:
                msg = "추출된 텍스트가 없습니다."
                file_errors.append(f"{name}: {msg}")
                error_infos.append({"file_name": name, "message": msg})
            else:
                file_source = _source_for_file(name, is_pasted=False)

                extracted.append((name, text, file_source))
                extracted_infos.append(
                    {
                        "name_key": name,
                        "file_name": name,
                        "is_pasted": False,
                        "size_bytes": getattr(f, "size", 0) or 0,
                        "text": text,
                        "source": file_source,
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
            extra={"where": "upload_doc_view", "stage": "no_text", "file_count": len(files)},
        )
        return {"error_msg": msg, "file_errors": file_errors, "result": None}

    # ─────────────────────────────────────────────
    # 2) 청킹 + 메타 생성
    # ─────────────────────────────────────────────
    size = int(getattr(settings, "EMBED_CHUNK_SIZE", 1600))
    overlap = int(getattr(settings, "EMBED_CHUNK_OVERLAP", 200))
    now_iso = timezone.now().isoformat()

    all_ids: List[str] = []
    all_docs: List[str] = []
    all_metas: List[Dict[str, Any]] = []
    per_file_cnt: defaultdict[str, int] = defaultdict(int)

    for name, text, file_source in extracted:
        chunks = _chunk(text, maxlen=size, overlap=overlap)

        # ✅ doc_id를 "내용 기반"으로 안정화: 같은 내용 재업로드 → 같은 ids로 업서트
        content_sig = _sha(text)[:24]
        doc_id = _sha(f"{name}::{content_sig}")[:20]

        for i, ch in enumerate(chunks):
            ch_s = (ch or "").strip()
            if not ch_s:
                continue

            # ✅ 핵심: RAG 필터용 source는 파일 종류에 따라 pdf/upload_doc로 저장
            meta: Dict[str, Any] = {
                # ✅ RAG 검색 필터에 걸리는 값
                # PDF면 "pdf", TXT/직접입력이면 "upload_doc"
                "source": file_source,

                # ✅ 업로드 문서라는 성격은 따로 보관
                "kind": UPLOAD_KIND,

                # ✅ 화면 표시용 라벨
                "source_name": source_name,

                "title": common_title or name,
                "file_name": name,
                "url": "",
                "doc_id": doc_id,
                "chunk_index": i,
                "ingested_at": now_iso,
            }

            all_docs.append(ch_s)
            all_metas.append(meta)
            all_ids.append(_sha(f"{doc_id}::{i}")[:64])
            per_file_cnt[name] += 1

    if not all_docs:
        msg = "청킹 결과가 비어 업서트를 진행하지 않았습니다."
        messages.warning(request, msg)
        log_error(
            mode_label="upload_doc",
            query_text=common_title or "(upload_doc)",
            err_msg=msg,
            request=request,
            extra={"where": "upload_doc_view", "stage": "no_chunks", "file_count": len(files)},
        )
        return {"error_msg": msg, "file_errors": file_errors, "result": None}

    # ─────────────────────────────────────────────
    # 3) 전체 청크 수 제한(.env)
    # ─────────────────────────────────────────────
    max_chunks_env = os.getenv("UPLOAD_MAX_EMBED_CHUNKS", "") or os.getenv("RAG_UPLOAD_MAX_CHUNKS", "")
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
            f"문서가 커서 앞쪽 {max_chunks}개 청크만 임베딩했습니다. (잘린 청크 수: {trimmed})",
        )

    # ─────────────────────────────────────────────
    # 4) 임베딩 + 업서트
    # ─────────────────────────────────────────────
    try:
        # 임베딩 함수
        try:
            from ragapp.services.vertex_embed import embed_texts as _embed_texts
        except Exception:
            from ragapp.services.news_services import _embed_texts  # 폴백

        batch_env = os.getenv("UPLOAD_EMBED_BATCH_SIZE", "") or os.getenv("EMBED_BATCH_SIZE", "")
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

        # ✅ vdb_store를 단일 SoT로 우선 (이름은 프로젝트마다 달라서 둘 다 시도)
        _vup = None
        try:
            from ragapp.services.vdb_store import vdb_upsert_docs as _vup  # type: ignore
        except Exception:
            try:
                from ragapp.services.vdb_store import vdb_upsert as _vup  # type: ignore
            except Exception:
                _vup = None

        if _vup is None:
            # (구버전 폴백)
            try:
                from ragapp.services.vector_store import vdb_upsert as _vup  # type: ignore
            except Exception as e:
                raise RuntimeError(f"vdb_upsert 함수를 찾지 못했습니다: {e}")

        _vup(all_ids, all_docs, all_metas, embs)

        # ─────────────────────────────────────────
        # RagChunk 감사 저장(파일별)
        # ─────────────────────────────────────────
        saved_total = 0
        by_file_docs: defaultdict[str, List[str]] = defaultdict(list)
        by_file_meta: dict[str, Dict[str, Any]] = {}

        for d, m in zip(all_docs, all_metas):
            fname = (m.get("file_name") or "").strip() or "(unknown)"
            by_file_docs[fname].append(d)
            if fname not in by_file_meta:
                by_file_meta[fname] = {
                    "kind": m.get("kind") or UPLOAD_KIND,
                    "file_name": fname,
                    "source": m.get("source"),
                    "source_name": m.get("source_name"),
                    "doc_id": m.get("doc_id"),
                    "ingested_at": m.get("ingested_at"),
                    "title": m.get("title"),
                }

        for fname, docs in by_file_docs.items():
            meta0 = by_file_meta.get(fname) or {
                "kind": UPLOAD_KIND,
                "file_name": fname,
                "source": TEXT_SOURCE,
            }

            saved_total += save_ragchunks_safe(
                texts=docs,
                title=(common_title or fname),
                url="",
                source=meta0.get("source") or TEXT_SOURCE,
                base_meta=meta0,
            )
        if saved_total:
            messages.info(request, f"RagChunk 저장(감사용): {saved_total} 청크")

        # ─────────────────────────────────────────
        # 5) 파일별 요약
        # ─────────────────────────────────────────
        file_rows: List[Dict[str, Any]] = []

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
                    "source": info.get("source") or TEXT_SOURCE,
                    "source_name": source_name,
                    "is_pasted": bool(info.get("is_pasted")),
                    "size_bytes": int(info.get("size_bytes") or 0),
                    "raw_chars": len(raw_text),
                    "chunk_count": chunk_cnt,
                    "inserted_chunks": chunk_cnt,
                    "status": status,
                    "message": msg,
                }
            )

        for einfo in error_infos:
            fname = (einfo.get("file_name") or "").strip() or "(알 수 없는 파일)"
            msg = einfo.get("message") or ""

            file_rows.append(
                {
                    "file_name": fname,
                    "title": fname,
                    "source": _source_for_file(fname, is_pasted=False),
                    "source_name": source_name,
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
            "sources": sorted({str(r.get("source") or "") for r in file_rows if r.get("source")}),
            "source_name": source_name,
        }

        messages.success(request, f"인덱싱 완료: 총 {total_chunks} 청크 업서트")

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
                "sources": sorted({str(r.get("source") or "") for r in file_rows if r.get("source")}),
                "source_name": source_name,
                "file_summaries": [
                    {
                        "file_name": r["file_name"],
                        "source": r.get("source"),
                        "chunk_count": r["chunk_count"],
                        "status": r["status"],
                    }
                    for r in file_rows
                ],
            },
        )

        return {"error_msg": None, "file_errors": file_errors, "result": result}

    except Exception as e:
        log.exception("업서트 실패")
        messages.error(request, f"업서트 실패: {e}")
        log_error(
            mode_label="upload_doc",
            query_text=common_title or "(upload_doc)",
            err_msg=str(e),
            request=request,
            extra={"where": "upload_doc_view", "stage": "exception", "kind": UPLOAD_KIND},
        )
        return {"error_msg": f"업서트 실패: {e}", "file_errors": file_errors, "result": None}
