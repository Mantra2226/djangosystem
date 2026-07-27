from django.contrib import admin
from django.utils.html import format_html
import json
from django.core.serializers.json import DjangoJSONEncoder
from django import forms
from .models import (PurchaseInvoice, Supplier, Product, PurchaseOrder, PurchaseOrderItem, ProcurementOrder, Inventory, StockTransaction, Employee, ProductionOrder, Customer, SalesOrder, SalesOrderItem, DispatchRecord, Invoice, Return, LossRecord, FinanceEntry, WorkOrder, WorkOrderInstruction, BillOfMaterial, BOMItem, SalesInvoicePayments, PurchasePayment, WorkOrderMaterialLine
)

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
admin.register(Invoice)
admin.register(SalesInvoicePayments)
admin.register(PurchaseInvoice) 
admin.register(PurchasePayment)
admin.register(Return)
admin.register(LossRecord)
admin.register(FinanceEntry)
admin.register(WorkOrder)
admin.register(WorkOrderInstruction)
admin.register(BillOfMaterial)
admin.register(WorkOrderMaterialLine)
admin.register(BOMItem)

# Register your models here.
class WorkOrderInstructionInline(admin.TabularInline):
    model = WorkOrderInstruction
    extra = 0
    fields = ('step_number', 'step_name', 'machine', 'instruction_text', 'estimated_time_minutes', 'status')
    ordering = ['step_number']
    readonly_fields = ['step_number']  # Auto-incremented field, should not be editable    

class BOMItemInline(admin.TabularInline):
    model = BOMItem
    extra = 1  # Provides one empty row by default for quick typing
    fk_name = 'bom'
    # Use autocomplete if your product catalog contains hundreds of items
    autocomplete_fields = ['component']    

