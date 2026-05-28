import uuid

class RequestIdMiddleware:
    HEADER_IN = "HTTP_X_REQUEST_ID"
    HEADER_OUT = "X-Request-ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rid = request.META.get(self.HEADER_IN) or f"req-{uuid.uuid4().hex[:16]}"
        request.request_id = rid
        response = self.get_response(request)
        response[self.HEADER_OUT] = rid
        return response
