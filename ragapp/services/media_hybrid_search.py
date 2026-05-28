# ragapp/services/media_hybrid_search.py
from __future__ import annotations

import math
import os
import re
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "an", "the", "of", "to", "in", "on", "for", "with", "and", "or",
    "is", "are", "was", "were",
    "사진", "이미지", "그림", "검색", "찾아줘", "관련", "같은", "느낌", "비슷한",
}

# ✅ 이미지 컬렉션명/차원 고정 (텍스트→이미지 검색 안정화 핵심)
IMAGES_COLLECTION_NAME = (os.getenv("CHROMA_IMAGES_COLLECTION", "media_images") or "").strip()
MM_FIXED_DIM = 1408  # multimodalembedding@001 기준 안전 고정


def _collection_name(collection: Any) -> str:
    """
    Chroma 컬렉션 객체에서 이름을 최대한 안전하게 뽑는다.
    (버전/구현에 따라 속성명이 다를 수 있어 후보를 여러 개 본다)
    """
    for k in ("name", "_name", "collection_name"):
        v = getattr(collection, k, None)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _looks_like_image_collection(collection: Any) -> bool:
    """
    이미지 컬렉션 여부 판단:
    - CHROMA_IMAGES_COLLECTION 과 정확히 일치하면 True
    - 아니면 이름 휴리스틱(image/images 포함)으로 보조 판단
    """
    name = _collection_name(collection)
    if not name:
        return False
    if IMAGES_COLLECTION_NAME and name == IMAGES_COLLECTION_NAME:
        return True
    n = name.lower()
    return ("image" in n) or ("images" in n)


def _get_hnsw_space(collection: Any) -> str:
    """
    Chroma 컬렉션의 distance space를 최대한 안전하게 가져온다.
    - 보통 metadata["hnsw:space"] 에 "cosine"|"l2"|"ip" 등이 들어간다.
    - 못 찾으면 cosine 로 가정(보수적 기본값).
    """
    md = getattr(collection, "metadata", None)
    if isinstance(md, dict):
        v = md.get("hnsw:space") or md.get("space")
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return "cosine"


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _tokenize(q: str) -> List[str]:
    qn = _norm(q)
    parts = re.findall(r"[0-9a-zA-Z가-힣_\-]+", qn)
    toks: List[str] = []
    for t in parts:
        if len(t) < 2:
            continue
        if t in _STOPWORDS:
            continue
        toks.append(t)

    # 중복 제거(순서 유지)
    seen = set()
    out: List[str] = []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _extract_quoted_phrase(q: str) -> Optional[str]:
    m = re.search(r'"([^"]+)"', q)
    if m and m.group(1).strip():
        return _norm(m.group(1))
    m = re.search(r"'([^']+)'", q)
    if m and m.group(1).strip():
        return _norm(m.group(1))
    return None


def _vec_norm(v: Optional[List[float]]) -> Optional[float]:
    if not v:
        return None
    try:
        return math.sqrt(sum((float(x) * float(x)) for x in v))
    except Exception:
        return None


def _distance_to_similarity(d: Optional[float], *, space: str = "cosine") -> float:
    """
    Chroma distance -> similarity (0..1) 변환 (space 기반 정확 변환).

    - cosine:
        distance = 1 - cosine_sim  (일반적으로 0..2 범위 가능)
        sim = 1 - distance

    - l2/euclidean:
        distance는 거리. sim = 1/(1+distance)

    - ip(inner product):
        구현/버전에 따라 값 스케일이 다를 수 있어 보수적으로 1/(1+max(0,d))로 처리
        (필요하면 프로젝트에서 ip 사용 시 별도 튜닝)
    """
    if d is None:
        return 0.0
    try:
        dd = float(d)
    except Exception:
        return 0.0

    if dd < 0.0:
        dd = 0.0

    sp = (space or "cosine").strip().lower()

    if sp == "cosine":
        sim = 1.0 - dd
    elif sp in ("l2", "euclidean"):
        sim = 1.0 / (1.0 + dd)
    elif sp in ("ip", "inner_product", "innerproduct"):
        # ip는 스케일이 들쭉날쭉할 수 있어 보수적으로 거리형 변환
        sim = 1.0 / (1.0 + dd)
    else:
        # 모를 때는 안전 폴백:
        # 0..2 정도면 cosine로 보는 게 보통이고, 그 외면 거리형으로
        sim = (1.0 - dd) if dd <= 2.0001 else (1.0 / (1.0 + dd))

    if sim < 0.0:
        sim = 0.0
    if sim > 1.0:
        sim = 1.0
    return float(sim)


