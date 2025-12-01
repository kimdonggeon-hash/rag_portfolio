from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List
from django.core.management.base import BaseCommand, CommandParser

from ragapp.services.vertex_embed import embed_texts
from ragapp.services.chroma_media import add_table_rows

class Command(BaseCommand):
    help = "CSV 파일을 읽어 행 단위로 Chroma(table_rows)에 인덱싱합니다."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("csv_path", type=str, help="CSV 파일 경로 (UTF-8 권장)")
        parser.add_argument("--table", type=str, default=None, help="테이블 이름(기본: 파일명)")
        parser.add_argument("--limit", type=int, default=0, help="최대 행 수(0=전체)")

    def handle(self, *args, **opts):
        csv_path = Path(opts["csv_path"]).resolve()
        if not csv_path.exists():
            self.stderr.write(self.style.ERROR(f"[!] 경로 없음: {csv_path}"))
            return
        table_name = opts["table"] or csv_path.stem
        limit = int(opts["limit"])

        rows: List[Dict] = []
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                rows.append(row)
                if limit and len(rows) >= limit:
                    break

        def row_to_str(r: Dict) -> str:
            return " | ".join(f"{k}:{r.get(k,'')}" for k in r.keys())

        texts = [row_to_str(r) for r in rows]
        embs = []
        # 임베딩 배치(간단 루프; 필요 시 최적화 가능)
        for t in texts:
            embs.append(embed_texts([t])[0])

        added = add_table_rows(table_name=table_name, rows=rows, embeddings=embs)
        self.stdout.write(self.style.SUCCESS(f"인덱싱 완료: {csv_path.name} → {table_name} ({added} rows)"))
