from django.utils.safestring import mark_safe
from django.contrib import admin
from django.utils.html import format_html
import json
from django.core.serializers.json import DjangoJSONEncoder
from django import forms
from .models import (PurchaseInvoice, Supplier, Product, PurchaseOrder, PurchaseOrderItem, ProcurementOrder, Inventory, StockTransaction, Employee, ProductionOrder, Customer, SalesOrder, SalesOrderItem, DispatchRecord, SalesInvoice, Return, MaterialVarianceRecord, FinanceEntry, WorkOrder, WorkOrderInstruction, BillOfMaterial, BOMItem, SalesInvoicePayments, PurchasePayment, WorkOrderMaterialLine
)
from decimal import Decimal

admin.register(Supplier)
admin.register(Product)
admin.register(PurchaseOrder)
admin.register(PurchaseOrderItem)
admin.register(ProcurementOrder)
admin.register(Inventory) 
admin.register(StockTransaction)  
admin.register(Employee)
admin.register(ProductionOrder)
admin.register(Customer)
admin.register(SalesOrder)
admin.register(SalesOrderItem)
admin.register(DispatchRecord)
admin.register(SalesInvoicePayments)
admin.register(PurchaseInvoice) 
admin.register(PurchasePayment)
admin.register(Return)
admin.register(FinanceEntry)
admin.register(WorkOrder)
admin.register(WorkOrderInstruction)
admin.register(BillOfMaterial)
admin.register(WorkOrderMaterialLine)
admin.register(BOMItem)

def export_as_csv(modeladmin, request, queryset):
    """
    GENERIC ADMIN ACTION:
    Exports selected records from any Django Admin changelist view into a downloadable CSV file.
    Extracts model field values dynamically and sanitizes output formatting.
    """
    import csv
    from django.http import HttpResponse

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

# Register your models here.
class WorkOrderInstructionInline(admin.TabularInline):
    model = WorkOrderInstruction
    extra = 0
    fields = ('step_number', 'step_name', 'machine', 'instruction_text', 'estimated_time_minutes', 'status')
    ordering = ['step_number']
    readonly_fields = ['step_number']  # Auto-incremented field, should not be editable    

class BOMItemInline(admin.TabularInline):
    model = BOMItem
    extra = 1  
    fk_name = 'bom'
    fields = ['component', 'quantity_required']
    # Use autocomplete if your product catalog contains hundreds of items
    autocomplete_fields = ['component']    

class WorkOrderMaterialLineInline(admin.TabularInline):
    model = WorkOrderMaterialLine
    extra = 0  # Don't show empty lines by default
    fields = ('component', 'quantity_expected', 'quantity_actual')
    readonly_fields = ('quantity_expected',)

class ChildPackagingInline(admin.TabularInline):
    """
    Inline UI for auditing Stage 2 child packaging work orders linked to a Stage 1 parent bulk order.
    Enables shop floor operators and managers to track downstream packaging runs without manually
    altering system-calculated parent-child relationships.
    """
    model = WorkOrder
    fk_name = 'parent_work_order'
    verbose_name = "Child Packaging Run"
    verbose_name_plural = "Child Packaging Runs"
    extra = 0
    can_delete = False
    fields = ('work_order_code', 'product', 'status', 'quantity_produced', 'production_start_date')
    readonly_fields = ('work_order_code', 'product', 'status', 'quantity_produced', 'production_start_date')  

class SalesInvoicePaymentsInline(admin.TabularInline):
    model = SalesInvoicePayments
    extra = 1  
    fields = ('amount', 'payment_method', 'paid_at', 'reference_number')   
    readonly_fields = ['paid_at']

class DispatchRecordInline(admin.TabularInline):
    """
    Displays a read-only shipping history directly inside the Sales Order page.
    This gives sales rep a view of order fulfillment!
    """
    model = DispatchRecord
    extra = 0
    fields = ('dispatch_number',  'sales_order_item', 'product', 'quantity_dispatched', 'dispatch_date')
    readonly_fields = ('dispatch_number', 'product', 'quantity_dispatched', 'dispatch_date')
    can_delete = False  # Shipments shouldn't be casually deleted from the order screen

    def has_add_permission(self, request, obj=None):
        # Dispatches can only be created on their own dedicated Dispatch page, not accidentally added from the Sales Order interface.
        return False   
     
