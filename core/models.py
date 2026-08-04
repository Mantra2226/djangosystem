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

    def update_delivery_status(self, save=True):
        """
        Recalculates and updates PO status automatically:
        - CANCELLED -> remains CANCELLED unless manually changed
        - No items or total_ordered <= 0 -> DRAFT
        - items exist and total_received == 0 -> SENT
        - 0 < total_received < total_ordered -> PARTIAL
        - total_received >= total_ordered (> 0) -> RECEIVED
        """
        if self.status == 'CANCELLED':
            return self.status

        if not self.pk:
            return self.status

        items = list(self.items.all())
        if not items:
            new_status = 'DRAFT'
        else:
            total_ordered = sum((item.quantity_ordered or Decimal('0.00')) for item in items)
            total_received = sum((item.quantity_received or Decimal('0.00')) for item in items)

            if total_ordered <= Decimal('0.00'):
                new_status = 'DRAFT'
            elif total_received <= Decimal('0.00'):
                new_status = 'SENT'
            elif total_received < total_ordered:
                new_status = 'PARTIAL'
            else:
                new_status = 'RECEIVED'

        if self.status != new_status:
            self.status = new_status
            if save:
                PurchaseOrder.objects.filter(pk=self.pk).update(status=new_status)
        return new_status

    def sync_received_quantities(self):
        """
        Recalculates quantity_received for all items on this Purchase Order based on linked DELIVERED ProcurementOrders,
        and updates the Purchase Order status automatically.
        """
        if not self.pk:
            return

        for item in self.items.all():
            total_received = ProcurementOrder.objects.filter(
                purchase_order=self,
                product=item.product,
                status='DELIVERED'
            ).aggregate(total=Sum('quantity'))['total'] or Decimal('0.00')

            if item.quantity_received != total_received:
                item.quantity_received = total_received
                item.save(update_fields=['quantity_received'])

        self.update_delivery_status(save=True)

    def clean(self):
        super().clean()
        
        # If an operator sets the status to a received state, check the line items
        if self.status in ['PARTIAL', 'RECEIVED']:
            if self.pk:
                total_received = self.items.aggregate(total=Sum('quantity_received'))['total'] or Decimal('0.00')
                if total_received <= Decimal('0.00'):
                    raise ValidationError({
                        'status': f"Cannot set status to '{self.get_status_display()}' because "
                                  f"no items have been marked as received yet. Please log and deliver "
                                  f"a Procurement Order for this PO first."
                    })
            else:
                raise ValidationError({
                    'status': "A brand new Purchase Order cannot be created as Partially or Fully Received. "
                              "It must start as 'Draft' or 'Sent to Supplier'."
                })

    def save(self, *args, **kwargs):
        # Only generate a PO number if it doesn't have one yet
        if not self.po_number:
            current_year = timezone.now().year
            prefix = f"PO-{current_year}-"
            latest_po = PurchaseOrder.objects.filter(
                po_number__startswith=prefix
            ).order_by('-po_number').first()
            
            if latest_po:
                try:
                    last_sequence = int(latest_po.po_number.split('-')[-1])
                    next_sequence = last_sequence + 1
                except (ValueError, IndexError):
                    next_sequence = 1
            else:
                next_sequence = 1
            
            self.po_number = f"{prefix}{next_sequence:05d}"
            
        super().save(*args, **kwargs)

        if self.pk and self.status != 'CANCELLED':
            self.update_delivery_status(save=True)

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
        return self.quantity_ordered * self.price_per_unit

    def clean(self):
        # Validation Rule: You cannot buy a product from Supplier A if it's assigned to Supplier B
        if self.product.supplier and self.product.supplier != self.purchase_order.supplier:
            raise ValidationError({
                'product': f"This product is strictly supplied by '{self.product.supplier.name}'. "
                           f"You cannot order it under a PO to '{self.purchase_order.supplier.name}'."
            })
            
        if self.quantity_received > self.quantity_ordered:
            raise ValidationError({
                'quantity_received': "Quantity received cannot exceed quantity ordered."
            })

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.purchase_order_id:
            self.purchase_order.update_delivery_status(save=True)

    def delete(self, *args, **kwargs):
        po = self.purchase_order
        super().delete(*args, **kwargs)
        if po and po.pk:
            po.update_delivery_status(save=True)

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
    order_date = models.DateField(default=timezone.now)
    delivery_date = models.DateTimeField(null=True, blank=True, editable=False, help_text="Automatically captures when status changes to Delivered.")
    status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='PENDING')   
    delivery_location = models.CharField(max_length=255, default='Main Warehouse')

    # Tracking changes for calculations
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._orig_quantity = self.quantity if self.pk else Decimal('0.00')
        self._orig_status = self.status if self.pk else None
        self._orig_po_id = self.purchase_order_id if self.pk else None
        self._orig_product_id = self.product_id if self.pk else None
        self._orig_location = self.delivery_location if self.pk else None
    
    @property
    def supplier(self):
        return self.purchase_order.supplier if self.purchase_order else None

    def clean(self):
        super().clean()
        if self.product and self.product.product_type == 'FINISHED':
            raise ValidationError({'product': 'Finished products are manufactured internally. Use a production order instead of a procurement order for finished products.'})
        
        if self.purchase_order and self.product:
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

        if self.status != 'CANCELLED':
            if self.delivery_date and self.status != 'DELIVERED':
                self.status = 'DELIVERED'
            elif self.status == 'DELIVERED' and not self.delivery_date:
                self.delivery_date = timezone.now()

        self.full_clean()

        orig_delivered_qty = self._orig_quantity if (self._orig_status == 'DELIVERED') else Decimal('0.00')
        new_delivered_qty = self.quantity if (self.status == 'DELIVERED') else Decimal('0.00')
        qty_delta = new_delivered_qty - orig_delivered_qty

        with transaction.atomic():
            super().save(*args, **kwargs)

            # Adjust inventory stock and log transaction if delivered quantity changed
            if qty_delta != Decimal('0.00') and self.product:
                target_location = self.delivery_location or 'Main Warehouse'
                inventory_item, _ = Inventory.objects.select_for_update().get_or_create(
                    product=self.product,
                    location=target_location,
                    defaults={
                        'quantity_available': Decimal('0.00'),
                        'unit_cost': self.price_per_unit
                    }
                )
                # Pulls figures directly from the target inventory row for AVCO calculation
                current_total_qty = inventory_item.quantity_available
                current_cost = inventory_item.unit_cost
                total_qty_after = current_total_qty + qty_delta

                if total_qty_after > 0:
                    # AVCO calculation
                    new_weighted_cost = (
                        (current_total_qty * current_cost) + (qty_delta * self.price_per_unit)
                    ) / total_qty_after
                    inventory_item.unit_cost = new_weighted_cost.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                inventory_item.quantity_available += qty_delta
                inventory_item.save()
                # Safe supplier reference for stock transaction
                supplier_name = self.supplier.name if self.supplier else "Unassigned Supplier"
                sign = "+" if qty_delta > 0 else ""
                StockTransaction.objects.create(
                    product=self.product,
                    quantity=qty_delta,
                    transaction_type='RECEIPT',
                    notes=f"Stock adjustment of {sign}{qty_delta} units via Procurement Order #{self.procurement_order_id} from {supplier_name}."
                )

            # Sync linked PurchaseOrder(s) received quantities & status
            if self.purchase_order:
                self.purchase_order.sync_received_quantities()

            # If PO link was changed, re-sync the old PO as well
            if self._orig_po_id and self._orig_po_id != self.purchase_order_id:
                old_po = PurchaseOrder.objects.filter(pk=self._orig_po_id).first()
                if old_po:
                    old_po.sync_received_quantities()

            # Refresh tracked properties for instance reuse
            self._orig_quantity = self.quantity
            self._orig_status = self.status
            self._orig_po_id = self.purchase_order_id
            self._orig_product_id = self.product_id
            self._orig_location = self.delivery_location

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            po = self.purchase_order
            if self.status == 'DELIVERED' and self.product:
                inventory_item = Inventory.objects.select_for_update().filter(
                    product=self.product,
                    location=self.delivery_location or 'Main Warehouse'
                ).first()
                if inventory_item:
                    inventory_item.quantity_available -= self.quantity
                    inventory_item.save()

                StockTransaction.objects.create(
                    product=self.product,
                    quantity=-self.quantity,
                    transaction_type='ADJUSTMENT',
                    notes=f"Rollback of -{self.quantity} units due to deletion of Procurement Order #{self.procurement_order_id}."
                )

            super().delete(*args, **kwargs)

            if po and po.pk:
                po.sync_received_quantities()

    def __str__(self):
        return f"PO #{self.procurement_order_id} - {self.product.name} ({self.status})"           
               
