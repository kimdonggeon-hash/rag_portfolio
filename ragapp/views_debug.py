# ragapp/views_debug.py  (원하면 분리)
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.conf import settings
from pathlib import Path
import os

@require_GET
def chroma_info(request):
    info = {}
    p = Path(settings.CHROMA_DB_DIR)
    info["CHROMA_DB_DIR"] = settings.CHROMA_DB_DIR
    info["exists"] = p.exists()
    info["writable"] = os.access(p if p.exists() else p.parent, os.W_OK)

    try:
        from ragapp.services.chroma_client import list_collections
        info["collections"] = list_collections()
    except Exception as e:
        info["error"] = str(e)
    return JsonResponse(info)
