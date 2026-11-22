from . import views


class HealthCheckMiddleware:
    """Answers /health before Django checks ALLOWED_HOSTS.

    Hosting platforms probe the container on its internal address, which isn't
    an allowed host, so the probe would otherwise get a 400.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/health":
            return views.health(request)
        return self.get_response(request)