class Inventory(models.Model):
    Inventory_id = models.AutoField(primary_key=True) 
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='stock')   
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, validators=[MinValueValidator(Decimal('0.00'))], help_text="Moving weighted average cost calculated from delivered procurements.")
    quantity_available = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Unreserved stock physically available for new production orders. Cannot drop below zero.")
    quantity_allocated = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Stock reserved for active Work Orders currently IN_PROGRESS.")
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
    is_inventory_allocated = models.BooleanField(default=False, help_text="Flag indicating BOM expected stock has been reserved on IN_PROGRESS.")
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

    def process_inventory(self):
        """
        HYBRID INVENTORY ENGINE:
        Phase 1: Reserve stock (Allocations) when status moves to IN_PROGRESS.
        Phase 2: Deduct incremental actuals (Deltas) during intermediate saves.
        Phase 3: Add finished goods & clear remaining allocations on COMPLETED.
        """
        current_status = (self.status or '').upper().strip()

        print("\n==================================================")
        print(f"[HYBRID INVENTORY ENGINE] Work Order ID: {self.pk} | Status: '{current_status}'")
        print(f"[LOG] Flags -> Allocated: {self.is_inventory_allocated} | Fully Updated: {self.is_inventory_updated}")

        with transaction.atomic():
            from .models import Inventory, StockTransaction

            # =========================================================================
            # PHASE 1: STOCK ALLOCATION (Runs once when status moves to IN_PROGRESS)
            # =========================================================================
            if current_status == 'IN_PROGRESS' and not self.is_inventory_allocated:
                print("--------------------------------------------------")
                print("[PHASE 1: STOCK ALLOCATION START]")
                
                for line in self.material_lines.all():
                    expected_qty = line.quantity_expected or Decimal('0.00')
                    if expected_qty <= Decimal('0.00'):
                        continue

                    raw_inv, _ = Inventory.objects.select_for_update().get_or_create(
                        product=line.component,
                        defaults={'quantity_available': Decimal('0.00'), 'quantity_allocated': Decimal('0.00')}
                    )

                    # Reserve expected stock: Shift from quantity_available to quantity_allocated
                    old_avail = raw_inv.quantity_available
                    old_alloc = raw_inv.quantity_allocated
                    
                    raw_inv.quantity_available -= expected_qty
                    raw_inv.quantity_allocated += expected_qty
                    raw_inv.save(update_fields=['quantity_available', 'quantity_allocated'])

                    print(f"    RESERVED ({line.component.name}): Expected={expected_qty}")
                    print(f"    Available: {old_avail} -> {raw_inv.quantity_available} | Allocated: {old_alloc} -> {raw_inv.quantity_allocated}")

                # Flip allocation lock
                self.is_inventory_allocated = True
                super().save(update_fields=['is_inventory_allocated'])
                print("[SAFETY GATE] Flipped self.is_inventory_allocated = True")

            # =========================================================================
            # PHASE 2: INCREMENTAL DELTA ISSUING (Runs during IN_PROGRESS or COMPLETED)
            # =========================================================================
            if current_status in ['IN_PROGRESS', 'COMPLETED'] and not self.is_inventory_updated:
                print("--------------------------------------------------")
                print("[PHASE 2: INCREMENTAL DELTA ISSUING START]")

                for line in self.material_lines.all():
                    actual_qty = line.quantity_actual or Decimal('0.00')
                    issued_qty = line.quantity_issued or Decimal('0.00')
                    
                    # Compute Line Delta (New consumption since last save)
                    delta = actual_qty - issued_qty

                    print(f"    Line '{line.component.name}': Actual={actual_qty} | Already Issued={issued_qty} | Delta={delta}")

                    if delta > Decimal('0.00'):
                        raw_inv, _ = Inventory.objects.select_for_update().get_or_create(
                            product=line.component,
                            defaults={'quantity_available': Decimal('0.00'), 'quantity_allocated': Decimal('0.00')}
                        )

                        old_alloc = raw_inv.quantity_allocated
                        
                        # Reduce allocation pool if available, otherwise deduct directly
                        if raw_inv.quantity_allocated >= delta:
                            raw_inv.quantity_allocated -= delta
                        else:
                            # Used more than originally allocated
                            excess = delta - raw_inv.quantity_allocated
                            raw_inv.quantity_allocated = Decimal('0.00')
                            raw_inv.quantity_available -= excess

                        raw_inv.save(update_fields=['quantity_available', 'quantity_allocated'])

                        # Record transaction history for delta
                        StockTransaction.objects.create(
                            product=line.component,
                            quantity=-delta,
                            transaction_type='PRODUCTION_CONSUMPTION',
                            work_order=self
                        )

                        # Update background issued counter
                        line.quantity_issued = actual_qty
                        line.save(update_fields=['quantity_issued'])

                        print(f"      ✓ DEDUCTED DELTA ({delta}): Allocated: {old_alloc} -> {raw_inv.quantity_allocated} | Issued Counter set to {line.quantity_issued}")

            # =========================================================================
            # PHASE 3: FINAL RECONCILIATION & OUTPUT (Runs when COMPLETED)
            # =========================================================================
            if current_status == 'COMPLETED' and not self.is_inventory_updated:
                print("--------------------------------------------------")
                print("[PHASE 3: FINAL RECONCILIATION & FINISHED GOODS START]")

                # --- A. Release any unconsumed allocated stock back to available pool ---
                if self.is_inventory_allocated:
                    for line in self.material_lines.all():
                        expected = line.quantity_expected or Decimal('0.00')
                        issued = line.quantity_issued or Decimal('0.00')
                        
                        unconsumed_alloc = expected - issued
                        if unconsumed_alloc > Decimal('0.00'):
                            raw_inv, _ = Inventory.objects.select_for_update().get_or_create(
                                product=line.component,
                                defaults={'quantity_available': Decimal('0.00'), 'quantity_allocated': Decimal('0.00')}
                            )
                            old_alloc = raw_inv.quantity_allocated
                            old_avail = raw_inv.quantity_available

                            # Release unused allocation back to available stock
                            raw_inv.quantity_allocated = max(Decimal('0.00'), raw_inv.quantity_allocated - unconsumed_alloc)
                            raw_inv.quantity_available += unconsumed_alloc
                            raw_inv.save(update_fields=['quantity_available', 'quantity_allocated'])

                            print(f"   ✓ RELEASED UNUSED ALLOCATION ({line.component.name}): Released={unconsumed_alloc}")
                            print(f"      Allocated: {old_alloc} -> {raw_inv.quantity_allocated} | Available: {old_avail} -> {raw_inv.quantity_available}")

                # --- B. Record Finished Goods Output ---
                finished_qty = self.quantity_produced or Decimal('0.00')
                if finished_qty > Decimal('0.00'):
                    finished_inv, _ = Inventory.objects.select_for_update().get_or_create(
                        product=self.product,
                        defaults={'quantity_available': Decimal('0.00')}
                    )
                    old_qty = finished_inv.quantity_available
                    finished_inv.quantity_available += finished_qty
                    finished_inv.save(update_fields=['quantity_available'])

                    StockTransaction.objects.create(
                        product=self.product,
                        quantity=finished_qty,
                        transaction_type='PRODUCTION_OUTPUT',
                        work_order=self
                    )
                    print(f"   ✓ ADDED FINISHED GOODS ({self.product.name}): +{finished_qty} | Stock: {old_qty} -> {finished_inv.quantity_available}")

                # --- C. Final Safety Gate ---
                self.is_inventory_updated = True
                super().save(update_fields=['is_inventory_updated', 'production_end_date'])
                print("[SAFETY GATE] Flipped self.is_inventory_updated = True")

        print("[HYBRID INVENTORY ENGINE END]")
        print("==================================================\n")

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        print("\n==================================================")
        print(f"[WORK ORDER SAVE START] ID: {self.pk} | Code: {self.work_order_code}")

        # AUTO-GENERATE CODE & ASSIGN BOM
        if not self.work_order_code:
            prefix = "WOC"
            last_wo = WorkOrder.objects.filter(work_order_code__startswith=prefix).order_by('work_order_id').last()
            new_seq = (int(last_wo.work_order_code.split('-')[-1]) + 1) if (last_wo and last_wo.work_order_code) else 1
            self.work_order_code = f"{prefix}-{new_seq:04d}"
            print(f"[LOG] Generated new Work Order Code: {self.work_order_code}")

        if not self.bill_of_material and self.product:
            active_bom = self.product.boms.filter(is_active=True).first()
            if active_bom:
                self.bill_of_material = active_bom
                print(f"[LOG] Auto-assigned Active BOM: {self.bill_of_material}")

        current_status = (self.status or '').upper().strip()
        is_completed = (current_status == 'COMPLETED')

        print(f"[LOG] Raw Status: '{self.status}' | Normalized: '{current_status}' | Is Completed: {is_completed}")
        print(f"[LOG] Inventory Already Updated Flag: {self.is_inventory_updated}")

        if is_completed and not self.production_end_date:
            self.production_end_date = timezone.now()

        # SAVE MAIN RECORD
        super().save(*args, **kwargs)
        print(f"[LOG] Main Work Order record saved to DB (PK: {self.pk})")

        # INITIALIZE MATERIAL LINES ON CREATION ONLY
        if is_new and self.bill_of_material:
            print("[LOG] Creating initial material lines from BOM...")
            
            # Direct Query to find linked ProductionOrder quantity
            from .models import ProductionOrder
            po = ProductionOrder.objects.filter(work_order=self).first()
            
            if po and po.quantity:
                target_qty = po.quantity
            elif self.quantity_produced and self.quantity_produced > Decimal('0.00'):
                target_qty = self.quantity_produced
            else:
                target_qty = Decimal('1.00')

            print(f"[LOG] Calculated Target Batch Quantity: {target_qty}")

            for item in self.bill_of_material.items.all():
                per_unit_req = item.quantity_required or Decimal('0.00')
                total_expected = per_unit_req * target_qty
                
                line, created = WorkOrderMaterialLine.objects.get_or_create(
                    work_order=self,
                    component=item.component,
                    defaults={
                        'quantity_expected': total_expected, 
                        'quantity_actual': Decimal('0.00'),
                        'quantity_issued': Decimal('0.00')
                    }
                )
                print(f"      Created material line for {item.component.name}: Expected={total_expected}")

        # AUTOMATED PRODUCTION ORDER STATUS SYNC
        if self.pk:
            from .models import ProductionOrder
            linked_pos = ProductionOrder.objects.filter(work_order=self)
            print(f"[PRODUCTION ORDER SYNC] Found {linked_pos.count()} linked Production Order(s).")
            for po in linked_pos:
                po_status = (po.status or '').upper().strip()
                if is_completed and po_status != 'COMPLETED':
                    po.status = 'COMPLETED'
                    po.completed_at = timezone.now()
                    po.save(update_fields=['status', 'completed_at'])
                    print(f"    Updated ProductionOrder #{po.pk} status to COMPLETED.")

        print("[WORK ORDER SAVE END]")
        print("==================================================\n")
                
        
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
        if self.component.boms.filter(items__component=self.bom.product).exists():
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
    # Automated counter tracking stock already deducted for this line in prior saves.
    quantity_issued = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0.00'),help_text="Automated counter tracking stock already deducted for this line in prior saves.")
    
    class Meta:
        # Prevents adding the same raw material/component to the same work order twice
        unique_together = ('work_order', 'component')
        verbose_name = "Work Order Material Line"
        verbose_name_plural = "Work Order Material Lines"

    @property
    def variance(self):
        if hasattr(self, 'loss_record') and self.loss_record:
            return self.loss_record.quantity_lost
        actual = self.quantity_actual or Decimal('0.00')
        expected = self.quantity_expected or Decimal('0.00')
        return (actual - expected).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def variance_percentage(self):
        if hasattr(self, 'loss_record') and self.loss_record:
            return self.loss_record.variance_percentage
        expected = self.quantity_expected or Decimal('0.00')
        if expected <= Decimal('0.00'):
            return Decimal('0.00')
        pct = (self.variance / expected) * Decimal('100.00')
        return pct.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def unit_cost(self):
        if hasattr(self, 'loss_record') and self.loss_record:
            return self.loss_record.unit_cost
        if self.component:
            inv = self.component.stock.first()
            if inv and inv.unit_cost:
                return inv.unit_cost
        return Decimal('0.00')

    @property
    def cost_variance(self):
        if hasattr(self, 'loss_record') and self.loss_record:
            return self.loss_record.financial_loss
        cost = self.variance * self.unit_cost
        return cost.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def waste(self):
        return max(Decimal('0.00'), self.variance)

    @property
    def efficiency_rate(self):
        if hasattr(self, 'loss_record') and self.loss_record:
            return self.loss_record.efficiency_rate
        actual = self.quantity_actual or Decimal('0.00')
        expected = self.quantity_expected or Decimal('0.00')
        if actual <= Decimal('0.00'):
            return Decimal('100.00')
        rate = (expected / actual) * Decimal('100.00')
        return rate.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def variance_status(self):
        if hasattr(self, 'loss_record') and self.loss_record:
            return self.loss_record.loss_type
        var = self.variance
        if var > Decimal('0.00'):
            return 'OVER_CONSUMPTION'
        elif var < Decimal('0.00'):
            return 'SAVINGS'
        return 'EXACT'

    @property
    def variance_summary(self):
        var = self.variance
        pct = self.variance_percentage
        cost = self.cost_variance
        
        sign = "+" if var > 0 else ""
        if var > 0:
            return f"{sign}{var:.2f} ({sign}{pct:.2f}%) — Over-consumption (+${cost:.2f} Cost Impact)"
        elif var < 0:
            return f"{var:.2f} ({pct:.2f}%) — Savings (${abs(cost):.2f} Saved)"
        return "0.00 (0.00%) — Exact Match"

    def clean(self):
        super().clean()
        # Fallback: Auto-calculate quantity_expected if blank but BOM/WorkOrder exists
        if (not self.quantity_expected or self.quantity_expected == Decimal('0.00')) and self.work_order_id:
            # Look up BOM item requirement for this component
            if self.work_order.bill_of_material:
                bom_item = self.work_order.bill_of_material.items.filter(component=self.component).first()
                if bom_item and self.work_order.quantity_produced:
                    self.quantity_expected = bom_item.quantity_required * self.work_order.quantity_produced

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        LossRecord.sync_from_material_line(self)

    def __str__(self):
        return f"{self.component.name} for Work Order #{self.work_order.work_order_id}"

