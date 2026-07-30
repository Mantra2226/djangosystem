from datetime import datetime
from itertools import product
import secrets
from sys import prefix
from django.db import models, transaction
from django.core.exceptions import ValidationError
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
    selling_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Required for Finished Goods. Leave blank for raw materials and intermediates.")
    # Added field-level validation rules
    def clean(self):       
        super().clean()
        # 1. Ensure Finished Goods always have a selling price
        if self.product_type == 'FINISHED' and self.selling_price is None:
            raise ValidationError({
                'selling_price': "Finished Goods must have a valid selling price."
            })
        # 2. Automatically clear selling price if the item is Raw or Intermediate
        if self.product_type != 'FINISHED' and self.selling_price is not None:
            self.selling_price = None

        if self.product_type == 'RAW' and not self.supplier:
            raise ValidationError({'supplier': 'Raw materials must have an associated supplier.'})
        
        if self.product_type in ['FINISHED', 'INTERMEDIATE'] and self.supplier:
            raise ValidationError({'supplier': 'Finished and intermediate goods cannot have an externally associated supplier.'})
        
        
    def save(self, *args, **kwargs):
        is_new = self.pk is None
        previously_completed = False
        
        if not is_new:
            previously_completed = Product.objects.filter(pk=self.pk, product_type='FINISHED').exists()

            
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
           
        # If the product is a finished good and it was not previously completed, create an inventory record for it 
        if self.product_type == 'FINISHED' and not previously_completed:
                inventory_item, created = Inventory.objects.get_or_create(
                    product=self,
                    location='Main Warehouse',
                    defaults={'quantity_available': Decimal('0.00')}
                )
                if not created:
                    inventory_item.quantity_available = Decimal('0.00')
                    inventory_item.save()
    # Essential for making dropdown menus and lists readable in the dashboard
    def __str__(self):
        return f"{self.name} ({self.get_product_type_display()}) - SKU: {self.sku}" 

class PurchaseOrder(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('SENT', 'Sent to Supplier'),
        ('PARTIAL', 'Partially Received'),
        ('RECEIVED', 'Fully Received / Completed'),
        ('CANCELLED', 'Cancelled'),
    ]
    po_id = models.AutoField(primary_key=True)
    po_number = models.CharField(max_length=100, unique=True, editable=False, help_text="System generated unique purchase order number.")
    supplier = models.ForeignKey('Supplier', on_delete=models.PROTECT, related_name='purchase_orders')
    order_date = models.DateField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    notes = models.TextField(blank=True, help_text="Delivery instructions or terms")

    def clean(self):
        super().clean()
        
        # If an operator sets the status to a received state, check the line items
        if self.status in ['PARTIAL', 'RECEIVED']:
            if self.pk:
                # Sum up all the quantities received across this PO's line items
                total_received = self.items.aggregate(total=Sum('quantity_received'))['total'] or Decimal('0.00')
                
                if total_received <= Decimal('0.00'):
                    raise ValidationError({
                        'status': f"Cannot set status to '{self.get_status_display()}' because "
                                  f"no items have been marked as received yet. Please log and deliver "
                                  f"a Procurement Order for this PO first."
                    })
            else:
                # Prevent creating a brand new PO directly into a received status
                raise ValidationError({
                    'status': "A brand new Purchase Order cannot be created as Partially or Fully Received. "
                              "It must start as 'Draft' or 'Sent to Supplier'."
                })
    def save(self, *args, **kwargs):
        # Only generate a PO number if it doesn't have one yet
        if not self.po_number:
            current_year = timezone.now().year
            prefix = f"PO-{current_year}-"
            # Find the highest existing PO number for this year to determine the next sequence
            latest_po = PurchaseOrder.objects.filter(
                po_number__startswith=prefix
            ).order_by('-po_number').first()
            
            if latest_po:
                try:
                    # Extract the serial suffix (e.g. '00005' from 'PO-2026-00005') and increment it
                    last_sequence = int(latest_po.po_number.split('-')[-1])
                    next_sequence = last_sequence + 1
                except (ValueError, IndexError):
                    next_sequence = 1
            else:
                next_sequence = 1
            
            # Format to 5 padded digits (e.g., PO-2026-00001)
            self.po_number = f"{prefix}{next_sequence:05d}"
            
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.po_number} - {self.supplier.name} ({self.get_status_display()})"    

class PurchaseOrderItem(models.Model):
    item_id = models.AutoField(primary_key=True)
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='po_items', limit_choices_to={'product_type': 'RAW'}, help_text="Only raw materials can be ordered via purchase orders.") 
    quantity_ordered = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    # Track physical units received when delivery arrives
    quantity_received = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))])
    price_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Negotiated cost per unit for this order.")
    @property
    def total_price(self):
        return self.quantity_ordered * self.unit_price

    def clean(self):
        # Validation Rule: You cannot buy a product from Supplier A if it's assigned to Supplier B
        if self.product.supplier and self.product.supplier != self.purchase_order.supplier:
            raise ValidationError({
                'product': f"This product is strictly supplied by '{self.product.supplier.name}'. "
                           f"You cannot order it under a PO to '{self.purchase_order.supplier.name}'."
            })
            
        # Validation Rule: Ensure quantity_received does not exceed quantity_ordered
        if self.quantity_received > self.quantity_ordered:
            raise ValidationError({
                'quantity_received': "Quantity received cannot exceed quantity ordered."
            })

    def __str__(self):
        return f"{self.product.sku} ({self.quantity_ordered} {self.product.unit_of_measurement}) on {self.purchase_order.po_number}"       

