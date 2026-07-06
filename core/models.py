from datetime import datetime
import secrets
from sys import prefix
from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from decimal import ROUND_HALF_UP, Decimal
from django.utils.text import slugify
from django.core.validators import MinValueValidator
# models.
class Supplier(models.Model):
    supplier_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255)
    contact_info = models.TextField()
    payment_terms = models.CharField(max_length=255, null=True, blank=True, default='Net 30')  # e.g., Net 30, Net 60, etc.

    def __str__(self):
        return self.name

class Product(models.Model):
    PRODUCT_TYPE_CHOICES = [
        ('RAW', 'Raw Material'),
        ('FINISHED', 'Finished Good'),
        ('INTERMEDIATE', 'Intermediate Product'),
    ]
    product_id = models.AutoField(primary_key=True)
    product_type = models.CharField(max_length=255, choices=PRODUCT_TYPE_CHOICES, default='RAW')
    sku = models.CharField(max_length=100, unique=True, blank=True, help_text="Stock Keeping Unit, auto-generated if left blank.")
    supplier = models.ForeignKey('Supplier', on_delete=models.PROTECT, blank=True, null=True, related_name='products')
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=255)
    unit_of_measurement = models.CharField(max_length=255)
    cost_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Cost per unit must be a positive amount greater than zero.")
    stock_level = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Stock level cannot drop below zero.")

    # Added field-level validation rules
    def clean(self):       
        if self.product_type == 'RAW' and not self.supplier:
            raise ValidationError({'supplier': 'Raw materials must have an associated supplier.'})
        
        if self.product_type in ['FINISHED', 'INTERMEDIATE'] and self.supplier:
            raise ValidationError({'supplier': 'Finished and intermediate goods cannot have an externally associated supplier.'})
        
    def save(self, *args, **kwargs):
        # Auto-generate SKU if not provided
        if not self.sku:
            prefix_map = {
                'RAW': 'RM',
                'FINISHED': 'FG',
                'INTERMEDIATE': 'INT'
            }
            prefix = prefix_map.get(self.product_type, 'UNK')
            base_sku = slugify(self.name).replace('-', '').upper()[:10] 
            while True: # Limit base SKU to 10 characters for brevity
                unique_suffix = secrets.token_hex(3).upper()  # Generate a random 6-character hex string
                potential_sku = f"{prefix}-{base_sku}-{unique_suffix}"
                # only assign the SKU if it is unique in the database
                if not Product.objects.filter(sku=potential_sku).exists():
                    self.sku = potential_sku
                    break

        self.full_clean()  # Ensure validation is performed before saving
        super().save(*args, **kwargs)    

    # Essential for making dropdown menus and lists readable in the dashboard
    def __str__(self):
        return f"{self.name} ({self.get_product_type_display()}) - SKU: {self.sku}"    

class ProcurementOrder(models.Model):
    ENTRY_TYPE_CHOICES = [
        ('DELIVERED', 'Delivered'),
        ('PENDING', 'Pending'),
        ('CANCELLED', 'Cancelled'),
    ]
    procurement_order_id = models.AutoField(primary_key=True)
    supplier = models.ForeignKey('Supplier', on_delete=models.PROTECT, related_name='procurement_order')
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='procurement_order')
    quantity_ordered = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Quantity ordered must be a positive amount greater than zero.")
    price_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Price per unit must be a positive amount greater than zero.")
    total_cost = models.DecimalField(max_digits=10, decimal_places=2, editable=False, blank=True, default=Decimal('0.00'), help_text="Total cost is automatically calculated based on quantity ordered and price per unit.")
    order_date = models.DateField()
    status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='PENDING')   
    delivery_location = models.CharField(max_length=255, default='Main Warehouse')
    
    # when status is updated to 'Delivered', the quantity should be added to inventory and total cost should be calculated based on quantity ordered and price per unit
    def clean(self):
        if self.product and self.product.product_type == 'INTERMEDIATE':
            raise ValidationError({'product': 'procurement orders can only be created for finished products.'})

    def save(self, *args, **kwargs):
        if self.product and self.quantity_ordered:
            raw_cost = self.quantity_ordered * self.price_per_unit
            self.total_cost = raw_cost.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            if not self.product or not self.quantity_ordered:
                self.total_cost = Decimal('0.00')
        self.full_clean()  # Ensure validation is performed before saving

        previously_delivered = False
        if self.pk:
            previously_delivered = ProcurementOrder.objects.filter(pk=self.pk, status='DELIVERED').exists()
        # Wrapped inventory modifications in an atomic transaction to avoid data corruption if a crash happens mid-save
        with transaction.atomic():
            super().save(*args, **kwargs)

            if self.status == 'DELIVERED' and not previously_delivered:
                inventory_item, created = Inventory.objects.get_or_create(
                    product=self.product,
                    location=self.delivery_location,
                    defaults={'quantity_available': Decimal('0.00')}
                )

                inventory_item.quantity_available += self.quantity_ordered
                inventory_item.save()

    def __str__(self):
        return f"PO #{self.procurement_order_id} - {self.product.name} ({self.status})"           
               
