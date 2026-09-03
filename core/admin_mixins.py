"""
ADMIN MIXINS (core/admin_mixins.py)

Provides reusable ModelAdmin mixins for corporate Excel exports and enhanced
Unfold admin integration.
"""

from unfold.decorators import action
from .services.excel_export_service import export_queryset_to_excel


class OpenPyXLExportMixin:
    """
    Mixin for Unfold ModelAdmin classes that introduces corporate-styled Excel export:
    1. Changelist top-bar button ('action_export_all_to_excel') via Unfold actions_list,
       which exports the full filtered dataset (respecting search, date filters, etc.).
    2. Bulk action dropdown ('action_bulk_export_selected_to_excel') for selected rows,
       which respects 'select_across' if clicked.

    Required class attributes on inheriting ModelAdmin:
    - export_fields_map: List of tuples (attr_or_callable, column_header, format_type)
    - export_filename_prefix: String identifier for the downloaded file
    """
    export_fields_map = []
    export_filename_prefix = "export"

    @action(description="Export to Excel (.xlsx)", url_path="export-excel", icon="download")
    def action_export_all_to_excel(self, request):
        """
        Top-bar changelist action: exports the entire filtered dataset
        without requiring checkboxes to be manually selected.
        """
        cl = self.get_changelist_instance(request)
        queryset = cl.get_queryset(request)
        fields_map = getattr(self, 'export_fields_map', [])
        prefix = getattr(self, 'export_filename_prefix', 'export')
        return export_queryset_to_excel(queryset, fields_map, prefix)

    def action_bulk_export_selected_to_excel(self, request, queryset):
        """
        Bulk changelist dropdown action: exports selected records.
        If 'select_across' is active, exports the full filtered queryset.
        """
        if request.POST.get('select_across') == '1':
            cl = self.get_changelist_instance(request)
            queryset = cl.get_queryset(request)

        fields_map = getattr(self, 'export_fields_map', [])
        prefix = getattr(self, 'export_filename_prefix', 'export')
        return export_queryset_to_excel(queryset, fields_map, prefix)

    action_bulk_export_selected_to_excel.short_description = "Export Selected to Excel (.xlsx)"

    def get_actions_list(self, request):
        """
        Merges action_export_all_to_excel into actions_list without overwriting
        existing actions.
        """
        actions = list(super().get_actions_list(request) or [])
        if getattr(self, 'export_fields_map', None):
            action_names = self._extract_action_names(actions)
            if 'action_export_all_to_excel' not in action_names:
                actions.append(self.action_export_all_to_excel)
        return actions

    def get_actions(self, request):
        """
        Ensures action_bulk_export_selected_to_excel is registered in changelist actions.
        """
        actions = super().get_actions(request)
        if getattr(self, 'export_fields_map', None):
            name = 'action_bulk_export_selected_to_excel'
            if name not in actions:
                func = self.action_bulk_export_selected_to_excel
                desc = getattr(func, 'short_description', name)
                actions[name] = (func, name, desc)
        return actions
