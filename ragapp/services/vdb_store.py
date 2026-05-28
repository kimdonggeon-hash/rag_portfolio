# ragapp/services/vdb_store.py
from __future__ import annotations

import inspect
import json
import logging
import re
import hashlib
from typing import Any, Dict, List, Mapping, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from django.conf import settings

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# ✅ Hybrid backend: sqlite_hybrid_store (단일 진실원천)
# ─────────────────────────────────────────
_HY = None
_HY_IMPORT_ERROR: str | None = None
try:
    from ragapp.services import sqlite_hybrid_store as _HY  # type: ignore
except Exception as e:  # pragma: no cover
    _HY = None
    _HY_IMPORT_ERROR = str(e)


def _require_hybrid():
    if _HY is None:
        raise RuntimeError(
            "sqlite_hybrid_store 를 임포트할 수 없습니다. "
            f"(error={_HY_IMPORT_ERROR})"
        )
    return _HY


# ─────────────────────────────────────────
# 공용 유틸
# ─────────────────────────────────────────
def _sha256_hex(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="ignore")).hexdigest()


def _unpack_bool_reason(x: Any) -> Tuple[bool, str]:
    """
    sqlite_hybrid_store 쪽이 bool/tuple/dict 등 어떤 형태로 반환해도 안전 언패킹.
    """
    if x is None:
        return False, ""
    if isinstance(x, bool):
        return x, ("ok" if x else "")
    if isinstance(x, (tuple, list)) and x:
        b = bool(x[0])
        r = ""
        if len(x) >= 2 and x[1] is not None:
            r = str(x[1])
        return b, r
    if isinstance(x, dict):
        b = bool(x.get("hit") or x.get("dup") or x.get("exists") or x.get("ok"))
        r = str(x.get("reason") or x.get("why") or x.get("code") or "")
        return b, r
    # 객체형
    try:
        b = bool(getattr(x, "hit", False) or getattr(x, "dup", False) or getattr(x, "exists", False))
        r = str(getattr(x, "reason", "") or getattr(x, "code", "") or "")
        return b, r
    except Exception:
        return False, ""


def _norm_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _norm_date_yyyy_mm_dd(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    return m.group(0) if m else ""


_TRACKING_DROP = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "igshid", "mc_cid", "mc_eid",
}

def _norm_url(u: str) -> str:
    """
    ✅ URL 정규화(중복 스킵용)
    - fragment 제거
    - host 소문자(+www 제거)
    - tracking query 제거(utm/gclid/fbclid 등)
    - query 파라미터 정렬(순서 차이 중복 방지)
    - trailing slash 정리
    """
    u = (u or "").strip()
    if not u:
        return ""

    u = u.split("#", 1)[0].strip()
    try:
        sp = urlsplit(u)
        scheme = (sp.scheme or "").lower()
        netloc = (sp.netloc or "").lower()

        if netloc.startswith("www."):
            netloc = netloc[4:]

        # default port 제거(옵션)
        if netloc.endswith(":80") and scheme == "http":
            netloc = netloc[:-3]
        if netloc.endswith(":443") and scheme == "https":
            netloc = netloc[:-4]

        path = sp.path or ""
        if path.endswith("/") and path != "/":
            path = path[:-1]

        q = sp.query or ""
        if q:
            kept = [
                (k, v)
                for (k, v) in parse_qsl(q, keep_blank_values=True)
                if (k or "").lower() not in _TRACKING_DROP
            ]
            kept.sort(key=lambda kv: (kv[0].lower(), kv[1]))
            q = urlencode(kept, doseq=True)

        return urlunsplit((scheme, netloc, path, q, "")).strip().lower()

    except Exception:
        u2 = (u or "").split("#", 1)[0].strip()
        if u2.endswith("/") and len(u2) > 1:
            u2 = u2[:-1]
        return u2.strip().lower()


def _meta_get(meta: Mapping[str, Any], key: str) -> Any:
    try:
        return meta.get(key)  # type: ignore[attr-defined]
    except Exception:
        return None