class Inventory(models.Model):
    Inventory_id = models.AutoField(primary_key=True) 
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='stock')   
    quantity_available = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Quantity available cannot drop below zero.")
    location = models.CharField(max_length=255) 
    valuation = models.DecimalField(max_digits=10, decimal_places=2, editable=False, blank=True)

    class Meta:
        # Prevents duplicate tracking entries for the exact same material in the exact same warehouse room
        unique_together = ('product', 'location')
        verbose_name_plural = "Inventory"
    # valuation is calculated based on quantity available and cost per unit of the raw material
    def save(self, *args, **kwargs):
        self.valuation = self.quantity_available * self.product.cost_per_unit
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product.name}({self.location}) - {self.quantity_available} {self.product.unit_of_measurement}"    

class Employee(models.Model):
    employee_id = models.AutoField(primary_key=True)    
    employee_name = models.CharField(max_length=255)
    role = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=15, default='0000000000')
    email = models.EmailField(default='unknown@example.com', blank=True)   
    hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Hourly rate must be a positive amount greater than zero.")

    def __str__(self):
        return f"{self.employee_name} ({self.role})"
class WorkOrder(models.Model):
    work_order_id = models.AutoField(primary_key=True)
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='work_order')
    employee = models.ManyToManyField('Employee', related_name='assigned_work_order', help_text="Employees assigned to this work order.")
    quantity_produced = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.00'))], help_text="Quantity produced must be a positive amount greater than zero.")
    production_start_date = models.DateField()
    production_end_date = models.DateField() 

# validation to ensure that production end date is not before production start date and quantity produced does not exceed quantity consumed
    def clean(self):
        if self.production_end_date and self.production_start_date:
            if self.production_end_date < self.production_start_date:
             raise ValidationError('Production end date cannot be before production start date.')

        if self.product and self.product.product_type not in ['FINISHED', 'INTERMEDIATE']:
            raise ValidationError({'product': 'Work orders can only be created for finished or intermediate products.'})

    def save(self, *args, **kwargs):
        self.full_clean()  # Ensure validation is performed before saving
        super().save(*args, **kwargs)        
        
    def __str__(self):
        return f"Work Order {self.work_order_id} — {self.product.name}"