class SalesOrderItemInline(admin.TabularInline):
    model = SalesOrderItem
    inlines = [DispatchRecordInline]
    extra = 1
    fields = ('product', 'quantity_ordered', 'quantity_dispatched', 'get_unit_price', 'get_total_price')
    search_fields = ['product__name']
    readonly_fields = ('quantity_dispatched', 'get_unit_price', 'get_total_price')

    @admin.display(description='Catalog Unit Price')
    def get_unit_price(self, obj):
        if obj.unit_price is not None:
            return f"${obj.unit_price:,.2f}"
        return "$0.00"

    @admin.display(description='Line Total')
    def get_total_price(self, obj):
        if obj.total_price:
            return f"${obj.total_price:,.2f}"
        return "$0.00"
class PurchasePaymentInline(admin.TabularInline):
    model = PurchasePayment
    extra = 1
    fields =  ('amount', 'payment_method', 'paid_at', 'reference_number') 
    readonly_fields = ['paid_at']

class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 1  # Shows one empty row by default to quickly add items
    fields = ('product', 'quantity_ordered', 'quantity_received', 'price_per_unit', 'get_total')
    readonly_fields = ('get_total', 'quantity_received', 'price_per_unit')

    def get_total(self, obj):
        if obj.pk:
            return f"${obj.total_price:.2f}"
        return "$0.00"
    get_total.short_description = "Total Cost"    

class ProcurementOrderForm(forms.ModelForm):
    class Meta:
        model = ProcurementOrder
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Check if a purchase order is selected in POST data or existing instance
        po_id = None
        if self.data and self.data.get('purchase_order'):
            po_id = self.data.get('purchase_order')
        elif self.instance and self.instance.purchase_order_id:
            po_id = self.instance.purchase_order_id

        # Filters the 'product' field choices to match the Purchase Order's items
        if po_id:
            po = PurchaseOrder.objects.filter(pk=po_id).first()
            if po:
                valid_product_ids = po.items.values_list('product_id', flat=True)
                self.fields['product'].queryset = Product.objects.filter(pk__in=valid_product_ids)    
  
@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'supplier_id', 'contact_info')
    search_fields = ('name', 'supplier_id','contact_info')

@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ('product', 'quantity_available', 'quantity_allocated', 'location', 'get_unit_cost', 'get_total_valuation', 'last_updated')
    list_filter = ['location', 'last_updated', 'quantity_available', 'quantity_allocated']
    search_fields = ('product__name', 'product__sku', 'location')
    readonly_fields = ['get_total_valuation', 'quantity_allocated']  
    autocomplete_fields = ['product']  # Enables searching products
    actions = [export_as_csv]

    def get_queryset(self, request):
        """N+1 Query Mitigation: Eagerly joins Product and Product Supplier."""
        return super().get_queryset(request).select_related('product', 'product__supplier')

    @admin.display(description='Avg Unit Cost')
    def get_unit_cost(self, obj):
        return f"${obj.unit_cost:,.2f}"

    @admin.display(description='Total Valuation')
    def get_total_valuation(self, obj):
        return f"${obj.total_valuation:,.2f}"

@admin.register(StockTransaction)
class StockTransactionAdmin(admin.ModelAdmin):
    list_display = ('product', 'quantity', 'transaction_type', 'created_at')
    list_filter = ('transaction_type', 'created_at')
    search_fields = ('product__name', 'product__sku', 'reference_type')
    readonly_fields = ('created_at', 'work_order', 'dispatch_record')
    
    # Prevents anyone from manually editing transaction logs to maintain audit integrity
    def has_add_permission(self, request):
        return True # Allowed to add manual adjustments if needed
        
    def has_change_permission(self, request, obj=None):
        return False # Locked!past records cannot be modified
        
    def has_delete_permission(self, request, obj=None):
        return False # Locked    

class ProductionOrderAdminForm(forms.ModelForm):
    class Meta:
        model = ProductionOrder
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # If editing an existing Production Order, filter the Work Order choices to only show those for this specific product.
        if self.instance and self.instance.product_id:
            self.fields['work_order'].queryset = WorkOrder.objects.filter(
                product=self.instance.product
            )    

