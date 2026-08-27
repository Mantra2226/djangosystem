"""
DJANGO UNFOLD ADMIN CONFIGURATION (core/admin.py)

Glass Putty Manufacturing ERP - Manufacturing Command Center
Provides enterprise Tailwind-based administrative interface with real-time KPI integration,
tabbed fieldset navigation, comprehensive autocomplete lookup graph, native action buttons,
and role-based lifecycle immutability guards.
"""

from decimal import Decimal
import io

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError, ObjectDoesNotExist, PermissionDenied
from django.db import models
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from unfold.admin import ModelAdmin, TabularInline, StackedInline
from unfold.contrib.filters.admin import RangeDateFilter, RangeNumericFilter, SingleNumericFilter, MultipleRelatedDropdownFilter
from unfold.decorators import display, action

from .models import (
    PurchaseInvoice, Supplier, Product, PurchaseOrder, PurchaseOrderItem, ProcurementOrder, Inventory,
    StockTransaction, Employee, ProductionOrder, ProductionOrderItem, Customer, SalesOrder, SalesOrderItem, DispatchRecord,
    SalesInvoice, Return, MaterialVarianceRecord, FinanceEntry, WorkOrder, WorkOrderInstruction,
    BillOfMaterial, BOMItem, SalesInvoicePayments, PurchasePayment, WorkOrderMaterialLine,
    DocumentSequence, SalesInvoiceLine, CreditNote, CreditNoteLine
)
from .forms import WorkOrderForm
from .utils.pdf_generator import generate_invoice_pdf, generate_credit_note_pdf, generate_finance_entry_pdf


def export_as_csv(modeladmin, request, queryset):
    """
    Exports selected records from any changelist view into a downloadable CSV file.
    Extracts model field values dynamically.
    """
    import csv

    opts = modeladmin.model._meta
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename={opts.verbose_name_plural.replace(" ", "_").lower()}_export.csv'

    writer = csv.writer(response)
    field_names = [field.name for field in opts.fields if not field.many_to_many]
    writer.writerow(field_names)

    for obj in queryset:
        row = []
        for field in field_names:
            val = getattr(obj, field)
            if callable(val):
                try:
                    val = val()
                except Exception:
                    val = ''
            row.append(val)
        writer.writerow(row)
    return response


export_as_csv.short_description = "Export Selected Records to CSV"


# =============================================================================
# INLINE ADMIN DEFINITIONS
# =============================================================================

class WorkOrderInstructionInline(TabularInline):
    model = WorkOrderInstruction
    extra = 0
    fields = ('step_number', 'step_name', 'machine', 'instruction_text', 'estimated_time_minutes', 'status')
    ordering = ['step_number']
    readonly_fields = ['step_number']

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields


class BOMItemInline(TabularInline):
    model = BOMItem
    extra = 1
    fk_name = 'bom'
    fields = ['component', 'quantity_required']
    autocomplete_fields = ['component']


class WorkOrderMaterialLineInline(TabularInline):
    model = WorkOrderMaterialLine
    extra = 0
    fields = ('component', 'quantity_expected', 'quantity_actual')
    readonly_fields = ('quantity_expected',)
    autocomplete_fields = ['component']


class ChildPackagingInline(TabularInline):
    """
    Inline UI for auditing Stage 2 child packaging work orders linked to a Stage 1 parent bulk order.
    """
    model = WorkOrder
    fk_name = 'parent_work_order'
    verbose_name = "Child Packaging Run"
    verbose_name_plural = "Child Packaging Runs"
    extra = 0
    can_delete = False
    fields = ('work_order_code', 'product', 'status', 'quantity_produced', 'production_start_date')
    readonly_fields = ('work_order_code', 'product', 'status', 'quantity_produced', 'production_start_date')
    autocomplete_fields = ['product']


class SalesInvoicePaymentsInline(TabularInline):
    model = SalesInvoicePayments
    extra = 1
    fields = ('amount', 'payment_method', 'paid_at', 'reference_number')
    readonly_fields = ['paid_at']


class DispatchRecordInline(TabularInline):
    """
    Displays a read-only shipping history directly inside the Sales Order page.
    """
    model = DispatchRecord
    extra = 0
    fields = ('dispatch_code', 'sales_order_item', 'product', 'quantity_dispatched', 'dispatch_date')
    readonly_fields = ('dispatch_code', 'product', 'quantity_dispatched', 'dispatch_date')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class SalesOrderItemInline(TabularInline):
    model = SalesOrderItem
    extra = 1
    fields = ('product', 'quantity_ordered', 'quantity_dispatched', 'get_unit_price', 'get_total_price')
    readonly_fields = ('quantity_dispatched', 'get_unit_price', 'get_total_price')
    autocomplete_fields = ['product']

    @display(description='Catalog Unit Price')
    def get_unit_price(self, obj):
        if obj.unit_price is not None:
            return f"${obj.unit_price:,.2f}"
        return "$0.00"

    @display(description='Line Total')
    def get_total_price(self, obj):
        if obj.total_price:
            return f"${obj.total_price:,.2f}"
        return "$0.00"

    def has_add_permission(self, request, obj=None):
        if obj and obj.status != 'draft' and not request.user.is_superuser:
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if obj and obj.status != 'draft' and not request.user.is_superuser:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status != 'draft' and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)


class SalesInvoiceLineInline(TabularInline):
    model = SalesInvoiceLine
    extra = 0
    fields = ('product', 'quantity', 'unit_price', 'tax_rate', 'subtotal', 'tax_amount', 'total_price')
    readonly_fields = ('subtotal', 'tax_amount', 'total_price')
    autocomplete_fields = ['product']

    def has_add_permission(self, request, obj=None):
        if obj and obj.status != 'DRAFT' and not request.user.is_superuser:
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if obj and obj.status != 'DRAFT' and not request.user.is_superuser:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status != 'DRAFT' and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)


class CreditNoteLineInline(TabularInline):
    model = CreditNoteLine
    extra = 0
    fields = ('product', 'quantity', 'unit_price', 'tax_rate', 'subtotal', 'tax_amount', 'total_price')
    readonly_fields = ('subtotal', 'tax_amount', 'total_price')
    autocomplete_fields = ['product']

    def has_add_permission(self, request, obj=None):
        if obj and obj.status != 'DRAFT' and not request.user.is_superuser:
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if obj and obj.status != 'DRAFT' and not request.user.is_superuser:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status != 'DRAFT' and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)


class PurchasePaymentInline(TabularInline):
    model = PurchasePayment
    extra = 1
    fields = ('amount', 'payment_method', 'paid_at', 'reference_number')
    readonly_fields = ['paid_at']


class PurchaseOrderItemInline(TabularInline):
    model = PurchaseOrderItem
    extra = 1
    fields = ('product', 'quantity_ordered', 'quantity_received', 'price_per_unit', 'get_total')
    readonly_fields = ('get_total', 'quantity_received', 'price_per_unit')
    autocomplete_fields = ['product']

    @display(description='Total Cost')
    def get_total(self, obj):
        if obj.pk:
            return f"${obj.total_price:.2f}"
        return "$0.00"


# =============================================================================
# MODEL ADMIN DEFINITIONS (UNFOLD)
# =============================================================================

def render_status_badge(text, bg_color, text_color='#ffffff', border_color=None):
    """
    Renders a unified, high-contrast, modern HTML status pill badge with no raw color name leakage.
    """
    if not text:
        return mark_safe('<span style="color: #94a3b8; font-style: italic;">-</span>')
    border_style = f"border: 1px solid {border_color};" if border_color else ""
    return format_html(
        '<span style="background-color: {bg}; color: {fg}; {border} '
        'padding: 3px 10px; border-radius: 9999px; font-weight: 600; '
        'font-size: 11px; display: inline-flex; align-items: center; justify-content: center; '
        'text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; line-height: 1.2; box-shadow: 0 1px 2px rgba(0,0,0,0.05);">'
        '{text}'
        '</span>',
        bg=bg_color,
        fg=text_color,
        border=border_style,
        text=text
    )


@admin.register(Supplier)
class SupplierAdmin(ModelAdmin):
    list_display = ('supplier_code', 'name', 'contact_info')
    search_fields = ('supplier_code', 'name', 'contact_info')
    readonly_fields = ('supplier_code',)


@admin.register(Product)
class ProductAdmin(ModelAdmin):
    list_display = ('sku', 'name', 'product_type_badge', 'supplier', 'get_selling_price')
    list_filter = ['product_type', 'supplier']
    search_fields = ('sku', 'name', 'category', 'supplier__name', 'supplier__supplier_code')
    readonly_fields = ('sku',)
    autocomplete_fields = ['supplier']

    @display(description='Product Type')
    def product_type_badge(self, obj):
        if not obj or not obj.product_type:
            return "-"
        cat = (obj.category or '').lower()
        name = (obj.name or '').lower()
        if obj.product_type == 'RAW' and any(k in cat or k in name for k in ['packaging', 'pack', 'tin', 'lid', 'label', 'container', 'box']):
            return render_status_badge('Packaging', '#f59e0b')

        color_map = {
            'FINISHED': '#10b981',     # Emerald Green
            'RAW': '#0284c7',          # Sky Blue
            'INTERMEDIATE': '#2563eb', # Royal Blue
        }
        bg = color_map.get(obj.product_type, '#64748b')
        return render_status_badge(obj.get_product_type_display(), bg)

    @display(description='Selling Price')
    def get_selling_price(self, obj):
        if obj.selling_price is not None:
            return f"${obj.selling_price:,.2f}"
        return "-"


