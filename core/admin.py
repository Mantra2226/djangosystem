from django.contrib import admin
from .models import (InvoiceLine, Supplier, RawMaterial, ProcurementOrder, Inventory, Employee, ProductionOrder, Customer, DispatchRecord, Invoice, Return, LossRecord, FinanceEntry, WorkOrder, WorkOrderInstruction
)

admin.register(Supplier)
admin.register(RawMaterial)
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
@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'supplier_id', 'contact_info', 'payment_terms')
    search_fields = ('name', 'supplier_id','contact_info')

@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ('material_id', 'quantity_available', 'location', 'valuation')
    search_fields = ('material_id', 'location')

@admin.register(ProductionOrder)
class ProductionOrderAdmin(admin.ModelAdmin):
    list_display = ('material_id', 'employee_id', 'work_order_id', 'quantity_consumed', 'quantity_produced', 'production_start_date', 'production_end_date')
    search_fields = ('material_id', 'employee_id', 'work_order_id')   

@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('invoice_id', 'customer_id', 'total_amount', 'invoice_date', 'status')
    search_fields = ('invoice_id', 'customer_id')

@admin.register(InvoiceLine)
class InvoiceLineAdmin(admin.ModelAdmin):
    list_display = ('invoice_line_id', 'invoice_id', 'supplier_id', 'description', 'quantity', 'unit_price', 'line_total')
    search_fields = ('invoice_line_id', 'invoice_id', 'description')

@admin.register(Return)
class ReturnAdmin(admin.ModelAdmin):
    list_display = ('dispatch_id', 'customer_id', 'quantity_returned', 'reason_for_return', 'quality_control_status')
    search_fields = ('dispatch_id__production_order__work_order_id', 'customer_id')

@admin.register(LossRecord)
class LossRecordAdmin(admin.ModelAdmin):
    list_display = ('material_id', 'quantity_lost', 'reason', 'loss_date')
    search_fields = ('material_id', 'reason')

@admin.register(FinanceEntry)
class FinanceEntryAdmin(admin.ModelAdmin):
    list_display = ('entry_type', 'amount', 'entry_date', 'category')
    search_fields = ('entry_type', 'category')

@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ('employee_name', 'role', 'phone_number', 'email')
    search_fields = ('employee_name', 'role')    

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('customer_name', 'contact_info')
    search_fields = ('customer_name', 'contact_info')

@admin.register(RawMaterial)
class RawMaterialAdmin(admin.ModelAdmin):
    list_display = ('name', 'supplier_id', 'cost_per_unit')
    search_fields = ('name', 'supplier_id')

@admin.register(ProcurementOrder)
class ProcurementOrderAdmin(admin.ModelAdmin):
    list_display = ('material_id', 'supplier_id', 'quantity_ordered', 'price_per_unit', 'total_cost', 'order_date', 'status')
    search_fields = ('material_id', 'supplier_id')

@admin.register(DispatchRecord)
class DispatchRecordAdmin(admin.ModelAdmin):
    list_display = ('production_order_id', 'customer_id', 'quantity_dispatched', 'dispatch_date', 'delivery_date')
    search_fields = ('production_order_id, work_order_id', 'customer_id')

@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ('work_order_id', 'material_id', 'employee_id', 'quantity_consumed', 'quantity_produced', 'production_start_date', 'production_end_date')
    search_fields = ('work_order_id', 'material_id', 'employee_id')

@admin.register(WorkOrderInstruction)
class WorkOrderInstructionAdmin(admin.ModelAdmin):
    list_display = ('work_order_id', 'step_number', 'machine', 'material_id', 'instruction_text', 'estimated_time_minutes')
    search_fields = ('work_order_id','material_id', 'instruction_text')