@admin.register(ProductionOrder)
class ProductionOrderAdmin(admin.ModelAdmin):
    form = ProductionOrderAdminForm
    list_display = ('production_order_code', 'product', 'work_order', 'quantity', 'status', 'get_unit_cost', 'created_at')
    list_filter = ['status', 'created_at']
    search_fields = ('production_order_code', 'work_order__work_order_code', 'work_order__work_order_id', 'employee__employee_name', 'product__name')  
    filter_horizontal = ('employee',)  # For ManyToManyField, use a horizontal filter widget 
    readonly_fields = ['production_order_code', 'status', 'mrp_resolution_pathways_viewer', 'work_order_details_viewer', 'created_at', 'completed_at']
    actions = [export_as_csv, 'trigger_mrp_auto_resume']

    def get_queryset(self, request):
        """N+1 Query Mitigation: Eagerly loads Product and WorkOrder."""
        return super().get_queryset(request).select_related('product', 'work_order')

    @admin.action(description="Check Stock & Auto-Resume On-Hold Orders")
    def trigger_mrp_auto_resume(self, request, queryset):
        from .services import check_and_auto_resume_on_hold_orders
        resumed = check_and_auto_resume_on_hold_orders()
        self.message_user(request, f"MRP evaluation complete. Auto-resumed {len(resumed)} order(s).")

    @admin.display(description='Batch Unit Cost')
    def get_unit_cost(self, obj):
        return f"${obj.unit_cost:,.2f}"
    
    fieldsets = (
       ('Order Information', {
            'fields': ('production_order_code', 'product', 'work_order', 'quantity', 'status')
        }),
        ('MRP Shortage Resolution Pathways', {
            'fields': ('mrp_resolution_pathways_viewer',),
        }),
        ('System Details (Read Only)', {
            'fields': ('work_order_details_viewer', 'created_at', 'completed_at'),
            'classes': ('collapse',), # Collapses this section by default to keep screen clean
        }),
    )

    def mrp_resolution_pathways_viewer(self, obj):
        if not obj or not obj.pk:
            return "Save record to evaluate MRP shortages."

        from .services import evaluate_mrp_shortages
        report = evaluate_mrp_shortages(obj)

        if not report:
            return mark_safe("<span style='color: #666;'>No material blueprint / BOM requirements evaluated.</span>")

        shortage_items = [r for r in report if r['has_shortage']]
        if not shortage_items:
            return mark_safe("<div style='padding: 10px; background: #e8f8f5; border-left: 4px solid #2ecc71; color: #27ae60; border-radius: 4px;'><strong>✓ All Stock Satisfied:</strong> All component inventory levels meet or exceed batch requirements.</div>")

        cards_html = []
        for item in shortage_items:
            comp = item['component']
            shortfall = item['shortfall_qty']
            req = item['required_qty']
            avail = item['available_qty']
            p_type = item['product_type']
            supplier_name = item['supplier'].name if item['supplier'] else "No Supplier Assigned"

            options_html = ""
            if p_type == 'RAW':
                options_html = f"""
                <div style="margin-top: 8px; font-size: 12px; background: #fff; padding: 10px; border-radius: 4px; border: 1px solid #e2e8f0;">
                    <strong style="color: #2b6cb0;">Tailored Resolution Pathways (Raw Material):</strong>
                    <div style="margin-top: 6px; display: grid; gap: 8px;">
                        <div style="background: #ebf8ff; padding: 8px; border-radius: 4px; border-left: 3px solid #3182ce;">
                            <strong>Option 1: Auto-Draft PO</strong> — Append {shortfall:.2f} units to an open DRAFT Purchase Order for supplier <em>{supplier_name}</em>.
                            <form method="POST" action="/mrp_resolve_action/" style="margin-top: 4px;">
                                <input type="hidden" name="production_order_id" value="{obj.pk}">
                                <input type="hidden" name="component_id" value="{comp.pk}">
                                <input type="hidden" name="shortfall_qty" value="{shortfall}">
                                <input type="hidden" name="resolution_action" value="raw_autodraft_po">
                                <button type="submit" style="background: #3182ce; color: white; border: none; padding: 4px 10px; border-radius: 3px; cursor: pointer; font-size: 11px;">Execute Option 1: Auto-Draft PO</button>
                            </form>
                        </div>
                        <div style="background: #feebc8; padding: 8px; border-radius: 4px; border-left: 3px solid #dd6b20;">
                            <strong>Option 2: Direct Procurement</strong> — Spawn a Fast-Track Procurement Order (PENDING status).
                            <form method="POST" action="/mrp_resolve_action/" style="margin-top: 4px;">
                                <input type="hidden" name="production_order_id" value="{obj.pk}">
                                <input type="hidden" name="component_id" value="{comp.pk}">
                                <input type="hidden" name="shortfall_qty" value="{shortfall}">
                                <input type="hidden" name="resolution_action" value="raw_direct_procurement">
                                <button type="submit" style="background: #dd6b20; color: white; border: none; padding: 4px 10px; border-radius: 3px; cursor: pointer; font-size: 11px;">Execute Option 2: Direct Procurement</button>
                            </form>
                        </div>
                        <div style="background: #edf2f7; padding: 8px; border-radius: 4px; border-left: 3px solid #718096;">
                            <strong>Option 3: Hold for Inbound Stock</strong> — Maintain ON_HOLD status to consume stock from in-transit POs (In-Transit Qty: {item['inbound_po_qty']:.2f}).
                            <form method="POST" action="/mrp_resolve_action/" style="margin-top: 4px;">
                                <input type="hidden" name="production_order_id" value="{obj.pk}">
                                <input type="hidden" name="component_id" value="{comp.pk}">
                                <input type="hidden" name="resolution_action" value="raw_hold_inbound">
                                <button type="submit" style="background: #718096; color: white; border: none; padding: 4px 10px; border-radius: 3px; cursor: pointer; font-size: 11px;">Execute Option 3: Hold for Inbound POs</button>
                            </form>
                        </div>
                    </div>
                </div>
                """
            else:
                max_prod = item['max_producible']
                options_html = f"""
                <div style="margin-top: 8px; font-size: 12px; background: #fff; padding: 10px; border-radius: 4px; border: 1px solid #e2e8f0;">
                    <strong style="color: #6b46c1;">Tailored Resolution Pathways (Sub-Assembly / Intermediate Good):</strong>
                    <div style="margin-top: 6px; display: grid; gap: 8px;">
                        <div style="background: #faf5ff; padding: 8px; border-radius: 4px; border-left: 3px solid #805ad5;">
                            <strong>Option 1: Build Sub-Assembly</strong> — Spawn a child Work Order & Production Run for {shortfall:.2f} units of {comp.name}.
                            <form method="POST" action="/mrp_resolve_action/" style="margin-top: 4px;">
                                <input type="hidden" name="production_order_id" value="{obj.pk}">
                                <input type="hidden" name="component_id" value="{comp.pk}">
                                <input type="hidden" name="shortfall_qty" value="{shortfall}">
                                <input type="hidden" name="resolution_action" value="intermediate_build">
                                <button type="submit" style="background: #805ad5; color: white; border: none; padding: 4px 10px; border-radius: 3px; cursor: pointer; font-size: 11px;">Execute Option 1: Build Sub-Assembly</button>
                            </form>
                        </div>
                        <div style="background: #ebf8ff; padding: 8px; border-radius: 4px; border-left: 3px solid #3182ce;">
                            <strong>Option 2: Hold for Active Run</strong> — Link parent order to active shop floor runs (Active Run Qty: {item['active_run_qty']:.2f}).
                            <form method="POST" action="/mrp_resolve_action/" style="margin-top: 4px;">
                                <input type="hidden" name="production_order_id" value="{obj.pk}">
                                <input type="hidden" name="component_id" value="{comp.pk}">
                                <input type="hidden" name="resolution_action" value="intermediate_hold_active">
                                <button type="submit" style="background: #3182ce; color: white; border: none; padding: 4px 10px; border-radius: 3px; cursor: pointer; font-size: 11px;">Execute Option 2: Hold for Active Run</button>
                            </form>
                        </div>
                        <div style="background: #f0fff4; padding: 8px; border-radius: 4px; border-left: 3px solid #38a169;">
                            <strong>Option 3: Partial Batch Run</strong> — Down-scale parent batch target size to match available stock ({max_prod:.2f} units).
                            <form method="POST" action="/mrp_resolve_action/" style="margin-top: 4px;">
                                <input type="hidden" name="production_order_id" value="{obj.pk}">
                                <input type="hidden" name="component_id" value="{comp.pk}">
                                <input type="hidden" name="max_producible" value="{max_prod}">
                                <input type="hidden" name="resolution_action" value="intermediate_partial_batch">
                                <button type="submit" style="background: #38a169; color: white; border: none; padding: 4px 10px; border-radius: 3px; cursor: pointer; font-size: 11px;">Execute Option 3: Scale Batch to {max_prod:.2f} Units</button>
                            </form>
                        </div>
                    </div>
                </div>
                """

            card = f"""
            <div style="margin-bottom: 12px; padding: 12px; background: #fff5f5; border-left: 4px solid #e53e3e; border-radius: 4px; color: #2d3748;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <strong style="color: #c53030; font-size: 14px;">⚠ Shortage: {comp.name} [{p_type}]</strong>
                    <span style="font-size: 12px; background: #feb2b2; color: #9b2c2c; padding: 2px 8px; border-radius: 10px; font-weight: bold;">Shortfall: -{shortfall:.2f}</span>
                </div>
                <div style="font-size: 12px; margin-top: 4px; color: #4a5568;">
                    <strong>Required:</strong> {req:.2f} | <strong>Available:</strong> {avail:.2f}
                </div>
                {options_html}
            </div>
            """
            cards_html.append(card)

        return mark_safe("".join(cards_html))

    @admin.display(description='Product')
    def get_product(self, obj):
        return obj.product.name if obj.product else 'N/A'
    
    @admin.display(description='Quantity')
    def get_quantity(self,obj):
        if obj.work_order:
            return getattr( obj.work_order, 'quantity_produced', getattr(obj.work_order, 'quantity', '0.00')) 
        return "0.00"

    def work_order_details_viewer(self, obj):
        """
        PERFORMANCE OPTIMIZATION:
        Targeted preview lookup that serializes only the specific linked WorkOrder 
        instead of executing full table scans across all historical WorkOrder records.
        """
        if not obj or not obj.work_order_id:
            return format_html("<span style='color: #666; font-style: italic;'>Select a Work Order from the dropdown above to view specifications...</span>")

        wo = obj.work_order
        if not wo:
            return format_html("<span style='color: #666; font-style: italic;'>No Work Order linked.</span>")

        emp_list = [str(emp) for emp in wo.employee.all()]
        html_string = f"""
        <div id="wo-preview-panel" style="margin-top: 10px; padding: 12px; background: #f8f9fa; border-left: 4px solid #79aec8; border-radius: 4px; box-shadow: inset 0 1px 3px rgba(0,0,0,0.05); color: #333; max-width: 600px;">
            <strong style="color: #555; display: block; margin-bottom: 5px;">Blueprint Live Specifications:</strong>
            <div id="wo-preview-content" style="font-size: 13px; line-height: 1.6;">
                <strong>Target Product:</strong> {wo.product.name} ({getattr(wo.product, 'sku', '')}) <br>
                <strong>Expected Yield:</strong> {wo.quantity_produced or '0.00'}<br>
                <strong>Assigned Team/Crew:</strong> {', '.join(emp_list) if emp_list else 'Unassigned'}<br>
                <strong>Current Step Status:</strong> <span style='text-transform: uppercase; font-weight: bold; color: #264b5d;'>{wo.status}</span>
            </div>
        </div>
        """
        return format_html(html_string)
    
    work_order_details_viewer.short_description = "Blueprint Live Specifications"


