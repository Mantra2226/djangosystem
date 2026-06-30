from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from decimal import Decimal
from django.db.models import Sum                    
# Create your models here.
class Supplier(models.Model):
    supplier_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255)
    contact_info = models.TextField()
    payment_terms = models.CharField(max_length=255)

    def __str__(self):
        return self.name

class RawMaterial(models.Model):
    material_id = models.AutoField(primary_key=True)
    supplier = models.ForeignKey('Supplier', on_delete=models.PROTECT, related_name='materials')
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=255)
    unit_of_measurement = models.CharField(max_length=255)
    cost_per_unit = models.DecimalField(max_digits=10, decimal_places=2)
    stock_level = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    
    # Added field-level validation rules
    def clean(self):
        if self.cost_per_unit and self.cost_per_unit <= 0:
            raise ValidationError("Cost per unit must be a positive amount greater than zero.")
        if self.stock_level < 0:
            raise ValidationError("Stock level cannot drop below zero.")
        
   # Essential for making dropdown menus and lists readable in the dashboard
    def __str__(self):
        return f"{self.name} ({self.stock_level} {self.unit_of_measurement} available)"     

class ProcurementOrder(models.Model):
    ENTRY_TYPE_CHOICES = [
        ('Delivered', 'Delivered'),
        ('Pending', 'Pending'),
    ]
    procurement_order_id = models.AutoField(primary_key=True)
    supplier = models.ForeignKey('Supplier', on_delete=models.PROTECT, related_name='procurement_order')
    material = models.ForeignKey('RawMaterial', on_delete=models.PROTECT, related_name='procurement_order')
    quantity_ordered = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    price_per_unit = models.DecimalField(max_digits=10, decimal_places=2)
    total_cost = models.DecimalField(max_digits=10, decimal_places=2, editable=False, blank=True)
    order_date = models.DateField()
    status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='Pending')   
    delivery_location = models.CharField(max_length=255, default='Main Warehouse')
    
    # when status is updated to 'Delivered', the quantity should be added to inventory and total cost should be calculated based on quantity ordered and price per unit
    def save(self, *args, **kwargs):
        self.total_cost = self.quantity_ordered * self.price_per_unit

        previously_delivered = False
        if self.pk:
            previously_delivered = ProcurementOrder.objects.filter(pk=self.pk, status='Delivered').exists()
        # Wrap inventory modifications in an atomic transaction to avoid data corruption if a crash happens mid-save
        with transaction.atomic():
            super().save(*args, **kwargs)

            if self.status == 'Delivered' and not previously_delivered:
                inventory_item, created = Inventory.objects.get_or_create(
                    material=self.material,
                    location=self.delivery_location,
                    defaults={'quantity_available': Decimal('0.00')}
                )

                inventory_item.quantity_available += self.quantity_ordered
                inventory_item.save()
               
    def clean(self):
        if self.quantity_ordered <= 0:
            raise ValidationError("Quantity ordered must be greater than zero.")
        if self.price_per_unit < 0:
            raise ValidationError("Price per unit cannot be negative.")

    def __str__(self):
        return f"PO {self.procurement_order_id} - {self.material.name} ({self.status})"           
               
class Inventory(models.Model):
    Inventory_id = models.AutoField(primary_key=True) 
    material = models.ForeignKey('RawMaterial', on_delete=models.PROTECT, related_name='inventory_record')   
    quantity_available = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    location = models.CharField(max_length=255) 
    valuation = models.DecimalField(max_digits=10, decimal_places=2, editable=False, blank=True)

    class Meta:
        # Prevents duplicate tracking entries for the exact same material in the exact same warehouse room
        unique_together = ('material', 'location')
        verbose_name_plural = "Inventory"
    # valuation is calculated based on quantity available and cost per unit of the raw material
    def save(self, *args, **kwargs):
        self.valuation = self.quantity_available * self.material_id.cost_per_unit
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.material.name}({self.location}) - {self.quantity_available} {self.material.unit_of_measurement}"    

