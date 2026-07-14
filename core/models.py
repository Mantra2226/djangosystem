from datetime import datetime
import secrets
from sys import prefix
from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from decimal import Decimal, ROUND_HALF_UP
from django.utils.text import slugify
from django.core.validators import MinValueValidator
from django.db.models import Sum
from django.utils import timezone
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
        ('INTERMEDIATE', 'component / sub-assembly'),
    ]
    product_id = models.AutoField(primary_key=True)
    product_type = models.CharField(max_length=20, choices=PRODUCT_TYPE_CHOICES, default='FINISHED')
    sku = models.CharField(max_length=100, unique=True, blank=True, help_text="Stock Keeping Unit, auto-generated if left blank.")
    supplier = models.ForeignKey('Supplier', on_delete=models.PROTECT, blank=True, null=True, related_name='products')
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=255)
    unit_of_measurement = models.CharField(max_length=255)
    cost_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Cost per unit must be a positive amount greater than zero.")
    ordered_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Ordered quantity cannot be negative.")

    # Added field-level validation rules
    def clean(self):       
        if self.product_type == 'RAW' and not self.supplier:
            raise ValidationError({'supplier': 'Raw materials must have an associated supplier.'})
        
        if self.product_type in ['FINISHED', 'INTERMEDIATE'] and self.supplier:
            raise ValidationError({'supplier': 'Finished and intermediate goods cannot have an externally associated supplier.'})
        
    def save(self, *args, **kwargs):
        previously_completed = False
        if self.pk:
            previously_completed = Product.objects.filter(pk=self.pk, product_type='FINISHED').exists()

            super().save(*args, **kwargs)

           # If the product is a finished good and it was not previously completed, create an inventory record for it 
            if self.product_type == 'FINISHED' and not previously_completed:
                inventory_item, created = Inventory.objects.get_or_create(
                    product=self,
                    location='Main Warehouse',
                    defaults={'quantity_available': Decimal('0.00')}
                )
                inventory_item.quantity_available = self.ordered_quantity
                inventory_item.save()
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
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Quantity ordered must be a positive amount greater than zero.")
    price_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Price per unit must be a positive amount greater than zero.")
    total_cost = models.DecimalField(max_digits=10, decimal_places=2, editable=False, blank=True, default=Decimal('0.00'), help_text="Total cost is automatically calculated based on quantity ordered and price per unit.")
    order_date = models.DateField()
    status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='PENDING')   
    delivery_location = models.CharField(max_length=255, default='Main Warehouse')
    
    # when status is updated to 'Delivered', the quantity should be added to inventory and total cost should be calculated based on quantity ordered and price per unit
    def clean(self):
        if self.product and self.product.product_type == 'FINISHED':
            raise ValidationError({'product': 'finished products are manufactured internally. Use a production order instead of a procurement order for finished products.'})

    def save(self, *args, **kwargs):
        if self.product and self.quantity:
            raw_cost = self.quantity * self.price_per_unit
            self.total_cost = raw_cost.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            if not self.product or not self.quantity:
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

                inventory_item.quantity_available += self.quantity
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
    bill_of_material = models.ForeignKey('BillOfMaterial', on_delete=models.PROTECT, blank=True, null=True, help_text="The snapshot version of the recipe locked in for this specific operational run.")
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='work_order')
    employee = models.ManyToManyField('Employee', related_name='assigned_work_order', help_text="Employees assigned to this work order.")
    quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    production_start_date = models.DateField()
    production_end_date = models.DateField(null=True, blank=True)  # Can be null until production is completed