class InventoryProductTypeFilter(admin.SimpleListFilter):
    """
    Unified multi-source inventory product type and classification filter.
    Filters Inventory records by product classification:
    - RAW_CHEMICALS: Raw Chemicals & Raw Materials (RAW excluding packaging)
    - PACKAGING: Packaging Materials & Containers (Tins, Lids, Labels, Bottles)
    - INTERMEDIATE: WIP / Bulk Base Putty (INTERMEDIATE)
    - FINISHED: Finished Packaged Goods (FINISHED)
    - ALL_RAW: All Raw Materials (incl. Packaging)
    """
    title = 'Product Classification'
    parameter_name = 'product_type'

    def lookups(self, request, model_admin):
        return (
            ('RAW_CHEMICALS', 'Raw Chemicals & Materials'),
            ('PACKAGING', 'Packaging & Containers'),
            ('INTERMEDIATE', 'WIP / Bulk Base Putty'),
            ('FINISHED', 'Finished Packaged Goods'),
            ('ALL_RAW', 'All Raw Materials (incl. Packaging)'),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if not val:
            return queryset
        val_upper = val.upper().strip()

        # Keyword classification for packaging items (Tins, Lids, Labels, Containers)
        packaging_q = (
            models.Q(product__category__icontains='packaging') |
            models.Q(product__category__icontains='pack') |
            models.Q(product__category__icontains='tin') |
            models.Q(product__category__icontains='lid') |
            models.Q(product__category__icontains='label') |
            models.Q(product__name__icontains='tin') |
            models.Q(product__name__icontains='lid') |
            models.Q(product__name__icontains='label') |
            models.Q(product__name__icontains='container') |
            models.Q(product__name__icontains='box')
        )

        if val_upper in ['RAW_CHEMICALS', 'RAW_CHEMICAL', 'CHEMICALS', 'RAW_MATERIALS']:
            return queryset.filter(product__product_type='RAW').exclude(packaging_q)
        elif val_upper in ['PACKAGING', 'PACK', 'CONTAINERS', 'TINS']:
            return queryset.filter(models.Q(product__product_type='RAW') & packaging_q)
        elif val_upper in ['INTERMEDIATE', 'WIP', 'BULK', 'BASE']:
            return queryset.filter(product__product_type='INTERMEDIATE')
        elif val_upper in ['FINISHED', 'FG', 'FINISHED_GOODS']:
            return queryset.filter(product__product_type='FINISHED')
        elif val_upper in ['ALL_RAW', 'RAW']:
            return queryset.filter(product__product_type='RAW')
        return queryset


@admin.register(Inventory)
class InventoryAdmin(ModelAdmin):
    list_display = (
        'product', 'product_type_badge', 'product_category_display',
        'quantity_available', 'quantity_allocated', 'location',
        'get_unit_cost', 'get_total_valuation', 'last_updated'
    )
    list_filter = [
        InventoryProductTypeFilter,
        'location',
        ('last_updated', RangeDateFilter),
        ('quantity_available', RangeNumericFilter)
    ]
    search_fields = ('product__name', 'product__sku', 'product__category', 'location')
    readonly_fields = ['get_total_valuation', 'quantity_allocated']
    autocomplete_fields = ['product']
    actions = [export_as_csv]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('product', 'product__supplier')

    @display(description='Product Type')
    def product_type_badge(self, obj):
        if not obj.product:
            return "-"
        p_type = obj.product.product_type
        cat = (obj.product.category or '').lower()
        name = (obj.product.name or '').lower()
        if p_type == 'RAW' and any(k in cat or k in name for k in ['packaging', 'pack', 'tin', 'lid', 'label', 'container', 'box']):
            return render_status_badge('Packaging', '#f59e0b')

        color_map = {
            'FINISHED': '#10b981',     # Emerald Green
            'RAW': '#0284c7',          # Sky Blue
            'INTERMEDIATE': '#2563eb', # Royal Blue
        }
        bg = color_map.get(p_type, '#64748b')
        return render_status_badge(obj.product.get_product_type_display(), bg)

    @display(description='Category')
    def product_category_display(self, obj):
        return obj.product.category if (obj.product and obj.product.category) else "-"

    @display(description='Avg Unit Cost')
    def get_unit_cost(self, obj):
        return f"${obj.unit_cost:,.2f}"

    @display(description='Total Valuation')
    def get_total_valuation(self, obj):
        return f"${obj.total_valuation:,.2f}"


@admin.register(StockTransaction)
class StockTransactionAdmin(ModelAdmin):
    list_display = ('product', 'quantity', 'transaction_type_badge', 'get_work_order_code', 'created_at')
    list_filter = ['transaction_type', ('created_at', RangeDateFilter)]
    search_fields = ('product__name', 'product__sku', 'work_order__work_order_code')
    readonly_fields = ('created_at', 'work_order', 'get_work_order_code', 'dispatch_record')
    autocomplete_fields = ['product', 'work_order', 'dispatch_record']

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('product', 'work_order')

    @display(description='Transaction Type')
    def transaction_type_badge(self, obj):
        color_map = {
            'INITIAL_STOCK': '#0284c7',           # Sky Blue
            'PURCHASE_RECEIPT': '#10b981',        # Emerald Green
            'PRODUCTION_CONSUMPTION': '#ef4444',   # Crimson Red
            'PRODUCTION_OUTPUT': '#10b981',        # Emerald Green
            'DISPATCH': '#f59e0b',                 # Amber Gold
            'CUSTOMER_RETURN': '#8b5cf6',          # Purple
            'ADJUSTMENT_IN': '#10b981',            # Emerald Green
            'ADJUSTMENT_OUT': '#ef4444',           # Crimson Red
        }
        bg = color_map.get(obj.transaction_type, '#64748b')
        return render_status_badge(obj.get_transaction_type_display(), bg)

    @display(description='Work Order')
    def get_work_order_code(self, obj):
        if obj.work_order and obj.work_order.work_order_code:
            return obj.work_order.work_order_code
        elif obj.work_order_id:
            return f"WO-{obj.work_order_id}"
        return "-"

    def has_add_permission(self, request):
        return True

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ProductionOrderAdminForm(forms.ModelForm):
    class Meta:
        model = ProductionOrder
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'work_order' in self.fields:
            self.fields['work_order'].required = False
            self.fields['work_order'].help_text = (
                "Optional: Select the Work Order blueprint for this production run. "
                "Only active (uncompleted) Work Orders can be linked to newly created production orders."
            )
            base_qs = WorkOrder.objects.all()
            if self.instance and getattr(self.instance, 'product_id', None):
                base_qs = base_qs.filter(product=self.instance.product)
            self.fields['work_order'].queryset = base_qs

    def clean_work_order(self):
        wo = self.cleaned_data.get('work_order')
        if wo:
            is_new = not self.instance or not self.instance.pk
            orig_wo_id = None
            if not is_new and self.instance.pk:
                orig_wo_id = ProductionOrder.objects.filter(pk=self.instance.pk).values_list('work_order_id', flat=True).first()

            if (is_new or orig_wo_id != wo.pk) and (wo.status or '').upper().strip() == 'COMPLETED':
                raise forms.ValidationError(
                    f"Work Order '{wo.work_order_code or wo.pk}' cannot be linked because it is already COMPLETED. "
                    f"Completed work orders cannot be linked to newly created or reassigned production orders "
                    f"to prevent status mismatch between Blueprint Live Specifications and the production order."
                )
        return wo


class ProductionOrderItemInline(TabularInline):
    model = ProductionOrderItem
    extra = 0
    can_delete = False
    fields = (
        'raw_material',
        'planned_quantity',
        'shortage_quantity',
        'resolution_status_badge',
        'linked_po_link',
        'resolution_notes',
        'resolved_by',
        'resolved_at',
        'action_links',
    )
    readonly_fields = (
        'raw_material',
        'planned_quantity',
        'shortage_quantity',
        'resolution_status_badge',
        'linked_po_link',
        'resolution_notes',
        'resolved_by',
        'resolved_at',
        'action_links',
    )

    @display(description='Resolution Status')
    def resolution_status_badge(self, obj):
        color_map = {
            'NO_SHORTAGE': '#10b981',     # Emerald Green
            'UNRESOLVED': '#ef4444',      # Crimson Red
            'PO_DRAFTED': '#f59e0b',      # Amber
            'OVERRIDDEN': '#0284c7',      # Sky Blue
            'DOWNSCALED': '#d97706',      # Burnt Orange
            'RESOLVED': '#10b981',        # Emerald Green
            'CHILD_WO_CREATED': '#8b5cf6', # Violet
            'HOLD_ACTIVE_RUN': '#06b6d4',  # Cyan
        }
        bg = color_map.get(obj.resolution_status, '#64748b')
        return render_status_badge(obj.get_resolution_status_display(), bg)

    @display(description='Linked PO')
    def linked_po_link(self, obj):
        if obj.linked_purchase_order:
            po = obj.linked_purchase_order
            url = reverse('admin:core_purchaseorder_change', args=[po.pk])
            return format_html('<a href="{}" class="text-primary-600 font-semibold underline">{}</a>', url, po.po_number or f"PO #{po.pk}")
        return "-"

    @display(description='Resolution Actions')
    def action_links(self, obj):
        if not obj or not obj.pk:
            return "-"
        if obj.resolution_status == 'UNRESOLVED':
            po_url = reverse('admin:core_productionorder_resolve_item_po', args=[obj.pk])
            override_url = reverse('admin:core_productionorder_resolve_item_override', args=[obj.pk])
            return format_html(
                '<div style="display: flex; gap: 6px;">'
                '<a href="{}" class="inline-flex items-center px-2 py-1 bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-semibold rounded shadow-sm">Draft PO</a>'
                '<a href="{}" class="inline-flex items-center px-2 py-1 bg-amber-600 hover:bg-amber-700 text-white text-xs font-semibold rounded shadow-sm">Override</a>'
                '</div>',
                po_url,
                override_url
            )
        return format_html('<span class="text-xs text-gray-500 font-medium">{}</span>', obj.get_resolution_status_display())


@admin.register(ProductionOrderItem)
class ProductionOrderItemAdmin(ModelAdmin):
    list_display = ('raw_material', 'production_order', 'planned_quantity', 'shortage_quantity', 'resolution_status_badge', 'linked_purchase_order', 'resolved_at')
    list_filter = ['resolution_status', 'created_at']
    search_fields = ('raw_material__name', 'production_order__production_order_code', 'linked_purchase_order__po_number')
    readonly_fields = ('raw_material', 'production_order', 'planned_quantity', 'shortage_quantity', 'resolution_status', 'linked_purchase_order', 'resolution_notes', 'resolved_by', 'resolved_at', 'created_at', 'updated_at')
    actions = ['action_resolve_item_po', 'action_resolve_item_override']

    @display(description='Status')
    def resolution_status_badge(self, obj):
        color_map = {
            'NO_SHORTAGE': '#10b981',     # Emerald Green
            'UNRESOLVED': '#ef4444',      # Crimson Red
            'PO_DRAFTED': '#f59e0b',      # Amber
            'OVERRIDDEN': '#0284c7',      # Sky Blue
            'DOWNSCALED': '#d97706',      # Burnt Orange
            'RESOLVED': '#10b981',        # Emerald Green
            'CHILD_WO_CREATED': '#8b5cf6', # Violet
            'HOLD_ACTIVE_RUN': '#06b6d4',  # Cyan
        }
        bg = color_map.get(obj.resolution_status, '#64748b')
        return render_status_badge(obj.get_resolution_status_display(), bg)

    @admin.action(description="Resolve Shortage: Auto-Draft PO")
    def action_resolve_item_po(self, request, queryset):
        count = 0
        for item in queryset:
            item.resolve_with_po()
            count += 1
        self.message_user(request, f"Successfully auto-drafted POs for {count} item(s).", level=messages.SUCCESS)

    @admin.action(description="Resolve Shortage: Authorize Override")
    def action_resolve_item_override(self, request, queryset):
        count = 0
        for item in queryset:
            item.resolve_with_override(user=request.user, notes=f"Authorized override by {request.user.username}")
            count += 1
        self.message_user(request, f"Successfully authorized override for {count} item(s).", level=messages.SUCCESS)


@admin.register(ProductionOrder)
class ProductionOrderAdmin(ModelAdmin):
    form = ProductionOrderAdminForm
    inlines = [ProductionOrderItemInline]
    list_display = ('production_order_code', 'product', 'work_order_link', 'quantity', 'status_badge', 'mrp_status_badge', 'get_unit_cost', 'created_at')
    list_filter = ['status', ('created_at', RangeDateFilter)]
    search_fields = ('production_order_code', 'work_order__work_order_code', 'employee__employee_name', 'product__name')
    filter_horizontal = ('employee',)
    readonly_fields = ['production_order_code', 'status', 'is_mrp_resolved', 'resolution_applied', 'resolved_at', 'mrp_resolution_pathways_viewer', 'work_order_details_viewer', 'created_at', 'completed_at']
    autocomplete_fields = ['product', 'work_order']
    actions = [export_as_csv, 'trigger_mrp_auto_resume']
    actions_detail = ['action_trigger_mrp_resume', 'action_evaluate_mrp_button']

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('<int:object_id>/resolve-po/<int:item_id>/', self.admin_site.admin_view(self.action_resolve_po_view), name='core_productionorder_resolve_po'),
            path('<int:object_id>/resolve-inbound/<int:item_id>/', self.admin_site.admin_view(self.action_resolve_inbound_view), name='core_productionorder_resolve_inbound'),
            path('<int:object_id>/resolve-downscale/<int:item_id>/', self.admin_site.admin_view(self.action_resolve_downscale_view), name='core_productionorder_resolve_downscale'),
            path('<int:object_id>/resolve-build-child/<int:item_id>/', self.admin_site.admin_view(self.action_resolve_build_child_view), name='core_productionorder_resolve_build_child'),
            path('<int:object_id>/resolve-hold-active/<int:item_id>/', self.admin_site.admin_view(self.action_resolve_hold_active_view), name='core_productionorder_resolve_hold_active'),
            path('<int:object_id>/resolve-override/<int:item_id>/', self.admin_site.admin_view(self.action_resolve_override_view), name='core_productionorder_resolve_override'),
            path('resolve-item-po/<int:item_id>/', self.admin_site.admin_view(self.resolve_item_po_view), name='core_productionorder_resolve_item_po'),
            path('resolve-item-override/<int:item_id>/', self.admin_site.admin_view(self.resolve_item_override_view), name='core_productionorder_resolve_item_override'),
            path('evaluate-mrp/<int:object_id>/', self.admin_site.admin_view(self.evaluate_mrp_view), name='core_productionorder_evaluate_mrp'),
        ]
        return custom_urls + urls

    def action_resolve_po_view(self, request, object_id, item_id):
        po = get_object_or_404(ProductionOrder, pk=object_id)
        item = get_object_or_404(ProductionOrderItem, pk=item_id, production_order=po)
        from .services import resolve_raw_autodraft_po
        draft_po = resolve_raw_autodraft_po(po, item.raw_material_id, item.shortage_quantity)
        self.message_user(
            request,
            f"Auto-drafted Purchase Order #{draft_po.po_number or draft_po.pk} for {item.raw_material.name}.",
            level=messages.SUCCESS
        )
        return redirect(reverse('admin:core_productionorder_change', args=[po.pk]))

    def action_resolve_inbound_view(self, request, object_id, item_id):
        po = get_object_or_404(ProductionOrder, pk=object_id)
        item = get_object_or_404(ProductionOrderItem, pk=item_id, production_order=po)
        from .services import resolve_raw_hold_inbound
        try:
            resolve_raw_hold_inbound(po, item.raw_material_id)
            self.message_user(
                request,
                f"Shortage for {item.raw_material.name} bound to incoming Purchase Order delivery.",
                level=messages.INFO
            )
        except Exception as e:
            self.message_user(request, f"Inbound hold error: {str(e)}", level=messages.ERROR)
        return redirect(reverse('admin:core_productionorder_change', args=[po.pk]))

    def action_resolve_downscale_view(self, request, object_id, item_id):
        po = get_object_or_404(ProductionOrder, pk=object_id)
        item = get_object_or_404(ProductionOrderItem, pk=item_id, production_order=po)
        from .services import resolve_batch_downscale
        try:
            resolve_batch_downscale(po, item.raw_material_id)
            self.message_user(
                request,
                f"Batch size downscaled to {po.quantity} units based on available stock of {item.raw_material.name}.",
                level=messages.SUCCESS
            )
        except Exception as e:
            self.message_user(request, f"Downscale error: {str(e)}", level=messages.ERROR)
        return redirect(reverse('admin:core_productionorder_change', args=[po.pk]))

    def action_resolve_build_child_view(self, request, object_id, item_id):
        po = get_object_or_404(ProductionOrder, pk=object_id)
        item = get_object_or_404(ProductionOrderItem, pk=item_id, production_order=po)
        from .services import resolve_intermediate_build
        try:
            child_wo, child_po = resolve_intermediate_build(po, item.raw_material_id, item.shortage_quantity)
            self.message_user(
                request,
                f"Spawned Sub-Assembly Work Order #{child_wo.work_order_code or child_wo.pk} (Run #{child_po.pk}) for {item.raw_material.name}.",
                level=messages.SUCCESS
            )
        except Exception as e:
            self.message_user(request, f"Sub-assembly spawn error: {str(e)}", level=messages.ERROR)
        return redirect(reverse('admin:core_productionorder_change', args=[po.pk]))

    def action_resolve_hold_active_view(self, request, object_id, item_id):
        po = get_object_or_404(ProductionOrder, pk=object_id)
        item = get_object_or_404(ProductionOrderItem, pk=item_id, production_order=po)
        from .services import resolve_intermediate_hold_active
        try:
            resolve_intermediate_hold_active(po, item.raw_material_id)
            self.message_user(
                request,
                f"Linked {item.raw_material.name} to active shop floor run.",
                level=messages.INFO
            )
        except Exception as e:
            self.message_user(request, f"Active run hold error: {str(e)}", level=messages.ERROR)
        return redirect(reverse('admin:core_productionorder_change', args=[po.pk]))

    def action_resolve_override_view(self, request, object_id, item_id):
        po = get_object_or_404(ProductionOrder, pk=object_id)
        item = get_object_or_404(ProductionOrderItem, pk=item_id, production_order=po)
        from .services import resolve_item_override
        notes = request.GET.get('notes') or request.POST.get('notes') or f"Authorized override by {request.user.username}"
        resolve_item_override(po, item.raw_material_id, user=request.user, notes=notes)
        self.message_user(
            request,
            f"Authorized shortage override for {item.raw_material.name}.",
            level=messages.SUCCESS
        )
        return redirect(reverse('admin:core_productionorder_change', args=[po.pk]))

    def resolve_item_po_view(self, request, item_id):
        item = get_object_or_404(ProductionOrderItem, pk=item_id)
        po = item.production_order
        item.resolve_with_po()
        self.message_user(
            request,
            f"Auto-drafted Purchase Order #{item.linked_purchase_order.po_number or item.linked_purchase_order.pk} for {item.raw_material.name}.",
            level=messages.SUCCESS
        )
        return redirect(reverse('admin:core_productionorder_change', args=[po.pk]))

    def resolve_item_override_view(self, request, item_id):
        item = get_object_or_404(ProductionOrderItem, pk=item_id)
        po = item.production_order
        notes = request.GET.get('notes') or request.POST.get('notes') or f"Authorized override by {request.user.username}"
        item.resolve_with_override(user=request.user, notes=notes)
        self.message_user(
            request,
            f"Authorized shortage override for {item.raw_material.name}.",
            level=messages.SUCCESS
        )
        return redirect(reverse('admin:core_productionorder_change', args=[po.pk]))

    def evaluate_mrp_view(self, request, object_id):
        po = get_object_or_404(ProductionOrder, pk=object_id)
        po.evaluate_mrp()
        self.message_user(request, f"Evaluated MRP shortages for {po.production_order_code or po.pk}.", level=messages.SUCCESS)
        return redirect(reverse('admin:core_productionorder_change', args=[object_id]))

    @action(description="Evaluate Granular MRP Shortages", url_path="evaluate-mrp")
    def action_evaluate_mrp_button(self, request, object_id):
        return self.evaluate_mrp_view(request, object_id)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('product', 'work_order')

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not obj.work_order_id:
            messages.warning(
                request,
                f"Reminder: Production Order '{obj.production_order_code or obj.pk}' was saved without a linked Work Order."
            )

    @display(description='Status')
    def status_badge(self, obj):
        color_map = {
            'DRAFT': '#64748b',                # Slate Grey
            'PENDING': '#f59e0b',              # Amber
            'IN_PROGRESS': '#2563eb',          # Royal Blue
            'COMPLETED': '#10b981',            # Emerald Green
            'CANCELLED': '#ef4444',            # Crimson Red
            'ON_HOLD_SHORTAGE': '#ef4444',     # Crimson
            'PARTIALLY_RESOLVED': '#f59e0b',   # Amber
            'AWAITING_PROCUREMENT': '#8b5cf6', # Violet
            'READY_TO_START': '#059669',       # Teal Green
            'MRP_RESOLVED': '#10b981',         # Emerald Green
            'AWAITING_RESOLUTION': '#d97706',  # Burnt Orange
        }
        bg = color_map.get(obj.status, '#64748b')
        return render_status_badge(obj.get_status_display(), bg)

    @display(description='MRP Resolution')
    def mrp_status_badge(self, obj):
        if obj.work_order and (obj.work_order.status or '').upper() in ['IN_PROGRESS', 'COMPLETED']:
            applied = obj.resolution_applied or "RESOLVED"
            return render_status_badge(f"LOCKED ({applied})", '#10b981')
        if (obj.status or '').upper() in ['IN_PROGRESS', 'COMPLETED']:
            applied = obj.resolution_applied or "RESOLVED"
            return render_status_badge(f"LOCKED ({applied})", '#10b981')
        if obj.status == 'AWAITING_PROCUREMENT':
            return render_status_badge("AWAITING PROCUREMENT", '#8b5cf6')
        if obj.status == 'READY_TO_START':
            return render_status_badge("READY TO START", '#059669')
        if obj.status == 'MRP_RESOLVED':
            return render_status_badge("RESOLVED / READY", '#10b981')
        if obj.status == 'PARTIALLY_RESOLVED':
            return render_status_badge("PARTIALLY RESOLVED", '#f59e0b')
        return render_status_badge("PENDING EVALUATION", '#f59e0b')

    @action(description="Check Stock & Auto-Resume Order", url_path="mrp-auto-resume")
    def action_trigger_mrp_resume(self, request, object_id):
        from .services import check_and_auto_resume_on_hold_orders
        resumed = check_and_auto_resume_on_hold_orders()
        self.message_user(request, f"MRP evaluation complete. Auto-resumed {len(resumed)} order(s).", level=messages.SUCCESS)
        return redirect(reverse('admin:core_productionorder_change', args=[object_id]))

    @admin.action(description="Check Stock & Auto-Resume On-Hold Orders")
    def trigger_mrp_auto_resume(self, request, queryset):
        from .services import check_and_auto_resume_on_hold_orders
        resumed = check_and_auto_resume_on_hold_orders()
        self.message_user(request, f"MRP evaluation complete. Auto-resumed {len(resumed)} order(s).")

    @display(description='Product')
    def get_product(self, obj):
        return obj.product.name if obj.product else 'N/A'

    @display(description='Work Order')
    def work_order_link(self, obj):
        if not obj.work_order_id:
            return "-"
        try:
            wo = obj.work_order
            if not wo:
                return "-"
            url = reverse('admin:core_workorder_change', args=[wo.pk])
            wo_code = wo.work_order_code or f"WOC-#{wo.pk}"
            prod_name = wo.product.name if wo.product else "No Product"
            display_text = f"{wo_code} — {prod_name}"
            return format_html(
                '<a href="{}" style="color: #2563eb; font-weight: 600; text-decoration: underline;">{}</a>',
                url,
                display_text
            )
        except ObjectDoesNotExist:
            return "-"

    @display(description='Quantity')
    def get_quantity(self, obj):
        if obj.work_order_id:
            try:
                wo = obj.work_order
                if wo:
                    return getattr(wo, 'actual_quantity_produced', getattr(wo, 'quantity_produced', getattr(wo, 'quantity', '0.00')))
            except ObjectDoesNotExist:
                pass
        return obj.quantity or "0.00"

    @display(description='Batch Unit Cost')
    def get_unit_cost(self, obj):
        return f"${obj.unit_cost:,.2f}"

    fieldsets = (
        ('Order Information', {
            'fields': ('production_order_code', 'product', 'work_order', 'quantity', 'status')
        }),
        ('MRP Shortage Resolution Pathways', {
            'fields': ('is_mrp_resolved', 'resolution_applied', 'resolved_at', 'mrp_resolution_pathways_viewer'),
        }),
        ('System Details (Read Only)', {
            'classes': ('collapse',),
            'fields': ('work_order_details_viewer', 'created_at', 'completed_at'),
        }),
    )

    def mrp_resolution_pathways_viewer(self, obj):
        if not obj or not obj.pk:
            return "Save record to evaluate MRP shortages."

        # Locked Banner: Only when production run is actively in progress or completed
        is_active_or_completed = (
            (obj.status or '').upper() in ['IN_PROGRESS', 'COMPLETED'] or
            (obj.work_order and (obj.work_order.status or '').upper() in ['IN_PROGRESS', 'COMPLETED'])
        )
        if is_active_or_completed:
            resolved_time = obj.resolved_at.strftime('%Y-%m-%d %H:%M') if obj.resolved_at else "Run Commencement"
            pathway_text = obj.resolution_applied or "INITIAL_STOCK_ALLOCATION"
            return format_html(
                "<div style='padding: 12px 16px; background: #f0fdf4; border-left: 4px solid #16a34a; color: #166534; border-radius: 4px;'>"
                "<strong>MRP Resolution Locked:</strong> Pathway <code>{}</code> applied at {}. "
                "Production run is active/completed and MRP shortage recalculation is frozen."
                "</div>",
                pathway_text,
                resolved_time
            )

        if not obj.work_order_id:
            return format_html(
                "<div style='padding: 10px; background: #fff8e1; border-left: 4px solid #f57f17; color: #795548; border-radius: 4px;'>"
                "<strong>No Work Order Linked:</strong> Link a Work Order to evaluate recipe requirements and shortage pathways."
                "</div>"
            )

        items = obj.items.select_related('raw_material', 'linked_purchase_order', 'resolved_by').all()
        if not items.exists():
            return mark_safe(
                "<div style='padding: 10px; background: #f8fafc; border-left: 4px solid #94a3b8; color: #475569; border-radius: 4px;'>"
                "<strong>No Material Items Evaluated:</strong> Click <em>'Evaluate Granular MRP Shortages'</em> button to scan recipe requirements against inventory."
                "</div>"
            )

        cards_html = []
        header_banner = ""

        if obj.status == 'AWAITING_PROCUREMENT':
            header_banner = (
                "<div style='margin-bottom: 12px; padding: 12px 16px; background: #f5f3ff; border-left: 4px solid #8b5cf6; color: #5b21b6; border-radius: 6px; box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);'>"
                "<strong>Awaiting Procurement (Purchase Orders Drafted / Sub-Assemblies Spawned):</strong> All component shortages have planned resolutions. "
                "Physical delivery must be received into unallocated warehouse inventory before production can commence."
                "</div>"
            )
        elif obj.status == 'READY_TO_START':
            header_banner = (
                "<div style='margin-bottom: 12px; padding: 12px 16px; background: #ecfdf5; border-left: 4px solid #059669; color: #065f46; border-radius: 6px; box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);'>"
                "<strong>Ready to Start (Stock Verified):</strong> All planned materials are physically available in unallocated warehouse stock. "
                "The Work Order can now start production."
                "</div>"
            )

        for item in items:
            comp = item.raw_material
            p_type = (comp.product_type or 'RAW').upper()
            is_intermediate = (p_type == 'INTERMEDIATE')
            status = item.resolution_status

            if status == 'NO_SHORTAGE':
                card = f"""
                <div style="padding: 12px 14px; background: #f0fdf4; border: 1px solid #bbf7d0; border-left: 4px solid #22c55e; border-radius: 6px; color: #166534; box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <strong style="font-size: 13px; color: #15803d;">{comp.name} [{p_type}]</strong>
                        <span style="font-size: 11px; background: #dcfce7; color: #166534; padding: 2px 8px; border-radius: 10px; font-weight: 700;">In Stock</span>
                    </div>
                    <div style="font-size: 12px; margin-top: 6px; color: #15803d;">
                        <strong>Planned Required:</strong> {item.planned_quantity:.2f} {comp.unit_of_measurement or 'units'}
                    </div>
                </div>
                """
            elif status == 'PO_DRAFTED':
                po_info = "-"
                if item.linked_purchase_order:
                    po = item.linked_purchase_order
                    po_url = reverse('admin:core_purchaseorder_change', args=[po.pk])
                    po_info = f'<a href="{po_url}" style="color: #2563eb; text-decoration: underline; font-weight: 600;">{po.po_number or f"PO #{po.pk}"}</a>'
                card = f"""
                <div style="padding: 12px 14px; background: #eff6ff; border: 1px solid #bfdbfe; border-left: 4px solid #3b82f6; border-radius: 6px; color: #1e40af; box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <strong style="font-size: 13px; color: #1d4ed8;">{comp.name} [{p_type}]</strong>
                        <span style="font-size: 11px; background: #dbeafe; color: #1e40af; padding: 2px 8px; border-radius: 10px; font-weight: 700;">Purchase Order Drafted</span>
                    </div>
                    <div style="font-size: 12px; margin-top: 6px; color: #1d4ed8;">
                        <strong>Shortfall:</strong> -{item.shortage_quantity:.2f} {comp.unit_of_measurement or 'units'} | <strong>Purchase Order:</strong> {po_info}
                    </div>
                    <div style="font-size: 11px; margin-top: 4px; color: #3b82f6;">
                        {item.resolution_notes or 'Awaiting supplier delivery and physical warehouse receipt.'}
                    </div>
                </div>
                """
            elif status == 'CHILD_WO_CREATED':
                card = f"""
                <div style="padding: 12px 14px; background: #faf5ff; border: 1px solid #e9d5ff; border-left: 4px solid #a855f7; border-radius: 6px; color: #6b21a8; box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <strong style="font-size: 13px; color: #7e22ce;">{comp.name} [{p_type}]</strong>
                        <span style="font-size: 11px; background: #f3e8ff; color: #6b21a8; padding: 2px 8px; border-radius: 10px; font-weight: 700;">Sub-Assembly Work Order Spawned</span>
                    </div>
                    <div style="font-size: 12px; margin-top: 6px; color: #7e22ce;">
                        <strong>Shortfall:</strong> -{item.shortage_quantity:.2f} {comp.unit_of_measurement or 'units'}
                    </div>
                    <div style="font-size: 11px; margin-top: 4px; color: #a855f7;">
                        {item.resolution_notes or 'Sub-assembly work order created.'}
                    </div>
                </div>
                """
            elif status == 'HOLD_ACTIVE_RUN':
                card = f"""
                <div style="padding: 12px 14px; background: #ecfeff; border: 1px solid #a5f3fc; border-left: 4px solid #06b6d4; border-radius: 6px; color: #0e7490; box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <strong style="font-size: 13px; color: #0891b2;">{comp.name} [{p_type}]</strong>
                        <span style="font-size: 11px; background: #cffafe; color: #0e7490; padding: 2px 8px; border-radius: 10px; font-weight: 700;">Held for Active Floor Run</span>
                    </div>
                    <div style="font-size: 12px; margin-top: 6px; color: #0891b2;">
                        <strong>Status:</strong> Awaiting completion of shop floor run
                    </div>
                    <div style="font-size: 11px; margin-top: 4px; color: #06b6d4;">
                        {item.resolution_notes or 'Holding for in-progress mixing/sub-assembly run.'}
                    </div>
                </div>
                """
            elif status == 'OVERRIDDEN':
                user_info = item.resolved_by.username if item.resolved_by else 'Supervisor'
                card = f"""
                <div style="padding: 12px 14px; background: #f0f9ff; border: 1px solid #bae6fd; border-left: 4px solid #0284c7; border-radius: 6px; color: #075985; box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <strong style="font-size: 13px; color: #0369a1;">{comp.name} [{p_type}]</strong>
                        <span style="font-size: 11px; background: #e0f2fe; color: #0369a1; padding: 2px 8px; border-radius: 10px; font-weight: 700;">Authorized Override</span>
                    </div>
                    <div style="font-size: 12px; margin-top: 6px; color: #0284c7;">
                        <strong>Shortage:</strong> -{item.shortage_quantity:.2f} {comp.unit_of_measurement or 'units'} | <strong>Authorized By:</strong> {user_info}
                    </div>
                    <div style="font-size: 11px; margin-top: 4px; color: #0284c7;">
                        {item.resolution_notes or 'Authorized to proceed with production despite stock deficit.'}
                    </div>
                </div>
                """
            elif status == 'DOWNSCALED':
                card = f"""
                <div style="padding: 12px 14px; background: #fffbeb; border: 1px solid #fde68a; border-left: 4px solid #d97706; border-radius: 6px; color: #92400e; box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <strong style="font-size: 13px; color: #b45309;">{comp.name} [{p_type}]</strong>
                        <span style="font-size: 11px; background: #fef3c7; color: #92400e; padding: 2px 8px; border-radius: 10px; font-weight: 700;">Downscaled Batch Target</span>
                    </div>
                    <div style="font-size: 12px; margin-top: 6px; color: #b45309;">
                        {item.resolution_notes or 'Batch production target downscaled to match currently available inventory.'}
                    </div>
                </div>
                """
            elif status == 'RESOLVED':
                card = f"""
                <div style="padding: 12px 14px; background: #ecfdf5; border: 1px solid #a7f3d0; border-left: 4px solid #10b981; border-radius: 6px; color: #065f46; box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <strong style="font-size: 13px; color: #047857;">{comp.name} [{p_type}]</strong>
                        <span style="font-size: 11px; background: #d1fae5; color: #065f46; padding: 2px 8px; border-radius: 10px; font-weight: 700;">Resolved</span>
                    </div>
                    <div style="font-size: 12px; margin-top: 6px; color: #047857;">
                        {item.resolution_notes or 'Shortage resolved.'}
                    </div>
                </div>
                """
            else:  # UNRESOLVED
                btn_style = "color: #ffffff !important; text-decoration: none; padding: 6px 12px; border-radius: 4px; font-weight: 600; font-size: 11px; display: inline-flex; align-items: center;"
                action_btns = []

                downscale_url = reverse('admin:core_productionorder_resolve_downscale', args=[obj.pk, item.pk])
                override_url = reverse('admin:core_productionorder_resolve_override', args=[obj.pk, item.pk])

                if not is_intermediate:
                    # RAW material options
                    draft_po_url = reverse('admin:core_productionorder_resolve_po', args=[obj.pk, item.pk])
                    action_btns.append(
                        f'<a href="{draft_po_url}" style="{btn_style} background: #2563eb;">Draft Purchase Order</a>'
                    )
                    # Check inbound PO existence
                    inbound_pos = PurchaseOrder.objects.filter(
                        items__product=comp,
                        status__in=['SENT', 'PARTIAL', 'DRAFT']
                    ).distinct()
                    if inbound_pos.exists():
                        inbound_url = reverse('admin:core_productionorder_resolve_inbound', args=[obj.pk, item.pk])
                        action_btns.append(
                            f'<a href="{inbound_url}" style="{btn_style} background: #475569;">Hold Inbound Purchase Order</a>'
                        )
                    action_btns.append(
                        f'<a href="{downscale_url}" style="{btn_style} background: #d97706;">Downscale Production Batch</a>'
                    )
                    action_btns.append(
                        f'<a href="{override_url}" style="{btn_style} background: #4b5563;">Authorize Supervisor Override</a>'
                    )
                else:
                    # INTERMEDIATE material options
                    child_wo_url = reverse('admin:core_productionorder_resolve_build_child', args=[obj.pk, item.pk])
                    action_btns.append(
                        f'<a href="{child_wo_url}" style="{btn_style} background: #7c3aed;">Trigger Child Work Order</a>'
                    )
                    # Check active runs
                    active_runs = ProductionOrder.objects.filter(
                        product=comp,
                        status='IN_PROGRESS'
                    ).exclude(pk=obj.pk)
                    if active_runs.exists():
                        hold_active_url = reverse('admin:core_productionorder_resolve_hold_active', args=[obj.pk, item.pk])
                        action_btns.append(
                            f'<a href="{hold_active_url}" style="{btn_style} background: #0891b2;">Hold for Active Production Run</a>'
                        )
                    action_btns.append(
                        f'<a href="{downscale_url}" style="{btn_style} background: #d97706;">Downscale Production Batch</a>'
                    )
                    action_btns.append(
                        f'<a href="{override_url}" style="{btn_style} background: #4b5563;">Authorize Supervisor Override</a>'
                    )

                card = f"""
                <div style="padding: 14px; background: #fff5f5; border: 1px solid #fecaca; border-left: 4px solid #ef4444; border-radius: 6px; color: #1f2937; box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <strong style="color: #dc2626; font-size: 13px;">{comp.name} [{p_type}]</strong>
                        <span style="font-size: 11px; background: #fee2e2; color: #dc2626; padding: 2px 8px; border-radius: 10px; font-weight: 700;">Deficit: -{item.shortage_quantity:.2f} {comp.unit_of_measurement or 'units'}</span>
                    </div>
                    <div style="font-size: 12px; margin-top: 6px; color: #4b5563;">
                        <strong>Planned Required:</strong> {item.planned_quantity:.2f} {comp.unit_of_measurement or 'units'}
                    </div>
                    <div style="margin-top: 10px; display: flex; gap: 6px; flex-wrap: wrap;">
                        {"".join(action_btns)}
                    </div>
                </div>
                """
            cards_html.append(card)

        grid_html = f'<div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 12px; margin-top: 8px;">{"".join(cards_html)}</div>'
        return mark_safe(header_banner + grid_html)

    def work_order_details_viewer(self, obj):
        if not obj or not obj.work_order_id:
            return format_html(
                "<div style='padding: 8px 12px; background: #fff3cd; border-left: 4px solid #ffc107; color: #856404; border-radius: 4px;'>"
                "<strong>Reminder:</strong> No Work Order linked to this Production Order."
                "</div>"
            )

        try:
            wo = obj.work_order
        except ObjectDoesNotExist:
            wo = None

        if not wo:
            return format_html(
                "<div style='padding: 8px 12px; background: #fff3cd; border-left: 4px solid #ffc107; color: #856404; border-radius: 4px;'>"
                "Linked Work Order could not be loaded."
                "</div>"
            )

        emp_list = [str(emp) for emp in wo.employee.all()]
        html_string = f"""
        <div id="wo-preview-panel" style="margin-top: 10px; padding: 12px; background: #f8f9fa; border-left: 4px solid #79aec8; border-radius: 4px; color: #333; max-width: 600px;">
            <strong style="color: #555; display: block; margin-bottom: 5px;">Blueprint Live Specifications:</strong>
            <div id="wo-preview-content" style="font-size: 13px; line-height: 1.6;">
                <strong>Target Product:</strong> {wo.product.name if wo.product else 'N/A'} ({getattr(wo.product, 'sku', '')}) <br>
                <strong>Expected Yield:</strong> {wo.actual_quantity_produced or wo.quantity_produced or '0.00'}<br>
                <strong>Assigned Team/Crew:</strong> {', '.join(emp_list) if emp_list else 'Unassigned'}<br>
                <strong>Current Step Status:</strong> <span style='text-transform: uppercase; font-weight: bold; color: #264b5d;'>{wo.status}</span>
            </div>
        </div>
        """
        return format_html(html_string)

    work_order_details_viewer.short_description = "Blueprint Live Specifications"