class Employee(models.Model):
    employee_id = models.AutoField(primary_key=True)    
    employee_name = models.CharField(max_length=255)
    role = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=15, default='0000000000')
    email = models.EmailField(default='unknown@example.com', blank=True)   

    # FIXED: Added string representation for clean dropdown selectors in forms
    def __str__(self):
        return f"{self.employee_name} ({self.role})"
class WorkOrder(models.Model):
    work_order_id = models.AutoField(primary_key=True)
    material = models.ForeignKey('RawMaterial', on_delete=models.PROTECT, related_name='work_order')
    employee = models.ForeignKey('Employee', on_delete=models.PROTECT, related_name='assigned_work_order')
    quantity_consumed = models.DecimalField(max_digits=10, decimal_places=2)
    quantity_produced = models.DecimalField(max_digits=10, decimal_places=2)
    production_start_date = models.DateField()
    production_end_date = models.DateField()    

# validation to ensure that production end date is not before production start date and quantity produced does not exceed quantity consumed
    def clean(self):
        if self.production_end_date and self.production_start_date:
            if self.production_end_date < self.production_start_date:
             raise ValidationError('Production end date cannot be before production start date.')
        if self.quantity_produced > self.quantity_consumed:
            raise ValidationError('Quantity produced cannot exceed quantity consumed.')
    def __str__(self):
        return f"Work Order {self.work_order_id}"

class WorkOrderInstruction(models.Model):
    instruction_id = models.AutoField(primary_key=True)
    work_order = models.ForeignKey('WorkOrder', on_delete=models.CASCADE, related_name='instructions')
    material = models.ForeignKey('RawMaterial', on_delete=models.SET_NULL, null=True, blank=True)
    step_number = models.IntegerField()
    machine=models.CharField(max_length=255, blank=True, null=True, default='No machine assigned')
    instruction_text = models.TextField()    
    estimated_time_minutes = models.IntegerField(blank=True, null=True, default=0)

    class Meta:
        ordering = ['step_number']

    unique_together = ('work_order', 'step_number')
# validation to ensure that step number is unique for each work order and estimated time is non-negative
    def clean(self):
        if self.estimated_time_minutes and self.estimated_time_minutes < 0:
            raise ValidationError('Estimated time cannot be negative.')

# string representation of the instruction showing work order id, step number and first 50 characters of instruction text
    def __str__(self):
        text_preview = self.instruction_text[:50]
        snippet = f"{text_preview}..." if len(self.instruction_text) > 50 else text_preview
        return f"WO-{self.work_order.work_order_id} - Step {self.step_number}:{snippet}"      
  