class WorkOrderInstruction(models.Model):
    instruction_id = models.AutoField(primary_key=True)
    work_order = models.ForeignKey('WorkOrder', on_delete=models.CASCADE, related_name='instructions')
    product = models.ForeignKey('Product', on_delete=models.SET_NULL, null=True, blank=True)
    step_number = models.IntegerField()
    machine=models.CharField(max_length=255, blank=True, null=True, default='No machine assigned')
    instruction_text = models.TextField()    
    estimated_time_minutes = models.PositiveIntegerField(blank=True, null=True, default=0, validators=[MinValueValidator(0)])

    class Meta:
        unique_together = ('work_order', 'step_number')
        ordering = ['step_number']

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
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='production_runs')
    employee = models.ManyToManyField('Employee', related_name='production_runs', help_text="Employees assigned to this production run.")
    work_order = models.ForeignKey('WorkOrder', on_delete=models.PROTECT, related_name='production_runs')
    quantity_produced = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.00'))], help_text="Quantity produced must be a positive amount greater than zero.")
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
    delivery_date = models.DateField(blank=True, null=True)

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
    ENTRY_TYPE_CHOICES = [
        ('Paid', 'Paid'),
        ('Unpaid', 'Unpaid'),
    ]
    invoice_id = models.AutoField(primary_key=True)
    invoice_number = models.CharField(max_length=255, unique=True, blank=True, help_text="Unique identifier for the invoice. Auto-generated if left blank.")
    customer = models.ForeignKey('Customer', on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    dispatch = models.ForeignKey('DispatchRecord', on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    invoice_date = models.DateField()
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False, default=Decimal('0.00'))
    status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='Unpaid')
    paid_date = models.DateField(null=True, blank=True)

    def clean(self):    
             # Implemented missing date validation 
        if self.dispatch and self.invoice_date < self.dispatch.dispatch_date:
                raise ValidationError({'invoice_date': 'Invoice date cannot be earlier than the physical dispatch date.'})
        
        if self.status == 'Paid' and not self.paid_date:
            raise ValidationError({'paid_date': 'A paid invoice must have an associated payment settlement date.'})
    # validation to ensure that invoice date is not before dispatch date and total amount is calculated based on quantity dispatched and cost per unit of the material in the production order
    def save(self, *args, **kwargs):
        self.full_clean()
        if self.dispatch:
            self.total_amount = self.dispatch.quantity_dispatched * self.dispatch.production_order.product.cost_per_unit
        else:
            if not self.total_amount:
                self.total_amount = Decimal('0.00')  # Default to zero if no dispatch is linked
        # auto calculate total amount for the invoice based on quantity dispatched and cost per unit
        if self.total_amount and self.dispatch:
            self.total_amount = self.dispatch.quantity_dispatched * self.dispatch.production_order.product.cost_per_unit

        if not self.invoice_number:
            # Generate a clean corporate format: INV-YEAR-MONTH-ID (e.g., INV-2026-07-0001)
            from datetime import datetime
            year_month = datetime.today().strftime("%Y-%m")
            
            # Find the last invoice ID to make it sequential
            last_invoice = Invoice.objects.all().order_by('invoice_id').last()
            next_id = (last_invoice.invoice_id + 1) if last_invoice else 1
            
            # Format with leading zeros so it stays neat (zfill padding)
            self.invoice_number = f"INV-{year_month}-{str(next_id).zfill(4)}"    
        super().save(*args, **kwargs)

    def __str__(self):
        return f"[Customer Invoice] #{self.invoice_number} — ${self.total_amount} ({self.customer.customer_name})"