class OpenSalesInvoiceInline(TabularInline):
    model = SalesInvoice
    extra = 0
    can_delete = False
    fields = ('invoice_number', 'sales_order', 'invoice_date', 'total_amount', 'get_total_paid', 'get_remaining_balance', 'status_badge')
    readonly_fields = ('invoice_number', 'sales_order', 'invoice_date', 'total_amount', 'get_total_paid', 'get_remaining_balance', 'status_badge')
    show_change_link = True
    verbose_name = "Open Sales Invoice"
    verbose_name_plural = "Open Sales Invoices (Awaiting Payment)"

    def get_queryset(self, request):
        return super().get_queryset(request).filter(status__in=['POSTED', 'PARTIALLY_PAID']).order_by('invoice_date', 'invoice_id')

    @display(description='Total Paid')
    def get_total_paid(self, obj):
        return f"${obj.total_paid:,.2f}"

    @display(description='Outstanding Balance')
    def get_remaining_balance(self, obj):
        return f"${obj.remaining_balance:,.2f}"

    @display(description='Status')
    def status_badge(self, obj):
        color_map = {
            'POSTED': '#2563eb',         # Royal Blue
            'PARTIALLY_PAID': '#f59e0b', # Amber
            'PAID': '#10b981',           # Emerald Green
        }
        bg = color_map.get(obj.status, '#64748b')
        return render_status_badge(obj.get_status_display(), bg)