class WorkOrderMaterialLineInline(admin.TabularInline):
    model = WorkOrderMaterialLine
    readonly_fields = ('quantity_expected', 'get_variance')
    extra = 0  # Don't show empty lines by default
    fields = ('component', 'quantity_expected', 'quantity_actual', 'get_variance')  

    @admin.display(description='Variance (Over/Under)')
    def get_variance(self, instance):
        if instance.pk:
            var = instance.variance
            if var > 0:
                return format_html('<span style="color: red; font-weight: bold;">+{} (Waste)</span>', var)
            elif var < 0:
                return format_html('<span style="color: green; font-weight: bold;">{} (Saved)</span>', var)
            return "0.00"
        return "-"  

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
    extra = 1  #
    fields = ('product', 'quantity_ordered', 'quantity_dispatched', 'unit_price', 'get_total_price')
    search_fields = ['product__name']
    readonly_fields = ('quantity_dispatched', 'get_total_price')

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
    list_display = ('product', 'quantity_available', 'location', 'get_unit_cost', 'get_total_valuation', 'last_updated')
    list_filter = ['location', 'last_updated']
    search_fields = ('product__name', 'product__sku', 'location')
    readonly_fields = ['get_total_valuation']  # Computed field, should not be editable

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
        return False # Locked!    

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
    list_display = ('product', 'work_order', 'quantity', 'status', 'get_unit_cost', 'created_at')
    list_filter = ['status', 'created_at']
    search_fields = ('work_order__work_order_id', 'employee__employee_name', 'product__name')  
    filter_horizontal = ('employee',)  # For ManyToManyField, use a horizontal filter widget 
    readonly_fields = ['work_order_details_viewer', 'created_at', 'completed_at']

    @admin.display(description='Batch Unit Cost')
    def get_unit_cost(self, obj):
        return f"${obj.unit_cost:,.2f}"
    
    fieldsets = (
       ('Order Information', {
            'fields': ('product', 'work_order', 'quantity', 'status')
        }),
        ('System Details (Read Only)', {
            'fields': ('work_order_details_viewer', 'created_at', 'completed_at'),
            'classes': ('collapse',), # Collapses this section by default to keep screen clean
        }),
    )

    @admin.display(description='Product')
    def get_product(self, obj):
        return obj.product.name if obj.product else 'N/A'
    
    @admin.display(description='Quantity')
    def get_quantity(self,obj):
        if obj.work_order:
            return getattr( obj.work_order, 'quantity_produced', getattr(obj.work_order, 'quantity', '0.00')) 
        return "0.00"

    def work_order_details_viewer(self, obj):
        work_orders = WorkOrder.objects.all()
        blueprint_data = {}
        for wo in work_orders:
            if hasattr(wo, 'product') and wo.product:

                emp_list = []
                if hasattr(wo, 'employee') and wo.employee:
                    emp_list = [getattr(emp, 'employee_name', str(emp)) for emp in wo.employee.all()]
                
                blueprint_data[wo.work_order_id] = {
                    'product_name': wo.product.name,
                    'product_sku': getattr(wo.product, 'sku', ''),
                    'assigned_employees': emp_list,
                    'quantity': getattr(wo, 'quantity', getattr(wo, 'quantity', '0.00')),
                    'production_start_date': getattr(wo, 'production_start_date', ''),
                    'production_end_date': getattr(wo, 'production_end_date', ''),
                    'status': getattr(wo, 'status', 'N/A'),
                }

        json_data = json.dumps(blueprint_data, cls=DjangoJSONEncoder)   

        html_string = f"""
        <div id="wo-preview-panel" style="margin-top: 10px; padding: 12px; background: #f8f9fa; border-left: 4px solid #79aec8; border-radius: 4px; box-shadow: inset 0 1px 3px rgba(0,0,0,0.05); color: #333; max-width: 600px;">
            <strong style="color: #555; display: block; margin-bottom: 5px;">Blueprint Live Specifications:</strong>
            <div id="wo-preview-content" style="font-size: 13px; line-height: 1.6;">
                <span style="color: #666; font-style: italic;">Select a Work Order from the dropdown above to look into its structural details...</span>
            </div>
        </div>
        
        <script type="text/javascript">
            document.addEventListener('DOMContentLoaded', function() {{
                var blueprintLookup = {json_data};
                var selectField = document.getElementById('id_work_order');
                var displayBox = document.getElementById('wo-preview-content');
                
                if (!selectField) return;
                
                function updateLiveUI() {{
                    var selectedId = selectField.value;
                    if (selectedId && blueprintLookup[selectedId]) {{
                        var info = blueprintLookup[selectedId];
                        displayBox.innerHTML = 
                            "<strong>Target Product:</strong> " + info.product_name + "(" + info.product_sku + ") <br>" +
                            "<strong>Expected Yield:</strong> " + info.quantity_produced + "<br>" +
                            "<strong>Assigned Team/Crew:</strong> " + info.assigned_employees + "<br>" +
                            "<strong>Current Step Status:</strong> <span style='text-transform: uppercase; font-weight: bold; color: #264b5d;'>" + info.status + "</span>";
                    }} else {{
                        displayBox.innerHTML = "<span style='color: #666; font-style: italic;'>Select a Work Order from the dropdown above to look into its structural details...</span>";
                    }}
                }}
                
                // Fire update every time the user clicks a different option
                selectField.addEventListener('change', updateLiveUI);
                
                // Fire instantly on page load if editing an existing run
                updateLiveUI(); 
            }});
        </script>
        """ 
        return format_html(html_string)
    
    work_order_details_viewer.short_description = "Blueprint Live Specifications"


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('invoice_number', 'customer', 'total_amount', 'remaining_balance', 'invoice_date', 'status')
    list_filter = ['status', 'invoice_date', 'customer']
    search_fields = ('invoice_number', 'customer__customer_name', 'dispatch__dispatch_id')
    inlines = [SalesInvoicePaymentsInline]
    readonly_fields = ['status', 'remaining_balance']  # Computed field, should not be editable

    def get_balance_status(self, obj):
        balance = obj.remaining_balance
        if balance < 0:
            return f"Overpaid (Credit Due): ${abs(balance)}"
        return f"${balance}"
    
    # Renames the column header in the admin table list view
    get_balance_status.short_description = 'Remaining Balance'