# validation to ensure that production end date is not before production start date and quantity produced does not exceed quantity consumed
    def clean(self):
        if self.production_end_date and self.production_start_date:
            if self.production_end_date < self.production_start_date:
             raise ValidationError('Production end date cannot be before production start date.')

        if self.product and self.product.product_type not in ['FINISHED', 'INTERMEDIATE']:
            raise ValidationError({'product': 'Work orders can only be created for finished or intermediate products.'})

    def save(self, *args, **kwargs):
        # AUTOMATION: Default to the active recipe for this product if left blank
        if not self.bill_of_material and self.product:
            active_bom = self.product.boms.filter(is_active=True).first()
            if active_bom:
                self.bill_of_material = active_bom
        self.full_clean()  # Ensure validation is performed before saving
        super().save(*args, **kwargs)        
        
    def __str__(self):
        return f"Work Order {self.work_order_id} — {self.product.name}"

class WorkOrderInstruction(models.Model):
    STATUS_CHOICES = [
        ('CANCELLED', 'Cancelled'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETED', 'Completed'),
    ]
    instruction_id = models.AutoField(primary_key=True)
    work_order = models.ForeignKey('WorkOrder', on_delete=models.CASCADE, related_name='instructions')
    product = models.ForeignKey('Product', on_delete=models.SET_NULL, null=True, blank=True)
    step_number = models.PositiveIntegerField(null=True, blank=True)
    step_name = models.CharField(max_length=255, null=True, blank=True)
    machine=models.CharField(max_length=255, blank=True, null=True, default='No machine assigned')
    instruction_text = models.TextField()    
    estimated_time_minutes = models.PositiveIntegerField(blank=True, null=True, default=0, validators=[MinValueValidator(0)])
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='IN_PROGRESS')

    class Meta:
        unique_together = ('work_order', 'step_number')
        ordering = ['step_number']

    def save(self, *args, **kwargs):
        if not self.step_number:
            highest_step = WorkOrderInstruction.objects.filter(work_order=self.work_order).aggregate(models.Max('step_number'))['step_number__max']
            self.step_number = (highest_step or 0) + 1

        self.full_clean()  # Ensure validation is performed before saving
        super().save(*args, **kwargs)    

# string representation of the instruction showing work order id, step number and first 50 characters of instruction text
    def __str__(self):
        text_preview = self.instruction_text[:50]
        snippet = f"{text_preview}..." if len(self.instruction_text) > 50 else text_preview
        return f"step {self.step_number}: {self.step_name} ({self.status})"    

class BillOfMaterial(models.Model):
    bom_id = models.AutoField(primary_key=True)
    product = models.ForeignKey('Product', on_delete=models.CASCADE, related_name='boms', limit_choices_to={'product_type__in': ['FINISHED', 'INTERMEDIATE']}, help_text="The finished or intermediate good this recipe creates.")
    name = models.CharField(max_length=255, blank=True, help_text="name is auto-generated after the selected product if left blank.")
    is_active = models.BooleanField(default=True, help_text="Designates whether this is the active recipe used for live manufacturing runs.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Bill of Material"
        verbose_name_plural = "Bills of Materials"

    def clean(self):
        super().clean()
        # auto-generates a descriptive name if left blank!
        if not self.name and self.product_id:
            self.name = f"BOM - {self.product.name}"
        # Enforce that raw materials cannot have a BOM blueprint
        if self.product and self.product.product_type == 'RAW':
            raise ValidationError({
                'product': 'You cannot create a Bill of Materials for a raw material.'
            })

        # Safeguard: Ensure only ONE active BOM exists per product at any given time
        if self.is_active:
            qs = BillOfMaterial.objects.filter(product=self.product, is_active=True)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError({
                    'is_active': f"An active BOM already exists for '{self.product.name}'. Please deactivate the old BOM first."
                })
            
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)        

    def __str__(self):
        return f"BOM: {self.product.name} ({self.name}) - Active: {self.is_active}"