@admin.register(Customer)
class CustomerAdmin(ModelAdmin):
    list_display = ('customer_name', 'contact_info', 'shipping_address', 'get_total_receivables', 'get_available_credit', 'get_open_invoices_count')
    search_fields = ('customer_name', 'contact_info', 'shipping_address')
    readonly_fields = ('outstanding_debt_viewer',)
    inlines = [OpenSalesInvoiceInline]
    actions = [export_as_csv]
    actions_detail = ['action_receive_deposit', 'action_link_invoice']

    fieldsets = (
        ('Customer Profile', {
            'fields': ('customer_name', 'contact_info', 'shipping_address')
        }),
        ('Accounts Receivable Summary', {
            'fields': ('outstanding_debt_viewer',)
        }),
    )

    @display(description='Outstanding Receivables')
    def get_total_receivables(self, obj):
        open_invoices = obj.sales_invoices.filter(status__in=['POSTED', 'PARTIALLY_PAID'])
        total_debt = sum(inv.remaining_balance for inv in open_invoices)
        if total_debt > 0:
            formatted_debt = f"${total_debt:,.2f}"
            return format_html("<span style='color: #dc2626; font-weight: bold;'>{}</span>", formatted_debt)
        return "$0.00"

    @display(description='Available Credit')
    def get_available_credit(self, obj):
        open_cns = obj.credit_notes.filter(status='POSTED')
        total_credit = sum(cn.remaining_credit for cn in open_cns)
        if total_credit > 0:
            formatted_credit = f"${total_credit:,.2f}"
            return format_html("<span style='color: #16a34a; font-weight: bold;'>{}</span>", formatted_credit)
        return "$0.00"

    @display(description='Open Invoices')
    def get_open_invoices_count(self, obj):
        return obj.sales_invoices.filter(status__in=['POSTED', 'PARTIALLY_PAID']).count()

    def outstanding_debt_viewer(self, obj):
        if not obj or not obj.pk:
            return "Save customer first to view accounts receivable."
        open_invoices = obj.sales_invoices.filter(status__in=['POSTED', 'PARTIALLY_PAID']).order_by('invoice_date', 'invoice_id')
        total_debt = sum(inv.remaining_balance for inv in open_invoices)
        count = open_invoices.count()

        open_cns = obj.credit_notes.filter(status='POSTED')
        total_credit = sum(cn.remaining_credit for cn in open_cns)
        deposit_url = reverse('admin:customer-receive-deposit', args=[obj.pk])
        link_invoice_url = reverse('admin:customer-link-invoice', args=[obj.pk])

        html_string = f"""
        <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; margin-top: 8px;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div style="display: flex; gap: 32px; align-items: center;">
                    <div>
                        <div style="font-size: 11px; text-transform: uppercase; font-weight: 700; color: #64748b; letter-spacing: 0.5px;">Outstanding Debt Pool</div>
                        <div style="font-size: 24px; font-weight: 800; color: {'#dc2626' if total_debt > 0 else '#16a34a'}; margin-top: 2px;">
                            ${total_debt:,.2f}
                        </div>
                        <div style="font-size: 12px; color: #64748b; margin-top: 4px;">
                            <strong>{count}</strong> open invoice(s) awaiting settlement
                        </div>
                    </div>
                    <div style="border-left: 1px solid #e2e8f0; padding-left: 32px;">
                        <div style="font-size: 11px; text-transform: uppercase; font-weight: 700; color: #64748b; letter-spacing: 0.5px;">Available Credit Notes Pool</div>
                        <div style="font-size: 24px; font-weight: 800; color: {'#16a34a' if total_credit > 0 else '#64748b'}; margin-top: 2px;">
                            ${total_credit:,.2f}
                        </div>
                        <div style="font-size: 12px; color: #64748b; margin-top: 4px;">
                            <strong>{open_cns.count()}</strong> active credit note(s)
                        </div>
                    </div>
                </div>
                <div style="display: flex; gap: 8px; align-items: center;">
                    <a href="{link_invoice_url}"
                       style="display: inline-block; background: #059669; color: #ffffff; padding: 8px 14px; border-radius: 6px; font-weight: 600; font-size: 13px; text-decoration: none;">
                        + Link Invoice by Number
                    </a>
                    <a href="{deposit_url}" 
                       style="display: inline-block; background: #2563eb; color: #ffffff; padding: 8px 16px; border-radius: 6px; font-weight: 600; font-size: 13px; text-decoration: none;">
                        Receive Customer Deposit (FIFO)
                    </a>
                </div>
            </div>

            <!-- Quick Link Open Invoice Bar -->
            <div style="margin-top: 16px; padding-top: 14px; border-top: 1px dashed #cbd5e1; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
                <div>
                    <div style="font-size: 12px; font-weight: 700; color: #334155;">
                        Quick Link Invoice to Customer
                    </div>
                    <div style="font-size: 11px; color: #64748b; margin-top: 1px;">
                        Attach an existing invoice that was not yet linked to this customer by entering its invoice number.
                    </div>
                </div>
                <div style="display: flex; gap: 8px; align-items: center;">
                    <input type="text" name="link_invoice_number" id="id_link_invoice_number" placeholder="Enter Invoice # (e.g. SINV-202608-0001)"
                           style="border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px 12px; font-size: 12px; min-width: 240px; outline: none; background: #ffffff;" />
                    <button type="submit" formaction="{link_invoice_url}" formmethod="POST"
                            style="background: #059669; color: #ffffff; border: none; padding: 6px 14px; border-radius: 6px; font-weight: 600; font-size: 12px; cursor: pointer;">
                        Link to Customer
                    </button>
                </div>
            </div>
        </div>
        """
        return format_html(html_string)

    outstanding_debt_viewer.short_description = "Accounts Receivable & Debt"

    @action(description="Receive Customer Deposit", url_path="receive-deposit-action")
    def action_receive_deposit(self, request, object_id):
        return redirect(reverse('admin:customer-receive-deposit', args=[object_id]))

    @action(description="Link Invoice by Number", url_path="link-invoice-action")
    def action_link_invoice(self, request, object_id):
        return redirect(reverse('admin:customer-link-invoice', args=[object_id]))

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:object_id>/receive-deposit/',
                self.admin_site.admin_view(self.receive_deposit_view),
                name='customer-receive-deposit',
            ),
            path(
                '<int:object_id>/link-invoice/',
                self.admin_site.admin_view(self.link_invoice_view),
                name='customer-link-invoice',
            ),
        ]
        return custom_urls + urls

    def link_invoice_view(self, request, object_id):
        from django.template.response import TemplateResponse
        from .services import apply_customer_credit_notes_to_invoice

        customer = get_object_or_404(Customer, pk=object_id)
        open_invoices = customer.sales_invoices.filter(status__in=['POSTED', 'PARTIALLY_PAID'])
        total_debt = sum(inv.remaining_balance for inv in open_invoices)

        if request.method == 'POST':
            invoice_number = (
                request.POST.get('link_invoice_number') or
                request.POST.get('invoice_number') or
                request.GET.get('invoice_number', '')
            ).strip()

            if not invoice_number:
                self.message_user(request, "Please enter or select a valid Invoice Number to link.", level=messages.ERROR)
                return redirect(reverse('admin:core_customer_change', args=[object_id]))

            invoice = SalesInvoice.objects.filter(invoice_number__iexact=invoice_number).first()
            if not invoice and invoice_number.isdigit():
                invoice = SalesInvoice.objects.filter(pk=int(invoice_number)).first()

            if not invoice:
                self.message_user(request, f"Sales Invoice '{invoice_number}' could not be found in the system.", level=messages.ERROR)
                return redirect(reverse('admin:core_customer_change', args=[object_id]))

            old_customer = invoice.customer
            if old_customer and old_customer.pk == customer.pk:
                self.message_user(request, f"Invoice '{invoice.invoice_number}' is already attached to {customer.customer_name}.", level=messages.INFO)
                return redirect(reverse('admin:core_customer_change', args=[object_id]))

            # Reassign/assign invoice to this customer
            invoice.customer = customer
            invoice.save(update_fields=['customer'])

            # Automatically apply open credit notes for this customer if available
            applied = apply_customer_credit_notes_to_invoice(invoice)

            reassigned_info = f" (reassigned from {old_customer.customer_name})" if old_customer else ""
            credit_info = f" and automatically applied {len(applied)} open Credit Note(s)" if applied else ""
            self.message_user(
                request,
                f"Successfully linked Invoice '{invoice.invoice_number}' (Balance: ${invoice.remaining_balance:,.2f}) to {customer.customer_name}{reassigned_info}{credit_info}.",
                level=messages.SUCCESS
            )
            return redirect(reverse('admin:core_customer_change', args=[object_id]))

        # GET request: render dedicated linker interface
        unassigned_invoices = SalesInvoice.objects.filter(customer__isnull=True).order_by('-invoice_date', '-invoice_id')[:50]
        context = {
            **self.admin_site.each_context(request),
            'customer': customer,
            'total_debt': total_debt,
            'current_invoices_count': open_invoices.count(),
            'unassigned_invoices': unassigned_invoices,
            'title': f"Link Invoice - {customer.customer_name}",
        }
        return TemplateResponse(request, 'admin/customer_link_invoice.html', context)

    def receive_deposit_view(self, request, object_id):
        from django.template.response import TemplateResponse
        from .services import preview_customer_bulk_allocation, execute_customer_bulk_allocation

        customer = get_object_or_404(Customer, pk=object_id)
        open_invoices = list(customer.sales_invoices.filter(
            status__in=['POSTED', 'PARTIALLY_PAID']
        ).order_by('invoice_date', 'invoice_id'))
        total_debt = sum(inv.remaining_balance for inv in open_invoices)

        preview_data = None
        form_amount = request.POST.get('amount', '')
        form_payment_method = request.POST.get('payment_method', 'BANK_TRANSFER')
        form_reference = request.POST.get('reference', '')
        form_payment_date = request.POST.get('payment_date', timezone.now().strftime('%Y-%m-%d'))

        if request.method == 'POST':
            if not (request.user.is_superuser or request.user.has_perm('core.can_record_sales_payment') or request.user.is_staff):
                raise PermissionDenied("You do not have permission to record customer deposit settlements.")

            amount_str = request.POST.get('amount', '').strip()
            try:
                amount = Decimal(amount_str)
                if amount <= Decimal('0.00'):
                    raise ValueError("Deposit amount must be greater than 0.")
            except Exception as e:
                self.message_user(request, f"Invalid deposit amount: {str(e)}", level=messages.ERROR)
                return TemplateResponse(request, 'admin/customer_receive_deposit.html', {
                    **self.admin_site.each_context(request),
                    'customer': customer,
                    'open_invoices': open_invoices,
                    'total_debt': total_debt,
                    'form_amount': form_amount,
                    'form_payment_method': form_payment_method,
                    'form_reference': form_reference,
                    'form_payment_date': form_payment_date,
                    'today_date': timezone.now().strftime('%Y-%m-%d'),
                    'title': f"Receive Deposit - {customer.customer_name}",
                })

            if 'action_preview' in request.POST:
                preview_data = preview_customer_bulk_allocation(customer, amount)
            elif 'action_settle' in request.POST:
                try:
                    result = execute_customer_bulk_allocation(
                        customer=customer,
                        total_received=amount,
                        payment_method=form_payment_method,
                        reference=form_reference,
                        payment_date=form_payment_date or None
                    )
                    count_settled = len(result.get('allocations', []))
                    unallocated = result.get('unallocated_amount', Decimal('0.00'))
                    msg = f"Successfully executed bulk deposit of ${amount:,.2f} across {count_settled} invoice(s) for {customer.customer_name}."
                    if unallocated > Decimal('0.00'):
                        msg += f" Surplus credit balance: ${unallocated:,.2f}."
                    self.message_user(request, msg, level=messages.SUCCESS)
                    return redirect(reverse('admin:core_customer_change', args=[customer.pk]))
                except Exception as e:
                    self.message_user(request, f"Error executing bulk payment settlement: {str(e)}", level=messages.ERROR)

        context = {
            **self.admin_site.each_context(request),
            'customer': customer,
            'open_invoices': open_invoices,
            'total_debt': total_debt,
            'preview_data': preview_data,
            'form_amount': form_amount,
            'form_payment_method': form_payment_method,
            'form_reference': form_reference,
            'form_payment_date': form_payment_date,
            'today_date': timezone.now().strftime('%Y-%m-%d'),
            'title': f"Receive Deposit - {customer.customer_name}",
        }
        return TemplateResponse(request, 'admin/customer_receive_deposit.html', context)