@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(admin.ModelAdmin):
    list_display = ('invoice_number', 'supplier', 'procurement_order', 'total_amount', 'invoice_date', 'status', 'remaining_balance')
    list_filter = ['status', 'invoice_date', 'supplier']
    search_fields = ('invoice_number', 'supplier__name', 'procurement_order__procurement_order_id')
    inlines = [PurchasePaymentInline]
    readonly_fields = ['status', 'remaining_balance', 'total_amount', 'created_at']

    def get_balance_status(self, obj):
        balance = obj.remaining_balance
        if balance < 0:
            return f"Overpaid (Credit Due): ${abs(balance)}"
        return f"${balance}"
    
    # Renames the column header in the admin table list view
    get_balance_status.short_description = 'Remaining Balance'
@admin.register(Return)
class ReturnAdmin(admin.ModelAdmin):
    list_display = ('dispatch_id', 'customer', 'quantity_returned', 'reason_for_return', 'quality_control_status')
    list_filter = ['quality_control_status']
    search_fields = ('dispatch_id__dispatch_id', 'customer__customer_name')

@admin.register(LossRecord)
class LossRecordAdmin(admin.ModelAdmin):
    list_display = ('product', 'quantity_lost', 'reason', 'loss_date')
    search_fields = ('product__name', 'product__sku', 'reason')

@admin.register(FinanceEntry)
class FinanceEntryAdmin(admin.ModelAdmin):
    list_display = ('finance_entry_id', 'entry_type', 'amount', 'entry_date', 'category')
    list_filter = ['entry_type', 'category']
    search_fields = ['category', 'invoice__invoice_id', 'procurement_order__procurement_order_id']

@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ( 'employee_code', 'employee_id', 'employee_name', 'role', 'phone_number', 'email')
    search_fields = ('employee_id', 'employee_code', 'employee_name', 'role')    
    readonly_fields = ['employee_code']
    ordering=['employee_id']
@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('customer_name', 'contact_info', 'shipping_address')
    search_fields = ('customer_name', 'contact_info')

@admin.register(SalesOrder)
class SalesOrderAdmin(admin.ModelAdmin):
    list_display = ('order_number', 'customer', 'status', 'created_at', 'updated_at', 'get_order_total')
    list_filter = ('status', 'created_at')
    search_fields = ('order_number', 'customer__name')
    inlines = [SalesOrderItemInline]

    @admin.display(description='Order Total')
    def get_order_total(self, obj):
        total = sum(item.total_price for item in obj.items.all())
        return f"${total:,.2f}"
