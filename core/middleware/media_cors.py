# core/middleware/media_cors.py
from django.conf import settings

class MediaCorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.allowed_origins = getattr(settings, "CORS_ALLOWED_ORIGINS", []) or []
        if not self.allowed_origins and getattr(settings, "DEBUG", False):
            self.allowed_origins = ["http://localhost:61554", "http://localhost:5173"]

    def __call__(self, request):
        response = self.get_response(request)
        media_url = getattr(settings, "MEDIA_URL", "/media/")
        if request.path.startswith(media_url) or request.path.startswith("/announcements"):
            origin = request.headers.get("Origin")
            if getattr(settings, "CORS_ALLOW_CREDENTIALS", False):
                if origin and origin in self.allowed_origins:
                    response["Access-Control-Allow-Origin"] = origin
                    response["Access-Control-Allow-Credentials"] = "true"
            else:
                response.setdefault("Access-Control-Allow-Origin", origin or "*")
                response.setdefault("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
                response.setdefault("Access-Control-Allow-Headers", "Authorization, Content-Type")
        return response