@admin.register(SalesInvoice)
class SalesInvoiceAdmin(admin.ModelAdmin):
    list_display = ('invoice_number', 'customer', 'dispatch', 'total_amount', 'get_total_paid', 'get_remaining_balance', 'invoice_date', 'status')
    list_filter = ['status', 'invoice_date', 'customer']
    search_fields = ('invoice_number', 'customer__customer_name', 'dispatch__dispatch_code')
    inlines = [SalesInvoicePaymentsInline]
    readonly_fields = ('invoice_number', 'total_amount', 'get_total_paid', 'get_remaining_balance', 'status')
    actions = [export_as_csv]

    def get_queryset(self, request):
        """N+1 Query Mitigation: Eagerly joins Customer, Dispatch, and Payments."""
        return super().get_queryset(request).select_related('customer', 'dispatch').prefetch_related('sales_payments')

    @admin.display(description='Total Paid')
    def get_total_paid(self, obj):
        return f"${obj.total_paid:,.2f}"

    @admin.display(description='Remaining Balance')
    def get_remaining_balance(self, obj):
        bal = obj.remaining_balance
        if bal > 0:
            formatted_bal = f"{bal:,.2f}"
            return format_html('<span style="color: #c53030; font-weight: bold;">${}</span>', formatted_bal)
        return "$0.00"

