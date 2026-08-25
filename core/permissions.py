"""
DRF PERMISSION CLASSES (core/permissions.py)

Role-Based Authorization & Object-Level Lifecycle Guards for Shop-Floor MES/ERP
and Sales/Billing Subsystems.
"""

from rest_framework.permissions import BasePermission


class IsProductionSupervisor(BasePermission):
    """
    Grants access if the user is a superuser, belongs to the 'Production Supervisor'
    group, or holds the custom permissions 'core.can_start_production' / 'core.can_resolve_shortage'.
    """
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_superuser:
            return True
        if request.user.groups.filter(name='Production Supervisor').exists():
            return True
        if request.user.has_perm('core.can_start_production') or request.user.has_perm('core.can_resolve_shortage'):
            return True
        return False


class IsShopFloorOperatorOrSupervisor(BasePermission):
    """
    Grants access if the user is a superuser, or belongs to either the 
    'Shop-Floor Operator' or 'Production Supervisor' groups.
    """
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_superuser:
            return True
        return request.user.groups.filter(
            name__in=['Shop-Floor Operator', 'Production Supervisor']
        ).exists()


class IsWorkOrderActiveForLogging(BasePermission):
    """
    Object-level permission enforcing that material consumption can ONLY be 
    logged on Work Orders currently in 'IN_PROGRESS' status.
    Rejects logging on DRAFT, COMPLETED, CANCELLED, AWAITING_RESOLUTION, or ON_HOLD_SHORTAGE.
    """
    message = "Material consumption can only be logged on active 'IN_PROGRESS' Work Orders."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        return getattr(obj, 'status', None) == 'IN_PROGRESS'


class IsSalesOrBillingStaff(BasePermission):
    """
    Grants access to Sales and Billing subsystem endpoints (Orders, Invoices, Payments, Credit Notes):
    - Superusers and members of 'Production Supervisor', 'Sales Representative', or 'Billing Officer'
      have full access to read and mutate.
    - Safe methods (GET, HEAD, OPTIONS) are allowed for any authenticated staff user.
    - Mutations (POST, PUT, PATCH, DELETE) require explicit membership in 'Sales Representative',
      'Billing Officer', 'Production Supervisor', or superuser status.
    """
    ALLOWED_GROUPS = {'Production Supervisor', 'Sales Representative', 'Billing Officer'}

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False

        if request.user.is_superuser:
            return True

        user_groups = set(request.user.groups.values_list('name', flat=True))
        if user_groups & self.ALLOWED_GROUPS:
            return True

        if request.method in ('GET', 'HEAD', 'OPTIONS') and request.user.is_staff:
            return True

        return False