class LossRecord(models.Model):
    LOSS_TYPE_CHOICES = [
        ('OVER_CONSUMPTION', 'Material Over-consumption / Scrap'),
        ('EFFICIENT_SAVINGS', 'Material Savings / Efficiency'),
        ('EXACT', 'Exact Match / Zero Variance'),
        ('DAMAGE', 'Physical Damage / Spoilage'),
        ('EXPIRATION', 'Expired Stock'),
    ]

    loss_id = models.AutoField(primary_key=True)
    work_order_material_line = models.OneToOneField(
        'WorkOrderMaterialLine',
        on_delete=models.CASCADE,
        related_name='loss_record',
        null=True,
        blank=True,
        help_text="The work order material line this usage variance originates from."
    )
    work_order = models.ForeignKey(
        'WorkOrder',
        on_delete=models.CASCADE,
        related_name='loss_records',
        null=True,
        blank=True,
        help_text="Parent Work Order."
    )
    product = models.ForeignKey(
        'Product',
        on_delete=models.PROTECT,
        related_name='loss_records',
        help_text="Component product associated with this loss/variance record."
    )
    quantity_expected = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Planned / theoretical BOM quantity required."
    )
    quantity_actual = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Actual physical quantity consumed during production."
    )
    quantity_lost = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Quantity variance (quantity_actual - quantity_expected)."
    )
    unit_cost = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Unit cost of component at time of variance calculation."
    )
    financial_loss = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Financial cost impact (quantity_lost * unit_cost)."
    )
    variance_percentage = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Percentage usage variance relative to expected."
    )
    efficiency_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('100.00'),
        help_text="Material utilization efficiency rate percentage."
    )
    loss_type = models.CharField(
        max_length=50, choices=LOSS_TYPE_CHOICES, default='EXACT'
    )
    loss_date = models.DateField(default=timezone.now)
    loss_location = models.CharField(max_length=255, default='Main Warehouse')
    reason = models.TextField(blank=True, help_text="Reason or notes for loss/variance.")
    notes = models.TextField(blank=True, help_text="Audit notes or breakdown for this variance record.")
    recorded_at = models.DateTimeField(auto_now=True)

    @classmethod
    def sync_from_material_line(cls, line):
        if not line or not line.pk:
            return None

        expected = line.quantity_expected or Decimal('0.00')
        actual = line.quantity_actual or Decimal('0.00')
        qty_var = (actual - expected).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        unit_cost = Decimal('0.00')
        if line.component:
            inv = line.component.stock.first()
            if inv and inv.unit_cost:
                unit_cost = inv.unit_cost

        cost_impact = (qty_var * unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if expected > Decimal('0.00'):
            pct = ((qty_var) / expected) * Decimal('100.00')
            pct = pct.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            pct = Decimal('0.00')

        if actual > Decimal('0.00'):
            eff = (expected / actual) * Decimal('100.00')
            eff = eff.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            eff = Decimal('100.00')

        if qty_var > Decimal('0.00'):
            l_type = 'OVER_CONSUMPTION'
        elif qty_var < Decimal('0.00'):
            l_type = 'EFFICIENT_SAVINGS'
        else:
            l_type = 'EXACT'

        loss_rec, created = cls.objects.get_or_create(
            work_order_material_line=line,
            defaults={
                'work_order': line.work_order,
                'product': line.component,
                'quantity_expected': expected,
                'quantity_actual': actual,
                'quantity_lost': qty_var,
                'unit_cost': unit_cost,
                'financial_loss': cost_impact,
                'variance_percentage': pct,
                'efficiency_rate': eff,
                'loss_type': l_type,
                'notes': f"Auto-calculated material variance for WO #{line.work_order_id} ({line.component.name})"
            }
        )

        if not created:
            loss_rec.work_order = line.work_order
            loss_rec.product = line.component
            loss_rec.quantity_expected = expected
            loss_rec.quantity_actual = actual
            loss_rec.quantity_lost = qty_var
            loss_rec.unit_cost = unit_cost
            loss_rec.financial_loss = cost_impact
            loss_rec.variance_percentage = pct
            loss_rec.efficiency_rate = eff
            loss_rec.loss_type = l_type
            loss_rec.notes = f"Auto-calculated material variance for WO #{line.work_order_id} ({line.component.name})"
            loss_rec.save()

        return loss_rec

    def __str__(self):
        sign = "+" if self.quantity_lost > 0 else ""
        return f"Loss Record #{self.loss_id} — {self.product.name} ({sign}{self.quantity_lost:.2f} units, ${self.financial_loss:.2f})"

class ProductionOrder(models.Model):
    STATUS_CHOICES = [
        ('IN_PROGRESS', 'In Progress'),
        ('ON_HOLD_SHORTAGE', 'On Hold (Stock Shortage)'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    ]
    production_order_id = models.AutoField(primary_key=True)
    production_order_code = models.CharField(max_length=20, unique=True, editable=False, blank=True, null=True, help_text="System-generated unique production order code.")
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='production_runs', limit_choices_to={'product_type__in': ['FINISHED', 'INTERMEDIATE']})
    work_order = models.ForeignKey('WorkOrder', on_delete=models.PROTECT, related_name='production_runs')
    employee = models.ManyToManyField('Employee', blank=True, related_name='production_runs', help_text="Employees assigned to this production run.")
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.01'))], help_text="Quantity to be produced in this specific run.")
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, validators=[MinValueValidator(Decimal('0.00'))], help_text="Manufacturing cost per unit for this specific batch.")
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='IN_PROGRESS')
    notes = models.TextField(blank=True, null=True, help_text="Any issues or notes during this production run.")

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(auto_now=True)

    def complete_production(self):
        """Calculates moving average cost (AVCO) without modifying quantity_available."""
        if (self.status or '').upper().strip() != 'COMPLETED':
            return

        inventory, created = Inventory.objects.get_or_create(
            product=self.product,
            defaults={
                'quantity_available': Decimal('0.00'),
                'unit_cost': Decimal('0.00')
            }
        )

        current_qty = inventory.quantity_available or Decimal('0.00')
        current_cost = inventory.unit_cost or Decimal('0.00')
        batch_qty = self.quantity or Decimal('0.00')
        batch_cost = self.unit_cost or Decimal('0.00')

        total_qty = current_qty + batch_qty

        if total_qty > Decimal('0.00'):
            current_value = current_qty * current_cost
            batch_value = batch_qty * batch_cost
            
            new_weighted_cost = (current_value + batch_value) / total_qty
            inventory.unit_cost = new_weighted_cost.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
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
        was_in_progress = (old_status in ['IN_PROGRESS', 'COMPLETED'])

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
                shortage_msgs = []
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
                        shortage_msgs.append(f"{item.component.name} (Need: {total_needed:.2f}, Avail: {total_available:.2f}, Short: {shortage:.2f})")

                if shortage_msgs:
                    self.status = 'ON_HOLD_SHORTAGE'
                    shortage_note = f"[MRP SHORTAGE FLAGGED] Insufficient inventory: {'; '.join(shortage_msgs)}"
                    if not self.notes:
                        self.notes = shortage_note
                    elif shortage_note not in self.notes:
                        self.notes = f"{self.notes}\n{shortage_note}"

    def save(self, *args, **kwargs):
        is_new = self.pk is None

        # Auto-generate unique production order code if missing
        if not self.production_order_code:
            prefix = "POC"
            last_po = ProductionOrder.objects.filter(
                production_order_code__startswith=prefix
            ).order_by('production_order_id').last()

            if last_po and last_po.production_order_code:
                try:
                    last_seq = int(last_po.production_order_code.split('-')[-1])
                    new_seq = last_seq + 1
                except (ValueError, IndexError):
                    new_seq = 1
            else:
                new_seq = 1

            self.production_order_code = f"{prefix}-{new_seq:04d}"

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
        code = self.production_order_code or f"POC-{self.production_order_id:04d}"
        wo_code = getattr(self.work_order, 'work_order_code', f"WO-{self.work_order_id}") if self.work_order else "N/A"
        return f"{code} ({self.get_status_display()}) - Blueprint: {wo_code}"            
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

    def update_status(self, save=True):
        """
        Recalculates and updates Sales Order status automatically based on items and dispatch progress:
        - If status is 'cancelled': preserve 'cancelled'.
        - If no items or total_ordered == 0: 'draft'
        - If items exist and total_dispatched == 0: 'approved'
        - If 0 < total_dispatched < total_ordered: 'partially_dispatched'
        - If total_dispatched >= total_ordered (> 0): 'completed'
        """
        if self.status == 'cancelled':
            return self.status

        if not self.pk:
            return self.status

        items = list(self.items.all())
        if not items:
            new_status = 'draft'
        else:
            total_ordered = sum(Decimal(str(item.quantity_ordered or 0)) for item in items)
            total_dispatched = sum(Decimal(str(item.quantity_dispatched or 0)) for item in items)

            if total_ordered <= Decimal('0.00'):
                new_status = 'draft'
            elif total_dispatched <= Decimal('0.00'):
                new_status = 'approved'
            elif total_dispatched < total_ordered:
                new_status = 'partially_dispatched'
            else:
                new_status = 'completed'

        if self.status != new_status:
            self.status = new_status
            if save:
                SalesOrder.objects.filter(pk=self.pk).update(status=new_status)
        return new_status

    def save(self, *args, **kwargs):
        # Auto-generate order number 
        if not self.order_number:
            year_month = timezone.now().strftime('%Y%m')
            prefix = f"SO-{year_month}-"

            latest_order = SalesOrder.objects.filter(
                order_number__startswith=prefix
            ).order_by('id').last()

            if latest_order and latest_order.order_number:
                try:
                    last_sequence = int(latest_order.order_number.split('-')[-1])
                    next_sequence = last_sequence + 1
                except (ValueError, IndexError):
                    next_sequence = 1
            else:
                next_sequence = 1

            self.order_number = f"{prefix}{next_sequence:04d}"

        super().save(*args, **kwargs)

        if self.pk and self.status != 'cancelled':
            self.update_status(save=True)

    def __str__(self):
        return f"{self.order_number} - {self.customer.customer_name} ({self.get_status_display()})"