class BOMItem(models.Model):
    bom_item_id = models.AutoField(primary_key=True)
    bom = models.ForeignKey('BillOfMaterial', on_delete=models.CASCADE, related_name='components')
    
    # The ingredient going into the recipe
    component = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='used_in_boms', limit_choices_to={'product_type__in': ['RAW', 'INTERMEDIATE']}, help_text="A raw material, retail item or intermediate sub-assembly required for production.")
    # Quantity required to manufacture exactly ONE unit of the parent product
    quantity_required = models.DecimalField(
        max_digits=10, 
        decimal_places=4,  # Expanded to 4 decimal places for precision blending/measurements
        validators=[MinValueValidator(Decimal('0.0001'))],
        help_text="The precise amount needed to create 1 unit of the parent product."
    )

    def clean(self):
        # Infinite Loop Prevention: A product cannot be an ingredient in its own recipe card!
        if self.bom and self.component == self.bom.product:
            raise ValidationError({
                'component': f"Circular Dependency Error: '{self.component.name}' cannot be an ingredient in its own build recipe."
            })
        # DEEP NESTED LOOP CHECK 
        # Checks if the component you are adding already relies on the parent product in its own BOM
        if self.component.boms.filter(components__component=self.bom.product).exists():
            raise ValidationError({
                'component': f"Circular Dependency Detected: You are trying to add '{self.component.name}' here, "
                             f"but '{self.component.name}' already requires '{self.bom.product.name}' in its own active BOM!"
            })

    def __str__(self):
        return f"{self.quantity_required}x {self.component.name} inside {self.bom}"

class ProductionOrder(models.Model):
    STATUS_CHOICES = [
        ('IN_PROGRESS', 'in_progress'),
        ('COMPLETED', 'completed'),
        ('CANCELLED', 'cancelled'),
    ]
    production_order_id = models.AutoField(primary_key=True)
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='production_runs', limit_choices_to={'product_type': 'FINISHED'})
    work_order = models.ForeignKey('WorkOrder', on_delete=models.PROTECT, related_name='production_runs')
    employee = models.ManyToManyField('Employee', blank=True, related_name='production_runs', help_text="Employees assigned to this production run.")
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.01'))], help_text="Quantity to be produced in this specific run.")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='IN_PROGRESS')
    notes = models.TextField(blank=True, null=True, help_text="Any issues or notes during this production run.")

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(auto_now=True)
 

    def clean(self):
        super().clean()   
        # Fallback quantity calculation
        if (not self.quantity or self.quantity == Decimal('0.00')) and self.work_order_id:
            self.quantity = self.work_order.quantity

        # Enforce finished goods only
        if self.product_id and not self.product.product_type != 'Finished Goods': # Or self.product.product_type != 'FINISHED_GOOD'
            raise ValidationError({
                'product': "Only products designated as 'Finished Goods' can be selected for a production run."
            })       
        if self.work_order_id:
            if not self.product_id:
                self.product = self.work_order.product

        if (not self.quantity or self.quantity == Decimal('0.00')) and self.work_order_id:
            self.quantity = self.work_order.quantity    
       # Checks if product and work_order match
        if self.product and self.work_order:
        # Assuming your WorkOrder model has a 'product' field (e.g. self.work_order.product)
            if self.work_order.product != self.product:
                raise ValidationError({
                 'work_order': (
                    f"Conflict: The selected Work Order ({self.work_order}) is for '{self.work_order.product}', "
                    f"but this Production Order is set to produce '{self.product}'."
                )
            })     

    def save(self, *args, **kwargs):
         # 1. If it transitioned to IN_PROGRESS and doesn't have a start date yet, stamp it
        if self.status == 'IN_PROGRESS' and not self.created_at:
            self.created_at = timezone.now()
        
        # 2. If it transitioned to COMPLETED and doesn't have a completion date yet, stamp it
        elif self.status == 'COMPLETED' and not self.completed_at:
             self.completed_at = timezone.now()
        
    # 3. If it gets cancelled or rolled back, clear the completion stamp
        elif self.status == 'CANCELLED':
            self.completed_at = None 
        is_new = self.pk is None
        self.full_clean()  # Ensure validation is performed before saving
        with transaction.atomic():
            super().save(*args, **kwargs)

        if is_new and self.work_order:
            if hasattr(self.work_order, 'employee'):
                self.employee.set(self.work_order.employee.all())
        
        is_transitioning_to_progress = False
        if self.pk:
            old_instance = ProductionOrder.objects.get(pk=self.pk)
            if old_instance.status != 'IN_PROGRESS' and self.status == 'IN_PROGRESS':
                is_transitioning_to_progress = True
        elif self.status == 'IN_PROGRESS':
            is_transitioning_to_progress = True   


        # 2. Execute within an atomic transaction block
        with transaction.atomic():
            super().save(*args, **kwargs)

            # AUTOMATED CONSUMPTION: Deduct ingredients if the build starts
            if is_transitioning_to_progress and self.work_order.bill_of_material:
                bom = self.work_order.bill_of_material
                target_quantity = self.work_order.quantity # Total amount being manufactured
                
                # Loop through every ingredient requirement row
                for item in bom.components.all():
                    # Total needed = multiplier quantity * batch run volume
                    total_needed = item.quantity_required * target_quantity
                    
                    # Target the physical ledger row for this component
                    inventory_item = Inventory.objects.filter(
                        product=item.component, 
                        location='Main Warehouse'
                    ).first()
                    
                    if not inventory_item or inventory_item.quantity_available < total_needed:
                        # Throw an error to halt the save if components are missing
                        raise ValidationError(
                            f"Insolvent Stock Error: Insufficient inventory for ingredient '{item.component.name}'. "
                            f"Required: {total_needed}, Available: {inventory_item.quantity_available if inventory_item else 0.00}"
                        )
                    
                    # Deduct stock and commit changes to the ledger
                    inventory_item.quantity_available -= total_needed
                    inventory_item.save()                                
            self.full_clean()
                
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
        if self.dispatch_date and self.delivery_date:
            if self.delivery_date < self.dispatch_date:
                raise ValidationError('Delivery date cannot be before dispatch date.')
        if self.quantity_dispatched and self.production_order:
           if self.quantity_dispatched > self.production_order.quantity: 
                raise ValidationError(f"Dispatched quantity ({self.quantity_dispatched}) cannot exceed "
                    f"produced quantity ({self.production_order.quantity})."
                    
                )

    def save(self, *args, **kwargs):
        self.full_clean()  # Ensure validation is performed before saving
        super().save(*args, **kwargs)   
  
        
    def __str__(self):
        return f"Dispatch {self.dispatch_id} - {self.quantity_dispatched} units to {self.customer.customer_name}"    

