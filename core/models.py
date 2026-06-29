from django.db import models
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

class RawMaterial(models.Model):
    material_id = models.AutoField(primary_key=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=255)
    unit_of_measurement = models.CharField(max_length=255)
    cost_per_unit = models.DecimalField(max_digits=10, decimal_places=2)

class ProcurementOrder(models.Model):
    ENTRY_TYPE_CHOICES = [
        ('Delivered', 'Delivered'),
        ('Pending', 'Pending'),
    ]
    procurement_order_id = models.AutoField(primary_key=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE)
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)
    quantity_ordered = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price_per_unit = models.DecimalField(max_digits=10, decimal_places=2)
    total_cost = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    order_date = models.DateField()
    status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='Pending')   
    
    # when status is updated to 'Delivered', the quantity should be added to inventory and total cost should be calculated based on quantity ordered and price per unit
    def save(self, *args, **kwargs):
        self.total_cost = self.quantity_ordered * self.price_per_unit

        previously_delivered = False
        if self.pk:
            previously_delivered = ProcurementOrder.objects.filter(pk=self.pk, status='Delivered').exists()

        super().save(*args, **kwargs)

        if self.status == 'Delivered' and not previously_delivered:
            Inventory.objects.filter(material_id=self.material_id).update(
                quantity_available=models.F('quantity_available') + self.quantity_ordered
            )

class Inventory(models.Model):
    Inventory_id = models.AutoField(primary_key=True) 
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)   
    quantity_available = models.DecimalField(max_digits=10, decimal_places=2)
    location = models.CharField(max_length=255) 
    valuation = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    
    # valuation is calculated based on quantity available and cost per unit of the raw material
    def save(self, *args, **kwargs):
        self.valuation = self.quantity_available * self.material_id.cost_per_unit
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.material_id.name} - {self.quantity_available} {self.material_id.unit_of_measurement}"    

class Employee(models.Model):
    employee_id = models.AutoField(primary_key=True)    
    employee_name = models.CharField(max_length=255)
    role = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=10, default='0000000000')
    email = models.EmailField(default='unknown@example.com', blank=True)   

class WorkOrder(models.Model):
    work_order_id = models.AutoField(primary_key=True, unique=True)
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE)
    quantity_consumed = models.DecimalField(max_digits=10, decimal_places=2)
    quantity_produced = models.DecimalField(max_digits=10, decimal_places=2)
    production_start_date = models.DateField()
    production_end_date = models.DateField()    

# validation to ensure that production end date is not before production start date and quantity produced does not exceed quantity consumed
    def clean(self):
        if self.production_end_date < self.production_start_date:
            raise ValidationError('Production end date cannot be before production start date.')
        if self.quantity_produced > self.quantity_consumed:
            raise ValidationError('Quantity produced cannot exceed quantity consumed.')
def __str__(self):
    return f"Work Order {self.work_order_id}"

class WorkOrderInstruction(models.Model):
    instruction_id = models.AutoField(primary_key=True)
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, null=True, blank=True)
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE, null=True, blank=True)
    step_number = models.IntegerField()
    machine=models.CharField(max_length=255, blank=True, null=True, default='No machine specified')
    instruction_text = models.TextField()    
    estimated_time_minutes = models.IntegerField(blank=True, null=True, default=0)

# validation to ensure that step number is unique for each work order and estimated time is non-negative
    def clean(self):
        if self.estimated_time_minutes and self.estimated_time_minutes < 0:
            raise ValidationError('Estimated time cannot be negative.')
        if WorkOrderInstruction.objects.filter(work_order_id=self.work_order, step_number=self.step_number).exclude(pk=self.pk).exists():
            raise ValidationError('Step number must be unique for each work order.')

class Meta:
    ordering = ['step_number']

    unique_together = ('work_order', 'step_number')
# string representation of the instruction showing work order id, step number and first 50 characters of instruction text
def __str__(self):
    return f"{self.work_order.work_order_id} - Step {self.step_number}"      
  
class ProductionOrder(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'pending')
        ('IN_PROGRESS', 'in_progress')
        ('COMPLETED', 'completed')
        ('CANCELLED', 'cancelled')
    ]
    production_order_id = models.AutoField(primary_key=True, unique=True)
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE)
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, null=True, blank=True, db_column='work_order_id_id')
    quantity_consumed = models.DecimalField(max_digits=10, decimal_places=2)
    quantity_produced = models.DecimalField(max_digits=10, decimal_places=2)
    production_start_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    production_end_date = models.DateField()  
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

class DispatchRecord(models.Model):
    dispatch_id = models.AutoField(primary_key=True)    
    production_order = models.ForeignKey(ProductionOrder, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE) 
    quantity_dispatched = models.DecimalField(max_digits=10, decimal_places=2)
    dispatch_date = models.DateField()
    delivery_date = models.DateField()

    # validation to ensure that delivery date is not before dispatch date and quantity dispatched does not exceed quantity produced in the production order
    def clean(self):
        if self.delivery_date < self.dispatch_date:
            raise ValidationError('Delivery date cannot be before dispatch date.')
        if self.quantity_dispatched > self.production_order_id.quantity_produced:
            raise ValidationError('Dispatched quantity cannot exceed produced quantity.')

    def save(self, *args, **kwargs):
        self.clean()  # Ensure validation is performed before saving
        super().save(*args, **kwargs)   
  
        
    def __str__(self):
        return f"Dispatch {self.dispatch_id} - {self.quantity_dispatched} units to {self.customer.customer_name}"    