@admin.register(SalesOrder)
class SalesOrderAdmin(ModelAdmin):
    list_display = ('order_number', 'customer', 'invoicing_policy', 'status_badge', 'created_at', 'updated_at', 'get_order_total')
    list_filter = ('status', 'invoicing_policy', ('created_at', RangeDateFilter))
    search_fields = ('order_number', 'customer__customer_name')
    readonly_fields = ('order_number', 'status', 'created_at', 'updated_at', 'invoices_viewer')
    inlines = [SalesOrderItemInline]
    autocomplete_fields = ['customer']
    actions = [export_as_csv]
    actions_detail = ['action_confirm_order']

    fieldsets = (
        ('Order Information', {
            'fields': ('order_number', 'customer', 'invoicing_policy', 'status')
        }),
        ('Commercial Invoices (Accounting)', {
            'fields': ('invoices_viewer',)
        }),
        ('Timestamps & Metrics', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    @display(description='Commercial Invoices')
    def invoices_viewer(self, obj):
        if not obj or not obj.pk:
            return format_html("<span style='color: #64748b; font-size: 12px;'>Save sales order first to view linked invoices.</span>")
        invoices = obj.invoices.all().order_by('-invoice_date', '-invoice_id')
        if not invoices.exists():
            if obj.invoicing_policy == 'ORDER_BASED':
                return format_html(
                    "<div style='padding: 10px 14px; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; color: #1e40af; font-size: 12px;'>"
                    "<strong>Advance/Upfront Invoicing:</strong> Invoices are automatically generated and posted upon order confirmation."
                    "</div>"
                )
            else:
                return format_html(
                    "<div style='padding: 10px 14px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; color: #64748b; font-size: 12px;'>"
                    "<strong>Post-Shipment Invoicing:</strong> Invoices are generated automatically as goods are dispatched."
                    "</div>"
                )

        rows_html = []
        for inv in invoices:
            url = reverse('admin:core_salesinvoice_change', args=[inv.pk])
            status_color = {
                'POSTED': '#0284c7',
                'PARTIALLY_PAID': '#d97706',
                'PAID': '#16a34a',
                'CREDITED': '#6b7280',
                'CANCELLED': '#dc2626'
            }.get(inv.status, '#475569')
            rows_html.append(f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 10px 12px; font-family: monospace; font-weight: 600;"><a href="{url}" style="color: #2563eb; text-decoration: underline;">{inv.invoice_number}</a></td>
                <td style="padding: 10px 12px; color: #475569;">{inv.invoice_date}</td>
                <td style="padding: 10px 12px; text-align: right; font-weight: 600;">${inv.total_amount:,.2f}</td>
                <td style="padding: 10px 12px; text-align: right; color: #16a34a;">${inv.total_paid:,.2f}</td>
                <td style="padding: 10px 12px; text-align: right; font-weight: 700; color: {'#dc2626' if inv.remaining_balance > 0 else '#16a34a'};">${inv.remaining_balance:,.2f}</td>
                <td style="padding: 10px 12px; text-align: center;">
                    <span style="background: {status_color}15; color: {status_color}; border: 1px solid {status_color}40; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 700; text-transform: uppercase;">
                        {inv.get_status_display()}
                    </span>
                </td>
                <td style="padding: 10px 12px; text-align: right;">
                    <a href="{url}" style="display: inline-block; padding: 4px 10px; background: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 4px; font-size: 11px; font-weight: 600; color: #334155; text-decoration: none;">View Invoice &rarr;</a>
                </td>
            </tr>
            """)

        table_html = f"""
        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; margin-top: 4px;">
            <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
                <thead>
                    <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0; text-align: left; font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 700; letter-spacing: 0.5px;">
                        <th style="padding: 8px 12px;">Invoice Number</th>
                        <th style="padding: 8px 12px;">Invoice Date</th>
                        <th style="padding: 8px 12px; text-align: right;">Total Amount</th>
                        <th style="padding: 8px 12px; text-align: right;">Total Paid</th>
                        <th style="padding: 8px 12px; text-align: right;">Balance</th>
                        <th style="padding: 8px 12px; text-align: center;">Status</th>
                        <th style="padding: 8px 12px; text-align: right;">Action</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows_html)}
                </tbody>
            </table>
        </div>
        """
        return mark_safe(table_html)

    @display(description='Status')
    def status_badge(self, obj):
        color_map = {
            'draft': '#64748b',                # Slate Grey
            'approved': '#2563eb',             # Royal Blue
            'partially_dispatched': '#f59e0b', # Amber
            'completed': '#10b981',            # Emerald Green
            'cancelled': '#ef4444',            # Crimson Red
        }
        bg = color_map.get(obj.status, '#64748b')
        return render_status_badge(obj.get_status_display(), bg)

    @action(description="Confirm Order & Generate Invoice", url_path="confirm-order-action")
    def action_confirm_order(self, request, object_id):
        order = get_object_or_404(SalesOrder, pk=object_id)
        if order.status == 'cancelled':
            self.message_user(request, f"Sales Order #{order.order_number} is cancelled.", level=messages.ERROR)
            return redirect(reverse('admin:core_salesorder_change', args=[object_id]))
        try:
            invoice = order.confirm_and_generate_invoice()
            if invoice:
                self.message_user(
                    request,
                    f"Sales Order #{order.order_number} confirmed and Invoice #{invoice.invoice_number} generated & posted to {order.customer.customer_name}.",
                    level=messages.SUCCESS
                )
            else:
                self.message_user(
                    request,
                    f"Sales Order #{order.order_number} confirmed (Deferred invoice policy: {order.get_invoicing_policy_display()}).",
                    level=messages.INFO
                )
        except Exception as e:
            self.message_user(request, f"Error confirming Sales Order: {str(e)}", level=messages.ERROR)
        return redirect(reverse('admin:core_salesorder_change', args=[object_id]))

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != 'draft' and not request.user.is_superuser:
            return ['customer', 'order_number', 'status', 'invoicing_policy', 'created_at', 'updated_at', 'invoices_viewer']
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('customer').prefetch_related('items__product')

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        order = form.instance
        order.update_status(save=True)
        if order.invoicing_policy == 'ORDER_BASED' and order.status in ['approved', 'partially_dispatched', 'completed']:
            if order.items.exists() and not order.invoices.filter(status__in=['DRAFT', 'POSTED', 'PARTIALLY_PAID', 'PAID']).exists():
                order.confirm_and_generate_invoice()

    @display(description='Order Total')
    def get_order_total(self, obj):
        total = sum(item.total_price for item in obj.items.all())
        return f"${total:,.2f}"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:object_id>/confirm-order/',
                self.admin_site.admin_view(self.confirm_order_view),
                name='salesorder-confirm-order',
            ),
        ]
        return custom_urls + urls

    def confirm_order_view(self, request, object_id):
        if not (request.user.is_superuser or request.user.has_perm('core.can_confirm_sales_order')):
            raise PermissionDenied("You do not have permission to confirm sales orders.")
        order = get_object_or_404(SalesOrder, pk=object_id)
        try:
            invoice = order.confirm_and_generate_invoice()
            self.message_user(
                request,
                f"Sales Order #{order.order_number} confirmed and Invoice #{invoice.invoice_number if invoice else 'Deferred'} generated successfully.",
                level=messages.SUCCESS
            )
        except ValidationError as e:
            if hasattr(e, 'messages'):
                for msg in e.messages:
                    self.message_user(request, msg, level=messages.ERROR)
            else:
                self.message_user(request, str(e), level=messages.ERROR)
        except Exception as e:
            self.message_user(request, f"Error confirming Sales Order #{object_id}: {str(e)}", level=messages.ERROR)

        referer = request.META.get('HTTP_REFERER')
        if referer:
            return HttpResponseRedirect(referer)
        return redirect(reverse('admin:core_salesorder_change', args=[object_id]))


@admin.register(SalesInvoice)
class SalesInvoiceAdmin(ModelAdmin):
    list_display = ('invoice_number', 'customer', 'sales_order', 'subtotal', 'tax_amount', 'total_amount', 'get_total_paid', 'get_remaining_balance', 'invoice_date', 'status_badge')
    list_filter = ['status', ('invoice_date', RangeDateFilter), 'customer']
    search_fields = ('invoice_number', 'customer__customer_name', 'sales_order__order_number', 'dispatch__dispatch_code')
    inlines = [SalesInvoiceLineInline, SalesInvoicePaymentsInline]
    autocomplete_fields = ['customer', 'sales_order', 'dispatch']
    readonly_fields = ('invoice_number', 'subtotal', 'tax_amount', 'total_amount', 'get_total_paid', 'get_remaining_balance')
    actions = [export_as_csv]
    actions_detail = ['action_download_pdf']

    fieldsets = (
        ('Invoice Details', {
            'fields': ('invoice_number', 'customer', 'sales_order', 'dispatch', 'invoice_date', 'due_date', 'status')
        }),
        ('Financial Summary', {
            'fields': ('subtotal', 'tax_amount', 'total_amount', 'get_total_paid', 'get_remaining_balance')
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('customer', 'dispatch', 'sales_order').prefetch_related('sales_payments', 'lines__product')

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status in ['POSTED', 'PAID', 'PARTIALLY_PAID', 'CREDITED'] and not request.user.is_superuser:
            fields = [f.name for f in self.model._meta.fields]
            return fields + ['get_total_paid', 'get_remaining_balance']
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @display(description='Status')
    def status_badge(self, obj):
        color_map = {
            'DRAFT': '#64748b',          # Slate Grey
            'POSTED': '#2563eb',         # Royal Blue
            'PARTIALLY_PAID': '#f59e0b', # Amber
            'PAID': '#10b981',           # Emerald Green
            'CREDITED': '#8b5cf6',       # Purple
            'CANCELLED': '#ef4444',      # Crimson Red
        }
        bg = color_map.get(obj.status, '#64748b')
        return render_status_badge(obj.get_status_display(), bg)

    @display(description='Total Paid')
    def get_total_paid(self, obj):
        return f"${obj.total_paid:,.2f}"

    @display(description='Remaining Balance')
    def get_remaining_balance(self, obj):
        bal = obj.remaining_balance
        if bal > 0:
            return f"${bal:,.2f}"
        return "$0.00"

    @action(description="Download Commercial Invoice PDF", url_path="download-pdf")
    def action_download_pdf(self, request, object_id):
        invoice = get_object_or_404(SalesInvoice, pk=object_id)
        pdf_buffer = generate_invoice_pdf(invoice)
        response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="Invoice_{invoice.invoice_number}.pdf"'
        return response


@admin.register(CreditNote)
class CreditNoteAdmin(ModelAdmin):
    list_display = ('credit_note_number', 'invoice', 'customer', 'total_amount', 'get_applied_amount', 'get_remaining_credit', 'issue_date', 'status_badge')
    list_filter = ['status', ('issue_date', RangeDateFilter), 'customer']
    search_fields = ('credit_note_number', 'invoice__invoice_number', 'customer__customer_name')
    inlines = [CreditNoteLineInline]
    autocomplete_fields = ['invoice', 'customer']
    readonly_fields = ('credit_note_number', 'subtotal', 'tax_amount', 'total_amount', 'applied_amount', 'get_remaining_credit')
    actions = [export_as_csv]
    actions_detail = ['action_download_pdf']

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('invoice', 'customer').prefetch_related('lines__product')

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status in ['POSTED', 'REFUNDED'] and not request.user.is_superuser:
            return [f.name for f in self.model._meta.fields]
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @display(description='Applied Credit')
    def get_applied_amount(self, obj):
        applied = obj.applied_amount or Decimal('0.00')
        return f"${applied:,.2f}"

    @display(description='Remaining Credit')
    def get_remaining_credit(self, obj):
        rem = obj.remaining_credit
        if rem > 0:
            formatted = f"${rem:,.2f}"
            return format_html("<span style='color: #16a34a; font-weight: bold;'>{}</span>", formatted)
        return "$0.00"

    @display(description='Status')
    def status_badge(self, obj):
        color_map = {
            'DRAFT': '#64748b',    # Slate Grey
            'POSTED': '#f59e0b',   # Amber
            'REFUNDED': '#10b981', # Emerald Green
        }
        bg = color_map.get(obj.status, '#64748b')
        return render_status_badge(obj.get_status_display(), bg)

    @action(description="Download Credit Note PDF", url_path="download-pdf")
    def action_download_pdf(self, request, object_id):
        credit_note = get_object_or_404(CreditNote, pk=object_id)
        pdf_buffer = generate_credit_note_pdf(credit_note)
        response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="CreditNote_{credit_note.credit_note_number}.pdf"'
        return response


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(ModelAdmin):
    list_display = ('document_type', 'prefix', 'last_sequence')
    readonly_fields = ('document_type', 'prefix', 'last_sequence')
    search_fields = ('document_type', 'prefix')


@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(ModelAdmin):
    list_display = ('invoice_number', 'supplier', 'procurement_order', 'total_amount', 'invoice_date', 'status_badge', 'paid_date', 'remaining_balance')
    list_filter = ['status', ('invoice_date', RangeDateFilter), 'supplier']
    search_fields = ('invoice_number', 'supplier__name', 'supplier__supplier_code', 'procurement_order__procurement_order_id')
    inlines = [PurchasePaymentInline]
    autocomplete_fields = ['supplier', 'procurement_order']
    readonly_fields = ['status', 'paid_date', 'remaining_balance', 'total_amount', 'created_at']
    actions = [export_as_csv]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('supplier', 'procurement_order').prefetch_related('payments')

    @display(description='Status')
    def status_badge(self, obj):
        color_map = {
            'UNPAID': '#ef4444',   # Crimson Red
            'PARTIAL': '#f59e0b',  # Amber
            'PAID': '#10b981',     # Emerald Green
        }
        bg = color_map.get(obj.status, '#64748b')
        return render_status_badge(obj.get_status_display(), bg)


@admin.register(Return)
class ReturnAdmin(ModelAdmin):
    list_display = ('dispatch', 'customer', 'quantity_returned', 'reason_for_return', 'qc_badge')
    list_filter = ['quality_control_status']
    search_fields = ('dispatch__dispatch_code', 'customer__customer_name')
    autocomplete_fields = ['dispatch', 'customer']
    actions = [export_as_csv]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('dispatch', 'customer')

    @display(description='QC Status')
    def qc_badge(self, obj):
        color_map = {
            'APPROVED': '#10b981', # Emerald Green
            'REJECTED': '#ef4444', # Crimson Red
            'PENDING': '#f59e0b',  # Amber
        }
        bg = color_map.get(obj.quality_control_status, '#64748b')
        return render_status_badge(obj.get_quality_control_status_display(), bg)


@admin.register(FinanceEntry)
class FinanceEntryAdmin(ModelAdmin):
    list_display = (
        'entry_code', 'timestamp_display', 'entry_type_badge', 'category',
        'amount_display', 'reference_document_display', 'logged_by_display', 'pdf_download_button'
    )
    list_display_links = ('entry_code',)
    list_filter = ['entry_type', 'category', ('timestamp', RangeDateFilter), ('amount', RangeNumericFilter)]
    search_fields = ('entry_code', 'reference_document', 'description', 'category', 'sales_invoice__invoice_number')
    readonly_fields = (
        'entry_code', 'timestamp', 'entry_type', 'category', 'amount',
        'reference_document', 'description', 'logged_by', 'procurement_order',
        'sales_invoice', 'material_variance', 'entry_date'
    )
    actions = [export_as_csv]
    actions_detail = ['action_download_pdf']

    fieldsets = (
        ('Voucher Identification', {
            'fields': ('entry_code', 'timestamp', 'entry_type', 'category')
        }),
        ('Financial & Reference Data', {
            'fields': ('amount', 'reference_document', 'sales_invoice', 'procurement_order', 'material_variance')
        }),
        ('Audit & Attribution', {
            'fields': ('logged_by', 'description', 'entry_date')
        }),
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @display(description='Entry Type')
    def entry_type_badge(self, obj):
        color_map = {
            'REVENUE': '#10b981',   # Emerald Green
            'EXPENSE': '#ef4444',   # Crimson Red
            'CAPITAL': '#8b5cf6',   # Purple
            'TRANSFER': '#2563eb',  # Royal Blue
            'ADJUSTMENT': '#f59e0b',# Amber
        }
        bg = color_map.get(obj.entry_type, '#64748b')
        return render_status_badge(obj.get_entry_type_display(), bg)

    @display(description='Amount')
    def amount_display(self, obj):
        return f"KES {obj.amount:,.2f}"

    @display(description='Posted At')
    def timestamp_display(self, obj):
        if obj.timestamp:
            return obj.timestamp.strftime('%Y-%m-%d %H:%M')
        return str(obj.entry_date)

    @display(description='Reference Document')
    def reference_document_display(self, obj):
        if obj.reference_document:
            return obj.reference_document
        if obj.sales_invoice and obj.sales_invoice.invoice_number:
            return obj.sales_invoice.invoice_number
        if obj.procurement_order:
            return f"PO #{obj.procurement_order_id}"
        return "-"

    @display(description='Logged By')
    def logged_by_display(self, obj):
        return obj.logged_by.username if obj.logged_by else "System Auto-Post"

    @display(description='Voucher PDF')
    def pdf_download_button(self, obj):
        url = reverse('admin:financeentry-pdf', args=[obj.pk])
        return format_html(
            '<a href="{}" style="display: inline-flex; align-items: center; padding: 4px 10px; background: #0284c7; color: #ffffff; '
            'border-radius: 4px; font-size: 11px; font-weight: 600; text-decoration: none; box-shadow: 0 1px 2px rgba(0,0,0,0.05);">'
            '📄 Voucher PDF'
            '</a>',
            url
        )

    @action(description="Download Journal Voucher PDF", url_path="download-pdf")
    def action_download_pdf(self, request, object_id):
        entry = get_object_or_404(FinanceEntry, pk=object_id)
        pdf_buffer = generate_finance_entry_pdf(entry)
        response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
        filename = f"Voucher_{entry.entry_code or entry.pk}.pdf"
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:object_id>/pdf/',
                self.admin_site.admin_view(self.download_pdf_view),
                name='financeentry-pdf',
            ),
        ]
        return custom_urls + urls

    def download_pdf_view(self, request, object_id):
        entry = get_object_or_404(FinanceEntry, pk=object_id)
        pdf_buffer = generate_finance_entry_pdf(entry)
        response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
        filename = f"Voucher_{entry.entry_code or entry.pk}.pdf"
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response


@admin.register(Employee)
class EmployeeAdmin(ModelAdmin):
    list_display = ('employee_code', 'employee_id', 'employee_name', 'role', 'phone_number', 'email')
    search_fields = ('employee_id', 'employee_code', 'employee_name', 'role')
    readonly_fields = ['employee_code']
    ordering = ['employee_id']
    actions = [export_as_csv]


@admin.register(BillOfMaterial)
class BillOfMaterialAdmin(ModelAdmin):
    list_display = ('product', 'name', 'is_active_badge', 'get_component_count', 'updated_at')
    list_filter = ['is_active']
    search_fields = ('product__name', 'name', 'product__sku', 'items__component__name')
    autocomplete_fields = ['product']
    inlines = [BOMItemInline]

    @display(description='Active Status')
    def is_active_badge(self, obj):
        if obj.is_active:
            return render_status_badge('Active', '#10b981')
        return render_status_badge('Inactive', '#64748b')

    @display(description='Total Ingredients')
    def get_component_count(self, obj):
        return obj.items.count()


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(ModelAdmin):
    list_display = ('po_number', 'supplier', 'order_date', 'status_badge')
    list_filter = ('status', ('order_date', RangeDateFilter), 'supplier')
    search_fields = ('po_number', 'supplier__name', 'supplier__supplier_code')
    autocomplete_fields = ['supplier']
    inlines = [PurchaseOrderItemInline]
    readonly_fields = ('po_number', 'order_date', 'status')
    actions = [export_as_csv]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('supplier').prefetch_related('items__product')

    fieldsets = (
        ('Order Details', {
            'fields': ('po_number', 'supplier', 'order_date', 'notes')
        }),
        ('Status & Lifecycle', {
            'fields': ('status',),
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status in ('RECEIVED', 'CANCELLED'):
            return [field.name for field in self.model._meta.fields]
        return self.readonly_fields

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        # When reviewing a DRAFT purchase order and clicking save, transition status to SENT if items exist
        if change and obj.status == 'DRAFT' and obj.items.filter(quantity_ordered__gt=0).exists():
            obj.status = 'SENT'
            obj.save(update_fields=['status'])
            messages.success(request, f"Purchase Order #{obj.po_number} reviewed, confirmed, and marked as 'Sent to Supplier'.")

    @display(description='Status')
    def status_badge(self, obj):
        color_map = {
            'DRAFT': '#64748b',            # Slate Grey
            'SENT': '#2563eb',             # Royal Blue
            'PARTIAL': '#f59e0b',          # Amber
            'RECEIVED': '#10b981',         # Emerald Green
            'CANCELLED': '#ef4444',        # Crimson Red
        }
        bg = color_map.get(obj.status, '#64748b')
        return render_status_badge(obj.get_status_display(), bg)


class ProcurementOrderForm(forms.ModelForm):
    class Meta:
        model = ProcurementOrder
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        po_id = None
        if self.data and self.data.get('purchase_order'):
            po_id = self.data.get('purchase_order')
        elif self.instance and self.instance.purchase_order_id:
            po_id = self.instance.purchase_order_id

        if po_id:
            po = PurchaseOrder.objects.filter(pk=po_id).first()
            if po:
                valid_product_ids = po.items.values_list('product_id', flat=True)
                self.fields['product'].queryset = Product.objects.filter(pk__in=valid_product_ids)


@admin.register(ProcurementOrder)
class ProcurementOrderAdmin(ModelAdmin):
    form = ProcurementOrderForm
    autocomplete_fields = ['purchase_order', 'product']
    list_display = ('procurement_order_id', 'purchase_order', 'product', 'quantity', 'price_per_unit', 'total_cost', 'delivery_date', 'status_badge')
    list_filter = ['status', ('delivery_date', RangeDateFilter), 'delivery_location']
    search_fields = ('product__name', 'product__sku', 'purchase_order__po_number')
    readonly_fields = ['total_cost', 'delivery_date']
    actions = [export_as_csv]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('purchase_order', 'product', 'purchase_order__supplier')

    @display(description='Status')
    def status_badge(self, obj):
        color_map = {
            'PENDING': '#f59e0b',          # Amber
            'DELIVERED': '#10b981',        # Emerald Green
            'CANCELLED': '#ef4444',        # Crimson Red
        }
        bg = color_map.get(obj.status, '#64748b')
        return render_status_badge(obj.get_status_display(), bg)


@admin.register(DispatchRecord)
class DispatchRecordAdmin(ModelAdmin):
    list_display = ['dispatch_code', 'dispatch_id', 'customer', 'sales_order_item', 'product', 'quantity_dispatched', 'dispatch_date', 'status_badge', 'delivery_date']
    list_filter = [('dispatch_date', RangeDateFilter), 'status', ('delivery_date', RangeDateFilter), 'customer']
    search_fields = ['dispatch_code', 'sales_order_item__sales_order__order_number', 'customer__customer_name', 'product__sku', 'product__name']
    autocomplete_fields = ['customer', 'product']
    readonly_fields = ('dispatch_code', 'delivery_date', 'is_stock_deducted')
    actions = [export_as_csv]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('customer', 'product', 'sales_order_item__sales_order')

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.is_stock_deducted:
            return [field.name for field in self.model._meta.fields]
        return self.readonly_fields

    fieldsets = (
        ('Order & Logistics Information', {
            'fields': ('dispatch_code', 'customer', 'sales_order_item', 'product', 'quantity_dispatched')
        }),
        ('Status & Timestamps', {
            'fields': ('status', 'dispatch_date', 'delivery_date', 'is_stock_deducted')
        }),
    )

    @display(description='Status')
    def status_badge(self, obj):
        color_map = {
            'pending': '#f59e0b',          # Amber
            'shipped': '#2563eb',          # Royal Blue
            'delivered': '#10b981',        # Emerald Green
            'cancelled': '#ef4444',        # Crimson Red
        }
        bg = color_map.get(obj.status, '#64748b')
        return render_status_badge(obj.get_status_display(), bg)


@admin.register(WorkOrder)
class WorkOrderAdmin(ModelAdmin):
    form = WorkOrderForm
    list_display = ('work_order_code', 'category_badge', 'product', 'display_employees', 'display_target_quantity', 'actual_quantity_produced', 'production_start_date', 'production_end_date', 'status_badge', 'is_inventory_updated')
    list_display_links = ('work_order_code',)
    readonly_fields = ['work_order_code', 'category', 'status', 'is_inventory_allocated', 'is_inventory_updated', 'production_end_date']
    inlines = [WorkOrderInstructionInline, WorkOrderMaterialLineInline, ChildPackagingInline]
    autocomplete_fields = ['product', 'bill_of_material', 'parent_work_order']
    list_filter = ['category', 'status', 'is_inventory_updated', ('production_start_date', RangeDateFilter)]
    search_fields = ('work_order_code', 'product__name', 'product__sku', 'employee__employee_name')
    filter_horizontal = ('employee',)
    actions = [export_as_csv, 'action_reconcile_production_stock', 'action_top_up_bulk', 'action_downscale_target', 'action_hold_for_existing']

    class Media:
        js = ('admin/js/workorder_toggle.js', 'admin/js/work_order_category_toggle.js')
        css = {
            'all': ('admin/css/work_order_admin.css',)
        }

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('product', 'bill_of_material', 'parent_work_order').prefetch_related('employee', 'production_runs', 'material_lines')

    fieldsets = (
        ('Order Specification', {
            'fields': (
                'category',
                'product',
                'bill_of_material',
                'quantity_produced',
                'actual_quantity_produced',
                'employee',
            )
        }),
        ('Execution & Timestamps', {
            'fields': (
                'parent_work_order',
                'status',
                'production_start_date',
                'production_end_date',
            )
        }),
        ('Automation & Audit Gates', {
            'classes': ('collapse',),
            'fields': (
                'is_inventory_allocated',
                'is_inventory_updated',
            ),
        }),
    )

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            if isinstance(instance, WorkOrderMaterialLine) and instance.component:
                WorkOrderMaterialLine.objects.update_or_create(
                    work_order=form.instance,
                    component=instance.component,
                    defaults={
                        'quantity_actual': instance.quantity_actual,
                    }
                )
            else:
                instance.work_order = form.instance
                instance.save()
        formset.save_m2m()
        for obj in formset.deleted_objects:
            obj.delete()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        work_order = form.instance
        work_order.refresh_from_db()
        work_order.process_inventory()

    @display(description='Category')
    def category_badge(self, obj):
        color_map = {
            'PRODUCTION': '#2563eb', # Royal Blue (Bulk Mixing)
            'PACKAGING': '#f59e0b',  # Amber Gold (Packaging)
        }
        bg = color_map.get(obj.category, '#64748b')
        return render_status_badge(obj.get_category_display(), bg)

    @display(description='Assigned Employees')
    def display_employees(self, obj):
        employees = obj.employee.all()
        if employees.exists():
            return ", ".join([str(emp) for emp in employees])
        return "-"

    @display(description='Target Quantity')
    def display_target_quantity(self, obj):
        return f"{obj.target_quantity:.2f}"

    @display(description='Status')
    def status_badge(self, obj):
        color_map = {
            'DRAFT': '#64748b',                # Slate Grey
            'IN_PROGRESS': '#2563eb',          # Royal Blue
            'COMPLETED': '#10b981',            # Emerald Green
            'CANCELLED': '#ef4444',            # Crimson Red
            'AWAITING_RESOLUTION': '#d97706',  # Burnt Orange
            'ON_HOLD_SHORTAGE': '#ef4444',     # Crimson
        }
        bg = color_map.get(obj.status, '#64748b')
        return render_status_badge(obj.get_status_display(), bg)

    @admin.action(description="Shortage Resolution: Top-Up Parent Bulk Order")
    def action_top_up_bulk(self, request, queryset):
        count = 0
        for wo in queryset:
            try:
                wo.resolve_bulk_shortage('TOP_UP_BULK')
                count += 1
            except Exception as e:
                self.message_user(request, f"Error resolving WO #{wo.pk}: {str(e)}", level=messages.ERROR)
        if count > 0:
            self.message_user(request, f"Successfully executed Top-Up Bulk resolution for {count} order(s).", level=messages.SUCCESS)

    @admin.action(description="Shortage Resolution: Downscale Target Batch")
    def action_downscale_target(self, request, queryset):
        count = 0
        for wo in queryset:
            try:
                wo.resolve_bulk_shortage('DOWNSCALE_TARGET')
                count += 1
            except Exception as e:
                self.message_user(request, f"Error resolving WO #{wo.pk}: {str(e)}", level=messages.ERROR)
        if count > 0:
            self.message_user(request, f"Successfully downscaled target batch for {count} order(s).", level=messages.SUCCESS)

    @admin.action(description="Shortage Resolution: Hold for Existing Bulk Run")
    def action_hold_for_existing(self, request, queryset):
        count = 0
        for wo in queryset:
            try:
                wo.resolve_bulk_shortage('HOLD_FOR_EXISTING')
                count += 1
            except Exception as e:
                self.message_user(request, f"Error resolving WO #{wo.pk}: {str(e)}", level=messages.ERROR)
        if count > 0:
            self.message_user(request, f"Successfully placed {count} order(s) on hold for bulk shortage.", level=messages.SUCCESS)

    @admin.action(description="Reconcile Production Stock & Post Output")
    def action_reconcile_production_stock(self, request, queryset):
        from core.services import ProductionReconciliationEngine, ProductionReconciliationError
        success_count = 0
        skipped_count = 0
        for wo in queryset:
            try:
                result = ProductionReconciliationEngine.reconcile_work_order_completion(wo)
                if result.get('skipped'):
                    skipped_count += 1
                else:
                    success_count += 1
            except ProductionReconciliationError as e:
                self.message_user(request, f"Reconciliation Error for WO #{wo.pk} ({wo.work_order_code}): {str(e)}", level=messages.ERROR)
            except Exception as e:
                self.message_user(request, f"Unexpected Error for WO #{wo.pk}: {str(e)}", level=messages.ERROR)
        
        if success_count > 0:
            self.message_user(request, f"Successfully reconciled and posted stock transactions for {success_count} Work Order(s).", level=messages.SUCCESS)
        if skipped_count > 0:
            self.message_user(request, f"{skipped_count} Work Order(s) were already reconciled and skipped safely.", level=messages.INFO)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == 'COMPLETED' and not request.user.is_superuser:
            return [f.name for f in self.model._meta.fields]

        if not request.user.is_superuser and request.user.groups.filter(name='Shop-Floor Operator').exists():
            return ['order_code', 'product', 'target_quantity', 'status', 'category', 'parent_work_order', 'bill_of_material']

        return super().get_readonly_fields(request, obj)

    def has_delete_permission(self, request, obj=None):
        return bool(request.user and request.user.is_superuser)

    change_form_template = 'admin/core/workorder/change_form.html'

    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra_context = extra_context or {}
        try:
            wo = self.get_object(request, object_id)
            if wo:
                metrics = wo.check_bulk_availability()
                extra_context['shortage_metrics'] = metrics
                inter_prod = metrics.get('intermediate_product')
                if inter_prod:
                    active_bulk_orders = WorkOrder.objects.filter(
                        product=inter_prod,
                        status='IN_PROGRESS'
                    ).exclude(pk=wo.pk)
                    extra_context['active_bulk_orders'] = active_bulk_orders
                else:
                    extra_context['active_bulk_orders'] = []
                extra_context['parent_bulk_order'] = wo.parent_work_order
        except Exception as e:
            pass

        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:object_id>/start-production/',
                self.admin_site.admin_view(self.start_production_view),
                name='workorder-start-production',
            ),
            path(
                '<int:object_id>/resolve-shortage/<str:choice>/',
                self.admin_site.admin_view(self.resolve_shortage_view),
                name='workorder-resolve-shortage',
            ),
            path(
                '<int:object_id>/top-up-bulk/',
                self.admin_site.admin_view(self.top_up_bulk_view),
                name='workorder-top-up-bulk',
            ),
            path(
                '<int:object_id>/downscale-target/',
                self.admin_site.admin_view(self.downscale_target_view),
                name='workorder-downscale-target',
            ),
            path(
                '<int:object_id>/hold-for-existing/',
                self.admin_site.admin_view(self.hold_for_existing_view),
                name='workorder-hold-for-existing',
            ),
            path(
                '<int:object_id>/check-stock-resume/',
                self.admin_site.admin_view(self.check_stock_resume_view),
                name='workorder-check-stock-resume',
            ),
        ]
        return custom_urls + urls

    def resolve_shortage_view(self, request, object_id, choice):
        if not (request.user.is_superuser or request.user.has_perm('core.can_resolve_shortage')):
            raise PermissionDenied("You do not have permission to resolve work order shortages.")
        valid_choices = ['TOP_UP_BULK', 'HOLD_FOR_EXISTING', 'DOWNSCALE_TARGET']
        if choice not in valid_choices:
            self.message_user(request, f"Invalid resolution choice '{choice}'. Must be one of {valid_choices}.", level=messages.ERROR)
            referer = request.META.get('HTTP_REFERER')
            if referer:
                return HttpResponseRedirect(referer)
            return redirect(reverse('admin:core_workorder_change', args=[object_id]))
        existing_bulk_wo_id = request.POST.get('bulk_wo_id') or request.GET.get('bulk_wo_id')
        if existing_bulk_wo_id:
            try:
                existing_bulk_wo_id = int(existing_bulk_wo_id)
            except (ValueError, TypeError):
                existing_bulk_wo_id = None
        return self._resolve_shortage_view(request, object_id, choice, existing_bulk_wo_id=existing_bulk_wo_id)

    def start_production_view(self, request, object_id):
        if not (request.user.is_superuser or request.user.has_perm('core.can_start_production')):
            raise PermissionDenied("You do not have permission to start production on work orders.")
        work_order = get_object_or_404(WorkOrder, pk=object_id)
        try:
            success, message = work_order.start_production()
            if success:
                self.message_user(request, message, level=messages.SUCCESS)
            else:
                self.message_user(request, message, level=messages.WARNING)
        except ValidationError as e:
            if hasattr(e, 'message_dict'):
                for field, msgs in e.message_dict.items():
                    for msg in msgs:
                        field_label = field.replace('_', ' ').title() if field != '__all__' else 'Validation Error'
                        self.message_user(request, f"{field_label}: {msg}", level=messages.ERROR)
            elif hasattr(e, 'messages'):
                for msg in e.messages:
                    self.message_user(request, msg, level=messages.ERROR)
            else:
                self.message_user(request, str(e), level=messages.ERROR)
        except Exception as e:
            self.message_user(request, f"Failed to start work order: {str(e)}", level=messages.ERROR)

        referer = request.META.get('HTTP_REFERER')
        if referer:
            return HttpResponseRedirect(referer)
        return redirect(reverse('admin:core_workorder_change', args=[object_id]))

    def _resolve_shortage_view(self, request, object_id, action_choice, existing_bulk_wo_id=None):
        work_order = get_object_or_404(WorkOrder, pk=object_id)
        try:
            work_order.resolve_bulk_shortage(action_choice, existing_bulk_wo_id=existing_bulk_wo_id)
            action_labels = {
                'TOP_UP_BULK': 'Top-Up Bulk resolution',
                'DOWNSCALE_TARGET': 'Downscale Target resolution',
                'HOLD_FOR_EXISTING': 'Hold for Existing Bulk Run',
            }
            label = action_labels.get(action_choice, action_choice)
            self.message_user(request, f"Successfully executed {label} for WO #{work_order.work_order_code}.", level=messages.SUCCESS)
        except ValidationError as e:
            if hasattr(e, 'messages'):
                for msg in e.messages:
                    self.message_user(request, msg, level=messages.ERROR)
            else:
                self.message_user(request, str(e), level=messages.ERROR)
        except Exception as e:
            self.message_user(request, f"Error resolving WO #{object_id}: {str(e)}", level=messages.ERROR)

        referer = request.META.get('HTTP_REFERER')
        if referer:
            return HttpResponseRedirect(referer)
        return redirect(reverse('admin:core_workorder_change', args=[object_id]))

    def top_up_bulk_view(self, request, object_id):
        if not (request.user.is_superuser or request.user.has_perm('core.can_resolve_shortage')):
            raise PermissionDenied("You do not have permission to resolve work order shortages.")
        return self._resolve_shortage_view(request, object_id, 'TOP_UP_BULK')

    def downscale_target_view(self, request, object_id):
        if not (request.user.is_superuser or request.user.has_perm('core.can_resolve_shortage')):
            raise PermissionDenied("You do not have permission to resolve work order shortages.")
        return self._resolve_shortage_view(request, object_id, 'DOWNSCALE_TARGET')

    def hold_for_existing_view(self, request, object_id):
        if not (request.user.is_superuser or request.user.has_perm('core.can_resolve_shortage')):
            raise PermissionDenied("You do not have permission to resolve work order shortages.")
        existing_bulk_wo_id = request.POST.get('bulk_wo_id') or request.GET.get('bulk_wo_id')
        if existing_bulk_wo_id:
            try:
                existing_bulk_wo_id = int(existing_bulk_wo_id)
            except (ValueError, TypeError):
                existing_bulk_wo_id = None
        return self._resolve_shortage_view(request, object_id, 'HOLD_FOR_EXISTING', existing_bulk_wo_id=existing_bulk_wo_id)

    def check_stock_resume_view(self, request, object_id):
        if not (request.user.is_superuser or request.user.has_perm('core.can_start_production')):
            raise PermissionDenied("You do not have permission to start production on work orders.")
        work_order = get_object_or_404(WorkOrder, pk=object_id)
        try:
            avail = work_order.check_bulk_availability()
            if avail.get('has_shortfall'):
                shortfall = avail.get('shortfall', Decimal('0.00'))
                self.message_user(
                    request,
                    f"Intermediate bulk shortage still unresolved: Shortfall is {shortfall:.2f} units. Order remains {work_order.get_status_display()}.",
                    level=messages.WARNING
                )
            else:
                success, msg = work_order.start_production()
                if success:
                    self.message_user(request, f"Stock verified! {msg}", level=messages.SUCCESS)
                else:
                    self.message_user(request, msg, level=messages.WARNING)
        except Exception as e:
            self.message_user(request, f"Error re-evaluating stock: {str(e)}", level=messages.ERROR)

        referer = request.META.get('HTTP_REFERER')
        if referer:
            return HttpResponseRedirect(referer)
        return redirect(reverse('admin:core_workorder_change', args=[object_id]))


@admin.register(MaterialVarianceRecord)
class MaterialVarianceRecordAdmin(ModelAdmin):
    list_display = ('variance_code', 'work_order', 'get_production_run_type', 'product', 'quantity_expected', 'quantity_actual', 'quantity_variance', 'get_financial_impact', 'classification_badge', 'recorded_at')
    list_filter = ['work_order__category', 'variance_classification', ('recorded_at', RangeDateFilter)]
    search_fields = ('variance_code', 'product__name', 'product__sku', 'work_order__work_order_code')
    readonly_fields = ('variance_code', 'get_production_run_type', 'work_order_material_line', 'work_order', 'product', 'quantity_expected', 'quantity_actual', 'quantity_variance', 'unit_cost', 'financial_impact', 'variance_percentage', 'efficiency_rate', 'variance_classification', 'notes', 'recorded_at')
    actions = [export_as_csv]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('work_order', 'product', 'work_order_material_line')

    @display(description='Run Type')
    def get_production_run_type(self, obj):
        run_type = obj.production_run_type
        bg = '#2563eb' if run_type == 'PRODUCTION' else '#f59e0b'
        return render_status_badge(run_type, bg)

    @display(description='Financial Impact')
    def get_financial_impact(self, obj):
        cost = obj.financial_impact or Decimal('0.00')
        if cost > 0:
            return f"+${cost:,.2f}"
        elif cost < 0:
            return f"-${abs(cost):,.2f}"
        return "$0.00"

    @display(description='Classification')
    def classification_badge(self, obj):
        color_map = {
            'UNFAVOURABLE': '#ef4444', # Crimson Red
            'FAVOURABLE': '#10b981',   # Emerald Green
            'EXACT': '#64748b',        # Slate Grey
        }
        bg = color_map.get(obj.variance_classification, '#64748b')
        return render_status_badge(obj.get_variance_classification_display(), bg)
