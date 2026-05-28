# ragapp/management/commands/chroma_media_fix_paths.py
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.core.files.storage import default_storage

from ragapp.services.chroma_media import images_coll


def _fix_to_storage_key(p: str) -> str | None:
    """
    /workspace/images/2026/01/x.jpg  -> images/2026/01/x.jpg
    C:\\...\\images\\2026\\01\\x.jpg -> images/2026/01/x.jpg
    """
    s = (p or "").strip()
    if not s or s == "-":
        return None
    if s.startswith("http://") or s.startswith("https://"):
        return s  # 이미 URL이면 유지

    s2 = s.replace("\\", "/")

    # 흔한 케이스: /workspace/images/... 또는 로컬 경로 .../images/...
    marker = "/images/"
    if marker in s2:
        return "images/" + s2.split(marker, 1)[1].lstrip("/")

    # 혹시 uploads/... 같은 형태가 들어온 경우도 방어
    marker2 = "/uploads/"
    if marker2 in s2:
        return "uploads/" + s2.split(marker2, 1)[1].lstrip("/")

    # 상대키(images/...)면 그대로
    if s2.startswith("images/") or s2.startswith("uploads/") or s2.startswith("meta/"):
        return s2.lstrip("/")

    return None


class Command(BaseCommand):
    help = "Fix bad image metadata paths in Chroma (e.g., /workspace/images/... -> images/...)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Actually update/delete. Default is dry-run.")
        parser.add_argument("--drop-unfixable", action="store_true", help="Delete records that cannot be fixed.")
        parser.add_argument("--drop-missing", action="store_true", help="Delete records whose fixed key is missing in storage.")
        parser.add_argument("--batch", type=int, default=200, help="Batch size for scanning.")
        parser.add_argument("--limit", type=int, default=0, help="Stop after N items (0 = no limit).")

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        drop_unfixable = bool(opts["drop_unfixable"])
        drop_missing = bool(opts["drop_missing"])
        batch = max(1, int(opts["batch"]))
        limit = max(0, int(opts["limit"]))

        c = images_coll()

        offset = 0
        seen = 0
        fixed = 0
        deleted = 0
        unchanged = 0

        self.stdout.write(self.style.WARNING(f"[chroma_media_fix_paths] mode={'APPLY' if apply else 'DRY-RUN'}"))

        while True:
            got = c.get(
                include=["metadatas", "documents"],
                limit=batch,
                offset=offset,
            )
            ids = got.get("ids") or []
            metas = got.get("metadatas") or []
            docs = got.get("documents") or []

            if not ids:
                break

            # 안전: 길이 맞추기
            if len(metas) < len(ids):
                metas = metas + [{} for _ in range(len(ids) - len(metas))]
            if len(docs) < len(ids):
                docs = docs + ["" for _ in range(len(ids) - len(docs))]

            to_delete = []
            for i, pid in enumerate(ids):
                if limit and seen >= limit:
                    break

                seen += 1
                meta = metas[i] if isinstance(metas[i], dict) else {}
                doc = docs[i] if isinstance(docs[i], str) else ""

                old_path = str(meta.get("path", "") or "").strip()
                new_path = _fix_to_storage_key(old_path)

                if not new_path:
                    if drop_unfixable:
                        to_delete.append(pid)
                        deleted += 1
                        self.stdout.write(f"DELETE(unfixable): {pid} path={old_path!r}")
                    else:
                        unchanged += 1
                        self.stdout.write(f"KEEP(unfixable): {pid} path={old_path!r}")
                    continue

                # URL이면 그냥 유지(존중)
                if new_path.startswith("http://") or new_path.startswith("https://"):
                    unchanged += 1
                    continue

                # storage에 없으면 드랍 옵션
                if drop_missing:
                    try:
                        exists = default_storage.exists(new_path)
                    except Exception:
                        exists = False
                    if not exists:
                        to_delete.append(pid)
                        deleted += 1
                        self.stdout.write(f"DELETE(missing): {pid} key={new_path!r}")
                        continue

                # 변경 없으면 패스
                if old_path.replace("\\", "/") == new_path:
                    unchanged += 1
                    continue

                # 업데이트
                new_meta = dict(meta)
                new_meta["path"] = new_path
                if apply:
                    c.update(ids=[pid], metadatas=[new_meta])
                fixed += 1
                self.stdout.write(f"FIX: {pid} {old_path!r} -> {new_path!r}")

            if apply and to_delete:
                c.delete(ids=to_delete)

            offset += batch

            if limit and seen >= limit:
                break

        self.stdout.write(self.style.SUCCESS(
            f"Done. scanned={seen}, fixed={fixed}, deleted={deleted}, unchanged={unchanged}"
        ))