def _meta_text(meta: Dict[str, Any], doc: str = "") -> str:
    """
    캡션/태그/파일명/원본명/검색용 합성 문자열(search_text)까지 포함.
    """
    chunks: List[str] = []
    if isinstance(doc, str) and doc.strip():
        chunks.append(doc)

    for k in (
        "search_text",
        "caption",
        "title",
        "name",
        "filename",
        "file_name",
        "original_name",
        "basename",
        "orig_name",
        "orig_stem",
        "source",
        "url",
        "path",
    ):
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            chunks.append(v)

    tags = meta.get("tags")
    if isinstance(tags, str) and tags.strip():
        chunks.append(tags)
    elif isinstance(tags, list):
        chunks.append(" ".join([str(x) for x in tags if x is not None]))

    return _norm(" ".join(chunks))


def _keyword_score(meta: Dict[str, Any], doc: str, keywords: List[str], phrase: Optional[str]) -> float:
    """
    0~1 점수: keywords 포함률 + phrase 보너스.
    """
    if not keywords and not phrase:
        return 0.0

    text = _meta_text(meta, doc)
    if not text:
        return 0.0

    bonus = 0.0
    if phrase and phrase in text:
        bonus += 0.35

    hits = 0
    for kw in keywords:
        if kw in text:
            hits += 1

    base = 0.0 if not keywords else (hits / max(1, len(keywords)))
    score = base + bonus
    return 1.0 if score > 1.0 else float(score)


@dataclass
class MediaHit:
    id: str
    meta: Dict[str, Any]
    doc: str
    distance: Optional[float]
    vec_sim: float
    kw: float
    score: float


def _iter_collection_metas(
    collection: Any,
    *,
    batch: int = 500,
    limit: int = 2000,
) -> Iterable[tuple[str, Dict[str, Any], str]]:
    """
    Chroma 컬렉션의 metadatas(+documents 일부)를 페이지로 훑는다(작은 컬렉션에서만 권장).
    """
    off = 0
    seen = 0
    while True:
        if limit and seen >= limit:
            return

        got = collection.get(include=["metadatas", "documents"], limit=int(batch), offset=int(off))
        ids = got.get("ids") or []
        metas = got.get("metadatas") or []
        docs = got.get("documents") or []

        if not ids:
            return

        for i, _id in enumerate(ids):
            if limit and seen >= limit:
                return
            m = metas[i] if i < len(metas) else {}
            d = docs[i] if i < len(docs) else ""
            yield str(_id), (m if isinstance(m, dict) else {}), (d if isinstance(d, str) else "")
            seen += 1

        off += int(batch)