class Invoice(models.Model):
    INVOICE_TYPE_CHOICES = [
        ('CUSTOMER', 'Customer invoice'),
        ('SUPPLIER', 'Supplier invoice'),
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
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    dispatch = models.ForeignKey(DispatchRecord, on_delete=models.CASCADE)
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, null=True, blank=True)
    expense_category = models.CharField(max_length=25, choices=EXPENSE_CATEGORY_CHOICES, blank=True, null=True)
    invoice_date = models.DateField()
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    invoice_type = models.CharField(max_length=255, choices=INVOICE_TYPE_CHOICES, default='CUSTOMER')
    status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='Unpaid')

    # validation to ensure that invoice date is not before dispatch date and total amount is calculated based on quantity dispatched and cost per unit of the material in the production order
    def save(self, *args, **kwargs):
        # auto calculate total amount for the invoice based on quantity dispatched and cost per unit
        if self.dispatch:
            self.total_amount = self.dispatch.quantity_dispatched * self.dispatch.production_order.material.cost_per_unit
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Invoice {self.invoice_id} - {self.total_amount} for {self.customer.customer_name}"

class InvoiceLine(models.Model):
    invoice_line_id = models.AutoField(primary_key=True)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE)
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, null=True, blank=True)
    Employee = models.ForeignKey(Employee, on_delete=models.CASCADE, null=True, blank=True)
    Production_order = models.ForeignKey(ProductionOrder, on_delete=models.CASCADE, null=True, blank=True)
    description = models.CharField(max_length=255, help_text="Description of the goods or manufacturing service rendered.")
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, editable=False, blank=True)
    tax_amount = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, help_text="Tax percentage applied to this line (e.g., 16.00 for 16%).")   
    line_total = models.DecimalField(max_digits=10, decimal_places=2, editable=False)

class Meta:
    ordering = ['invoice_line_id']

### Dynamic Financial Properties
    @property
    def subtotal(self):
        return self.quantity * self.unit_price  

    @property
    def tax_amount(self):
        """Calculates the absolute tax amount for this line item."""
        # Dividing by 100 as an integer or float can cause precision issues with Decimal, 
        # so we cast the denominator to Decimal.
        return self.subtotal * (self.tax_rate / Decimal('100.00'))

    @property
    def grand_total(self):
        """The absolute final total for this line item (Subtotal + Tax)."""
        return self.subtotal + self.tax_amount  

    def save(self, *args, **kwargs):
        # Calculate the base costs
        self.subtotal = self.quantity * self.unit_price
        self.tax_amount = self.subtotal * (self.tax_rate / Decimal('100.00'))
        
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
    dispatch = models.ForeignKey(DispatchRecord, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE) 
    quantity_returned = models.DecimalField(max_digits=10, decimal_places=2)
    reason_for_return = models.TextField(max_length=255, default='No reason provided')
    ENTRY_TYPE_CHOICES = [
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
    ]
    quality_control_status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='Rejected')

        # validation to ensure that quantity returned does not exceed quantity dispatched in the dispatch record
    def clean(self):
        if self.quantity_returned > self.dispatch.quantity_dispatched:
            raise ValidationError('Returned quantity cannot exceed dispatched quantity.')
    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

        # if return is approved, the quantity returned should be added back to inventory and total amount for the invoice should be reduced based on quantity returned and cost per unit of the material
        if self.quality_control_status == 'Approved':
            Inventory.objects.filter(material=self.dispatch.production_order.material).update(
                quantity_available=models.F('quantity_available') + self.quantity_returned
            )
            Invoice.objects.filter(dispatch_id=self.dispatch).update(
                total_amount=models.F('total_amount') - (self.quantity_returned * self.dispatch.production_order.material.cost_per_unit)
            )
            
class LossRecord(models.Model):
    loss_id = models.AutoField(primary_key=True)
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)
    quantity_lost = models.DecimalField(max_digits=10, decimal_places=2)
    loss_date = models.DateField()
    reason = models.TextField()

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
    entry_type = models.CharField(max_length=10, choices=ENTRY_TYPE_CHOICES, default='Expense')  # e.g., 'Revenue', 'Expense'
    ProcurementOrder = models.ForeignKey(ProcurementOrder, on_delete=models.CASCADE, null=True, blank=True)
    Invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, null=True, blank=True)
    loss = models.ForeignKey(LossRecord, on_delete=models.CASCADE, null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    entry_date = models.DateField()
    category = models.CharField(max_length=20, choices=ENTRY_CATEGORY_CHOICES, default='Sales')  # e.g., 'Raw Material', 'Labor', 'Overhead'        

    # validation to ensure that if entry type is 'Revenue', category cannot be 'Procurement' or 'Loss', and if entry type is 'Expense', category cannot be 'Sales'
    def clean(self):
        if self.entry_type == 'Revenue' and self.category in ['Procurement', 'Loss']:
            raise ValidationError('Revenue entry cannot have category Procurement or Loss.')
        if self.entry_type == 'Expense' and self.category == 'Sales':
            raise ValidationError('Expense entry cannot have category Sales.')
    def __str__(self):
        return f"{self.entry_type} - {self.amount} on {self.entry_date} ({self.category})"
    
    