class ProcurementOrder(models.Model):
    ENTRY_TYPE_CHOICES = [
        ('DELIVERED', 'Delivered'),
        ('PENDING', 'Pending'),
        ('CANCELLED', 'Cancelled'),
    ]
    procurement_order_id = models.AutoField(primary_key=True)
    purchase_order = models.ForeignKey('PurchaseOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='procurements', help_text="The source Purchase Order this delivery belongs to.")
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='procurement_order', limit_choices_to={'product_type': 'RAW'}, help_text="Only raw materials can be selected for procurement.")
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Quantity ordered must be a positive amount greater than zero.")
    price_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Price per unit must be a positive amount greater than zero.")
    total_cost = models.DecimalField(max_digits=10, decimal_places=2, editable=False, blank=True, default=Decimal('0.00'), help_text="Total cost is automatically calculated based on quantity ordered and price per unit.")
    order_date = models.DateField()
    delivery_date = models.DateTimeField(null=True, blank=True, editable=False, help_text="Automatically captures when status changes to Delivered.")
    status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='PENDING')   
    delivery_location = models.CharField(max_length=255, default='Main Warehouse')
    
    @property
    def supplier(self):
        return self.purchase_order.supplier if self.purchase_order else None
    # when status is updated to 'Delivered', the quantity should be added to inventory and total cost should be calculated based on quantity ordered and price per unit
    def clean(self):
        super().clean()
        if self.product and self.product.product_type == 'FINISHED':
            raise ValidationError({'product': 'finished products are manufactured internally. Use a production order instead of a procurement order for finished products.'})
        
        if self.purchase_order and self.product:
            # Gets the list of product IDs present on this Purchase Order's items
            valid_product_ids = self.purchase_order.items.values_list('product_id', flat=True)

            if self.product.pk not in valid_product_ids:
                raise ValidationError({
                    'product': f"'{self.product.name}' is not listed on Purchase Order #{self.purchase_order.pk}. "
                               f"Please select a product included in this purchase order."
                })

    def save(self, *args, **kwargs):
        # Calculate total cost of this delivery
        if self.product and self.quantity:
            raw_cost = self.quantity * self.price_per_unit
            self.total_cost = raw_cost.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            self.total_cost = Decimal('0.00')
            
        self.full_clean()

        previously_delivered = False
        if self.pk:
            previously_delivered = ProcurementOrder.objects.filter(pk=self.pk, status='DELIVERED').exists()
        # Captures timestamp when status changes to DELIVERED
        if self.status == 'DELIVERED' and not previously_delivered:
            self.delivery_date = timezone.now()
        # Wrapped inventory modifications in an atomic transaction to avoid data corruption if a crash happens mid-save
        with transaction.atomic():
            super().save(*args, **kwargs)

            if self.status == 'DELIVERED' and not previously_delivered:
                # Fetches or initializes the specific location inventory record bucket
                inventory_item, created = Inventory.objects.get_or_create(
                    product=self.product,
                    location=self.delivery_location,
                    defaults={
                        'quantity_available': Decimal('0.00'),
                        'unit_cost': self.price_per_unit  # Initial cost matches first delivery
                    }
                )
                # Pulls figures directly from the target inventory row for AVCO calculation
                current_total_qty = inventory_item.quantity_available
                current_cost = inventory_item.unit_cost
                incoming_qty = self.quantity
                incoming_price = self.price_per_unit
                
                total_qty_after = current_total_qty + incoming_qty
                
                if total_qty_after > 0:
                    # AVCO calculation
                    new_weighted_cost = (
                        (current_total_qty * current_cost) + (incoming_qty * incoming_price)
                    ) / total_qty_after
                    # Increment stock levels and trigger inventory save processing mechanics
                    inventory_item.unit_cost = new_weighted_cost.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    # Increment stock & save inventory once
                inventory_item.quantity_available += incoming_qty
                inventory_item.save()
                # Safe supplier reference for stock transaction
                supplier_name = self.supplier.name if self.supplier else "Unassigned Supplier"
                # Write to StockTransaction Ledger for audit trailing
                StockTransaction.objects.create(
                    product=self.product,
                    quantity=self.quantity,
                    transaction_type='RECEIPT',
                    notes=f"Receipt via Procurement Order #{self.procurement_order_id} from {supplier_name}."
                )

                # Update the matching PurchaseOrderItem if linked to a PO
                if self.purchase_order:
                    po_item = self.purchase_order.items.filter(product=self.product).first()
                    if po_item:
                        po_item.quantity_received += self.quantity
                        po_item.save()
                    
    def __str__(self):
        return f"PO #{self.procurement_order_id} - {self.product.name} ({self.status})"           
               
