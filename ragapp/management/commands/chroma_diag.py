from django.core.management.base import BaseCommand
import os

class Command(BaseCommand):
    help = "Chroma(media/db) collections/count quick diagnostics"

    def handle(self, *args, **opts):
        db_dir = os.environ.get("CHROMA_DB_DIR", "")
        media_dir = os.environ.get("CHROMA_MEDIA_DIR", "")
        self.stdout.write(f"CHROMA_DB_DIR={db_dir}")
        self.stdout.write(f"CHROMA_MEDIA_DIR={media_dir}")

        try:
            import chromadb
            from chromadb import PersistentClient
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"chromadb import failed: {e!r}"))
            return

        def stat(path: str):
            self.stdout.write("")
            self.stdout.write(f"-- {path} --")
            self.stdout.write(f"exists={os.path.exists(path)}")
            try:
                client = PersistentClient(path=path)
                cols = client.list_collections()
                self.stdout.write("collections=" + str([c.name for c in cols]))
                for c in cols:
                    col = client.get_collection(c.name)
                    self.stdout.write(f"count {c.name} = {col.count()}")
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"open/query failed: {e!r}"))

        if db_dir:
            stat(db_dir)
        if media_dir:
            stat(media_dir)