def hybrid_search_chroma(
    collection: Any,
    query_text: str,
    *,
    query_embedding: Optional[List[float]] = None,
    fetch_k: int = 120,
    top_k: int = 30,
    min_vec_sim: float = 0.18,
    w_vec: float = 0.60,
    w_kw: float = 0.40,
    enable_meta_scan: bool = True,
    meta_scan_limit: int = 2000,
    meta_scan_batch: int = 500,
    mode: str = "hybrid",  # "hybrid" | "vec"
    debug_out: Optional[Dict[str, Any]] = None,
) -> List[MediaHit]:
    q = (query_text or "").strip()
    if not q:
        return []

    mode = (mode or "hybrid").strip().lower()
    if mode not in ("hybrid", "vec"):
        mode = "hybrid"

    keywords = _tokenize(q)
    phrase = _extract_quoted_phrase(q)

    if mode == "vec":
        keywords = []
        phrase = None
        w_vec, w_kw = 1.0, 0.0
        enable_meta_scan = False

    # ✅ 이미지 컬렉션인지 판단
    is_img_coll = _looks_like_image_collection(collection)

    # ✅ 이미지 컬렉션에서 query_embedding이 없으면, query_texts 경로(내장 임베딩)로 타는 것을 막고
    #    멀티모달 텍스트 임베딩(1408)로 직접 생성한다.
    skip_query_texts = False
    need_vec = (mode == "vec") or (float(w_vec) > 0.0)

    if is_img_coll and need_vec and not query_embedding:
        try:
            from ragapp.services.vertex_embed import embed_text_mm
            query_embedding = embed_text_mm(q, dim=MM_FIXED_DIM)
            if debug_out is not None:
                debug_out["q_embed_source"] = "auto_vertex_mm_text"
        except Exception as e:
            # 임베딩 생성 실패 시: query_texts로 벡터검색 하지 말고(엉뚱한 결과 방지) 메타스캔으로 폴백
            query_embedding = None
            skip_query_texts = True
            w_vec, w_kw = 0.0, 1.0
            mode = "hybrid"
            if debug_out is not None:
                debug_out["q_embed_source"] = "auto_failed"
                debug_out["q_embed_error"] = str(e)

    # ✅ 이미지 컬렉션에서는 1408 차원만 허용(혼용 방지)
    if is_img_coll and query_embedding:
        if len(query_embedding) != MM_FIXED_DIM:
            if debug_out is not None:
                debug_out["q_embed_dim_mismatch"] = True
                debug_out["q_embed_dim"] = len(query_embedding)
            query_embedding = None
            skip_query_texts = True
            w_vec, w_kw = 0.0, 1.0
            mode = "hybrid"

    qnorm = _vec_norm(query_embedding)

    # ✅ 컬렉션 distance space 확보(거리→유사도 변환 정확화)
    space = _get_hnsw_space(collection)

    # ─────────────────────────────────────────────
    # 1) 벡터 후보 확보
    # ─────────────────────────────────────────────
    raw = None
    raw_query_kind = "embeddings" if query_embedding is not None else ("skipped" if skip_query_texts else "texts")

    try:
        if query_embedding is not None:
            raw = collection.query(
                query_embeddings=[query_embedding],
                n_results=int(fetch_k),
                include=["metadatas", "distances", "documents"],
            )
        else:
            # ✅ 이미지 컬렉션에서 query_texts 경로는 차단(임베딩 공간 혼용 방지)
            if skip_query_texts:
                raw = None
            else:
                raw = collection.query(
                    query_texts=[q],
                    n_results=int(fetch_k),
                    include=["metadatas", "distances", "documents"],
                )
    except Exception as e:
        raw = None
        if debug_out is not None:
            debug_out["raw_query_error"] = str(e)

    if debug_out is not None:
        debug_out["collection_name"] = _collection_name(collection)
        debug_out["is_image_collection"] = bool(is_img_coll)
        debug_out["raw_query_kind"] = raw_query_kind
        debug_out["q_embed_provided"] = bool(query_embedding)
        debug_out["q_embed_dim"] = (len(query_embedding) if query_embedding else 0)
        debug_out["query_vec_norm"] = None if qnorm is None else float(round(qnorm, 6))
        debug_out["hnsw_space"] = space

    hits_raw: List[MediaHit] = []
    ids_len = 0

    if raw:
        ids0 = (raw.get("ids") or [[]])[0]
        metas0 = (raw.get("metadatas") or [[]])[0]
        docs0 = (raw.get("documents") or [[]])[0]
        dists0 = (raw.get("distances") or [[]])[0]

        ids_len = len(ids0)

        # 디버그: dist 샘플을 함께 남기면 cosine/l2 판단이 쉬움
        if debug_out is not None and isinstance(dists0, list):
            debug_out["dist_samples"] = dists0[: min(10, len(dists0))]

        for i, _id in enumerate(ids0):
            meta = metas0[i] if i < len(metas0) and isinstance(metas0[i], dict) else {}
            doc = docs0[i] if i < len(docs0) and isinstance(docs0[i], str) else ""
            dist = dists0[i] if i < len(dists0) else None
            vec_sim = _distance_to_similarity(dist, space=space)

            kw = _keyword_score(meta, doc, keywords, phrase) if (keywords or phrase) else 0.0
            score = (float(w_vec) * vec_sim) + (float(w_kw) * kw)

            hits_raw.append(
                MediaHit(
                    id=str(_id),
                    meta=meta,
                    doc=doc,
                    distance=dist if isinstance(dist, (int, float)) else None,
                    vec_sim=float(vec_sim),
                    kw=float(kw),
                    score=float(score),
                )
            )

    top_vec = max((h.vec_sim for h in hits_raw), default=0.0)
    dynamic_floor = max(float(min_vec_sim), float(top_vec) - 0.30)

    # ─────────────────────────────────────────────
    # 2) 후보 필터링 (0점 꼬리 차단 포함)
    # ─────────────────────────────────────────────
    candidates = [h for h in hits_raw if (h.kw > 0.0) or (h.vec_sim >= dynamic_floor)]

    if mode == "hybrid" and keywords:
        rel_floor = max(dynamic_floor, top_vec * 0.78)
        candidates = [h for h in candidates if (h.kw > 0.0) or (h.vec_sim >= rel_floor)]

        if any(h.kw > 0.0 for h in candidates):
            keep_vec_floor = max(dynamic_floor, top_vec * 0.82)
            pruned = [h for h in candidates if (h.kw > 0.0) or (h.vec_sim >= keep_vec_floor)]
            if pruned:
                candidates = pruned

    if phrase:
        text_hit_any = any(phrase in _meta_text(h.meta, h.doc) for h in candidates)
        if text_hit_any:
            candidates = [h for h in candidates if phrase in _meta_text(h.meta, h.doc)]

    candidates = [h for h in candidates if not (h.vec_sim <= 0.0 and h.kw <= 0.0)]

    # ─────────────────────────────────────────────
    # 3) 벡터 후보가 없으면 메타 스캔(캡션/태그 매치)
    # ─────────────────────────────────────────────
    if (not candidates) and enable_meta_scan and (keywords or phrase):
        found: List[MediaHit] = []
        for _id, meta, doc in _iter_collection_metas(
            collection,
            batch=int(meta_scan_batch),
            limit=int(meta_scan_limit),
        ):
            kw = _keyword_score(meta, doc, keywords, phrase)
            if kw <= 0.0:
                continue
            if phrase and phrase not in _meta_text(meta, doc):
                continue
            found.append(
                MediaHit(
                    id=str(_id),
                    meta=meta,
                    doc=doc,
                    distance=None,
                    vec_sim=0.0,
                    kw=float(kw),
                    score=float(kw),
                )
            )

        found.sort(key=lambda x: x.score, reverse=True)
        out = found[: int(top_k)]

        if debug_out is not None:
            debug_out.update(
                {
                    "mode": mode,
                    "q": q,
                    "fetch_k": int(fetch_k),
                    "ids_len": int(ids_len),
                    "raw_candidates_len": int(len(hits_raw)),
                    "kept_candidates_len": 0,
                    "top_vec": float(round(top_vec, 4)),
                    "min_vec_sim": float(round(float(min_vec_sim), 4)),
                    "dynamic_floor": float(round(dynamic_floor, 4)),
                    "keywords": keywords,
                    "phrase": phrase,
                    "meta_scan_used": True,
                    "sample_raw_candidates": [
                        {
                            "pid": h.id,
                            "vec_sim": round(h.vec_sim, 4),
                            "kw": round(h.kw, 4),
                            "path": (h.meta.get("path") if isinstance(h.meta, dict) else None),
                            "storage_key": (h.meta.get("storage_key") if isinstance(h.meta, dict) else None),
                            "url": (h.meta.get("url") if isinstance(h.meta, dict) else None),
                        }
                        for h in out[:10]
                    ],
                }
            )
        return out

    # ─────────────────────────────────────────────
    # 4) 최종 정렬/반환 + 디버그
    # ─────────────────────────────────────────────
    candidates.sort(key=lambda x: x.score, reverse=True)
    out = candidates[: int(top_k)]

    if debug_out is not None:
        debug_out.update(
            {
                "mode": mode,
                "q": q,
                "fetch_k": int(fetch_k),
                "ids_len": int(ids_len),
                "raw_candidates_len": int(len(hits_raw)),
                "kept_candidates_len": int(len(candidates)),
                "top_vec": float(round(top_vec, 4)),
                "min_vec_sim": float(round(float(min_vec_sim), 4)),
                "dynamic_floor": float(round(dynamic_floor, 4)),
                "keywords": keywords,
                "phrase": phrase,
                "meta_scan_used": False,
                "sample_raw_candidates": [
                    {
                        "pid": h.id,
                        "vec_sim": round(h.vec_sim, 4),
                        "kw": round(h.kw, 4),
                        "path": (h.meta.get("path") if isinstance(h.meta, dict) else None),
                        "storage_key": (h.meta.get("storage_key") if isinstance(h.meta, dict) else None),
                        "url": (h.meta.get("url") if isinstance(h.meta, dict) else None),
                    }
                    for h in out[:10]
                ],
            }
        )

    return out