class Inventory(models.Model):
    Inventory_id = models.AutoField(primary_key=True) 
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='stock')   
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, validators=[MinValueValidator(Decimal('0.00'))], help_text="Moving weighted average cost calculated from delivered procurements.")
    quantity_available = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Quantity available cannot drop below zero.")
    location = models.CharField(max_length=255, default='Main Warehouse') 
    last_updated = models.DateTimeField(auto_now=True)
    valuation = models.DecimalField(max_digits=10, decimal_places=2, editable=False, blank=True)

    @property
    def total_valuation(self):
        """Calculates total inventory valuation based on current moving average cost."""
        return self.quantity_available * self.unit_cost

    class Meta:
        # Prevents duplicate tracking entries for the exact same material in the exact same warehouse room
        unique_together = ('product', 'location')
        verbose_name_plural = "Inventory"
    # valuation is calculated based on quantity available and cost per unit of the raw material
    def save(self, *args, **kwargs):
        if self.quantity_available and self.unit_cost:
            raw_valuation = self.quantity_available * self.unit_cost
            self.valuation = raw_valuation.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            self.valuation = Decimal('0.00')
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product.sku} - {self.location}: {self.quantity_available} (Valued at ${self.valuation})"
class StockTransaction(models.Model):
    TRANSACTION_TYPE_CHOICES = [
        ('RECEIPT', 'Goods Receipt (Supplier Purchase)'),
        ('PRODUCTION_OUTPUT', 'Production Output (Finished Goods)'),
        ('PRODUCTION_CONSUMPTION', 'Material Consumption (BOM Use)'),
        ('SHIPMENT', 'Customer Shipment (Sales Order)'),
        ('ADJUSTMENT', 'Manual Stock Adjustment / Correction'),
    ]
    transaction_id = models.AutoField(primary_key=True)
    product = models.ForeignKey('Product', on_delete=models.CASCADE, related_name='stock_transactions')
    work_order = models.ForeignKey('WorkOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='stock_transactions')
    dispatch_record = models.ForeignKey('DispatchRecord', on_delete=models.SET_NULL, null=True, blank=True, related_name='stock_transactions')
    quantity = models.DecimalField(max_digits=10, decimal_places=2, help_text="Use positive numbers for stock additions, negative numbers for deductions.")
    transaction_type = models.CharField(max_length=30, choices=TRANSACTION_TYPE_CHOICES)
    # Generic references to link this transaction back to the system document that caused it
    notes = models.TextField(blank=True, help_text="Reason for adjustment, operator name, etc.")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        sign = "+" if self.quantity > 0 else ""
        formatted_date = self.created_at.strftime('%Y-%m-%d') if self.created_at else "Draft"
        sku = self.product.sku if self.product else "UNKNOWN_SKU"
        
        return f"{sku} | {sign}{self.quantity} | {self.get_transaction_type_display()} | {formatted_date}"
class Employee(models.Model):
    employee_id = models.AutoField(primary_key=True) 
    employee_code = models.CharField(max_length=20, unique=True, editable=False, blank=True, null=True)   
    employee_name = models.CharField(max_length=255)
    role = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=15, default='0000000000')
    email = models.EmailField(default='unknown@example.com', blank=True)   
    hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Hourly rate must be a positive amount greater than zero.")

    def save(self, *args, **kwargs):
        # Auto-generate employee code if missing
        if not self.employee_code:
            prefix = "EMP"

            # Look up the last created employee to increment the sequence
            last_employee = Employee.objects.filter(
                employee_code__startswith=prefix
            ).order_by('employee_id').last()

            if last_employee and last_employee.employee_code:
                try:
                    last_sequence = int(last_employee.employee_code.split('-')[-1])
                    new_sequence = last_sequence + 1
                except (ValueError, IndexError):
                    new_sequence = 1
            else:
                new_sequence = 1

            # Results in: EMP-0001, EMP-0002, etc.
            self.employee_code = f"{prefix}-{new_sequence:04d}"

        super().save(*args, **kwargs)
    def __str__(self):
        return f"{self.employee_code} - {self.employee_name} - ({self.role})"
    
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
        # Pushes updates up to the parent WorkOrder after saving the step
        if self.work_order:
            self.work_order.recalculate_status()

# string representation of the instruction showing work order id, step number and first 50 characters of instruction text
    def __str__(self):
        text_preview = self.instruction_text[:50]
        snippet = f"{text_preview}..." if len(self.instruction_text) > 50 else text_preview
        return f"step {self.step_number}: {self.step_name} ({self.status})"     