class PurchaseInvoice(models.Model):
    STATUS_CHOICES = [
        ('PAID', 'Paid'),
        ('UNPAID', 'Unpaid'),
        ('PARTIALLY_PAID', 'Partially Paid'),
    ]   
    invoice_id = models.AutoField(primary_key=True)
    invoice_number = models.CharField(max_length=50, unique=True, help_text="Unique identifier for the purchase invoice from the supplier.")
    supplier = models.ForeignKey('Supplier', on_delete=models.PROTECT, related_name='purchase_invoices')
    procurement_order = models.ForeignKey('ProcurementOrder', on_delete=models.PROTECT, blank=True, null=True, related_name='purchase_invoices')
    invoice_date = models.DateField()
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='UNPAID')
    paid_date = models.DateField(blank=True, null=True, help_text="Date when the invoice was fully paid. Leave blank if unpaid or partially paid.")
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        # Ensure that the invoice date is not before the procurement order date
        if self.procurement_order and self.invoice_date < self.procurement_order.order_date:
            raise ValidationError({'invoice_date': 'Invoice date cannot be earlier than the procurement order date.'})
        # Ensure that the supplier on the invoice matches the supplier on the procurement order
        if self.procurement_order and self.procurement_order.supplier != self.supplier:
            raise ValidationError({'supplier': 'The supplier on the invoice must match the supplier on the procurement order.'})
    
        if self.status == 'PAID' and not self.paid_date:
            raise ValidationError({'paid_date': 'An invoice marked as PAID must have an associated payment settlement date.'})
        if self.status != 'PAID' and self.paid_date:
            raise ValidationError({'paid_date': 'Paid date should only be set when the invoice status is PAID.'})
        
    def save(self, *args, **kwargs):
        self.full_clean()  # Ensure validation is performed before saving
        # auto calculate total amount for the purchase invoice based on quantity ordered and price per unit
        if self.procurement_order:
            self.total_amount = self.procurement_order.quantity_ordered * self.procurement_order.price_per_unit
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Purchase Invoice #{self.invoice_number} — ${self.total_amount or 0.00} ({self.supplier.name})"        
class InvoiceLine(models.Model):
    invoice_line_id = models.AutoField(primary_key=True)
    invoice = models.ForeignKey('Invoice', on_delete=models.CASCADE)
    supplier = models.ForeignKey('Supplier', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoice_line')
    Employee = models.ForeignKey('Employee', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoice_line')
    Production_order = models.ForeignKey('ProductionOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoice_line')
    description = models.CharField(max_length=255, help_text="Description of the goods or manufacturing service rendered.")
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Unit price must be a positive amount greater than zero.")
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], editable=False, blank=True)
    tax_rate = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal('0.00'), help_text="Tax percentage applied to this line (e.g., 16.00 for 16%).", validators=[MinValueValidator(Decimal('0.00'))])   
    line_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], editable=False)

    class Meta:
        ordering = ['invoice_line_id'] 

    def save(self, *args, **kwargs):
        # Calculate the base costs
        self.subtotal = self.quantity * self.unit_price
        self.line_total = self.subtotal + (self.subtotal * self.tax_rate / 100)
        # Force Django validation to run (checks for negative values, etc.)
        self.full_clean()
        
        super().save(*args, **kwargs)  
    
    def __str__(self):
        return f"Invoice Line {self.invoice_line_id} - {self.description} ({self.quantity} @ {self.unit_price})"     

class Return(models.Model):
    STATUS_TYPE_CHOICES = [
         ('PENDING', 'Pending Inspection'),
         ('APPROVED', 'Approved'),
         ('REJECTED', 'Rejected'),
    ]
    return_id = models.AutoField(primary_key=True)
    dispatch = models.ForeignKey('DispatchRecord', on_delete=models.CASCADE)
    customer = models.ForeignKey('Customer', on_delete=models.CASCADE) 
    quantity_returned = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.00'))], help_text="Quantity returned must be a positive amount greater than zero.")
    reason_for_return = models.TextField(max_length=255, default='No reason provided')
    
    quality_control_status = models.CharField(max_length=255, choices=STATUS_TYPE_CHOICES, default='PENDING')
    return_warehouse_location = models.CharField(max_length=255, default='Main Warehouse')
        # validation to ensure that quantity returned does not exceed quantity dispatched in the dispatch record
    def save(self, *args, **kwargs):
        # 1. Secure our references (handles whether production_order is a direct field or accessed via dispatch)
        prod_order = getattr(self, 'production_order', None) or (self.dispatch.production_order if self.dispatch else None)

        # 2. Quantize internal financial fields BEFORE running any validation checks
        if prod_order and self.quantity_returned:
            raw_amount = self.quantity_returned * prod_order.product.cost_per_unit
            
            if hasattr(self, 'total_amount'):
                self.total_amount = raw_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if hasattr(self, 'refund_amount'):
                self.refund_amount = raw_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            if hasattr(self, 'total_amount') and not self.total_amount:
                self.total_amount = Decimal('0.00')
            if hasattr(self, 'refund_amount') and not self.refund_amount:
                self.refund_amount = Decimal('0.00')

        # Run model clean data validations now that our numbers are perfectly formatted
        self.full_clean()

        # 3. Detect historical state to prevent duplicate stock updates on multiple edits
        previously_approved = False
        if self.pk:
            previously_approved = Return.objects.filter(pk=self.pk, quality_control_status='APPROVED').exists()

        # 4. Execute database operations in a single, safe atomic pass
        with transaction.atomic():
            # Run the primary database save exactly ONCE
            super().save(*args, **kwargs)

            # If return is approved, adjust inventory and credit the customer's invoice balance
            if self.quality_control_status == 'APPROVED' and not previously_approved:
                
                # Step A: Return the physical items back into warehouse inventory logs
                if prod_order:
                    inventory_item, created = Inventory.objects.get_or_create(
                        product=prod_order.product,
                        location=self.return_warehouse_location,
                        defaults={'quantity_available': Decimal('0.00')}
                    )
                    inventory_item.quantity_available += self.quantity_returned
                    inventory_item.save()

                # Step B: Safely deduct returned items cost from the associated Invoice row
                invoice_record = Invoice.objects.filter(dispatch=self.dispatch).first()
                if invoice_record and invoice_record.total_amount and prod_order:
                    raw_return_value = self.quantity_returned * prod_order.product.cost_per_unit
                    
                    # FIXED: We clip the return value to exactly 2 decimal places BEFORE modifying the invoice!
                    return_value = raw_return_value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    
                    invoice_record.total_amount -= return_value
                    # Extra safety shield: ensure the final invoice total is cleanly quantized too
                    invoice_record.total_amount = invoice_record.total_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    
                    invoice_record.save()

    def __str__(self):
        return f"Return #{self.return_id} — {self.quantity_returned} units from Dispatch #{self.dispatch.dispatch_id} ({self.quality_control_status})"        
            
