from django.contrib import admin
from django.utils.html import format_html
import json
from django.core.serializers.json import DjangoJSONEncoder
from django import forms
from .models import (PurchaseInvoice, Supplier, Product, ProcurementOrder, Inventory, Employee, ProductionOrder, Customer, DispatchRecord, Invoice, Return, LossRecord, FinanceEntry, WorkOrder, WorkOrderInstruction, BillOfMaterial, BOMItem, SalesInvoicePayments, PurchasePayment
)

admin.register(Supplier)
admin.register(Product)
admin.register(ProcurementOrder)
admin.register(Inventory)   
admin.register(Employee)
admin.register(ProductionOrder)
admin.register(Customer)
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
admin.register(BOMItem)

# Register your models here.
class WorkOrderInstructionInline(admin.TabularInline):
    model = WorkOrderInstruction
    extra = 1
    fields = ('step_number', 'step_name', 'machine', 'instruction_text', 'estimated_time_minutes', 'status')
    readonly_fields = ['step_number']  # Auto-incremented field, should not be editable    

class BOMItemInline(admin.TabularInline):
    model = BOMItem
    extra = 1  # Provides one empty row by default for quick typing
    fk_name = 'bom'
    # Use autocomplete if your product catalog contains hundreds of items
    autocomplete_fields = ['component']    

class SalesInvoicePaymentsInline(admin.TabularInline):
    model = SalesInvoicePayments
    extra = 1  
    fields = ('amount', 'payment_method', 'paid_at', 'reference_number')   
    readonly_fields = ['paid_at']

class PurchasePaymentInline(admin.TabularInline):
    model = PurchasePayment
    extra = 1
    fields =  ('amount', 'payment_method', 'paid_at', 'reference_number') 
    readonly_fields = ['paid_at']
  
@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'supplier_id', 'contact_info', 'payment_terms')
    search_fields = ('name', 'supplier_id','contact_info')

@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ('product', 'quantity_available', 'location', 'valuation')
    list_filter = ['location', 'product__product_type']
    search_fields = ('product__name', 'product__sku', 'location')
    readonly_fields = ['valuation']  # Computed field, should not be editable

class ProductionOrderAdminForm(forms.ModelForm):
    class Meta:
        model = ProductionOrder
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # THE UX FILTER:
        # If editing an existing Production Order, 
        # filter the Work Order choices to only show those for this specific product.
        if self.instance and self.instance.product_id:
            self.fields['work_order'].queryset = WorkOrder.objects.filter(
                product=self.instance.product
            )    

@admin.register(ProductionOrder)
class ProductionOrderAdmin(admin.ModelAdmin):
    form = ProductionOrderAdminForm
    list_display = ('product', 'work_order', 'quantity', 'status', 'created_at')
    list_filter = ['status', 'created_at']
    search_fields = ('work_order__work_order_id', 'employee__employee_name', 'product__name')  
    filter_horizontal = ('employee',)  # For ManyToManyField, use a horizontal filter widget 
    readonly_fields = ['work_order_details_viewer', 'created_at', 'completed_at']
    
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
    list_display = ('invoice_number', 'supplier', 'total_amount', 'invoice_date', 'status', 'remaining_balance')
    list_filter = ['status', 'invoice_date']
    search_fields = ('invoice_number', 'supplier__name')
    inlines = [PurchasePaymentInline]
    readonly_fields = ['status', 'remaining_balance']

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
    list_display = ('employee_name', 'role', 'phone_number', 'email')
    search_fields = ('employee_name', 'role')    

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('customer_name', 'contact_info')
    search_fields = ('customer_name', 'contact_info')

@admin.register(BillOfMaterial)
class BillOfMaterialAdmin(admin.ModelAdmin):
    list_display = ('product', 'name', 'is_active', 'get_component_count', 'updated_at')
    list_filter = ['is_active']
    search_fields = ('product__name', 'name', 'product__sku')
    inlines = [BOMItemInline]

    @admin.display(description='Total Ingredients')
    def get_component_count(self, obj):
        return obj.components.count()    

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('sku', 'name', 'product_type', 'supplier', 'cost_per_unit')
    list_filter = ['product_type', 'supplier']
    search_fields = ('sku', 'name', 'supplier__name')

@admin.register(ProcurementOrder)
class ProcurementOrderAdmin(admin.ModelAdmin):
    list_display = ('procurement_order_id', 'product', 'supplier', 'quantity', 'price_per_unit', 'total_cost', 'order_date', 'status')
    list_filter = ['status', 'order_date', 'supplier']
    search_fields = ('product__name', 'product__sku', 'supplier__name')
    readonly_fields = ['total_cost']  # Computed field, should not be editable

@admin.register(DispatchRecord)
class DispatchRecordAdmin(admin.ModelAdmin):
    list_display = ['dispatch_id', 'customer', 'quantity_dispatched', 'dispatch_date', 'delivery_date']
    list_filter = ['dispatch_date', 'delivery_date']
    search_fields = ['customer__customer_name', 'production_order__production_order_id']


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ('work_order_id', 'product', 'display_employees', 'quantity', 'production_start_date', 'production_end_date')
    inlines = [WorkOrderInstructionInline]
    search_fields = ('product__name', 'product__sku', 'employee__employee_name')
    filter_horizontal = ('employee',)  # For ManyToManyField, use a horizontal filter widget

    def display_employees(self, obj):
        return ", ".join([employee.employee_name for employee in obj.employee.all()])
    display_employees.short_description = 'Assigned Crew Members'


   
