from __future__ import annotations

import os
from pathlib import Path
from typing import List
from django.core.management.base import BaseCommand, CommandParser

from ragapp.services.vertex_embed import embed_image_file
from ragapp.services.chroma_media import add_image_item

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}

class Command(BaseCommand):
    help = "이미지 폴더를 재귀적으로 순회하며 Chroma(media_images)에 인덱싱합니다."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("root", type=str, help="이미지 폴더 경로")
        parser.add_argument("--caption-from-name", action="store_true",
                            help="파일명을 캡션으로 사용")

    def handle(self, *args, **opts):
        root = Path(opts["root"]).resolve()
        if not root.exists():
            self.stderr.write(self.style.ERROR(f"[!] 경로 없음: {root}"))
            return

        total, ok = 0, 0
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in IMAGE_EXTS:
                continue
            total += 1
            try:
                vec = embed_image_file(str(p))
                pid = add_image_item(path=str(p), embedding=vec,
                                     caption=(p.stem if opts["caption_from_name"] else ""))
                ok += 1
                self.stdout.write(self.style.SUCCESS(f"[+]{pid}"))
            except Exception as e:
                self.stderr.write(self.style.WARNING(f"[skip]{p}: {e}"))

        self.stdout.write(self.style.NOTICE(f"완료: {ok}/{total} 파일 인덱싱"))