@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(admin.ModelAdmin):
    list_display = ('invoice_number', 'supplier', 'procurement_order', 'total_amount', 'invoice_date', 'status', 'paid_date', 'remaining_balance')
    list_filter = ['status', 'invoice_date', 'supplier']
    search_fields = ('invoice_number', 'supplier__name', 'procurement_order__procurement_order_id')
    inlines = [PurchasePaymentInline]
    readonly_fields = ['status', 'paid_date', 'remaining_balance', 'total_amount', 'created_at']
    actions = [export_as_csv]

    def get_queryset(self, request):
        """N+1 Query Mitigation: Eagerly joins Supplier, ProcurementOrder, and Payments."""
        return super().get_queryset(request).select_related('supplier', 'procurement_order').prefetch_related('payments')

    def get_balance_status(self, obj):
        balance = obj.remaining_balance
        if balance < 0:
            return f"Overpaid (Credit Due): ${abs(balance)}"
        return f"${balance}"
    
    get_balance_status.short_description = 'Remaining Balance'

@admin.register(Return)
class ReturnAdmin(admin.ModelAdmin):
    list_display = ('dispatch_id', 'customer', 'quantity_returned', 'reason_for_return', 'quality_control_status')
    list_filter = ['quality_control_status']
    search_fields = ('dispatch_id__dispatch_id', 'customer__customer_name')
    actions = [export_as_csv]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('dispatch_id', 'customer')

