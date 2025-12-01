# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Dict, List

from django.core.management.base import BaseCommand, CommandParser

# ✅ 멀티모달 대신 안전한 텍스트 임베딩 전용 함수로 교체
from ragapp.services.vertex_embed import embed_texts
from ragapp.services.chroma_media import search_images_by_text_embedding


class Command(BaseCommand):
    help = "텍스트 쿼리로 이미지 벡터 공간(top-k) 검색"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("query", type=str, help="텍스트 쿼리 (예: '노을 바다 풍경')")
        parser.add_argument("--k", type=int, default=8, help="반환 수")
        parser.add_argument("--show-distance", action="store_true", help="거리/유사도 출력")

    def handle(self, *args, **opts):
        q: str = opts["query"]
        k: int = opts["k"]
        show_distance: bool = bool(opts.get("show_distance"))

        # 1) 텍스트 임베딩: 항상 TextEmbeddingModel 경로 사용
        vecs = embed_texts([q])  # List[List[float]]
        if not vecs or not vecs[0]:
            self.stderr.write(self.style.ERROR("임베딩 실패: 벡터가 비었습니다."))
            return
        vec = vecs[0]

        # 2) Chroma 검색
        res: Dict[str, Any] = search_images_by_text_embedding(text_embedding=vec, k=k) or {}

        # Chroma 반환 형태 방어적 파싱
        ids: List[str] = (res.get("ids", [[]]) or [[]])[0]
        metas: List[Dict[str, Any]] = (res.get("metadatas", [[]]) or [[]])[0]
        docs: List[str] = (res.get("documents", [[]]) or [[]])[0]
        dists: List[float] = (res.get("distances", [[]]) or [[]])[0]

        self.stdout.write(self.style.NOTICE(f"검색: '{q}' → top-{k}"))
        if not ids:
            self.stdout.write(self.style.WARNING("결과 없음"))
            return

        for i, pid in enumerate(ids, 1):
            meta = metas[i - 1] if i - 1 < len(metas) else {}
            doc = docs[i - 1] if i - 1 < len(docs) else ""
            dist = dists[i - 1] if i - 1 < len(dists) else None

            path = meta.get("path") or meta.get("url") or ""
            line = f"{i:>2}. {pid} | {path} | {doc}"
            if show_distance and dist is not None:
                line += f" | dist={dist:.4f}"
            self.stdout.write(line)