class WorkOrder(models.Model):
    work_order_id = models.AutoField(primary_key=True)
    work_order_code = models.CharField(max_length=20, unique=True, editable=False, blank=True, null=True)
    bill_of_material = models.ForeignKey('BillOfMaterial', on_delete=models.PROTECT, blank=True, null=True, help_text="The snapshot version of the recipe locked in for this specific operational run.")
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='work_order', limit_choices_to={'product_type__in': ['FINISHED', 'INTERMEDIATE']})
    employee = models.ManyToManyField('Employee', related_name='assigned_work_order', help_text="Employees assigned to this work order.")
    quantity_produced = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    production_start_date = models.DateField()
    production_end_date = models.DateTimeField(null=True, blank=True, editable=False, help_text="Automatically captured when work order status turns to Completed.")
    is_inventory_updated = models.BooleanField(default=False, editable=False)    
    status = models.CharField(max_length=20, choices=WorkOrderInstruction.STATUS_CHOICES, default='IN_PROGRESS', editable=False, help_text="Automatically managed based on step completion statuses.")
    # automated state evaluation machine logic
    def recalculate_status(self):
        """Scans all child instructions to dynamically compute macro status."""
        instructions = self.instructions.all()
        if not instructions.exists():
            return
            
        total_steps = instructions.count()
        completed_steps = instructions.filter(status='COMPLETED').count()
        cancelled_steps = instructions.filter(status='CANCELLED').count()
        
        # State translation math rules
        if completed_steps == total_steps:
            new_status = 'COMPLETED'
        elif cancelled_steps == total_steps:
            new_status = 'CANCELLED'
        else:
            new_status = 'IN_PROGRESS'
            
        # Only issue a save request if an actual status boundary change occurs
        if self.status != new_status:
            self.status = new_status
            self.save()    
    def clean(self):
        super().clean()
        # Validate that all instruction steps are complete before allowing status = COMPLETED
        if self.status == 'COMPLETED' and self.pk:
            incomplete_steps = self.instructions.exclude(status__iexact='COMPLETED').count()
            if incomplete_steps > 0:
                raise ValidationError({
                    'status': f"Cannot complete Work Order. There are still {incomplete_steps} incomplete instruction step(s)."
                })
        # Prevents completion without a valid quantity
        if self.status == 'COMPLETED' and not self.is_inventory_updated:
            if not self.quantity_produced or self.quantity_produced <= 0:
                raise ValidationError({'quantity_produced': "Quantity produced must be greater than 0 to complete."})
            # VALIDATES RAW MATERIAL STOCK USING ACTUAL CONSUMPTION (quantity_actual)
            for line in self.material_lines.all():
                from .models import Inventory
                available_stock = Inventory.objects.filter(
                    product=line.component).aggregate(
                    total=Sum('quantity_available')
                )['total'] or Decimal('0.00')

                actual_used = line.quantity_actual or Decimal('0.00')

                if available_stock < actual_used:
                    raise ValidationError({
                        'status': f"Cannot complete Work Order. Insufficient stock for raw material: {line.component.name}. "
                                  f"Actual required: {actual_used}, Available in warehouse: {available_stock}."
                    })
                    
        # Date validation
        if self.production_end_date and self.production_start_date:
            end_date = self.production_end_date.date() if isinstance(self.production_end_date, datetime) else self.production_end_date
            if end_date < self.production_start_date:
                raise ValidationError({'production_end_date': 'Production end date cannot be before production start date.'})

        if self.product and self.product.product_type not in ['FINISHED', 'INTERMEDIATE']:
            raise ValidationError({'product': 'Work orders can only be created for finished or intermediate products.'})

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        # AUTO-GENERATE WORK ORDER CODE
        if not self.work_order_code:
            prefix = "WOC"
            last_work_order = WorkOrder.objects.filter(
                work_order_code__startswith=prefix
            ).order_by('work_order_id').last()

            if last_work_order and last_work_order.work_order_code:
                try:
                    last_sequence = int(last_work_order.work_order_code.split('-')[-1])
                    new_sequence = last_sequence + 1
                except (ValueError, IndexError):
                    new_sequence = 1
            else:
                new_sequence = 1

            self.work_order_code = f"{prefix}-{new_sequence:04d}"

        # AUTO-ASSIGN ACTIVE BOM
        if not self.bill_of_material and self.product:
            active_bom = self.product.boms.filter(is_active=True).first()
            if active_bom:
                self.bill_of_material = active_bom

        # Set end date timestamp if completed
        if self.status == 'COMPLETED' and not self.production_end_date:
            self.production_end_date = timezone.now()

        # Save main instance
        super().save(*args, **kwargs)

        # INITIALIZE MATERIAL LINES (ONLY ON CREATION - ONE TIME)
        if is_new and self.bill_of_material:
            for item in self.bill_of_material.items.all():
                # Initial baseline reference calculation based on BOM item default
                base_qty = item.quantity_required or Decimal('0.00')

                WorkOrderMaterialLine.objects.get_or_create(
                    work_order=self,
                    component=item.component,
                    defaults={
                        'quantity_expected': base_qty,
                        'quantity_actual': base_qty  # Initial suggestion only; operator can edit anytime
                    }
                )

        # EXECUTE PHYSICAL INVENTORY DEDUCTION (UNTOUCHED BY BOM MATH)
        current_status = (self.status or '').upper().strip()
        if current_status == 'COMPLETED' and not self.is_inventory_updated:
            with transaction.atomic():
                from .models import Inventory, StockTransaction

                # ADD FINISHED PRODUCT TO STOCK
                finished_qty = self.quantity_produced or Decimal('0.00')
                if finished_qty > Decimal('0.00'):
                    finished_inventory, _ = Inventory.objects.select_for_update().get_or_create(
                        product=self.product,
                        defaults={'quantity_available': Decimal('0.00')}
                    )
                    finished_inventory.quantity_available += finished_qty
                    finished_inventory.save(update_fields=['quantity_available'])

                    StockTransaction.objects.create(
                        product=self.product,
                        quantity=finished_qty,
                        transaction_type='PRODUCTION_OUTPUT',
                        work_order=self
                    )

                # DEDUCT RAW MATERIALS (Deducts EXACTLY what the operator entered in quantity_actual)
                if current_status == 'COMPLETED' and not self.is_inventory_updated:
                    with transaction.atomic():
                        from .models import Inventory, StockTransaction

                        # --- A. FINISHED GOODS (ADD TO STOCK) ---
                        finished_qty = self.quantity_produced or Decimal('0.00')
                        if finished_qty > Decimal('0.00'):
                            finished_inv, _ = Inventory.objects.select_for_update().get_or_create(
                                product=self.product,
                                defaults={'quantity_available': Decimal('0.00')}
                            )
                            finished_inv.quantity_available += finished_qty
                            finished_inv.save(update_fields=['quantity_available'])

                            StockTransaction.objects.create(
                                product=self.product,
                                quantity=finished_qty,
                                transaction_type='PRODUCTION_OUTPUT',
                                work_order=self
                            )

                    # B. RAW MATERIALS (DEDUCT EVERY LINE RELIABLY)
                        for line in self.material_lines.all():
                            needed_to_deduct = line.quantity_actual or Decimal('0.00')
                            if needed_to_deduct <= Decimal('0.00'):
                                continue

                    # Log Consumption History
                            StockTransaction.objects.create(
                                product=line.component,
                                quantity=-needed_to_deduct,
                                transaction_type='PRODUCTION_CONSUMPTION',
                                work_order=self
                            )

                    # Deduct directly from Inventory record
                            raw_inv, _ = Inventory.objects.select_for_update().get_or_create(
                                product=line.component,
                                defaults={'quantity_available': Decimal('0.00')}
                            )
                            raw_inv.quantity_available -= needed_to_deduct
                            raw_inv.save(update_fields=['quantity_available'])
                # LOCK SAFETY SWITCH
                self.is_inventory_updated = True
                super().save(update_fields=['is_inventory_updated', 'production_end_date'])    

        # AUTOMATED PRODUCTION ORDER STATUS SYNC
        if self.pk:
            from .models import ProductionOrder
            linked_production_orders = ProductionOrder.objects.filter(work_order=self)
            for po in linked_production_orders:
                if self.status == 'COMPLETED' and po.status != 'COMPLETED':
                    po.status = 'COMPLETED'
                    po.completed_at = timezone.now()
                    po.save(update_fields=['status', 'completed_at'])
                elif self.status == 'IN_PROGRESS' and po.status != 'IN_PROGRESS':
                    po.status = 'IN_PROGRESS'
                    po.save(update_fields=['status'])
                
        
    def __str__(self):
        return f"Work Order {self.work_order_id} — {self.product.name}"

   

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