@admin.register(BillOfMaterial)
class BillOfMaterialAdmin(admin.ModelAdmin):
    list_display = ('product', 'name', 'is_active', 'get_component_count', 'updated_at')
    list_filter = ['is_active']
    search_fields = ('product__name', 'name', 'product__sku')
    inlines = [BOMItemInline]

    @admin.display(description='Total Ingredients')
    def get_component_count(self, obj):
        return obj.items.count()    

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('sku', 'name', 'product_type', 'supplier', 'get_selling_price')
    list_filter = ['product_type', 'supplier']
    search_fields = ('sku', 'name', 'supplier__name')

    # 'sku' has editable=False on the model, so Django won't include it in the
    # form automatically. Adding it here as a readonly_field makes it still
    # visible in the admin detail view — just not as an editable input.
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

    # -------------------------------------------------------------------------
    # 'status' is system-managed via signals and update_delivery_status().
    # Operators must never be able to edit it manually — mark it read-only here.
    # 'po_number' and 'order_date' are also auto-generated / auto-stamped.
    # -------------------------------------------------------------------------
    readonly_fields = ('po_number', 'order_date', 'status')

    fieldsets = (
        ('Order Details', {
            'fields': ('po_number', 'supplier', 'order_date', 'notes')
        }),
        ('Status (Auto-Managed)', {
            # Grouped separately to make it obvious this field is read-only
            'fields': ('status',),
            'description': (
                'Status is automatically updated by the system: '
                'DRAFT → Sent (when items are added) → '
                'Partially Received / Fully Received (driven by procurement deliveries).'
            ),
        }),
    )

    # -------------------------------------------------------------------------
    # Dynamic field lockdown: once the PO is in a terminal state (RECEIVED or
    # CANCELLED) every field on the form becomes read-only to protect the record.
    # -------------------------------------------------------------------------
    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status in ('RECEIVED', 'CANCELLED'):
            # Lock the entire form — return every field name as read-only
            return [field.name for field in self.model._meta.fields]
        # For active orders, only lock the auto-managed fields
        return self.readonly_fields

    # -------------------------------------------------------------------------
    # Coloured status badge for the list view — makes the current state
    # immediately obvious without opening the record.
    # -------------------------------------------------------------------------
    @admin.display(description='Status')
    def status_badge(self, obj):
        colours = {
            'DRAFT':      '#718096',   # grey
            'SENT':       '#3182ce',   # blue  — pending delivery
            'PARTIAL':    '#d69e2e',   # amber — some goods received
            'RECEIVED':   '#38a169',   # green — fully received
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
    autocomplete_fields = ['purchase_order']    # Optional: Use autocomplete for quick search if POs grow large
    list_display = ('procurement_order_id', 'purchase_order', 'product', 'quantity', 'price_per_unit', 'total_cost', 'delivery_date', 'status')
    list_filter = ['status', 'delivery_date', 'delivery_location']
    search_fields = ('product__name', 'product__sku', 'purchase_order__po_number')
    readonly_fields = ['total_cost', 'delivery_date']  # Computed field, should not be editable

@admin.register(DispatchRecord)
class DispatchRecordAdmin(admin.ModelAdmin):
    list_display = ['dispatch_id', 'sales_order_item', 'product', 'quantity_dispatched', 'dispatch_date', 'status', 'delivery_date']
    list_filter = ['dispatch_date', 'status', 'delivery_date']
    search_fields = ['sales_order__order_number', 'product__sku', 'product__name']
    readonly_fields = ('delivery_date', 'is_stock_deducted')

    # DYNAMIC FIELD LOCKDOWN: Once delivered, lock the whole form!
    def get_readonly_fields(self, request, obj=None):
        # If the record already exists and stock has already been deducted...
        if obj and obj.is_stock_deducted:
            # Return ALL fields as read-only to prevent tampering with historical data
            return [field.name for field in self.model._meta.fields]
        
        # If it's still pending/shipped, only the automated fields are read-only
        return self.readonly_fields

    # better UI presentation grouping
    fieldsets = (
        ('Order & Logistics Information', {
            'fields': ('sales_order_item', 'product', 'quantity_dispatched')
        }),
        ('Status & Timestamps', {
            'fields': ('status', 'dispatch_date', 'delivery_date', 'is_stock_deducted')
        }),
    )


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ('work_order_id', 'product', 'display_employees', 'quantity_produced', 'production_start_date', 'production_end_date', 'status', 'is_inventory_updated')
    readonly_fields = ['status', 'is_inventory_updated', 'production_end_date']
    inlines = [WorkOrderInstructionInline, WorkOrderMaterialLineInline]
    list_filter = ['status', 'is_inventory_updated', 'production_start_date']
    search_fields = ('product__name', 'product__sku', 'employee__employee_name')
    filter_horizontal = ('employee',)  # For ManyToManyField, use a horizontal filter widget
    fieldsets = (
        ('Order Overview', {
            'fields': (
                'product',
                'bill_of_material',
                'quantity_produced',
                'employee',
            )
        }),
        ('Execution Tracking', {
            'fields': (
                'status',
                'production_start_date',
                'production_end_date',
                'is_inventory_updated',
            )
        }),
    )
    @admin.display(description='Assigned Employees')
    def display_employees(self, obj):
        employees = obj.employee.all()
        if employees.exists():
            return ", ".join([str(emp) for emp in employees])
        return "-"

    @admin.display(description='Status')
    def status_badge(self, obj):
        colors = {
            'COMPLETED': 'green',
            'IN_PROGRESS': '#3182ce',  # blue
            'CANCELLED': 'red',
        }
        color = colors.get(obj.status, 'gray')
        return format_html(
            '<b style="color: {};">{}</b>', 
            color, 
            obj.get_status_display()
        )




   
