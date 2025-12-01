# ragapp/management/commands/check_chroma.py
from __future__ import annotations
import os
import platform
from pathlib import Path
from typing import Optional

from django.core.management.base import BaseCommand
from django.conf import settings

class Command(BaseCommand):
    help = "Chroma DB 경로/버전/컬렉션 상태를 점검합니다."

    def add_arguments(self, parser):
        parser.add_argument("--collection", "-c", help="특정 컬렉션의 문서 수만 보고", default=None)

    def handle(self, *args, **opts):
        self.stdout.write("=== Chroma DB 점검 ===")
        self.stdout.write(f"Python    : {platform.python_version()} ({platform.platform()})")
        try:
            import importlib.metadata as md
            ver = md.version("chromadb")
        except Exception:
            ver = "unknown"
        self.stdout.write(f"chromadb  : {ver}")

        db_dir = settings.CHROMA_DB_DIR
        self.stdout.write(f"DB DIR    : {db_dir}")
        p = Path(db_dir)

        self.stdout.write(f"Exists    : {p.exists()}")
        self.stdout.write(f"Writable  : {os.access(p if p.exists() else p.parent, os.W_OK)}")

        # 안쪽 파일 몇 개 프리뷰
        if p.exists():
            children = list(p.glob("*"))[:10]
            if children:
                self.stdout.write("Dir list  :")
                for ch in children:
                    self.stdout.write(f"  - {ch.name}/" if ch.is_dir() else f"  - {ch.name}")

        # 클라이언트 접속 & 컬렉션
        try:
            from ragapp.services.chroma_client import get_chroma_client
            client = get_chroma_client()
            cols = client.list_collections()
            names = [c.name for c in cols]
            self.stdout.write(f"Collections ({len(names)}): {names}")

            # 선택 컬렉션 카운트
            target = opts.get("collection")
            if target:
                try:
                    col = client.get_collection(target)
                except Exception:
                    col = client.get_or_create_collection(target)
                try:
                    n = col.count()
                except TypeError:
                    n = col.count
                self.stdout.write(f"[{target}] count = {n}")
        except Exception as e:
            self.stderr.write(f"Chroma 접속 실패: {e}")
            self.stderr.write("→ 가상환경/패키지/권한/경로를 확인하세요.")
