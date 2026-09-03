"""
API EXCEPTION MIDDLEWARE (core/middleware.py)

Intercepts backend exceptions, validation errors, and business lock failures 
occurring during JSON API requests and converts them into standardized JSON error responses.
"""

from django.http import JsonResponse
from django.core.exceptions import ValidationError, PermissionDenied
try:
    from rest_framework.exceptions import (
        APIException,
        PermissionDenied as DRFPermissionDenied,
        ValidationError as DRFValidationError,
        NotAuthenticated,
        AuthenticationFailed
    )
except ImportError:
    APIException = None
    DRFPermissionDenied = None
    DRFValidationError = None
    NotAuthenticated = None
    AuthenticationFailed = None


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
            # Check for permission denied
            perm_types = (PermissionDenied,)
            if DRFPermissionDenied:
                perm_types = (PermissionDenied, DRFPermissionDenied)
            if isinstance(exception, perm_types):
                return JsonResponse({
                    "status": "error",
                    "message": "Permission Denied: Unauthorized Access"
                }, status=403)

            # Check for authentication failures
            auth_types = ()
            if NotAuthenticated:
                auth_types = (NotAuthenticated, AuthenticationFailed)
            if auth_types and isinstance(exception, auth_types):
                return JsonResponse({
                    "status": "error",
                    "message": "Authentication Required"
                }, status=401)

            # Check for validation errors
            val_types = (ValidationError,)
            if DRFValidationError:
                val_types = (ValidationError, DRFValidationError)
            if isinstance(exception, val_types):
                error_dict = exception.message_dict if hasattr(exception, 'message_dict') else {'detail': exception.messages if hasattr(exception, 'messages') else str(exception)}
                return JsonResponse({
                    "status": "error",
                    "message": "Validation Failure",
                    "errors": error_dict
                }, status=400)

            # General DRF APIException
            if APIException and isinstance(exception, APIException):
                return JsonResponse({
                    "status": "error",
                    "message": str(exception.detail) if hasattr(exception, 'detail') else str(exception)
                }, status=exception.status_code)

            return JsonResponse({
                "status": "error",
                "message": str(exception) or "Internal Operational Error"
            }, status=500)

        return None


class ProcessExecutionUserMiddleware:
    """
    Middleware that captures the authenticated user of the active HTTP request
    into a thread-safe ContextVar, ensuring operational audit logs automatically
    attribute the triggering user.
    Uses try...finally to strictly guarantee ContextVar token reset and prevent
    user state leakage across re-used ASGI/WSGI worker threads.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user if getattr(request, 'user', None) and request.user.is_authenticated else None
        from core.services.logging_service import set_current_authenticated_user, reset_current_authenticated_user
        token = set_current_authenticated_user(user)
        try:
            return self.get_response(request)
        finally:
            reset_current_authenticated_user(token)