class Invoice(models.Model):
    ENTRY_TYPE_CHOICES = [
        ('Paid', 'Paid'),
        ('Partial', 'Partial Payment'),
        ('Unpaid', 'Unpaid'),
    ]
    invoice_id = models.AutoField(primary_key=True)
    invoice_number = models.CharField(max_length=255, unique=True, blank=True, help_text="Unique identifier for the invoice. Auto-generated if left blank.")
    customer = models.ForeignKey('Customer', on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    dispatch = models.ForeignKey('DispatchRecord', on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    invoice_date = models.DateField()
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False, default=Decimal('0.00'))
    status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='Unpaid')

    def clean_fields(self, exclude=None):
        # Round right as Django starts validating individual fields
        if self.total_amount is not None:
            self.total_amount = Decimal(str(self.total_amount)).quantize(
                Decimal('0.01'), 
                rounding=ROUND_HALF_UP
            )
        super().clean_fields(exclude=exclude)

    def clean(self):  
        super().clean()     
        if self.total_amount is not None:
                self.total_amount = Decimal(str(self.total_amount)).quantize(
                    Decimal('0.01'), 
                    rounding=ROUND_HALF_UP
                )
        if self.dispatch and self.invoice_date < self.dispatch.dispatch_date:
            raise ValidationError({'invoice_date': 'Invoice date cannot be earlier than the physical dispatch date.'}) 
                   
    # validation to ensure that invoice date is not before dispatch date and total amount is calculated based on quantity dispatched and cost per unit of the material in the production order
            
        
    def save(self, *args, **kwargs):
        if self.total_amount is not None:
            self.total_amount = Decimal(str(self.total_amount)).quantize(
                Decimal('0.01'), 
                rounding=ROUND_HALF_UP
            )
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
        self.full_clean()    
        super().save(*args, **kwargs)

    @property
    def remaining_balance(self):
        """Calculates the live remaining balance on the invoice."""
        total_paid = self.sales_payments.aggregate(total=models.Sum('amount'))['total'] or Decimal('0.01')
        return self.total_amount - total_paid

    def update_payment_status(self):
        """Auto-updates customer bill status based on incoming payments."""
        total_paid = self.sales_payments.aggregate(total=models.Sum('amount'))['total'] or Decimal('0.01')
        
        if total_paid >= self.total_amount:
            self.status = 'Paid'
        elif total_paid > 0:
            self.status = 'Partial'
        else:
            self.status = 'Unpaid'
        self.save(update_fields=['status']) 


    def __str__(self):
        return f"[Customer Invoice] #{self.invoice_number} — ${self.total_amount} ({self.customer.customer_name})"

