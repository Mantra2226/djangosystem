"""
API EXCEPTION MIDDLEWARE (core/middleware.py)

Intercepts backend exceptions, validation errors, and business lock failures 
occurring during JSON API requests and converts them into standardized JSON error responses.
"""

from django.http import JsonResponse
from django.core.exceptions import ValidationError, PermissionDenied


class ApiExceptionMiddleware:
    """
    Middleware that catches unhandled exceptions for API endpoints and 
    formats them into structured JSON response objects.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        # Intercept requests targeting /api/ routes or AJAX calls
        is_api_route = request.path.startswith('/api/')
        is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'

        if is_api_route or is_ajax:
            if isinstance(exception, ValidationError):
                error_dict = exception.message_dict if hasattr(exception, 'message_dict') else {'detail': exception.messages}
                return JsonResponse({
                    "status": "error",
                    "message": "Validation Failure",
                    "errors": error_dict
                }, status=400)

            if isinstance(exception, PermissionDenied):
                return JsonResponse({
                    "status": "error",
                    "message": "Permission Denied: Unauthorized Access"
                }, status=403)

            return JsonResponse({
                "status": "error",
                "message": str(exception) or "Internal Operational Error"
            }, status=500)

        return None
