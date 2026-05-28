# ragapp/views_legal_modal.py
from django.shortcuts import render
from django.views.decorators.http import require_GET

@require_GET
def legal_modal_fragment(request):
    # 모달 HTML partial만 렌더링해서 반환
    return render(request, "ragapp/partials/legal_bundle_modal.html")