@admin.register(FinanceEntry)
class FinanceEntryAdmin(admin.ModelAdmin):
    list_display = ('finance_entry_id', 'entry_type', 'amount', 'entry_date', 'category')
    list_filter = ['entry_type', 'category']
    search_fields = ['category', 'sales_invoice__invoice_number', 'procurement_order__procurement_order_id']
    actions = [export_as_csv]

@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ( 'employee_code', 'employee_id', 'employee_name', 'role', 'phone_number', 'email')
    search_fields = ('employee_id', 'employee_code', 'employee_name', 'role')    
    readonly_fields = ['employee_code']
    ordering=['employee_id']
    actions = [export_as_csv]

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('customer_name', 'contact_info', 'shipping_address')
    search_fields = ('customer_name', 'contact_info')
    actions = [export_as_csv]

@admin.register(SalesOrder)
class SalesOrderAdmin(admin.ModelAdmin):
    list_display = ('order_number', 'customer', 'status', 'created_at', 'updated_at', 'get_order_total')
    list_filter = ('status', 'created_at')
    search_fields = ('order_number', 'customer__customer_name')
    readonly_fields = ('order_number', 'status', 'created_at', 'updated_at')
    inlines = [SalesOrderItemInline]
    actions = [export_as_csv]

    def get_queryset(self, request):
        """N+1 Query Mitigation: Eagerly joins Customer and prefetches SalesOrderItems with Product."""
        return super().get_queryset(request).select_related('customer').prefetch_related('items__product')

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.update_status(save=True)

    @admin.display(description='Order Total')
    def get_order_total(self, obj):
        total = sum(item.total_price for item in obj.items.all())
        return f"${total:,.2f}"