def _meta_pick_url(meta: Mapping[str, Any]) -> str:
    # ✅ 가능한 경로 최대한 끌어오기(우선순위)
    for k in ("final_url", "canonical_url", "url", "link", "original_url"):
        v = _meta_get(meta, k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _meta_pick_title(meta: Mapping[str, Any]) -> str:
    for k in ("title", "page_title", "headline"):
        v = _meta_get(meta, k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _meta_pick_source(meta: Mapping[str, Any]) -> str:
    for k in ("source_name", "source", "publisher"):
        v = _meta_get(meta, k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


# ─────────────────────────────────────────
# ✅ Dedup API (news_views.py에서 쓰는 함수들)
#   - 실제 판정은 sqlite_hybrid_store로 위임
# ─────────────────────────────────────────
def vdb_url_exists(url: str) -> bool:
    """
    ✅ URL 중복 존재 여부 확인 (하이브리드 스토어에 위임)
    """
    hy = _require_hybrid()
    u = _norm_url(url)
    if not u:
        return False

    fn = getattr(hy, "url_exists", None)
    if callable(fn):
        try:
            b, _ = _unpack_bool_reason(fn(u))
            return bool(b)
        except Exception:
            return False

    # 폴백: vector_dedup_exists가 있으면 거기로 시도
    fn2 = getattr(hy, "vector_dedup_exists", None)
    if callable(fn2):
        dup, _ = _call_vector_dedup_exists(fn2, url=u, title="", source_name="", published_at="")
        return bool(dup)

    return False


def vdb_title_source_exists(title: str, source: str, published_at: str = "") -> bool:
    """
    ✅ URL이 없을 때 title+source(+date)로 중복 여부 확인 (하이브리드에 위임)
    """
    hy = _require_hybrid()
    t = (title or "").strip()
    s = (source or "").strip()
    if not t or not s:
        return False

    # 만약 sqlite_hybrid_store에 title_source_exists 같은 전용 함수가 있으면 우선 사용
    fn = getattr(hy, "title_source_exists", None)
    if callable(fn):
        try:
            # signature에 따라 published_at 지원 여부가 다를 수 있어 안전 호출
            sig = inspect.signature(fn)
            params = sig.parameters
            if "published_at" in params or "published" in params or "date" in params:
                try:
                    b, _ = _unpack_bool_reason(fn(t, s, published_at))
                    return bool(b)
                except Exception:
                    pass
            b, _ = _unpack_bool_reason(fn(t, s))
            return bool(b)
        except Exception:
            pass

    # 폴백: vector_dedup_exists로 판정
    fn2 = getattr(hy, "vector_dedup_exists", None)
    if callable(fn2):
        dup, _ = _call_vector_dedup_exists(fn2, url="", title=t, source_name=s, published_at=published_at)
        return bool(dup)

    return False


def vdb_item_exists(
    *,
    url_candidates: Sequence[str],
    title: str,
    source_name: str,
    published_at: str = "",
) -> Tuple[bool, str]:
    """
    ✅ 통합 중복 판정
    1) URL 후보 중 하나라도 있으면 URL 기준으로 먼저 중복 체크
    2) URL이 없으면 title+source(+date)로 중복 체크
    """
    # 1) URL 후보들
    for u in (url_candidates or []):
        if u and vdb_url_exists(u):
            return True, "db_url"

    # 2) title+source(+date)
    if title and source_name:
        if vdb_title_source_exists(title, source_name, published_at=published_at):
            d = _norm_date_yyyy_mm_dd(published_at)
            return True, ("db_title_source_date" if d else "db_title_source")

    return False, ""


def _call_vector_dedup_exists(fn, *, url: str, title: str, source_name: str, published_at: str) -> Tuple[bool, str]:
    """
    sqlite_hybrid_store.vector_dedup_exists 시그니처가 프로젝트마다 다를 수 있어서
    최대한 안전하게 호출 패턴을 흡수.
    """
    u = _norm_url(url)
    t = (title or "").strip()
    s = (source_name or "").strip()
    p = (published_at or "").strip()

    # ✅ (추가) sqlite_hybrid_store 스타일: vector_dedup_exists(meta: dict) 지원
    meta_obj = {
        "final_url": u or (url or ""),
        "canonical_url": u or (url or ""),
        "url": u or (url or ""),
        "title": t,
        "source_name": s,
        "published_at": p,
    }
    try:
        sig = inspect.signature(fn)
        params = sig.parameters

        # 1) kw로 meta를 받는 경우
        if "meta" in params:
            out = fn(meta=meta_obj)
            b, r = _unpack_bool_reason(out)
            return bool(b), (r or "")

        # 2) 파라미터가 1개뿐이면 dict 1개 받는 형태로 간주
        if len(params) == 1:
            out = fn(meta_obj)
            b, r = _unpack_bool_reason(out)
            return bool(b), (r or "")
    except Exception:
        pass

    # 1) 키워드 기반 (가장 안전)
    try:
        sig = inspect.signature(fn)
        params = sig.parameters

        kw: Dict[str, Any] = {}
        # url 계열
        if "url_candidates" in params:
            kw["url_candidates"] = [u] if u else []
        elif "url_norm" in params:
            kw["url_norm"] = u
        elif "url" in params:
            kw["url"] = u

        # title/source 계열
        if "title" in params:
            kw["title"] = t
        if "source_name" in params:
            kw["source_name"] = s
        elif "source" in params:
            kw["source"] = s

        # date 계열
        if "published_at" in params:
            kw["published_at"] = p
        elif "published" in params:
            kw["published"] = p
        elif "date" in params:
            kw["date"] = p

        out = fn(**kw)
        b, r = _unpack_bool_reason(out)
        return bool(b), (r or "")
    except Exception:
        pass

    # 2) 포지셔널 폴백 (몇 가지 흔한 패턴만)
    patterns = [
        (u, t, s, p),
        (u, t, s),
        (u, t),
        (u,),
    ]
    for args in patterns:
        try:
            out = fn(*args)
            b, r = _unpack_bool_reason(out)
            return bool(b), (r or "")
        except Exception:
            continue

    return False, ""



# ─────────────────────────────────────────
# ✅ RagChunk 미러링(옵션): admin에서 보기용
#   - 하AGCHUNK_MIRROR_ENABLED / VDB_MIRROR_TO_RAGCHUNK 토글
# ─────────────────────────────────────────
def _vec_to_f32_bytes(vec: Sequence[float]) -> bytes:
    """
    RagChunk.embedding(BinaryField)에 넣기 위한 float32 bytes.
    """
    try:
        import numpy as np  # type: ignore
        return np.asarray(list(vec), dtype=np.float32).tobytes()
    except Exception:
        from array import array
        arr = array("f", (float(x) for x in vec))
        return arr.tobytes()


def _json_safe_obj(obj: Any) -> Any:
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        return {}


def _django_upsert_ragchunks(
    ids: Sequence[str],
    docs: Sequence[str],
    metas: Sequence[Mapping[str, Any]],
    embs: Sequence[Sequence[float]],
) -> Dict[str, Any]:
    enabled = bool(getattr(settings, "RAGCHUNK_MIRROR_ENABLED", True))
    if not enabled:
        return {"status": "disabled"}

    try:
        from ragapp.models import RagChunk  # 늦은 import
    except Exception as e:
        return {"status": f"error_import: {e}"}

    rows = []
    skipped = 0

    for i, doc, meta, vec in zip(ids, docs, metas, embs):
        sid = (str(i) if i is not None else "").strip()
        if not sid:
            skipped += 1
            continue

        m0 = dict(meta) if isinstance(meta, Mapping) else {}
        m0.setdefault("vdb_id", sid)

        # url 원문 최대 경로
        url_full = str(_meta_pick_url(m0) or (m0.get("url") or ""))
        m0.setdefault("url_full", url_full)

        m = _json_safe_obj(m0)

        doc_id = str(m.get("doc_id") or sid)[:191]
        url = url_full[:200]
        title = str(m.get("title") or "")[:500]
        text = (doc or "").strip()
        if not text:
            skipped += 1
            continue

        try:
            emb_bytes = _vec_to_f32_bytes(vec)
            dim = int(len(vec))
        except Exception:
            skipped += 1
            continue

        rows.append(
            RagChunk(
                unique_hash=_sha256_hex(sid),
                doc_id=doc_id,
                url=url,
                title=title,
                text=text,
                meta=m,
                embedding=emb_bytes,
                dim=dim,
            )
        )

    if not rows:
        return {"status": "empty", "skipped": skipped}

    try:
        RagChunk.objects.bulk_create(
            rows,
            batch_size=int(getattr(settings, "RAGCHUNK_MIRROR_BATCH", 500)),
            update_conflicts=True,
            unique_fields=["unique_hash"],
            update_fields=["doc_id", "url", "title", "text", "meta", "embedding", "dim"],
        )
        return {"status": "ok", "upserted": len(rows), "skipped": skipped}
    except Exception as e:
        return {"status": f"error_upsert: {e}", "attempted": len(rows), "skipped": skipped}


# 외부 노출 별칭
django_upsert_ragchunks = _django_upsert_ragchunks


# ─────────────────────────────────────────
# ✅ vdb_upsert: 하이브리드 upsert_docs로 위임 + (옵션)RagChunk 미러링
# ─────────────────────────────────────────
def vdb_upsert(
    ids: Sequence[str],
    docs: Sequence[str],
    metas: Sequence[Mapping[str, Any]],
    embs: Sequence[Sequence[float]],
) -> Dict[str, Any]:
    """
    하이브리드 스토어(sqlite_hybrid_store)에 업서트.
    - 시그니처가 다를 수 있어서 안전 호출(kw/positional/패턴)로 흡수.
    - ✅ VDB_MIRROR_TO_RAGCHUNK 켜져 있으면 RagChunk에도 미러링(옵션)
    """
    if not (len(ids) == len(docs) == len(metas) == len(embs)):
        raise ValueError("vdb_upsert: ids/docs/metas/embs 길이가 일치해야 합니다.")

    hy = _require_hybrid()
    upsert_fn = getattr(hy, "upsert_docs", None)
    if not callable(upsert_fn):
        raise RuntimeError("sqlite_hybrid_store.upsert_docs 를 찾을 수 없습니다.")

    # 0) meta 보강: URL/Title/Source 가능한 경로를 meta에 넣어두면 downstream에서 활용 가능
    metas2: List[Dict[str, Any]] = []
    for i, m in zip(ids, metas):
        m0 = dict(m) if isinstance(m, Mapping) else {}
        m0.setdefault("vdb_id", str(i))
        # best-effort: url/title/source 후보를 meta에 보강
        if not m0.get("url"):
            u = _meta_pick_url(m0)
            if u:
                m0["url"] = u
        if not m0.get("title"):
            t = _meta_pick_title(m0)
            if t:
                m0["title"] = t
        if not (m0.get("source_name") or m0.get("source")):
            s = _meta_pick_source(m0)
            if s:
                m0["source_name"] = s
        metas2.append(_json_safe_obj(m0) if isinstance(m0, dict) else {})

    # 1) 안전 호출 (kw 우선)
    result: Any = None
    called = False

    try:
        sig = inspect.signature(upsert_fn)
        params = sig.parameters
        kw: Dict[str, Any] = {}

        if "ids" in params:
            kw["ids"] = list(ids)
        if "docs" in params:
            kw["docs"] = list(docs)
        if "metas" in params:
            kw["metas"] = metas2
        elif "meta" in params:
            kw["meta"] = metas2
        if "embs" in params:
            kw["embs"] = embs
        elif "embeddings" in params:
            kw["embeddings"] = embs

        if kw:
            result = upsert_fn(**kw)
            called = True
    except Exception:
        called = False

    # 2) 포지셔널 폴백 (자주 나오는 패턴)
    if not called:
        patterns = [
            (list(ids), list(docs), metas2, embs),
            (list(docs), metas2, embs),
            (list(ids), list(docs), embs),
        ]
        last_err = None
        for args in patterns:
            try:
                result = upsert_fn(*args)
                called = True
                break
            except Exception as e:
                last_err = e
        if not called:
            raise RuntimeError(f"sqlite_hybrid_store.upsert_docs 호출 실패: {last_err}")

    # 3) (옵션) RagChunk 미러링
    dj = {"status": "disabled"}
    if getattr(settings, "VDB_MIRROR_TO_RAGCHUNK", False):
        try:
            dj = _django_upsert_ragchunks(ids, docs, metas2, embs)
        except Exception as e:
            dj = {"status": f"error_exception: {e}"}

    # 4) 결과 정규화
    out: Dict[str, Any] = {"ok": True}
    if isinstance(result, dict):
        out.update(result)
    else:
        out["result"] = result

    out["django_ragchunk"] = dj
    return out


# ─────────────────────────────────────────
# 선택: count/clear/info (하이브리드에 위임)
# ─────────────────────────────────────────
def vdb_count() -> int:
    hy = _require_hybrid()
    # 있으면 그대로 사용
    for name in ("vdb_count", "count", "vector_count"):
        fn = getattr(hy, name, None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:
                pass
    # 없으면 -1 (스키마 모르면 직접 카운트 불가)
    return -1


def vdb_clear() -> None:
    hy = _require_hybrid()
    for name in ("vdb_clear", "clear", "reset"):
        fn = getattr(hy, name, None)
        if callable(fn):
            fn()
            return
    raise RuntimeError("sqlite_hybrid_store에 clear/reset 계열 함수가 없어 vdb_clear를 수행할 수 없습니다.")


def vdb_info() -> Dict[str, Any]:
    hy = _require_hybrid()
    p = None
    fn = getattr(hy, "db_path", None)
    if callable(fn):
        try:
            p = str(fn())
        except Exception:
            p = None

    info = {"backend": "sqlite_hybrid_store", "path": p}
    c = vdb_count()
    if c >= 0:
        info["count"] = c
    return info


# ─────────────────────────────────────────
# ✅ 호환용 별칭 (기존 코드가 이름으로 import하던 것들 대비)
# ─────────────────────────────────────────
# news_views.py에서 예전 이름을 유지하고 싶으면 이런 식으로도 쓸 수 있음:
# from ragapp.services.vdb_store import vdb_url_exists as url_exists
# from ragapp.services.vdb_store import vdb_title_source_exists as title_source_exists
url_exists = vdb_url_exists
title_source_exists = vdb_title_source_exists


def vector_url_exists(url: str) -> bool:
    return vdb_url_exists(url)


def vector_dedup_exists(
    *,
    url: str = "",
    title: str = "",
    source_name: str = "",
    published_at: str = "",
    url_candidates: Sequence[str] | None = None,
) -> Tuple[bool, str]:
    """
    기존 코드가 vector_dedup_exists를 기대하는 경우를 위한 래퍼.
    - url_candidates가 있으면 그걸 우선 사용
    - 없으면 url 하나를 후보로 사용
    """
    cands = list(url_candidates or [])
    if (not cands) and url:
        cands = [url]
    return vdb_item_exists(
        url_candidates=cands,
        title=title,
        source_name=source_name,
        published_at=published_at,
    )

# ─────────────────────────────────────────
# ✅ Legacy API compatibility (news_services.py imports)
#   sqlite_hybrid_store에 이미 구현된 함수들을 vdb_store에서 예전 이름으로 노출
# ─────────────────────────────────────────

def vdb_db_path() -> str:
    hy = _require_hybrid()
    fn = getattr(hy, "db_path", None)
    if callable(fn):
        return str(fn())
    return ""


def vdb_query_vector_only(*, q_emb, topk: int, where=None) -> Dict[str, Any]:
    """
    sqlite_hybrid_store.query_vector_only에 위임
    반환 형태: {documents/metadatas/distances/ids: [[...]]}
    """
    hy = _require_hybrid()
    fn = getattr(hy, "query_vector_only", None)
    if not callable(fn):
        raise RuntimeError("sqlite_hybrid_store.query_vector_only 를 찾을 수 없습니다.")
    return fn(q_emb=list(q_emb), topk=int(topk), where=where)


def vdb_query_hybrid(
    *,
    query_text: str,
    q_emb,
    topk: int,
    where=None,
    vec_topk: int | None = None,
    kw_topk: int | None = None,
) -> Dict[str, Any]:
    """
    sqlite_hybrid_store.query_hybrid에 위임
    """
    hy = _require_hybrid()
    fn = getattr(hy, "query_hybrid", None)
    if not callable(fn):
        raise RuntimeError("sqlite_hybrid_store.query_hybrid 를 찾을 수 없습니다.")
    return fn(
        query_text=str(query_text or ""),
        q_emb=list(q_emb),
        topk=int(topk),
        where=where,
        vec_topk=(int(vec_topk) if vec_topk is not None else None),
        kw_topk=(int(kw_topk) if kw_topk is not None else None),
    )


def _auto_make_ids_for_upsert(docs: Sequence[str], metas: Sequence[Mapping[str, Any]]) -> List[str]:
    ids: List[str] = []
    for doc, meta in zip(docs, metas):
        m0 = dict(meta) if isinstance(meta, Mapping) else {}

        # 1) 이미 제공된 id류 우선
        for k in ("vdb_id", "id", "doc_id"):
            v = m0.get(k)
            if v:
                ids.append(str(v).strip())
                break
        else:
            # 2) URL 기반
            u = ""
            try:
                u = str(m0.get("url") or _meta_pick_url(m0) or "").strip()
            except Exception:
                u = ""
            if u:
                ids.append(_sha256_hex("url:" + _norm_url(u)))
                continue

            # 3) title/source + doc 일부 기반
            t = str(m0.get("title") or _meta_pick_title(m0) or "").strip()
            s = str(m0.get("source_name") or m0.get("source") or _meta_pick_source(m0) or "").strip()
            basis = f"ts:{t}|{s}|doc:{(doc or '')[:256]}"
            ids.append(_sha256_hex(basis))
    return ids


def vdb_upsert_docs(*args, **kwargs) -> Dict[str, Any]:
    """
    레거시 호환: 예전 코드가 vdb_upsert_docs를 호출하던 것을 vdb_upsert로 연결.

    지원 패턴:
      - vdb_upsert_docs(ids, docs, metas, embs)
      - vdb_upsert_docs(docs, metas, embs)  (ids 자동 생성)
      - vdb_upsert_docs(ids=..., docs=..., metas=..., embs=...)
    """
    ids = kwargs.pop("ids", None)
    docs = kwargs.pop("docs", None)
    metas = kwargs.pop("metas", None) or kwargs.pop("meta", None)
    embs = kwargs.pop("embs", None) or kwargs.pop("embeddings", None)

    # positional 지원
    if docs is None and metas is None and embs is None and args:
        if len(args) == 4:
            ids, docs, metas, embs = args
        elif len(args) == 3:
            docs, metas, embs = args
        else:
            raise TypeError("vdb_upsert_docs: expected (ids, docs, metas, embs) or (docs, metas, embs)")

    if docs is None or metas is None or embs is None:
        raise TypeError("vdb_upsert_docs: docs/metas/embs are required")

    docs_l = list(docs)
    metas_l = list(metas)
    embs_l = list(embs)

    if ids is None:
        ids_l = _auto_make_ids_for_upsert(docs_l, metas_l)
    else:
        ids_l = list(ids)

    return vdb_upsert(ids_l, docs_l, metas_l, embs_l)