class SalesInvoicePayments(models.Model):
    invoice = models.ForeignKey('Invoice', on_delete=models.CASCADE, related_name='sales_payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    payment_method = models.CharField(max_length=50, choices=[('CASH', 'Cash'), ('CARD', 'Card Transfer'), ('TRANSFER', 'Bank Transfer')])
    reference_number = models.CharField(max_length=100, blank=True)
    paid_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        
        # Normalizing the payment method to uppercase to handle any casing variations
        method = (self.payment_method or '').upper()
        
        # Clean up the reference number by stripping empty spaces
        ref_num = (self.reference_number or '').strip()
        method = (self.payment_method or '').upper()
        
        # Clean up the reference number by stripping empty spaces
        ref_num = (self.reference_number or '').strip()

        requires_reference = ['CARD', 'BANK']

        if method in requires_reference and not ref_num:
            raise ValidationError({
                'reference_number': "A reference number (transaction ID or deposit confirmation) is required for payments made by card or bank transfer."
            })
        # Skip validation if invoice or amount missing during form typing
        if not hasattr(self, 'invoice') or self.amount is None:
            return

        # Gather all OTHER historical payments for this specific invoice
        other_payments = self.invoice.sales_payments.all()
        
        # Edge Case Safeguard: If EDITING an existing payment row, 
        # exclude its own old value from the history so we don't double-count it.
        if self.pk:
            other_payments = other_payments.exclude(pk=self.pk)

        # Summing up what has already been collected
        total_already_paid = other_payments.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        
        # Calculating remaining threshold limit
        remaining_balance = self.invoice.total_amount - total_already_paid

    @property
    def remaining_balance(self):
        """
        Calculates live outstanding balance. 
        """
        total_paid = self.payments.aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')
        return self.total_amount - total_paid    
        
        

    def save(self, *args, **kwargs):
        # Force the full validation routine to run before saving
        self.full_clean()
        
        with transaction.atomic():
            super().save(*args, **kwargs)
            self.invoice.update_payment_status()
class PurchaseInvoice(models.Model):
    STATUS_CHOICES = [
        ('PAID', 'Paid'),
        ('UNPAID', 'Unpaid'),
        ('PARTIAL', 'Partially Paid'),
    ]   
    invoice_id = models.AutoField(primary_key=True)
    invoice_number = models.CharField(max_length=50, unique=True, help_text="Unique identifier for the purchase invoice from the supplier.")
    supplier = models.ForeignKey('Supplier', on_delete=models.PROTECT, related_name='purchase_invoices')
    procurement_order = models.ForeignKey('ProcurementOrder', on_delete=models.PROTECT, blank=True, null=True, related_name='purchase_invoices')
    invoice_date = models.DateField()
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='UNPAID')
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        # Ensure that the invoice date is not before the procurement order date
        if self.procurement_order and self.invoice_date < self.procurement_order.order_date:
            raise ValidationError({'invoice_date': 'Invoice date cannot be earlier than the procurement order date.'})
        # Ensure that the supplier on the invoice matches the supplier on the procurement order
        if self.procurement_order and self.procurement_order.supplier != self.supplier:
            raise ValidationError({'supplier': 'The supplier on the invoice must match the supplier on the procurement order.'})
        
    def save(self, *args, **kwargs):
        self.full_clean()  # Ensure validation is performed before saving
        # auto calculate total amount for the purchase invoice based on quantity ordered and price per unit
        if self.procurement_order:
            self.total_amount = self.procurement_order.quantity * self.procurement_order.price_per_unit
        if self.total_amount is not None:
            self.total_amount = Decimal(str(self.total_amount)).quantize(Decimal('0.01'))

        self.full_clean()    
        super().save(*args, **kwargs)
    
    @property
    def remaining_balance(self):
        """Calculates live outstanding balance owed to the supplier."""
        total_paid = self.purchase_payments.aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')
        return self.total_amount - total_paid

    def update_payment_status(self):
        """Auto-updates supplier bill status based on outgoing payments."""
        total_paid = self.purchase_payments.aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')
        
        if total_paid >= self.total_amount:
            self.status = 'PAID'
        elif total_paid > 0:
            self.status = 'PARTIAL'
        else:
            self.status = 'UNPAID'
        self.save(update_fields=['status'])  

    # Inside your PurchaseInvoice model in core/models.py