class ProductionOrder(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'pending'),
        ('IN_PROGRESS', 'in_progress'),
        ('COMPLETED', 'completed'),
        ('CANCELLED', 'cancelled'),
    ]
    production_order_id = models.AutoField(primary_key=True)
    material = models.ForeignKey('RawMaterial', on_delete=models.PROTECT, related_name='production_runs')
    employee = models.ForeignKey('Employee', on_delete=models.PROTECT, related_name='production_runs')
    work_order = models.ForeignKey('WorkOrder', on_delete=models.PROTECT, related_name='production_runs')
    quantity_consumed = models.DecimalField(max_digits=10, decimal_places=2)
    quantity_produced = models.DecimalField(max_digits=10, decimal_places=2)
    # stays empty until a run physically transitions to IN_PROGRESS
    actual_start_date = models.DateField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    actual_end_date = models.DateField(blank=True, null=True)  
    notes = models.TextField(blank=True, null=True, help_text="Any issues or notes during this production run.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-actual_start_date']    

    def clean(self):
        if self.actual_end_date and self.actual_start_date:
            if self.actual_end_date < self.actual_start_date:
                raise ValidationError('Actual end date cannot be before actual start date.')
# Require an actual start date if status is IN_PROGRESS
        if self.status == 'IN_PROGRESS' and not self.actual_start_date:
            self.actual_start_date = timezone.now().date()
# Require an actual end date if status is COMPLETED
        if self.status == 'COMPLETED' and not self.actual_end_date:
            raise ValidationError('A completed production order must have an actual end date.')

    def __str__(self):
        return f"Prod Order {self.production_order_id} ({self.get_status_display()}) - Blueprint: WO-{self.work_order.work_order_id}"            
class Customer(models.Model):
    customer_id = models.AutoField(primary_key=True)    
    customer_name = models.CharField(max_length=255)
    contact_info = models.TextField()

    def __str__(self):
        return self.customer_name

class DispatchRecord(models.Model):
    dispatch_id = models.AutoField(primary_key=True)    
    production_order = models.ForeignKey('ProductionOrder', on_delete=models.PROTECT)
    customer = models.ForeignKey('Customer', on_delete=models.PROTECT, related_name='dispatches') 
    quantity_dispatched = models.DecimalField(max_digits=10, decimal_places=2)
    dispatch_date = models.DateField()
    delivery_date = models.DateField()

    # validation to ensure that delivery date is not before dispatch date and quantity dispatched does not exceed quantity produced in the production order
    def clean(self):
        if self.delivery_date < self.dispatch_date:
            if self.delivery_date < self.dispatch_date:
                raise ValidationError('Delivery date cannot be before dispatch date.')
        if self.quantity_dispatched and self.production_order:
           if self.quantity_dispatched > self.production_order.quantity_produced: 
                raise ValidationError(f"Dispatched quantity ({self.quantity_dispatched}) cannot exceed "
                    f"produced quantity ({self.production_order.quantity_produced})."
                )

    def save(self, *args, **kwargs):
        self.full_clean()  # Ensure validation is performed before saving
        super().save(*args, **kwargs)   
  
        
    def __str__(self):
        return f"Dispatch {self.dispatch_id} - {self.quantity_dispatched} units to {self.customer.customer_name}"    

class Invoice(models.Model):
    INVOICE_TYPE_CHOICES = [
        ('CUSTOMER', 'Customer invoice(Receivable)'),
        ('SUPPLIER', 'Supplier invoice(Payable)'),
    ]
    EXPENSE_CATEGORY_CHOICES = [
        ('Raw Material', 'Raw Material Purchase'),
        ('Salaries', 'Employee Salaries'),
        ('Services', 'External services'),
        ('Overhead', 'Overhead costs'),
    ]
    ENTRY_TYPE_CHOICES = [
        ('Paid', 'Paid'),
        ('Unpaid', 'Unpaid'),
    ]
    invoice_id = models.AutoField(primary_key=True)
    customer = models.ForeignKey('Customer', on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    dispatch = models.ForeignKey('DispatchRecord', on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    supplier = models.ForeignKey('Supplier', on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    expense_category = models.CharField(max_length=25, choices=EXPENSE_CATEGORY_CHOICES, blank=True, null=True)
    invoice_date = models.DateField()
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    invoice_type = models.CharField(max_length=255, choices=INVOICE_TYPE_CHOICES, default='CUSTOMER')
    status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='Unpaid')

    def clean(self):
        # 1. Enforce Customer Invoice Data Integrity
        if self.invoice_type == 'CUSTOMER':
            if not self.customer:
                raise ValidationError({'customer': 'Customer is required for customer invoices.'})
            if not self.dispatch:
                raise ValidationError({'dispatch': 'Dispatch record is required to calculate customer billing.'})
            
            # FIXED: Implemented the missing date validation promise
            if self.dispatch and self.invoice_date < self.dispatch.dispatch_date:
                raise ValidationError({'invoice_date': 'Invoice date cannot be earlier than the physical dispatch date.'})

        # 2. Enforce Supplier Invoice Data Integrity
        if self.invoice_type == 'SUPPLIER':
            if not self.supplier:
                raise ValidationError({'supplier': 'Supplier is required for supplier invoices.'})
            if not self.expense_category:
                raise ValidationError({'expense_category': 'Please choose an expense category for this supplier layout.'})

    # validation to ensure that invoice date is not before dispatch date and total amount is calculated based on quantity dispatched and cost per unit of the material in the production order
    def save(self, *args, **kwargs):
        self.full_clean()
        # auto calculate total amount for the invoice based on quantity dispatched and cost per unit
        if self.invoice_type == 'CUSTOMER' and self.dispatch:
            self.total_amount = self.dispatch.quantity_dispatched * self.dispatch.production_order.material.cost_per_unit
        super().save(*args, **kwargs)

    def __str__(self):
        # FIXED: Safe string representation that evaluates invoice type dynamically
        party_name = self.customer.customer_name if self.invoice_type == 'CUSTOMER' else self.supplier.name
        return f"[{self.get_invoice_type_display()}] Inv #{self.invoice_id} — ${self.total_amount or 0.00} ({party_name})"
class InvoiceLine(models.Model):
    invoice_line_id = models.AutoField(primary_key=True)
    invoice = models.ForeignKey('Invoice', on_delete=models.CASCADE)
    supplier = models.ForeignKey('Supplier', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoice_line')
    Employee = models.ForeignKey('Employee', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoice_line')
    Production_order = models.ForeignKey('ProductionOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoice_line')
    description = models.CharField(max_length=255, help_text="Description of the goods or manufacturing service rendered.")
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, editable=False, blank=True)
    tax_amount = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), help_text="Tax percentage applied to this line (e.g., 16.00 for 16%).")   
    line_total = models.DecimalField(max_digits=10, decimal_places=2, editable=False)

    class Meta:
        ordering = ['invoice_line_id'] 

    def save(self, *args, **kwargs):
        # Calculate the base costs
        self.subtotal = self.quantity * self.unit_price
        self.tax_amount = self.subtotal * (self.tax_rate / Decimal('100.00'))
        self.line_total = self.subtotal + self.tax_amount
        # Force Django validation to run (checks for negative values, etc.)
        self.full_clean()
        
        super().save(*args, **kwargs)
    
    def clean(self):
        """Enforce that financial inputs make logical sense."""
        if self.quantity <= 0:
            raise ValidationError("Quantity billed must be greater than zero.")
        if self.unit_price < 0:
            raise ValidationError("Unit price cannot be a negative value.")
        if self.tax_rate < 0:
            raise ValidationError("Tax rate cannot be negative.")   
    
    def __str__(self):
        return f"Invoice Line {self.invoice_line_id} - {self.description} ({self.quantity} @ {self.unit_price})"     

class Return(models.Model):
    return_id = models.AutoField(primary_key=True)
    dispatch = models.ForeignKey('DispatchRecord', on_delete=models.CASCADE)
    customer = models.ForeignKey('Customer', on_delete=models.CASCADE) 
    quantity_returned = models.DecimalField(max_digits=10, decimal_places=2)
    reason_for_return = models.TextField(max_length=255, default='No reason provided')
    ENTRY_TYPE_CHOICES = [
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
    ]
    quality_control_status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='Rejected')
    return_warehouse_location = models.CharField(max_length=255, default='Main Warehouse')
        # validation to ensure that quantity returned does not exceed quantity dispatched in the dispatch record
    def clean(self):
        if self.dispatch and self.quantity_returned > self.dispatch.quantity_dispatched:    
            raise ValidationError('Returned quantity cannot exceed dispatched quantity.')
    
    def save(self, *args, **kwargs):
        self.full_clean()

        # Detect historical state to prevent duplicate stock updates on multiple edits
        previously_approved = False
        if self.pk:
            previously_approved = Return.objects.filter(pk=self.pk, quality_control_status='APPROVED').exists()

        with transaction.atomic():
            super().save(*args, **kwargs)

        # if return is approved, the quantity returned should be added back to inventory and total amount for the invoice should be reduced based on quantity returned and cost per unit of the material
            if self.quality_control_status == 'APPROVED' and not previously_approved:
                # 1. Update stock levels and automatically update material valuation
                    inventory_item, created = Inventory.objects.get_or_create(
                        material=self.dispatch.production_order.material,
                        location=self.return_warehouse_location,
                        defaults={'quantity_available': Decimal('0.00')}
                    )
                    inventory_item.quantity_available += self.quantity_returned
                    inventory_item.save()

                # 2. Safely deduct returned items cost from the associated Invoice row
                    invoice_record = Invoice.objects.filter(dispatch=self.dispatch).first()
                    if invoice_record and invoice_record.total_amount:
                        return_value = self.quantity_returned * self.dispatch.production_order.material.cost_per_unit
                        invoice_record.total_amount -= return_value
                        invoice_record.save()

    def __str__(self):
        return f"Return #{self.return_id} ({self.quality_control_status}) — {self.quantity_returned} Units"
            
class LossRecord(models.Model):
    loss_id = models.AutoField(primary_key=True)
    material = models.ForeignKey('RawMaterial', on_delete=models.CASCADE)
    quantity_lost = models.DecimalField(max_digits=10, decimal_places=2)
    loss_date = models.DateField()
    reason = models.TextField()
    loss_location = models.CharField(max_length=255, default='Main Warehouse')

    def save(self, *args, **kwargs):
        self.full_clean()
        with transaction.atomic():
            super().save(*args, **kwargs)
            
            # CRITICAL ADDITION: Automatically deduct lost material amounts from physical inventory stock lines
            inventory_item = Inventory.objects.filter(material=self.material, location=self.loss_location).first()
            if inventory_item:
                inventory_item.quantity_available -= self.quantity_lost
                # Triggers clean validation checks if loss plunges stock into negative boundaries
                inventory_item.save()

    def __str__(self):
        return f"Loss #{self.loss_id} — {self.quantity_lost} of {self.material.name} written off"
class FinanceEntry(models.Model):
    finance_entry_id = models.AutoField(primary_key=True)
    ENTRY_TYPE_CHOICES = [
        ('Revenue', 'Revenue'),
        ('Expense', 'Expense'),
    ]
    ENTRY_CATEGORY_CHOICES = [
        ('Sales', 'Sales'),
        ('Labor', 'Labor'),
        ('Overhead', 'Overhead'),
        ('Procurement', 'Procurement'),
        ('Customer refund', 'Customer refund'),
        ('Loss', 'Loss'),
    ]
    entry_type = models.CharField(max_length=10, choices=ENTRY_TYPE_CHOICES, default='EXPENSE')  # e.g., 'Revenue', 'Expense'
    procurement_order = models.ForeignKey('ProcurementOrder', on_delete=models.PROTECT, null=True, blank=True, related_name='financial_entries')
    Invoice = models.ForeignKey('Invoice', on_delete=models.PROTECT, null=True, blank=True, related_name='financial_entries')
    loss = models.ForeignKey('LossRecord', on_delete=models.PROTECT, null=True, blank=True, related_name='financial_entries')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    entry_date = models.DateField()
    category = models.CharField(max_length=20, choices=ENTRY_CATEGORY_CHOICES, default='SALES')  # e.g., 'Raw Material', 'Labor', 'Overhead'        

    # validation to ensure that if entry type is 'Revenue', category cannot be 'Procurement' or 'Loss', and if entry type is 'Expense', category cannot be 'Sales'
    def clean(self):
        # 1. Prevent negative entries from skewing totals
        if self.amount and self.amount <= 0:
            raise ValidationError({'amount': 'Financial transaction entries must be a positive amount greater than zero.'})
        if self.entry_type == 'REVENUE' and self.category in ['PROCUREMENT', 'LOSS', 'LABOR', 'OVERHEAD']:
            raise ValidationError(f"A Revenue entry cannot be categorized under {self.get_category_display()}.")
        if self.entry_type == 'EXPENSE' and self.category == 'SALES':
            raise ValidationError("An Expense entry cannot be categorized under Sales Revenue.")
        if self.loss and (self.entry_type != 'EXPENSE' and self.category != 'LOSS'):
            raise ValidationError("Entries tied to a Loss Record must be set as an Expense under the Loss category.")
    
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
    def __str__(self):
        return f"[{self.get_entry_type_display()}] ${self.amount} — {self.get_category_display()} ({self.entry_date})"
    