@admin.register(BillOfMaterial)
class BillOfMaterialAdmin(admin.ModelAdmin):
    list_display = ('product', 'name', 'is_active', 'get_component_count', 'updated_at')
    list_filter = ['is_active']
    search_fields = ('product__name', 'name', 'product__sku', 'items__component__name')
    inlines = [BOMItemInline]

    @admin.display(description='Total Ingredients')
    def get_component_count(self, obj):
        return obj.items.count()    

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('sku', 'name', 'product_type', 'supplier', 'get_selling_price')
    list_filter = ['product_type', 'supplier']
    search_fields = ('sku', 'name', 'supplier__name')
    # 'sku' has editable=False on the model
    readonly_fields = ('sku',)

    @admin.display(description='Selling Price')
    def get_selling_price(self, obj):
        if obj.selling_price is not None:
            return f"${obj.selling_price:,.2f}"
        return "-"  # Shows dash for Raw Materials & Intermediates


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ('po_number', 'supplier', 'order_date', 'status_badge')
    list_filter = ('status', 'order_date', 'supplier')
    search_fields = ('po_number', 'supplier__name')
    inlines = [PurchaseOrderItemInline]
    readonly_fields = ('po_number', 'order_date', 'status')
    actions = [export_as_csv]

    def get_queryset(self, request):
        """N+1 Query Mitigation: Eagerly joins Supplier and prefetches items with Product."""
        return super().get_queryset(request).select_related('supplier').prefetch_related('items__product')

    fieldsets = (
        ('Order Details', {
            'fields': ('po_number', 'supplier', 'order_date', 'notes')
        }),
        ('Status (Auto-Managed)', {
            'fields': ('status',),
            'description': (
                'Status is automatically updated by the system: '
                'DRAFT → Sent (when items are added) → '
                'Partially Received / Fully Received (driven by procurement deliveries).'
            ),
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status in ('RECEIVED', 'CANCELLED'):
            return [field.name for field in self.model._meta.fields]
        return self.readonly_fields

    @admin.display(description='Status')
    def status_badge(self, obj):
        colours = {
            'DRAFT':      '#718096',   # grey
            'SENT':       '#3182ce',   # blue
            'PARTIAL':    '#d69e2e',   # amber
            'RECEIVED':   '#38a169',   # green
            'CANCELLED':  '#e53e3e',   # red
        }
        colour = colours.get(obj.status, '#718096')
        return format_html(
            '<b style="color: {};">{}</b>',
            colour,
            obj.get_status_display()
        )

@admin.register(ProcurementOrder)
class ProcurementOrderAdmin(admin.ModelAdmin):
    form = ProcurementOrderForm
    autocomplete_fields = ['purchase_order']
    list_display = ('procurement_order_id', 'purchase_order', 'product', 'quantity', 'price_per_unit', 'total_cost', 'delivery_date', 'status')
    list_filter = ['status', 'delivery_date', 'delivery_location']
    search_fields = ('product__name', 'product__sku', 'purchase_order__po_number')
    readonly_fields = ['total_cost', 'delivery_date']
    actions = [export_as_csv]

    def get_queryset(self, request):
        """N+1 Query Mitigation: Eagerly joins PurchaseOrder, Product, and Supplier."""
        return super().get_queryset(request).select_related('purchase_order', 'product', 'purchase_order__supplier')

@admin.register(DispatchRecord)
class DispatchRecordAdmin(admin.ModelAdmin):
    list_display = ['dispatch_code', 'dispatch_id', 'customer', 'sales_order_item', 'product', 'quantity_dispatched', 'dispatch_date', 'status', 'delivery_date']
    list_filter = ['dispatch_date', 'status', 'delivery_date', 'customer']
    search_fields = ['dispatch_code', 'sales_order_item__sales_order__order_number', 'customer__customer_name', 'product__sku', 'product__name']
    readonly_fields = ('dispatch_code', 'delivery_date', 'is_stock_deducted')
    actions = [export_as_csv]

    def get_queryset(self, request):
        """N+1 Query Mitigation: Eagerly joins Customer, Product, and SalesOrderItem."""
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

@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ('work_order_code', 'work_order_id', 'product', 'display_employees', 'display_target_quantity', 'production_start_date', 'production_end_date', 'status', 'is_inventory_updated')
    readonly_fields = ['work_order_code', 'status', 'is_inventory_allocated', 'is_inventory_updated', 'production_end_date', 'parent_work_order']
    inlines = [WorkOrderInstructionInline, WorkOrderMaterialLineInline, ChildPackagingInline]
    list_filter = ['status', 'is_inventory_updated', 'production_start_date']
    search_fields = ('work_order_code', 'product__name', 'product__sku', 'employee__employee_name')
    filter_horizontal = ('employee',)
    actions = [export_as_csv]

    def get_queryset(self, request):
        """N+1 Query Mitigation: Eagerly loads product, BOM, parent order, employees, and production runs."""
        return super().get_queryset(request).select_related('product', 'bill_of_material', 'parent_work_order').prefetch_related('employee', 'production_runs', 'material_lines')

    fieldsets = (
        ('Order Specification', {
            'fields': (
                'product',
                'bill_of_material',
                'quantity_produced',
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
                'is_inventory_updated',
            ),
        }),
    )

    def save_formset(self, request, form, formset, change):
        """
        Prevents UNIQUE constraint crashes by merging Admin Inline edits 
        with records auto-generated by WorkOrder.save().
        """
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
        print("\n[ADMIN SAVE_RELATED] Saving inline material lines to DB first...", flush=True)
        super().save_related(request, form, formsets, change)
        
        print("[ADMIN SAVE_RELATED] Inline material lines saved. Calling process_inventory()...", flush=True)
        work_order = form.instance
        work_order.process_inventory()

    @admin.display(description='Assigned Employees')
    def display_employees(self, obj):
        employees = obj.employee.all()
        if employees.exists():
            return ", ".join([str(emp) for emp in employees])
        return "-"

    @admin.display(description='Target Quantity')
    def display_target_quantity(self, obj):
        return f"{obj.target_quantity:.2f}"

    @admin.display(description='Status')
    def status_badge(self, obj):
        colors = {
            'COMPLETED': 'green',
            'IN_PROGRESS': '#3182ce',
            'CANCELLED': 'red',
        }
        color = colors.get(obj.status, 'gray')
        return format_html(
            '<b style="color: {};">{}</b>', 
            color, 
            obj.get_status_display()
        )

@admin.register(MaterialVarianceRecord)
class MaterialVarianceRecordAdmin(admin.ModelAdmin):
    list_display = ('variance_code', 'work_order', 'product', 'quantity_expected', 'quantity_actual', 'quantity_variance', 'get_financial_impact', 'get_classification_badge', 'recorded_at')
    list_filter = ['variance_classification', 'recorded_at']
    search_fields = ('variance_code', 'product__name', 'product__sku', 'work_order__work_order_code')
    readonly_fields = ('variance_code', 'work_order_material_line', 'work_order', 'product', 'quantity_expected', 'quantity_actual', 'quantity_variance', 'unit_cost', 'financial_impact', 'variance_percentage', 'efficiency_rate', 'variance_classification', 'notes', 'recorded_at')
    actions = [export_as_csv]

    def get_queryset(self, request):
        """N+1 Query Mitigation: Eagerly joins WorkOrder, Product, and MaterialLine."""
        return super().get_queryset(request).select_related('work_order', 'product', 'work_order_material_line')

    @admin.display(description='Financial Impact')
    def get_financial_impact(self, obj):
        cost = obj.financial_impact or Decimal('0.00')
        if cost > 0:
            formatted_cost = f"{cost:,.2f}"
            return format_html('<span style="color: #c53030; font-weight: bold;">+${}</span>', formatted_cost)
        elif cost < 0:
            formatted_cost = f"{abs(cost):,.2f}"
            return format_html('<span style="color: #27ae60; font-weight: bold;">-${}</span>', formatted_cost)
        return "$0.00"

    @admin.display(description='Variance Classification')
    def get_classification_badge(self, obj):
        if obj.variance_classification == 'UNFAVOURABLE':
            return format_html('<span style="color: #c53030; font-weight: bold; background: #fff5f5; padding: 2px 6px; border-radius: 4px;">Unfavourable (Scrap/Waste)</span>')
        elif obj.variance_classification == 'FAVOURABLE':
            return format_html('<span style="color: #27ae60; font-weight: bold; background: #e8f8f5; padding: 2px 6px; border-radius: 4px;">Favourable (Efficiency/Saved)</span>')
        return format_html('<span style="color: #4a5568; font-weight: bold;">Exact Match</span>')




   
