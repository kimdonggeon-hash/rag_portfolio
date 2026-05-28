from django.core.management.base import BaseCommand
import os

class Command(BaseCommand):
    help = "Probe chroma image collection count/peek"

    def handle(self, *args, **options):
        from ragapp.services.chroma_media import _client

        c = _client()

        name = os.environ.get("CHROMA_IMAGES_COLLECTION", "media_images")
        col = c.get_or_create_collection(name=name)

        self.stdout.write(f"collection={name}")
        self.stdout.write(f"count={col.count()}")
        self.stdout.write(f"peek={col.peek(5)}")

        # 환경/경로도 같이 확인(원인 추적용)
        self.stdout.write(f"CHROMA_MEDIA_DIR={os.environ.get('CHROMA_MEDIA_DIR')}")