# The ingredient going into the recipe
class BOMItem(models.Model):
    bom_item_id = models.AutoField(primary_key=True)
    bom = models.ForeignKey('BillOfMaterial', on_delete=models.CASCADE, related_name='items') 
    component = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='used_in_boms', limit_choices_to={'product_type__in': ['RAW', 'INTERMEDIATE']}, help_text="A raw material, retail item or intermediate sub-assembly required for production.")
    # Quantity required to manufacture exactly ONE unit of the parent product
    quantity_required = models.DecimalField(max_digits=10, decimal_places=4, validators=[MinValueValidator(Decimal('0.0001'))], help_text="The precise amount needed to create 1 unit of the parent product.")
    def clean(self):
        # Infinite Loop Prevention: A product cannot be an ingredient in its own recipe card!
        if self.bom and self.component == self.bom.product:
            raise ValidationError({
                'component': f"Circular Dependency Error: '{self.component.name}' cannot be an ingredient in its own build recipe."
            })
        # DEEP NESTED LOOP CHECK. Checks if the component being added already relies on the parent product in its own BOM
        if self.component.boms.filter(components__component=self.bom.product).exists():
            raise ValidationError({
                'component': f"Circular Dependency Detected: You are trying to add '{self.component.name}' here, "
                             f"but '{self.component.name}' already requires '{self.bom.product.name}' in its own active BOM!"
            })

    def __str__(self):
        return f"{self.quantity_required}x {self.component.name} inside {self.bom}"
    
class WorkOrderMaterialLine(models.Model):
    work_order = models.ForeignKey('WorkOrder', on_delete=models.CASCADE, related_name='material_lines')
    component = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='work_order_usages', limit_choices_to={'product_type__in': ['RAW', 'INTERMEDIATE']})
    # What the recipe (BOM) says we should use for the PLANNED quantity
    quantity_expected = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="Theoretical quantity calculated from BOM.") 
    # What the team ACTUALLY consumed (default matches expected, but can be edited)
    quantity_actual = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="The actual physical quantity consumed during this run.")

    class Meta:
        # Prevents adding the same raw material/component to the same work order twice
        unique_together = ('work_order', 'component')
        verbose_name = "Work Order Material Line"
        verbose_name_plural = "Work Order Material Lines"
    @property
    def variance(self):
        """
        Calculates material usage variance.
        Positive = Over-consumption.
        Negative = Under-consumption / Savings due to efficiency.
        """
        actual = self.quantity_actual or Decimal('0.00')
        expected = self.quantity_expected or Decimal('0.00')
        return actual - expected

    @property
    def waste(self):
        """
        Waste is only recorded when actual consumption exceeds expected usage.
        If actual usage is <= expected, waste is 0.00.
        """
        return max(Decimal('0.00'), self.variance)

    def clean(self):
        super().clean()
        # Fallback: Auto-calculate quantity_expected if blank but BOM/WorkOrder exists
        if (not self.quantity_expected or self.quantity_expected == Decimal('0.00')) and self.work_order_id:
            # Look up BOM item requirement for this component
            if self.work_order.bill_of_material:
                bom_item = self.work_order.bill_of_material.items.filter(component=self.component).first()
                if bom_item and self.work_order.quantity_produced:
                    self.quantity_expected = bom_item.quantity_required * self.work_order.quantity_produced

    def __str__(self):
        return f"{self.component.name} for Work Order #{self.work_order.work_order_id}"
