# ragapp/management/commands/chroma_init.py
from django.core.management.base import BaseCommand
from ragapp import chroma_utils as CU

class Command(BaseCommand):
    help = "Chroma 벡터 DB 초기화(시드 문서 2개 추가)"

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.NOTICE(f"Dir={CU.settings.CHROMA_DB_DIR}, Collection={CU.settings.CHROMA_COLLECTION}"))
        res = CU.seed_minimal()
        self.stdout.write(self.style.SUCCESS(f"Inserted: {res.get('inserted')}"))
        self.stdout.write(self.style.SUCCESS(f"Collection: {res.get('collection')}"))
        self.stdout.write(self.style.SUCCESS(f"Count now: {CU.count()}"))