class SalesOrderItem(models.Model):
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('Product', on_delete=models.PROTECT, limit_choices_to={'product_type__in': ['FINISHED', 'INTERMEDIATE']})  
    quantity_ordered = models.DecimalField(max_digits=10, decimal_places=2)
    quantity_dispatched = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    @property
    def unit_price(self):
        if self.product and self.product.selling_price is not None:
            return self.product.selling_price
        return Decimal('0.00')

    @property
    def total_price(self):
        qty = Decimal(str(self.quantity_ordered or '0.00'))
        return qty * self.unit_price

    def update_dispatched_quantity(self):
        """Recalculates the exact sum of all related delivered dispatches from the ground truth."""
        total = self.dispatch_records.filter(status='delivered').aggregate(
            total_dispatched=Sum('quantity_dispatched')
        )['total_dispatched'] or Decimal('0.00')
        
        if self.quantity_dispatched != total:
            self.quantity_dispatched = total
            self.save(update_fields=['quantity_dispatched'])

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.sales_order_id:
            self.sales_order.update_status(save=True)

    def delete(self, *args, **kwargs):
        so = self.sales_order
        super().delete(*args, **kwargs)
        if so and so.pk:
            so.update_status(save=True)

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
    dispatch_code = models.CharField(max_length=30, unique=True, editable=False, blank=True, null=True, help_text="System-generated unique dispatch code (e.g. DISP-0001).")
    sales_order_item = models.ForeignKey('SalesOrderItem', on_delete=models.PROTECT, related_name='dispatch_records')
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='dispatches', limit_choices_to={'product_type': 'FINISHED'}, help_text="Only finished goods can be selected for dispatch.")  
    quantity_dispatched = models.DecimalField(max_digits=10, decimal_places=2)
    dispatch_date = models.DateField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    delivery_date = models.DateField(blank=True, null=True, editable=False)
    is_stock_deducted = models.BooleanField(default=False, editable=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._orig_quantity = self.quantity_dispatched if self.pk else Decimal('0.00')
        self._orig_status = self.status if self.pk else None
    
    def clean(self):
        super().clean()
        
        if not self.product or not self.quantity_dispatched or not self.sales_order_item:
            return

        diff = self.quantity_dispatched - self._orig_quantity
        
        if diff > 0 and self.product:
            inventory = Inventory.objects.filter(product=self.product).first()
            current_available = inventory.quantity_available if inventory else Decimal('0.00')
            
            if diff > current_available:
                raise ValidationError({
                    'quantity_dispatched': f"Insufficient stock available! You requested {self.quantity_dispatched} "
                                           f"(Net addition of +{diff}), but only {current_available} units of "
                                           f"'{self.product.name}' are currently available in inventory."
                })

    def save(self, *args, **kwargs):
        # Auto-generate unique dispatch code if missing
        if not self.dispatch_code:
            prefix = "DISP"
            latest = DispatchRecord.objects.filter(
                dispatch_code__startswith=prefix
            ).order_by('-dispatch_code').first()

            if latest and latest.dispatch_code:
                try:
                    last_seq = int(latest.dispatch_code.split('-')[-1])
                    new_seq = last_seq + 1
                except (ValueError, IndexError):
                    new_seq = 1
            else:
                new_seq = 1

            self.dispatch_code = f"{prefix}-{new_seq:04d}"

        # Status Automation & Stock Deductions
        becoming_delivered = (self.status == 'delivered' and self._orig_status != 'delivered')
        leaving_delivered = (self.status != 'delivered' and self._orig_status == 'delivered')
        quantity_changed = (self.quantity_dispatched != self._orig_quantity and self.status == 'delivered')

        if becoming_delivered:
            self.is_stock_deducted = True
            if not self.delivery_date:
                self.delivery_date = timezone.now().date()
        elif leaving_delivered:
            self.is_stock_deducted = False
            self.delivery_date = None

        self.full_clean()

        with transaction.atomic():
            super().save(*args, **kwargs)

            if becoming_delivered:
                self._apply_stock_change(self.quantity_dispatched)
            elif leaving_delivered:
                self._apply_stock_change(-self._orig_quantity)
            elif quantity_changed:
                diff = self.quantity_dispatched - self._orig_quantity
                self._apply_stock_change(diff)

            self._sync_parent_order_status()

    def _apply_stock_change(self, qty_to_deduct):
        """
        Deducts (or restores) stock in Inventory for this dispatch record's product
        and records a StockTransaction entry.
        """
        qty = Decimal(str(qty_to_deduct))
        if qty == Decimal('0.00'):
            return

        inventory_item, _ = Inventory.objects.select_for_update().get_or_create(
            product=self.product,
            defaults={'quantity_available': Decimal('0.00')}
        )

        inventory_item.quantity_available -= qty
        inventory_item.save()

        StockTransaction.objects.create(
            product=self.product,
            dispatch_record=self,
            quantity=-qty,
            transaction_type='SHIPMENT',
            notes=f"Stock adjustment of {-qty} units via Dispatch #{self.dispatch_code or self.dispatch_id}"
        )

    def _sync_parent_order_status(self):
        total_shipped = DispatchRecord.objects.filter(
            sales_order_item=self.sales_order_item,
            status='delivered'
        ).aggregate(total=Sum('quantity_dispatched'))['total'] or Decimal('0.00')
        
        sales_order_item = self.sales_order_item
        sales_order_item.quantity_dispatched = total_shipped
        sales_order_item.save(update_fields=['quantity_dispatched'])

        if sales_order_item.sales_order:
            sales_order_item.sales_order.update_status(save=True)

    def delete(self, *args, **kwargs):
        # Capture the item reference before deleting the row
        with transaction.atomic():
            if self.is_stock_deducted:
                self._apply_stock_change(-self.quantity_dispatched)
            parent_item = self.sales_order_item
            super().delete(*args, **kwargs)
            
            # Re-sync parent order item and order status after deletion
            if parent_item:
                parent_item.update_dispatched_quantity()
                if parent_item.sales_order:
                    parent_item.sales_order.update_status(save=True)
        
    def __str__(self):
        code = self.dispatch_code or f"DISP-{self.dispatch_id:04d}"
        status_str = f" [{self.get_status_display()}]"
        date_str = f" delivered {self.delivery_date}" if self.delivery_date else ""
        return f"{code}{status_str} — {self.quantity_dispatched}x {self.product.name}{date_str}"
class SalesInvoice(models.Model):
    ENTRY_TYPE_CHOICES = [
        ('Paid', 'Paid'),
        ('Partial', 'Partial Payment'),
        ('Unpaid', 'Unpaid'),
    ]
    invoice_id = models.AutoField(primary_key=True)
    invoice_number = models.CharField(max_length=255, unique=True, editable=False, blank=True, help_text="Unique identifier for the invoice. Auto-generated.")
    customer = models.ForeignKey('Customer', on_delete=models.PROTECT, null=True, blank=True, related_name='sales_invoices')
    dispatch = models.ForeignKey('DispatchRecord', on_delete=models.PROTECT, null=True, blank=True, related_name='sales_invoices')
    invoice_date = models.DateField(default=timezone.now)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False, default=Decimal('0.00'))
    status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='Unpaid')

    def clean(self):  
        super().clean()     
        if self.total_amount is not None:
            self.total_amount = Decimal(str(self.total_amount)).quantize(
                Decimal('0.01'), 
                rounding=ROUND_HALF_UP
            )
        if self.dispatch and self.dispatch.dispatch_date and self.invoice_date and self.invoice_date < self.dispatch.dispatch_date:
            raise ValidationError({'invoice_date': 'Invoice date cannot be earlier than the physical dispatch date.'}) 

    def save(self, *args, **kwargs):
        # Auto-calculate total amount based on dispatch volume and selling price
        if self.dispatch and self.dispatch.product:
            price = self.dispatch.product.selling_price or Decimal('0.00')
            if price == Decimal('0.00') and hasattr(self.dispatch.product, 'stock'):
                inv = self.dispatch.product.stock.first()
                if inv and inv.unit_cost:
                    price = inv.unit_cost
            self.total_amount = (self.dispatch.quantity_dispatched or Decimal('0.00')) * price

        if self.total_amount is not None:
            self.total_amount = Decimal(str(self.total_amount)).quantize(
                Decimal('0.01'), 
                rounding=ROUND_HALF_UP
            )
       
        if not self.invoice_number:
            year_month = timezone.now().strftime('%Y%m')
            prefix = f"SINV-{year_month}-"
            latest = SalesInvoice.objects.filter(
                invoice_number__startswith=prefix
            ).order_by('-invoice_number').first()

            if latest and latest.invoice_number:
                try:
                    last_seq = int(latest.invoice_number.split('-')[-1])
                    next_seq = last_seq + 1
                except (ValueError, IndexError):
                    next_seq = 1
            else:
                next_seq = 1

            self.invoice_number = f"{prefix}{next_seq:04d}"

        self.full_clean()    
        super().save(*args, **kwargs)

    @property
    def total_paid(self):
        """Calculates exact total payments collected for this sales invoice."""
        total = self.sales_payments.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        return Decimal(str(total)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def remaining_balance(self):
        """Calculates accurate live remaining balance on the sales invoice."""
        total = self.total_amount or Decimal('0.00')
        rem = total - self.total_paid
        return max(Decimal('0.00'), rem.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

    def update_payment_status(self, save=True):
        """Auto-updates customer invoice status based on incoming payments."""
        paid = self.total_paid
        total = self.total_amount or Decimal('0.00')
        
        if paid >= total and total > Decimal('0.00'):
            new_status = 'Paid'
        elif paid > Decimal('0.00'):
            new_status = 'Partial'
        else:
            new_status = 'Unpaid'

        if self.status != new_status:
            self.status = new_status
            if save and self.pk:
                SalesInvoice.objects.filter(pk=self.pk).update(status=new_status)
        return new_status

    def __str__(self):
        return f"Sales Invoice #{self.invoice_number} — ${self.total_amount:.2f} ({self.customer.customer_name if self.customer else 'N/A'})"

class SalesInvoicePayments(models.Model):
    invoice = models.ForeignKey('SalesInvoice', on_delete=models.CASCADE, related_name='sales_payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    payment_method = models.CharField(max_length=50, choices=[('CASH', 'Cash'), ('CARD', 'Card Transfer'), ('TRANSFER', 'Bank Transfer')])
    reference_number = models.CharField(max_length=100, blank=True)
    paid_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        method = (self.payment_method or '').upper()
        ref_num = (self.reference_number or '').strip()
        requires_reference = ['CARD', 'BANK']

        if method in requires_reference and not ref_num:
            raise ValidationError({
                'reference_number': "A reference number (transaction ID or deposit confirmation) is required for payments made by card or bank transfer."
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        with transaction.atomic():
            super().save(*args, **kwargs)
            if self.invoice:
                self.invoice.update_payment_status(save=True)

    def delete(self, *args, **kwargs):
        inv = self.invoice
        super().delete(*args, **kwargs)
        if inv and inv.pk:
            inv.update_payment_status(save=True)
class PurchaseInvoice(models.Model):
    STATUS_CHOICES = [
        ('PAID', 'Paid'),
        ('UNPAID', 'Unpaid'),
        ('PARTIAL', 'Partially Paid'),
    ]   
    invoice_id = models.AutoField(primary_key=True)
    invoice_number = models.CharField(max_length=50, unique=True, null=True, blank=True, help_text="Unique identifier for the purchase invoice from the supplier.")
    supplier = models.ForeignKey('Supplier', on_delete=models.PROTECT, related_name='purchase_invoices')
    procurement_order = models.ForeignKey('ProcurementOrder', on_delete=models.PROTECT, blank=True, null=True, related_name='purchase_invoices')
    invoice_date = models.DateField(default=timezone.now)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False, default=Decimal('0.00'))

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='UNPAID')
    paid_date = models.DateField(null=True, blank=True, editable=False, help_text="Date when this invoice was fully settled.")
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        
        if self.procurement_order:
            po_date = getattr(self.procurement_order, 'order_date', None)
            
            if po_date and self.invoice_date and self.invoice_date < po_date:
                raise ValidationError({'invoice_date': 'Invoice date cannot be earlier than the procurement order date.'})
            
            if self.procurement_order.supplier != self.supplier:
                raise ValidationError({'supplier': 'The supplier on the invoice must match the supplier on the procurement order.'})
            
    def save(self, *args, **kwargs):
        if self.procurement_order:
            self.total_amount = self.procurement_order.total_cost
        else:
            if not self.total_amount:
                self.total_amount = Decimal('0.00')

        self.total_amount = Decimal(str(self.total_amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if not self.invoice_date:
            self.invoice_date = timezone.now().date()
        
        self.full_clean()    
        super().save(*args, **kwargs)
    
    @property
    def total_paid(self):
        """Calculates total payments made for this purchase invoice."""
        if not self.pk:
            return Decimal('0.00')
        total = self.purchase_payments.aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')
        return Decimal(str(total)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def remaining_balance(self):
        """Calculates live outstanding balance owed to the supplier."""
        invoice_total = self.total_amount or Decimal('0.00')
        balance = invoice_total - self.total_paid
        return max(Decimal('0.00'), balance.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

    def update_payment_status(self, save=True):
        """Auto-updates supplier bill status and paid_date stamp based on outgoing payments."""
        total_paid = self.total_paid
        invoice_total = self.total_amount or Decimal('0.00')
        
        if total_paid >= invoice_total and invoice_total > Decimal('0.00'):
            self.status = 'PAID'
            if not self.paid_date:
                latest = self.purchase_payments.order_by('-paid_at').first()
                if latest and latest.paid_at:
                    self.paid_date = latest.paid_at.date()
                else:
                    self.paid_date = timezone.now().date()
        elif total_paid > Decimal('0.00'):
            self.status = 'PARTIAL'
            self.paid_date = None
        else:
            self.status = 'UNPAID'
            self.paid_date = None

        if save and self.pk:
            PurchaseInvoice.objects.filter(pk=self.pk).update(
                status=self.status,
                paid_date=self.paid_date
            )
        return self.status


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

                # Step B: Safely deduct returned items cost from the associated SalesInvoice row
                invoice_record = SalesInvoice.objects.filter(dispatch=self.dispatch).first()
                if invoice_record and invoice_record.total_amount and prod_order:
                    raw_return_value = self.quantity_returned * prod_order.product.cost_per_unit
                    
                    return_value = raw_return_value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    
                    invoice_record.total_amount -= return_value
                    # Extra safety shield: ensure the final invoice total is cleanly quantized too
                    invoice_record.total_amount = invoice_record.total_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    invoice_record.save()

    def __str__(self):
        return f"Return #{self.return_id} — {self.quantity_returned} units from Dispatch #{self.dispatch.dispatch_id} ({self.quality_control_status})"        

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
    entry_type = models.CharField(max_length=10, choices=ENTRY_TYPE_CHOICES, default='EXPENSE')
    category = models.CharField(max_length=20, choices=ENTRY_CATEGORY_CHOICES, default='SALES')
    procurement_order = models.ForeignKey('ProcurementOrder', on_delete=models.PROTECT, null=True, blank=True, related_name='financial_entries')
    sales_invoice = models.ForeignKey('SalesInvoice', on_delete=models.PROTECT, null=True, blank=True, related_name='financial_entries')
    loss = models.ForeignKey('LossRecord', on_delete=models.PROTECT, null=True, blank=True, related_name='financial_entries')
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
    