class ProductionOrder(models.Model):
    STATUS_CHOICES = [
        ('IN_PROGRESS', 'in_progress'),
        ('COMPLETED', 'completed'),
        ('CANCELLED', 'cancelled'),
    ]
    production_order_id = models.AutoField(primary_key=True)
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='production_runs', limit_choices_to={'product_type__in': ['FINISHED', 'INTERMEDIATE']})
    work_order = models.ForeignKey('WorkOrder', on_delete=models.PROTECT, related_name='production_runs')
    employee = models.ManyToManyField('Employee', blank=True, related_name='production_runs', help_text="Employees assigned to this production run.")
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.01'))], help_text="Quantity to be produced in this specific run.")
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, validators=[MinValueValidator(Decimal('0.00'))], help_text="Manufacturing cost per unit for this specific batch.")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='IN_PROGRESS')
    notes = models.TextField(blank=True, null=True, help_text="Any issues or notes during this production run.")

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(auto_now=True)

    def complete_production(self):
        """Calculates moving average cost and adds finished goods to Inventory."""
        if self.status != 'completed':
            return

        inventory, created = Inventory.objects.get_or_create(
            product=self.product,
            defaults={
                'quantity_available': Decimal('0.00'),
                'unit_cost': Decimal('0.00')
            }
        )

        current_qty = inventory.quantity_available
        current_cost = inventory.unit_cost
        batch_qty = self.quantity_produced
        batch_cost = self.unit_cost

        total_qty = current_qty + batch_qty

        if total_qty > 0:
            current_value = current_qty * current_cost
            batch_value = batch_qty * batch_cost
            
            new_weighted_cost = (current_value + batch_value) / total_qty
            
            inventory.unit_cost = round(new_weighted_cost, 2)
            inventory.quantity_available = total_qty
            inventory.save()
 

    def clean(self):
        super().clean()

        # Auto-assign product from work order if blank
        if self.work_order_id and not self.product_id:
            self.product = self.work_order.product

        # Normalize status string for case-insensitive checks
        current_status = (self.status or '').upper().strip()

        # PRODUCT & WORK ORDER CONSTRAINTS
        if self.product and self.product.product_type not in ['Finished Goods', 'FINISHED', 'INTERMEDIATE']:
            raise ValidationError({
                'product': "Only products designated as 'Finished Goods' or 'Intermediate' can be selected for a production run."
            })

        if self.product and self.work_order:
            if self.work_order.product != self.product:
                raise ValidationError({
                    'work_order': (
                        f"Conflict: The selected Work Order ({self.work_order}) is for '{self.work_order.product}', "
                        f"but this Production Order is set to produce '{self.product}'."
                    )
                })

        # PRE-RUN INVENTORY AVAILABILITY CHECK
        old_status = None
        if self.pk:
            old_status = ProductionOrder.objects.filter(pk=self.pk).values_list('status', flat=True).first()
            if old_status:
                old_status = old_status.upper().strip()

        is_now_in_progress = (current_status == 'IN_PROGRESS')
        was_in_progress = (old_status == 'IN_PROGRESS')

        # Trigger stock pre-check when starting production run
        if is_now_in_progress and not was_in_progress:
            # Use planned target quantity directly (does not fall back to quantity_produced)
            target_qty = self.quantity or Decimal('0.00')

            if target_qty <= Decimal('0.00'):
                raise ValidationError({
                    'quantity': "Production Order target quantity must be greater than 0 before transitioning to IN_PROGRESS."
                })

            bom = None
            if self.work_order and self.work_order.bill_of_material:
                bom = self.work_order.bill_of_material

            if bom:
                for item in bom.items.all():
                    item_req = item.quantity_required or Decimal('0.00')
                    total_needed = item_req * target_qty

                    # Sum available stock across all warehouse records
                    total_available = Inventory.objects.filter(
                        product=item.component
                    ).aggregate(
                        total=Sum('quantity_available')
                    )['total'] or Decimal('0.00')

                    if total_available < total_needed:
                        shortage = total_needed - total_available
                        raise ValidationError(
                            f"Insolvent Stock Error: Insufficient inventory for ingredient '{item.component.name}'. "
                            f"Required for planned run: {total_needed:.2f}, Available: {total_available:.2f} (Shortage: {shortage:.2f})."
                        )

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_status = None
        if not is_new:
            old_status = ProductionOrder.objects.filter(pk=self.pk).values_list('status', flat=True).first()

        is_transitioning_to_completed = (old_status != 'COMPLETED' and self.status == 'COMPLETED')

        # Sets Timestamps
        if self.status == 'IN_PROGRESS' and not getattr(self, 'created_at', None):
            self.created_at = timezone.now()
        elif self.status == 'COMPLETED' and not getattr(self, 'completed_at', None):
            self.completed_at = timezone.now()
        elif self.status == 'CANCELLED':
            self.completed_at = None


        with transaction.atomic():
            super().save(*args, **kwargs)

            # Assign M2M Employees (Requires self.pk to exist)
            if is_new and self.work_order_id and hasattr(self.work_order, 'employee'):
                self.employee.set(self.work_order.employee.all())

            # Non-inventory completion logic (ensure this method does NOT update stock!)
            if is_transitioning_to_completed:
                self.complete_production()
                
    def __str__(self):
        return f"Prod Order {self.production_order_id} ({self.get_status_display()}) - Blueprint: WO-{self.work_order.work_order_id}"            
class Customer(models.Model):
    customer_id = models.AutoField(primary_key=True)    
    customer_name = models.CharField(max_length=255)
    contact_info = models.TextField()
    shipping_address = models.TextField()

    def __str__(self):
        return self.customer_name
    