class LossRecord(models.Model):
    loss_id = models.AutoField(primary_key=True)
    product = models.ForeignKey('Product', on_delete=models.CASCADE)
    quantity_lost = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.00'))], help_text="Quantity lost must be a positive amount greater than zero.")
    loss_date = models.DateField()
    reason = models.TextField()
    loss_location = models.CharField(max_length=255, default='Main Warehouse')

    def save(self, *args, **kwargs):
        self.full_clean()
        with transaction.atomic():
            super().save(*args, **kwargs)
            
            # CRITICAL ADDITION: Automatically deduct lost material amounts from physical inventory stock lines
            inventory_item = Inventory.objects.filter(product=self.product, location=self.loss_location).first()
            if inventory_item:
                inventory_item.quantity_available -= self.quantity_lost
                # Triggers clean validation checks if loss plunges stock into negative boundaries
                inventory_item.save()

    def __str__(self):
        return f"Loss #{self.loss_id} — {self.quantity_lost} of {self.product.name} written off"
class FinanceEntry(models.Model):
    finance_entry_id = models.AutoField(primary_key=True)
    ENTRY_TYPE_CHOICES = [
        ('REVENUE', 'Revenue'),
        ('EXPENSE', 'Expense'),
    ]
    ENTRY_CATEGORY_CHOICES = [
        ('SALES', 'Sales'),
        ('LABOR', 'Labor cost'),
        ('OVERHEAD', 'Overhead'),
        ('PROCUREMENT', 'material Procurement'),
        ('CUSTOMER_REFUND', 'Customer refund'),
        ('LOSS', 'Inventory Loss'),
    ]
    entry_type = models.CharField(max_length=10, choices=ENTRY_TYPE_CHOICES, default='EXPENSE')  # e.g., 'Revenue', 'Expense'
    category = models.CharField(max_length=20, choices=ENTRY_CATEGORY_CHOICES, default='SALES')  # e.g., 'Raw Material', 'Labor', 'Overhead'        
    procurement_order = models.ForeignKey('ProcurementOrder', on_delete=models.PROTECT, null=True, blank=True, related_name='financial_entries')
    Invoice = models.ForeignKey('Invoice', on_delete=models.PROTECT, null=True, blank=True, related_name='financial_entries')
    loss = models.ForeignKey('LossRecord', on_delete=models.PROTECT, null=True, blank=True, related_name='financial_entries')
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.00'))], help_text="Amount must be a positive amount greater than zero.")
    entry_date = models.DateField()

    # validation to ensure that if entry type is 'Revenue', category cannot be 'Procurement' or 'Loss', and if entry type is 'Expense', category cannot be 'Sales'
    def clean(self):
        # 1. Prevent negative entries from skewing totals
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
    
