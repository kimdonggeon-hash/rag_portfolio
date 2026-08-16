"""Compatibility boundary for optional legal-document views.

The fallback responses were historically defined in ``ragsite.urls``. They
remain identical, but isolating them keeps route registration declarative.
"""

from django.http import HttpResponse


try:
    from ragapp.news_views.legal_views import (
        legal_bundle,
        legal_guide,
        legal_overseas,
        legal_privacy,
        legal_tester,
        legal_tos,
    )
except Exception:  # pragma: no cover - preserves the existing startup fallback
    def _legal_stub(_request, what="문서"):
        return HttpResponse(f"{what} 페이지가 준비되지 않았습니다.", status=200)

    def legal_bundle(_request):
        return HttpResponse("{}", content_type="application/json")

    def legal_privacy(request):
        return _legal_stub(request, "개인정보처리방침")

    def legal_tos(request):
        return _legal_stub(request, "이용약관")

    def legal_overseas(request):
        return _legal_stub(request, "국외이전 고지")

    def legal_tester(request):
        return _legal_stub(request, "테스터 안내")

    def legal_guide(request):
        return _legal_stub(request, "이용 가이드")