class SalesOrder(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('approved', 'Approved (Pending Fulfillment)'),
        ('partially_dispatched', 'Partially Dispatched'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    order_number = models.CharField(max_length=50, unique=True, editable=False, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    def save(self, *args, **kwargs):
        # Auto-generate order number 
        if not self.order_number:
            year_month = timezone.now().strftime('%Y%m') # e.g. "202607"
            prefix = f"SO-{year_month}"

            # Looks up the last created order for the current year & month
            last_order = SalesOrder.objects.filter(
                order_number__startswith=prefix
            ).order_by('id').last()

            if last_order and last_order.order_number:
                # Extracts trailing digits and increments by 1
                try:
                    last_sequence = int(last_order.order_number.split('-')[-1])
                    new_sequence = last_sequence + 1
                except ValueError:
                    new_sequence = 1
            else:
                new_sequence = 1

            # Formats like: SO-202607-0001, SO-202607-0002, etc.
            self.order_number = f"{prefix}-{new_sequence:04d}"

        super().save(*args, **kwargs)

    def __str__(self):
        return f"SO-{self.order_number}"

class SalesOrderItem(models.Model):
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('Product', on_delete=models.PROTECT, limit_choices_to={'product_type': 'FINISHED'})  
    quantity_ordered = models.DecimalField(max_digits=10, decimal_places=2)
    quantity_dispatched = models.PositiveIntegerField(default=0)  # Crucial for tracking partial shipments
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True)
    # Use Sum Aggregation instead of `+=` to handle additions, updates, and deletes
    def update_dispatched_quantity(self):
        """Recalculates the exact sum of all related dispatches from the ground truth."""
        # 'dispatch_records' is the related_name from the ForeignKey on DispatchRecord
        total = self.dispatch_records.aggregate(
            total_dispatched=Sum('quantity_dispatched')
        )['total_dispatched'] or Decimal('0.00')
        
        # Only save if the data actually drifted
        if self.quantity_dispatched != total:
            self.quantity_dispatched = total
            # update_fields is a win and avoids overwriting other fields
            self.save(update_fields=['quantity_dispatched'])

    def save(self, *args, **kwargs):
        # Auto-populate line item price from the finished good's catalog selling price
        if not self.unit_price and self.product and self.product.selling_price:
            self.unit_price = self.product.selling_price
        super().save(*args, **kwargs)        

    @property
    def total_price(self):
        return (self.quantity_ordered or 0) * (self.unit_price or 0)    

    def __str__(self):
        return f"Item: {self.product.name} ({self.quantity_dispatched}/{self.quantity_ordered} Dispatched)"
class DispatchRecord(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending / Preparing'),
        ('shipped', 'Shipped / In Transit'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
    ]
    dispatch_id = models.AutoField(primary_key=True)  
    sales_order_item = models.ForeignKey('SalesOrder', on_delete=models.PROTECT, related_name='dispatches')
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='dispatches', limit_choices_to={'product_type': 'FINISHED'}, help_text="Only finished goods can be selected for dispatch.")  
    quantity_dispatched = models.DecimalField(max_digits=10, decimal_places=2)
    dispatch_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    delivery_date = models.DateField(blank=True, null=True, editable=False)
    is_stock_deducted = models.BooleanField(default=False, editable=False)

        # Store original database values to track net differences on edit
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._orig_quantity = self.quantity_dispatched if self.pk else Decimal('0.00')
        self._orig_status = self.status if self.pk else None
    
    def clean(self):
        super().clean()
        
        if not self.product or not self.quantity_dispatched or not self.sales_order_item:
            return

        # 1. Calculate the net extra stock required for this save operation
        if self.is_stock_deducted and self.status == 'delivered':
            # Record was already deducted; check if quantity increased
            additional_stock_needed = self.quantity_dispatched - self._orig_quantity
        elif self.status == 'delivered':
            # Moving to delivered for the first time
            additional_stock_needed = self.quantity_dispatched
        else:
            additional_stock_needed = Decimal('0.00')

        # 2. Check LIVE warehouse inventory if more stock is needed
        if additional_stock_needed > 0:
            total_available = self.product.stock.aggregate(
                total=Sum('quantity_available')
            )['total'] or Decimal('0.00')

            if additional_stock_needed > total_available:
                raise ValidationError({
                    'quantity_dispatched': f"Cannot process request. Additional {additional_stock_needed} units required, "
                                          f"but only {total_available} units are currently available in inventory."
                })

        # 3. Order line validation based on current DB state
        order_item = self.sales_order_item.items.filter(product=self.product).first()
        if not order_item:
            raise ValidationError({
                'product': f"This product is not part of Sales Order #{self.sales_order_item.order_number}."
            })

        other_dispatches = DispatchRecord.objects.filter(
            sales_order_item=self.sales_order_item,
            product=self.product
        )
        if self.pk:
            other_dispatches = other_dispatches.exclude(pk=self.pk)

        already_shipped = other_dispatches.aggregate(
            total=Sum('quantity_dispatched')
        )['total'] or Decimal('0.00')

        remaining_order_qty = order_item.quantity_ordered - already_shipped

        if self.quantity_dispatched > remaining_order_qty:
            raise ValidationError({
                'quantity_dispatched': f"Cannot dispatch {self.quantity_dispatched} units. "
                                      f"Only {remaining_order_qty} units remain on this order line."
            })

    def save(self, *args, **kwargs):
        self.full_clean()  
        
        # Executes once when status is marked 'delivered'
        with transaction.atomic():
            # Handle stock deductions or dynamic adjustments
            if self.status == 'delivered':
                self.delivery_date = timezone.now().date()
                
                # Compute net stock change
                if not self.is_stock_deducted:
                    qty_to_deduct = self.quantity_dispatched
                else:
                    qty_to_deduct = self.quantity_dispatched - self._orig_quantity

                if qty_to_deduct != 0:
                    self._apply_stock_change(qty_to_deduct)
                    self.is_stock_deducted = True

            elif self._orig_status == 'delivered' and self.status != 'delivered':
                # Status changed away from delivered -> restore stock back to warehouse
                self._apply_stock_change(-self.quantity_dispatched)
                self.is_stock_deducted = False
                self.delivery_date = None

            super().save(*args, **kwargs)
            
            # Recalculate parent order item state dynamically from database
            self._sync_parent_order_status()

    def _sync_parent_order_status(self):
        # Calculate total shipped for this order item
        total_shipped = DispatchRecord.objects.filter(
            sales_order_item=self.sales_order_item,
            product=self.product,
            status='delivered'
        ).aggregate(total=Sum('quantity_dispatched'))['total'] or Decimal('0.00')
        
        sales_order_item = self.sales_order_item
        sales_order_item.quantity_dispatched = total_shipped
        sales_order_item.save(update_fields=['quantity_dispatched'])

        # Local reference for clarity (self.sales_order_item points to SalesOrderItem)
        sales_order = sales_order_item.sales_order
        order_items = sales_order.items.all()

        if order_items.exists():
            all_completed = all(item.quantity_dispatched >= item.quantity_ordered for item in order_items)
            any_shipped = any(item.quantity_dispatched > 0 for item in order_items)

            if all_completed:
                sales_order.status = 'completed'
            elif any_shipped:
                sales_order.status = 'partially_dispatched'
            else:
                # Only revert to 'approved' if it was previously dispatched/completed
                if sales_order.status in ['completed', 'partially_dispatched']:
                    sales_order.status = 'approved'

            sales_order.save(update_fields=['status'])
    def delete(self, *args, **kwargs):
        # Capture the item reference before deleting the row
        with transaction.atomic():
            if self.is_stock_deducted:
                self._apply_stock_change(-self.quantity_dispatched)
            parent_order = self.sales_order_item
            super().delete(*args, **kwargs)
            
            # Re-sync parent order after deletion
            if parent_order:
                for item in parent_order.items.all():
                    item.quantity_dispatched = DispatchRecord.objects.filter(
                        sales_order_item=parent_order,
                        product=item.product,
                        status='delivered'
                    ).aggregate(total=Sum('quantity_dispatched'))['total'] or Decimal('0.00')
                    item.save(update_fields=['quantity_dispatched'])            
        
        # Save modifications cleanly
        super().save(*args, **kwargs)   
        
    def __str__(self):
        status_str = f" [{self.get_status_display()}]"
        date_str = f" delivered {self.delivery_date}" if self.delivery_date else ""
        return f"Dispatch {self.dispatch_id}{status_str} — {self.quantity_dispatched}x {self.product.name}{date_str}"
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
        # Auto-calculates total amount based on dispatch volume and true inventory cost
        if self.dispatch and hasattr(self.dispatch, 'production_order') and self.dispatch.production_order.product:
            target_product = self.dispatch.production_order.product
            
            # Look up the warehouse bucket for this item to grab its live average asset cost
            inventory_record = Inventory.objects.filter(
                product=target_product, 
                location='Main Warehouse' # Adjust to the primary location string if different
            ).first()
            # Fall back to 0.00 if no inventory record exists yet
            current_unit_cost = inventory_record.unit_cost if inventory_record else Decimal('0.00')
            self.total_amount = self.dispatch.quantity_dispatched * current_unit_cost
        if self.total_amount is not None:
            self.total_amount = Decimal(str(self.total_amount)).quantize(
                Decimal('0.01'), 
                rounding=ROUND_HALF_UP
            )
       
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
    paid_date = models.DateField(null=True, blank=True, help_text="Date when this invoice was fully settled.")
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        
        if self.procurement_order:
            # Used getattr to safely fetch 'order_date'. 
            # If it doesn't exist on ProcurementOrder, it returns None instead of crashing.
            po_date = getattr(self.procurement_order, 'order_date', None)
            
            if po_date and self.invoice_date < po_date:
                raise ValidationError({'invoice_date': 'Invoice date cannot be earlier than the procurement order date.'})
            
            # Ensure supplier match
            if self.procurement_order.supplier != self.supplier:
                raise ValidationError({'supplier': 'The supplier on the invoice must match the supplier on the procurement order.'})
            
    def save(self, *args, **kwargs):
        if self.procurement_order:
            # Pulls the final calculated cost straight from the delivery record
            self.total_amount = self.procurement_order.total_cost
        else:
            if not self.total_amount:
                self.total_amount = Decimal('0.00')

        self.total_amount = Decimal(str(self.total_amount)).quantize(Decimal('0.01'))
        
        self.full_clean()    
        super().save(*args, **kwargs)
    
    @property
    def remaining_balance(self):
        """Calculates live outstanding balance owed to the supplier."""
        # Forced the invoice total to be a Decimal, even if Django/DB thinks it is None
        invoice_total = self.total_amount
        if invoice_total is None:
            invoice_total = Decimal('0.00')
            
        # If the invoice has not been saved to the database yet (it has no primary key),
        # there cannot possibly be any payments yet. Return the total immediately.
        if not self.pk:
            return invoice_total.quantize(Decimal('0.01'))
            
        # Safely calculate payments if the invoice exists in the database
        try:
            total_paid = self.purchase_payments.aggregate(total=models.Sum('amount'))['total']
            if total_paid is None:
                total_paid = Decimal('0.00')
        except Exception:
            total_paid = Decimal('0.00')
            
        # Run the math safely with guaranteed Decimal types on both sides
        balance = invoice_total - total_paid
        return balance.quantize(Decimal('0.01'))

    def update_payment_status(self):
        """Auto-updates supplier bill status and date stamps based on outgoing payments."""
        total_paid = self.purchase_payments.aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')
        
        if total_paid >= self.total_amount:
            self.status = 'PAID'
            
                
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
        # Prevents AttributeError if supplier or total_amount is empty on a new form
        supplier_name = self.supplier.name if self.supplier else "No Supplier Selected"
        invoice_total = self.total_amount or Decimal('0.00')
        inv_number = self.invoice_number or "New"
        return f"Purchase Invoice #{inv_number} — ${invoice_total} ({supplier_name})"
class PurchasePayment(models.Model):
    purchase_invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name='purchase_payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.00'))])
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
    