def update_payment_status(self):
    """Auto-updates supplier bill status and date stamps based on outgoing payments."""
    total_paid = self.purchase_payments.aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')
    
    if total_paid >= self.total_amount:
        self.status = 'PAID'
        # If it's newly paid and has no date, stamp it with the current time automatically
        if not self.paid_date:
            self.paid_date = timezone.now()
            
    elif total_paid > 0:
        self.status = 'PARTIAL'
        # Clear the date stamp if a payment is deleted and it drops back to partial
        self.paid_date = None
        
    else:
        self.status = 'UNPAID'
        # Clear the date stamp if all payments are deleted
        self.paid_date = None
        
    # Add 'paid_date' to the update_fields list so Django saves it
    self.save(update_fields=['status', 'paid_date'])      

    def __str__(self):
        return f"Purchase Invoice #{self.invoice_number} — ${self.total_amount or 0.00} ({self.supplier.name})"

class PurchasePayment(models.Model):
    purchase_invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name='purchase_payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=50, choices=[('CASH', 'Cash'), ('CARD', 'Card Transfer'), ('TRANSFER', 'Bank Transfer')])
    reference_number = models.CharField(max_length=100, blank=True, null=True, help_text="Transaction reference/receipt ID for Bank and Mobile money transfers")
    paid_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()        
        # Normalizing the payment method to uppercase to handle any casing variations
        method = (self.payment_method or '').upper()
        
        # Clean up the reference number by stripping empty spaces
        ref_num = (self.reference_number or '').strip()
        method = (self.payment_method or '').upper()
        
        # Clean up the reference number by stripping empty spaces
        ref_num = (self.reference_number or '').strip()

        requires_reference = ['CARD', 'BANK']

        if method in requires_reference and not ref_num:
            raise ValidationError({
                'reference_number': "A reference number (transaction ID or deposit confirmation) is required for payments made by card or bank transfer."
            })
    def save(self, *args, **kwargs):
        # Keeps native Django field validations active
        self.full_clean()
        with transaction.atomic():
            super().save(*args, **kwargs)
            self.purchase_invoice.update_payment_status()

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            invoice = self.purchase_invoice
            super().delete(*args, **kwargs)
            invoice.update_payment_status()    
     
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
    
