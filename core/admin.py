from django.contrib import admin
from django.utils.html import format_html
from .models import (InvoiceLine, Supplier, Product, ProcurementOrder, Inventory, Employee, ProductionOrder, Customer, DispatchRecord, Invoice, Return, LossRecord, FinanceEntry, WorkOrder, WorkOrderInstruction
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
admin.register(InvoiceLine)
admin.register(Return)
admin.register(LossRecord)
admin.register(FinanceEntry)
admin.register(WorkOrder)
admin.register(WorkOrderInstruction)

# Register your models here.
class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 1
    fields = ('description', 'quantity', 'unit_price', 'line_total')
    readonly_fields = ['line_total'] #computed field on save, should not be editable
class WorkOrderInstructionInline(admin.TabularInline):
    model = WorkOrderInstruction
    extra = 1
    fields = ('step_number', 'machine', 'instruction_text', 'estimated_time_minutes')    
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

@admin.register(ProductionOrder)
class ProductionOrderAdmin(admin.ModelAdmin):
    list_display = ('product', 'display_employees', 'work_order', 'quantity_produced', 'actual_start_date', 'actual_end_date', 'status')
    list_filter = ['status', 'actual_start_date']
    search_fields = ('work_order__work_order_id', 'employee__employee_name')  
    filter_horizontal = ('employee',)  # For ManyToManyField, use a horizontal filter widget 

    def display_employees(self, obj):
        return ", ".join([employee.employee_name for employee in obj.employee.all()])
    display_employees.short_description = 'Assigned Employees'


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('invoice_id', 'invoice_type', 'customer', 'total_amount', 'invoice_date', 'status')
    list_filter = ['invoice_type', 'status', 'invoice_date']
    inlines = [InvoiceLineInline]
    readonly_fields = ['total_amount']  # Computed field, should not be editable
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

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('sku', 'name', 'product_type', 'supplier', 'cost_per_unit')
    list_filter = ['product_type', 'supplier']
    search_fields = ('sku', 'name', 'supplier__name')

@admin.register(ProcurementOrder)
class ProcurementOrderAdmin(admin.ModelAdmin):
    list_display = ('procurement_order_id', 'product', 'supplier', 'quantity_ordered', 'price_per_unit', 'total_cost', 'order_date', 'status')
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
    list_display = ('work_order_id', 'product', 'display_employees', 'quantity_produced', 'production_start_date', 'production_end_date')
    inlines = [WorkOrderInstructionInline]
    search_fields = ('product__name', 'product__sku', 'employee__employee_name')
    filter_horizontal = ('employee',)  # For ManyToManyField, use a horizontal filter widget

    def display_employees(self, obj):
        return ", ".join([employee.employee_name for employee in obj.employee.all()])
    display_employees.short_description = 'Assigned Crew Members'


   
