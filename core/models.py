from datetime import datetime
from itertools import product
import secrets
from sys import prefix
from django.db import models, transaction
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from decimal import Decimal, ROUND_HALF_UP
from django.utils.text import slugify
from django.core.validators import MinValueValidator
from django.db.models import Sum
from django.utils import timezone
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
    selling_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Required for Finished Goods. Allowed for Sub-assemblies/Intermediates. Leave blank for raw materials.")
    # Added field-level validation rules
    def clean(self):       
        super().clean()
        # 1. Ensure Finished Goods always have a selling price
        if self.product_type == 'FINISHED' and self.selling_price is None:
            raise ValidationError({
                'selling_price': "Finished Goods must have a valid selling price."
            })
        # 2. Automatically clear selling price if the item is Raw Material
        if self.product_type == 'RAW' and self.selling_price is not None:
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
    order_date = models.DateField(auto_now_add=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT', db_index=True)
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
    order_date = models.DateField(default=timezone.now, db_index=True)
    delivery_date = models.DateTimeField(null=True, blank=True, editable=False, help_text="Automatically captures when status changes to Delivered.")
    status = models.CharField(max_length=255, choices=ENTRY_TYPE_CHOICES, default='PENDING', db_index=True)   
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
    quantity_available = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), db_index=True, validators=[MinValueValidator(Decimal('0.00'))], help_text="Unreserved stock physically available for new production orders. Cannot drop below zero.")
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

    @property
    def work_order_code(self):
        """Returns the linked Work Order code or 'No WO' / None."""
        if self.work_order and self.work_order.work_order_code:
            return self.work_order.work_order_code
        elif self.work_order_id:
            return f"WO-{self.work_order_id}"
        return None

    def __str__(self):
        sign = "+" if self.quantity > 0 else ""
        formatted_date = self.created_at.strftime('%Y-%m-%d') if self.created_at else "Draft"
        sku = self.product.sku if self.product else "UNKNOWN_SKU"
        wo_part = f" | {self.work_order.work_order_code}" if (self.work_order and self.work_order.work_order_code) else (f" | WO-{self.work_order_id}" if self.work_order_id else " | No WO")
        
        return f"{sku} | {sign}{self.quantity} | {self.get_transaction_type_display()}{wo_part} | {formatted_date}"
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

    def clean(self):
        super().clean()
        if self.work_order and self.work_order.status in ['DRAFT', 'AWAITING_RESOLUTION', 'ON_HOLD_SHORTAGE']:
            if (self.status or '').upper() == 'COMPLETED':
                raise ValidationError({
                    'status': f"Instruction steps cannot be marked as COMPLETED while Work Order #{self.work_order.work_order_code or self.work_order.pk} is in '{self.work_order.status}' status. Start production first."
                })

    def save(self, *args, **kwargs):
        if self.work_order:
            conflict = False
            if self.step_number:
                conflict = WorkOrderInstruction.objects.filter(
                    work_order=self.work_order,
                    step_number=self.step_number
                ).exclude(pk=self.pk).exists()

            if not self.step_number or conflict:
                highest_step = WorkOrderInstruction.objects.filter(
                    work_order=self.work_order
                ).exclude(pk=self.pk).aggregate(models.Max('step_number'))['step_number__max']
                self.step_number = (highest_step or 0) + 1

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
    CATEGORY_CHOICES = [
        ('PRODUCTION', 'Production (Bulk Mixing)'),
        ('PACKAGING', 'Packaging'),
    ]
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
        ('AWAITING_RESOLUTION', 'Awaiting Shortage Resolution'),
        ('ON_HOLD_SHORTAGE', 'On Hold (Bulk Shortage)'),
    ]
    work_order_id = models.AutoField(primary_key=True)
    work_order_code = models.CharField(max_length=20, unique=True, editable=False, blank=True, null=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, blank=True, null=True, help_text="Auto-assigned based on product type: INTERMEDIATE -> PRODUCTION, FINISHED -> PACKAGING.")
    bill_of_material = models.ForeignKey('BillOfMaterial', on_delete=models.PROTECT, blank=True, null=True, help_text="The snapshot version of the recipe locked in for this specific operational run.")
    parent_work_order = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='child_packaging_orders', help_text="The Stage 1 Bulk Intermediate work order required prior to running packaging operations.")
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='work_order', limit_choices_to={'product_type__in': ['FINISHED', 'INTERMEDIATE']})
    employee = models.ManyToManyField('Employee', related_name='assigned_work_order', help_text="Employees assigned to this work order.")
    quantity_produced = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))])
    actual_quantity_produced = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.00'))], help_text="Actual physical quantity produced in this Run (saved to inventory upon work order completion).")
    scrap_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))])
    scrap_reason = models.CharField(max_length=255, blank=True, default='')
    production_start_date = models.DateField(null=True, blank=True, db_index=True)
    production_end_date = models.DateTimeField(null=True, blank=True, editable=False, help_text="Automatically captured when work order status turns to Completed.")
    is_inventory_updated = models.BooleanField(default=False, editable=False)    
    is_inventory_allocated = models.BooleanField(default=False, help_text="Flag indicating BOM expected stock has been reserved on IN_PROGRESS.")
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='DRAFT', db_index=True, help_text="State machine status of the work order run.")

    @property
    def id(self):
        """Alias property for primary key work_order_id."""
        return self.pk

    @property
    def order_code(self):
        """Alias property for work_order_code."""
        return self.work_order_code

    @property
    def target_quantity(self):
        """
        Resolves planned production batch target quantity STRICTLY from the linked ProductionOrder.
        If no ProductionOrder is linked, falls back to self.quantity_produced.
        Returns Decimal('0.00') if neither is available.
        """
        if self.pk:
            po = self.production_runs.first()
            if po and po.quantity and po.quantity > Decimal('0.00'):
                return po.quantity
        if self.quantity_produced and self.quantity_produced > Decimal('0.00'):
            return self.quantity_produced
        return Decimal('0.00')
    # automated state evaluation machine logic
    def recalculate_status(self):
        """Scans all child instructions to dynamically compute macro status."""
        if self.status in ['DRAFT', 'AWAITING_RESOLUTION', 'ON_HOLD_SHORTAGE']:
            return

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
            # Use update_fields to avoid re-entering the full WorkOrder.save() chain
            # (which would re-run sync_material_lines, instruction auto-gen, etc.)
            super(WorkOrder, self).save(update_fields=['status'])
            if new_status == 'COMPLETED':
                self.sync_child_packaging_expectations()

    def check_bulk_availability(self):
        """
        BULK SHORTAGE DETECTION ENGINE:
        Queries live intermediate warehouse inventory stock against required packaging BOM quantities
        and returns calculated shortfall metrics, available stock, required quantity, and maximum achievable units.
        """
        target_qty = self.target_quantity or Decimal('0.00')
        active_bom = self.bill_of_material or (self.product.boms.filter(is_active=True).first() if self.product else None)

        if not active_bom:
            return {
                'has_shortfall': False,
                'intermediate_product': None,
                'required_quantity': Decimal('0.00'),
                'available_stock': Decimal('0.00'),
                'shortfall': Decimal('0.00'),
                'max_achievable_units': target_qty
            }

        intermediate_item = active_bom.items.filter(component__product_type='INTERMEDIATE').first()
        if not intermediate_item:
            return {
                'has_shortfall': False,
                'intermediate_product': None,
                'required_quantity': Decimal('0.00'),
                'available_stock': Decimal('0.00'),
                'shortfall': Decimal('0.00'),
                'max_achievable_units': target_qty
            }

        intermediate_product = intermediate_item.component
        qty_req_per_unit = intermediate_item.quantity_required or Decimal('0.00')
        required_qty = (target_qty * qty_req_per_unit).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        from .models import Inventory
        available_stock = Inventory.objects.filter(
            product=intermediate_product
        ).aggregate(total=Sum('quantity_available'))['total'] or Decimal('0.00')

        shortfall = max(Decimal('0.00'), required_qty - available_stock)
        has_shortfall = shortfall > Decimal('0.00')

        if qty_req_per_unit > Decimal('0.00'):
            max_achievable_units = (available_stock / qty_req_per_unit).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            max_achievable_units = target_qty

        return {
            'has_shortfall': has_shortfall,
            'intermediate_product': intermediate_product,
            'required_quantity': required_qty,
            'available_stock': available_stock,
            'shortfall': shortfall,
            'max_achievable_units': max_achievable_units
        }

    def resolve_bulk_shortage(self, action_choice, existing_bulk_wo_id=None, existing_wo_id=None):
        existing_bulk_wo_id = existing_bulk_wo_id or existing_wo_id
        """
        INTERACTIVE SHORTAGE RESOLUTION PATHWAYS:
        Executes resolution strategy inside an atomic transaction:
          - TOP_UP_BULK: Spawns supplemental bulk parent WorkOrder for missing shortfall,
                         links it as parent_work_order, creates linked ProductionOrder,
                         allocates raw ingredients, and sets packaging status to ON_HOLD_SHORTAGE.
          - DOWNSCALE_TARGET: Recalculates maximum achievable packaging units from available stock,
                               scales down quantity_produced, clears parent link, transitions to IN_PROGRESS,
                               and runs process_inventory().
          - HOLD_FOR_EXISTING: Attaches an active in-progress bulk order (by existing_bulk_wo_id) as
                                parent_work_order and sets status to ON_HOLD_SHORTAGE.

        CONCURRENCY HARDENING:
        - All branches re-evaluate bulk availability from locked inventory rows inside the
          atomic block to prevent TOCTOU races between check and mutation.
        """
        valid_choices = ['TOP_UP_BULK', 'DOWNSCALE_TARGET', 'HOLD_FOR_EXISTING']
        if action_choice not in valid_choices:
            raise ValidationError(f"Invalid resolution choice '{action_choice}'. Must be one of {valid_choices}.")

        current_status = (self.status or '').upper().strip()
        if current_status not in ['DRAFT', 'AWAITING_RESOLUTION']:
            raise ValidationError(
                f"Cannot execute shortage resolution '{action_choice}': "
                f"Work Order #{self.work_order_code or self.pk} is currently in '{self.status}' status. "
                f"Resolution pathways can only be executed on orders in 'DRAFT' or 'AWAITING_RESOLUTION' status."
            )

        with transaction.atomic():
            # Re-evaluate bulk availability INSIDE the atomic block.
            # For DOWNSCALE_TARGET and TOP_UP_BULK, lock the intermediate inventory row
            # to get a consistent snapshot before acting on it.
            active_bom = self.bill_of_material or (self.product.boms.filter(is_active=True).first() if self.product else None)
            intermediate_item = active_bom.items.filter(component__product_type='INTERMEDIATE').first() if active_bom else None

            if intermediate_item:
                from .models import Inventory
                locked_bulk_inv = Inventory.objects.select_for_update().filter(
                    product=intermediate_item.component
                ).first()
                locked_available = locked_bulk_inv.quantity_available if locked_bulk_inv else Decimal('0.00')
            else:
                locked_bulk_inv = None
                locked_available = Decimal('0.00')

            # Recalculate metrics from the locked row
            metrics = self.check_bulk_availability()
            # Override available_stock with the locked value for consistency
            if intermediate_item:
                qty_req_per_unit = intermediate_item.quantity_required or Decimal('0.00')
                target_qty = self.target_quantity or Decimal('0.00')
                required_qty = (target_qty * qty_req_per_unit).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                metrics['available_stock'] = locked_available
                metrics['shortfall'] = max(Decimal('0.00'), required_qty - locked_available)
                metrics['has_shortfall'] = metrics['shortfall'] > Decimal('0.00')
                if qty_req_per_unit > Decimal('0.00'):
                    metrics['max_achievable_units'] = (locked_available / qty_req_per_unit).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                else:
                    metrics['max_achievable_units'] = target_qty

            if action_choice == 'TOP_UP_BULK':
                shortfall = metrics['shortfall']
                intermediate_product = metrics['intermediate_product']

                if not intermediate_product:
                    raise ValidationError("Cannot execute TOP_UP_BULK: No intermediate bulk component found in BOM.")

                if shortfall <= Decimal('0.00'):
                    self.status = 'IN_PROGRESS'
                    super().save(update_fields=['status'])
                    from .models import ProductionOrder
                    for po in ProductionOrder.objects.filter(work_order=self):
                        po.status = 'IN_PROGRESS'
                        po.save(update_fields=['status'])
                    self.process_inventory()
                    return self

                bulk_bom = intermediate_product.boms.filter(is_active=True).first()
                parent_wo = WorkOrder.objects.create(
                    product=intermediate_product,
                    bill_of_material=bulk_bom,
                    production_start_date=self.production_start_date or timezone.now().date(),
                    status='IN_PROGRESS',
                    quantity_produced=shortfall
                )

                # Trigger Phase 1 stock allocation for the newly spawned bulk parent order
                parent_wo.process_inventory()

                # Also create linked ProductionOrder for parent bulk work order
                from .models import ProductionOrder
                ProductionOrder.objects.create(
                    product=intermediate_product,
                    work_order=parent_wo,
                    quantity=shortfall,
                    status='IN_PROGRESS',
                    notes=f"Auto-generated Top-Up Bulk run for child Packaging WorkOrder #{self.work_order_code or self.pk}."
                )

                self.parent_work_order = parent_wo
                self.status = 'ON_HOLD_SHORTAGE'
                super().save(update_fields=['parent_work_order', 'status'])

                for po in ProductionOrder.objects.filter(work_order=self):
                    po.status = 'ON_HOLD_SHORTAGE'
                    po.save(update_fields=['status'])

                print(f"[SHORTAGE RESOLUTION] Executed TOP_UP_BULK: Spawned parent Bulk WorkOrder #{parent_wo.pk} for {shortfall} units.", flush=True)

            elif action_choice == 'DOWNSCALE_TARGET':
                max_units = metrics['max_achievable_units']
                if max_units <= Decimal('0.00'):
                    raise ValidationError("Cannot execute DOWNSCALE_TARGET: Available bulk inventory is zero.")

                self.quantity_produced = max_units
                self.status = 'IN_PROGRESS'
                self.parent_work_order = None  # Detach parent link since we are running standalone with existing warehouse stock
                super().save(update_fields=['quantity_produced', 'status', 'parent_work_order'])

                from .models import ProductionOrder
                for po in ProductionOrder.objects.filter(work_order=self):
                    po.quantity = max_units
                    po.status = 'IN_PROGRESS'
                    po.save(update_fields=['quantity', 'status'])

                self.sync_material_lines()
                self.process_inventory()
                print(f"[SHORTAGE RESOLUTION] Executed DOWNSCALE_TARGET: Scaled batch quantity down to {max_units}.", flush=True)

            elif action_choice == 'HOLD_FOR_EXISTING':
                if existing_bulk_wo_id:
                    try:
                        existing_bulk_wo = WorkOrder.objects.get(pk=existing_bulk_wo_id)
                    except WorkOrder.DoesNotExist:
                        raise ValidationError(f"Cannot execute HOLD_FOR_EXISTING: Bulk WorkOrder #{existing_bulk_wo_id} does not exist.")
                    self.parent_work_order = existing_bulk_wo
                    self.status = 'ON_HOLD_SHORTAGE'
                    super().save(update_fields=['parent_work_order', 'status'])
                else:
                    self.status = 'ON_HOLD_SHORTAGE'
                    super().save(update_fields=['status'])

                from .models import ProductionOrder
                for po in ProductionOrder.objects.filter(work_order=self):
                    po.status = 'ON_HOLD_SHORTAGE'
                    po.save(update_fields=['status'])

                print(f"[SHORTAGE RESOLUTION] Executed HOLD_FOR_EXISTING: WorkOrder #{self.pk} placed on ON_HOLD_SHORTAGE.", flush=True)

        return self

    def clean(self):
        super().clean()
        errors = {}

        current_status = (self.status or '').upper().strip()

        # Date validation (applies generally)
        if self.production_end_date and self.production_start_date:
            end_date = self.production_end_date.date() if isinstance(self.production_end_date, datetime) else self.production_end_date
            if end_date < self.production_start_date:
                errors['production_end_date'] = 'Production end date cannot be before production start date.'

        # Product classification validation (applies generally)
        if self.product and self.product.product_type not in ['FINISHED', 'INTERMEDIATE']:
            errors['product'] = 'Work orders can only be created for finished or intermediate products.'

        # STATUS GATES: IN_PROGRESS and COMPLETED require operational readiness checks
        if current_status in ['IN_PROGRESS', 'COMPLETED']:
            # 1. Target Quantity validation
            target_qty = self.target_quantity
            if target_qty is None or target_qty <= Decimal('0.00'):
                errors['target_quantity'] = "Target Quantity must be greater than 0 to start production."

            # 2. Production Start Date validation
            if not self.production_start_date:
                errors['production_start_date'] = "Please provide a Production Start Date before moving to IN_PROGRESS."

            # 3. Bill of Materials validation
            has_bom = bool(self.bill_of_material or (self.product and self.product.boms.filter(is_active=True).first()))
            if not has_bom:
                errors['bill_of_material'] = "Cannot start order: Assign an active Bill of Materials (BOM) for this product."

            # 4. Packaging Stage 2 parent bulk dependency validation
            is_packaging = (self.category == 'PACKAGING') or (self.product and self.product.product_type == 'FINISHED')
            if is_packaging and self.parent_work_order:
                parent_status = (self.parent_work_order.status or '').upper().strip()
                if parent_status != 'COMPLETED':
                    errors['parent_work_order'] = (
                        f"Cannot start packaging: Linked parent bulk order #{self.parent_work_order.work_order_code} "
                        f"is currently '{self.parent_work_order.status}'. It must reach COMPLETED status first."
                    )

            # 5. Additional checks for COMPLETED status
            if current_status == 'COMPLETED':
                if self.pk:
                    incomplete_steps = self.instructions.exclude(status__iexact='COMPLETED').count()
                    if incomplete_steps > 0:
                        errors['__all__'] = f"Cannot complete Work Order. There are still {incomplete_steps} incomplete instruction step(s)."

                if not self.is_inventory_updated:
                    for line in self.material_lines.all():
                        from .models import Inventory
                        available_stock = Inventory.objects.filter(
                            product=line.component
                        ).aggregate(total=Sum('quantity_available'))['total'] or Decimal('0.00')

                        actual_used = line.quantity_actual or Decimal('0.00')
                        if available_stock < actual_used:
                            errors['__all__'] = (
                                f"Cannot complete Work Order. Insufficient stock for raw material: {line.component.name}. "
                                f"Actual required: {actual_used}, Available in warehouse: {available_stock}."
                            )

        if errors:
            raise ValidationError(errors)

    def start_production(self):
        """
        EXPLICIT STATE TRANSITION WORKFLOW: DRAFT / ON_HOLD_SHORTAGE -> IN_PROGRESS / AWAITING_RESOLUTION.
        Validates operational readiness, checks intermediate bulk material availability,
        and triggers hybrid stock allocation engine upon transition.

        CONCURRENCY HARDENING:
        - Bulk availability is checked inside the atomic block with locked inventory rows
          to eliminate TOCTOU race conditions between shortage detection and allocation.
        """
        current_status = (self.status or '').upper().strip()
        if current_status not in ['DRAFT', 'ON_HOLD_SHORTAGE']:
            raise ValidationError("Only DRAFT or ON_HOLD_SHORTAGE work orders can be started.")

        # Temporarily evaluate status as IN_PROGRESS so clean() enforces operational gates
        old_status = self.status
        self.status = 'IN_PROGRESS'
        try:
            self.clean()
        except ValidationError:
            self.status = old_status
            raise

        # Reset status back to old_status prior to shortage check and state save
        self.status = old_status

        with transaction.atomic():
            # Check for intermediate bulk shortage (packaging orders) INSIDE atomic block
            # so the availability snapshot is consistent with the allocation that follows.
            availability = self.check_bulk_availability()
            if availability.get('has_shortfall'):
                self.status = 'AWAITING_RESOLUTION'
                super().save(update_fields=['status'])
                from .models import ProductionOrder
                for po in ProductionOrder.objects.filter(work_order=self):
                    po.status = 'ON_HOLD_SHORTAGE'
                    po.save(update_fields=['status'])
                return (False, "Bulk shortage detected. Moved to Awaiting Resolution.")

            self.status = 'IN_PROGRESS'
            super().save(update_fields=['status'])
            from .models import ProductionOrder
            for po in ProductionOrder.objects.filter(work_order=self):
                po.status = 'IN_PROGRESS'
                po.save(update_fields=['status'])
            self.process_inventory()

        return (True, "Work order started successfully and stock allocated.")

    def process_inventory(self):
        """
        HYBRID INVENTORY ENGINE:
        Phase 1: Reserve stock (Allocations) when status moves to IN_PROGRESS.
                 Calculates allocated_qty = bom_item.quantity_required * target_qty
                 where target_qty comes STRICTLY AND ONLY from ProductionOrder.quantity.
                 Deducts allocated_qty from Inventory.quantity_available into Inventory.quantity_allocated.
        Phase 2: Deduct incremental actuals (Deltas) during production.
        Phase 3: Add finished goods & release remaining unconsumed allocations on COMPLETED.
        """
        # =====================================================================
        # SAFETY GUARD: Refresh flags from DB and early-exit if already done.
        # Prevents double-execution when multiple call sites trigger this
        # method within the same request cycle (e.g. ProductionOrder.save()
        # -> process_inventory() AND admin save_related() -> process_inventory()).
        # =====================================================================
        if self.pk:
            db_flags = WorkOrder.objects.filter(pk=self.pk).values_list(
                'is_inventory_allocated', 'is_inventory_updated'
            ).first()
            if db_flags:
                self.is_inventory_allocated, self.is_inventory_updated = db_flags

        current_status = (self.status or '').upper().strip()

        # Early exit: nothing to do if already fully updated and not in a
        # state that requires incremental processing.
        if self.is_inventory_updated:
            print(f"\n[HYBRID INVENTORY ENGINE] Work Order ID: {self.pk} — SKIPPED (is_inventory_updated=True)", flush=True)
            return

        from .models import Inventory, StockTransaction
        target_qty = self.target_quantity
        linked_po = self.production_runs.first() if self.pk else None
        if target_qty > Decimal('0.00') and linked_po:
            target_source = f"Production Order #{linked_po.pk} ({linked_po.production_order_code or 'POC'})"
        else:
            target_source = "No linked Production Order (0.00 units)"

        print("\n==================================================", flush=True)
        print(f"[HYBRID INVENTORY ENGINE] Work Order ID: {self.pk} ({self.work_order_code}) | Status: '{current_status}'", flush=True)
        print(f"[LOG] Target Production Batch Quantity (STRICTLY FROM ProductionOrder.quantity): {target_qty} units (Source: {target_source})", flush=True)
        print(f"[LOG] Flags -> Allocated: {self.is_inventory_allocated} | Fully Updated: {self.is_inventory_updated}", flush=True)

        with transaction.atomic():
            # =========================================================================
            # PHASE 1: STOCK ALLOCATION (Runs once when status moves to IN_PROGRESS)
            #
            # CONCURRENCY HARDENING:
            # - Collects all BOM component product IDs and sorts them in ascending order
            #   to acquire exclusive row locks in a deterministic sequence, preventing
            #   database deadlocks when multiple WorkOrders allocate concurrently.
            # - Pre-flight gate inspects ALL locked rows before mutating any, ensuring
            #   either all allocations succeed atomically or none are applied.
            # =========================================================================
            if current_status == 'IN_PROGRESS' and not self.is_inventory_allocated and self.bill_of_material:
                print("--------------------------------------------------", flush=True)
                print("[PHASE 1: STOCK ALLOCATION RESERVATION START]", flush=True)

                # Step 1: Build allocation requirements map (component_id -> required_qty)
                bom_items = list(self.bill_of_material.items.select_related('component').all())
                allocation_plan = {}
                for item in bom_items:
                    per_unit_req = item.quantity_required or Decimal('0.00')
                    expected_allocated_qty = (per_unit_req * target_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    if expected_allocated_qty > Decimal('0.00'):
                        allocation_plan[item.component.pk] = {
                            'component': item.component,
                            'per_unit_req': per_unit_req,
                            'required_qty': expected_allocated_qty,
                        }

                if allocation_plan:
                    # Step 2: Sort component IDs for deterministic lock ordering (prevents deadlocks)
                    sorted_ids = sorted(allocation_plan.keys())
                    print(f"   [LOCK ORDER] Acquiring row locks in deterministic order: {sorted_ids}", flush=True)

                    # Step 3: Acquire exclusive locks on ALL required inventory rows in one query
                    locked_inventories = {}
                    existing_locked = Inventory.objects.select_for_update().filter(
                        product_id__in=sorted_ids
                    )
                    for inv in existing_locked:
                        locked_inventories[inv.product_id] = inv

                    # Create inventory records for any missing components (rare but safe)
                    for comp_id in sorted_ids:
                        if comp_id not in locked_inventories:
                            new_inv = Inventory.objects.create(
                                product_id=comp_id,
                                quantity_available=Decimal('0.00'),
                                quantity_allocated=Decimal('0.00'),
                            )
                            # Re-acquire with lock
                            locked_inventories[comp_id] = Inventory.objects.select_for_update().get(pk=new_inv.pk)

                    # Step 4: PRE-FLIGHT GATE - Check ALL components have sufficient stock
                    #         before mutating any row. Prevents partial allocations.
                    shortage_errors = []
                    for comp_id in sorted_ids:
                        plan = allocation_plan[comp_id]
                        inv = locked_inventories[comp_id]
                        if inv.quantity_available < plan['required_qty']:
                            shortage_errors.append(
                                f"Insufficient stock for '{plan['component'].name}': "
                                f"Available={inv.quantity_available}, Required={plan['required_qty']}."
                            )

                    if shortage_errors:
                        raise ValidationError(
                            " | ".join(shortage_errors) + " Please restock before starting production."
                        )

                    # Step 5: ATOMIC ALLOCATION - All pre-flight checks passed, mutate all rows
                    for comp_id in sorted_ids:
                        plan = allocation_plan[comp_id]
                        inv = locked_inventories[comp_id]
                        old_avail = inv.quantity_available
                        old_alloc = inv.quantity_allocated

                        inv.quantity_available -= plan['required_qty']
                        inv.quantity_allocated += plan['required_qty']
                        inv.save(update_fields=['quantity_available', 'quantity_allocated'])

                        print(f"   [OK] [RESERVED ALLOCATION] Component: '{plan['component'].name}'", flush=True)
                        print(f"      Allocated Quantity for Line: {plan['required_qty']} units (Formula: {plan['per_unit_req']} BOM Req/unit x {target_qty} Target Batch Qty)", flush=True)
                        print(f"      Inventory Shift -> Available: {old_avail} => {inv.quantity_available} | Allocated: {old_alloc} => {inv.quantity_allocated}", flush=True)

                self.is_inventory_allocated = True
                super().save(update_fields=['is_inventory_allocated'])
                print("[SAFETY GATE] Flipped self.is_inventory_allocated = True", flush=True)

            # =========================================================================
            # PHASE 2: INCREMENTAL ACTUAL CONSUMPTION DEDUCTION
            # =========================================================================
            if current_status in ['IN_PROGRESS', 'COMPLETED'] and not self.is_inventory_updated:
                print("--------------------------------------------------", flush=True)
                print("[PHASE 2: INCREMENTAL ACTUAL CONSUMPTION DEDUCTION START]", flush=True)

                for line in self.material_lines.all():
                    actual_qty = line.quantity_actual or Decimal('0.00')
                    already_deducted = line.deducted_quantity or Decimal('0.00')
                    allocated_qty = line.quantity_allocated
                    delta = actual_qty - already_deducted

                    print(f"   Line '{line.component.name}': Allocated Qty={allocated_qty} | Actual Consumed={actual_qty} | Already Deducted={already_deducted} | Delta={delta}", flush=True)

                    if delta != Decimal('0.00'):
                        raw_inv, _ = Inventory.objects.select_for_update().get_or_create(
                            product=line.component,
                            defaults={'quantity_available': Decimal('0.00'), 'quantity_allocated': Decimal('0.00')}
                        )

                        old_alloc = raw_inv.quantity_allocated
                        old_avail = raw_inv.quantity_available

                        if delta > Decimal('0.00'):
                            # Deduct from allocation pool first if available, otherwise from available pool
                            if raw_inv.quantity_allocated >= delta:
                                raw_inv.quantity_allocated -= delta
                            else:
                                excess = delta - raw_inv.quantity_allocated
                                raw_inv.quantity_allocated = Decimal('0.00')
                                raw_inv.quantity_available -= excess
                            trans_type = 'PRODUCTION_CONSUMPTION'
                        else:
                            # Return stock back if actual consumption was reduced
                            raw_inv.quantity_available += (-delta)
                            trans_type = 'ADJUSTMENT'

                        raw_inv.save(update_fields=['quantity_available', 'quantity_allocated'])

                        StockTransaction.objects.create(
                            product=line.component,
                            quantity=-delta,
                            transaction_type=trans_type,
                            work_order=self,
                            notes=f"Stock change of {-delta} units for Work Order #{self.pk} ({line.component.name})"
                        )

                        line.deducted_quantity = actual_qty
                        line.save(update_fields=['deducted_quantity'])
                        print(f"      [OK] [DEDUCTED CONSUMPTION DELTA] Delta={delta} for '{line.component.name}'", flush=True)
                        print(f"         Inventory Updated -> Available: {old_avail} => {raw_inv.quantity_available} | Allocated: {old_alloc} => {raw_inv.quantity_allocated}", flush=True)

            # =========================================================================
            # PHASE 3: FINAL RECONCILIATION & FINISHED GOODS OUTPUT (Runs on COMPLETED)
            # =========================================================================
            if current_status == 'COMPLETED' and not self.is_inventory_updated:
                print("--------------------------------------------------", flush=True)
                print("[PHASE 3: RECONCILIATION & FINISHED GOODS OUTPUT START]", flush=True)

                with transaction.atomic():
                    # --- Step 3a: Process any remaining consumption delta not yet deducted ---
                    for line in self.material_lines.select_related('component').all():
                        actual_qty = line.quantity_actual or Decimal('0.00')
                        already_deducted = line.deducted_quantity or Decimal('0.00')
                        delta = actual_qty - already_deducted

                        if delta > Decimal('0.00'):
                            raw_inv = Inventory.objects.select_for_update().filter(product=line.component).first()
                            if not raw_inv:
                                raw_inv = Inventory.objects.create(
                                    product=line.component,
                                    quantity_available=Decimal('0.00'),
                                    quantity_allocated=Decimal('0.00'),
                                )
                                raw_inv = Inventory.objects.select_for_update().get(pk=raw_inv.pk)

                            old_alloc = raw_inv.quantity_allocated
                            old_avail = raw_inv.quantity_available

                            if raw_inv.quantity_allocated >= delta:
                                raw_inv.quantity_allocated -= delta
                            else:
                                excess = delta - raw_inv.quantity_allocated
                                raw_inv.quantity_allocated = Decimal('0.00')
                                raw_inv.quantity_available -= excess

                            raw_inv.save(update_fields=['quantity_available', 'quantity_allocated'])

                            StockTransaction.objects.create(
                                product=line.component,
                                quantity=-delta,
                                transaction_type='PRODUCTION_CONSUMPTION',
                                work_order=self,
                                notes=f"Phase 3 final deduction of {delta} units for Work Order #{self.pk} ({line.component.name})"
                            )

                            line.deducted_quantity = actual_qty
                            line.save(update_fields=['deducted_quantity'])

                            print(f"   [OK] [PHASE 3 FINAL DEDUCTION] Component: '{line.component.name}' | Delta={delta}", flush=True)
                            print(f"      Inventory -> Available: {old_avail} => {raw_inv.quantity_available} | Allocated: {old_alloc} => {raw_inv.quantity_allocated}", flush=True)

                    # --- Step 3b: Release any unconsumed allocated stock back to available pool ---
                    for line in self.material_lines.select_related('component').all():
                        already_deducted = line.deducted_quantity or Decimal('0.00')
                        allocated_qty = line.quantity_allocated  # BOM-calculated allocation for this line

                        residual_allocated = max(Decimal('0.00'), allocated_qty - already_deducted)

                        if residual_allocated > Decimal('0.00'):
                            raw_inv = Inventory.objects.select_for_update().filter(product=line.component).first()
                            if not raw_inv:
                                continue

                            old_alloc = raw_inv.quantity_allocated
                            old_avail = raw_inv.quantity_available

                            released = min(residual_allocated, raw_inv.quantity_allocated)
                            raw_inv.quantity_allocated -= released
                            raw_inv.quantity_available += released
                            raw_inv.save(update_fields=['quantity_available', 'quantity_allocated'])

                            print(f"   [OK] [RELEASED UNUSED ALLOCATION] Component: '{line.component.name}' | Original Allocated={allocated_qty} | Already Deducted by Phase 2={already_deducted} | Remaining Released={released}", flush=True)
                            print(f"      Inventory -> Allocated: {old_alloc} => {raw_inv.quantity_allocated} | Available: {old_avail} => {raw_inv.quantity_available}", flush=True)

                    # --- Step 3c: Record Finished Goods Output ---
                    effective_qty = self.actual_quantity_produced if self.actual_quantity_produced is not None else self.target_quantity
                    finished_qty = effective_qty
                    if finished_qty > Decimal('0.00'):
                        finished_inv = Inventory.objects.select_for_update().filter(product=self.product).first()
                        if not finished_inv:
                            finished_inv = Inventory.objects.create(
                                product=self.product,
                                quantity_available=Decimal('0.00')
                            )
                            finished_inv = Inventory.objects.select_for_update().get(pk=finished_inv.pk)
                        old_qty = finished_inv.quantity_available
                        finished_inv.quantity_available += finished_qty
                        finished_inv.save(update_fields=['quantity_available'])

                        StockTransaction.objects.create(
                            product=self.product,
                            quantity=finished_qty,
                            transaction_type='PRODUCTION_OUTPUT',
                            work_order=self
                        )
                        print(f"   [OK] [ADDED FINISHED GOODS] Product: '{self.product.name}' | Quantity: +{finished_qty} | Stock: {old_qty} => {finished_inv.quantity_available}", flush=True)

                    # --- Step 3d: Flip safety gate ONLY after all mutations succeed ---
                    self.is_inventory_updated = True
                    super().save(update_fields=['is_inventory_updated', 'production_end_date'])
                    self.sync_child_packaging_expectations()
                    print("[SAFETY GATE] Flipped self.is_inventory_updated = True and synced child packaging expectations", flush=True)

        print("[HYBRID INVENTORY ENGINE END]", flush=True)
        print("==================================================\n", flush=True)

    def sync_child_packaging_expectations(self):
        """
        DYNAMIC YIELD AUTO-SCALING & AUTO-RESUME:
        When a Stage 1 Bulk WorkOrder reaches COMPLETED status, synchronizes the actual bulk yield
        (self.actual_quantity_produced or self.quantity_produced) to all linked child Stage 2 packaging work order material lines 
        where component == self.product. Updates quantity_expected on each matching WorkOrderMaterialLine.
        Also re-checks and auto-resumes linked child packaging orders currently in ON_HOLD_SHORTAGE.
        """
        bulk_yield = (self.actual_quantity_produced if self.actual_quantity_produced is not None else self.quantity_produced) or Decimal('0.00')
        with transaction.atomic():
            for child_wo in self.child_packaging_orders.all():
                mat_lines = child_wo.material_lines.filter(component=self.product)
                for line in mat_lines:
                    line.quantity_expected = bulk_yield
                    line.save(update_fields=['quantity_expected'])
                    # Synchronize corresponding MaterialVarianceRecord if exists
                    MaterialVarianceRecord.sync_from_material_line(line)

                # Auto-resume child packaging work order if it was on hold and now has sufficient stock!
                if child_wo.status == 'ON_HOLD_SHORTAGE':
                    child_avail = child_wo.check_bulk_availability()
                    if not child_avail.get('has_shortfall'):
                        child_wo.status = 'IN_PROGRESS'
                        child_wo.save(update_fields=['status'])
                        child_wo.process_inventory()
                        from .models import ProductionOrder
                        for child_po in ProductionOrder.objects.filter(work_order=child_wo):
                            child_po.status = 'IN_PROGRESS'
                            child_po.save(update_fields=['status'])
                        print(f"[AUTO-RESUME] Child Packaging WorkOrder #{child_wo.pk} auto-resumed to IN_PROGRESS and stock allocated.", flush=True)

    def sync_material_lines(self):
        """
        Ensures a WorkOrderMaterialLine exists for every BOM item in the selected BillOfMaterial.
        Does NOT alter existing lines' quantity_actual or auto-fill other lines!
        """
        if not self.bill_of_material:
            return

        for item in self.bill_of_material.items.all():
            target_qty = self.target_quantity
            expected_qty = (item.quantity_required * target_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if target_qty else Decimal('0.00')
            line, created = WorkOrderMaterialLine.objects.get_or_create(
                work_order=self,
                component=item.component,
                defaults={
                    'quantity_actual': Decimal('0.00'),
                    'quantity_expected': expected_qty,
                }
            )
            if not created and expected_qty > Decimal('0.00') and line.quantity_expected != expected_qty:
                line.quantity_expected = expected_qty
                line.save(update_fields=['quantity_expected'])

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        print("\n==================================================", flush=True)
        print(f"[WORK ORDER SAVE START] ID: {self.pk} | Code: {self.work_order_code}", flush=True)

        with transaction.atomic():
            # AUTO-ASSIGN CATEGORY BASED ON PRODUCT CLASSIFICATION
            if self.product:
                if self.product.product_type == 'INTERMEDIATE':
                    self.category = 'PRODUCTION'
                elif self.product.product_type == 'FINISHED':
                    self.category = 'PACKAGING'

            # AUTO-GENERATE CODE & ASSIGN BOM
            if not self.work_order_code:
                prefix = "WOC"
                last_wo = WorkOrder.objects.filter(work_order_code__startswith=prefix).order_by('work_order_id').last()
                new_seq = (int(last_wo.work_order_code.split('-')[-1]) + 1) if (last_wo and last_wo.work_order_code) else 1
                self.work_order_code = f"{prefix}-{new_seq:04d}"
                print(f"[LOG] Generated new Work Order Code: {self.work_order_code}", flush=True)

            if not self.bill_of_material and self.product:
                active_bom = self.product.boms.filter(is_active=True).first()
                if active_bom:
                    self.bill_of_material = active_bom
                    print(f"[LOG] Auto-assigned Active BOM: {self.bill_of_material}", flush=True)

            current_status = (self.status or '').upper().strip()
            is_completed = (current_status == 'COMPLETED')

            print(f"[LOG] Raw Status: '{self.status}' | Normalized: '{current_status}' | Is Completed: {is_completed}", flush=True)
            print(f"[LOG] Inventory Already Updated Flag: {self.is_inventory_updated}", flush=True)

            if is_completed and not self.production_end_date:
                self.production_end_date = timezone.now()

            # SAVE MAIN RECORD
            super().save(*args, **kwargs)
            print(f"[LOG] Main Work Order record saved to DB (PK: {self.pk})", flush=True)

            # INITIALIZE / SYNC MATERIAL LINES FROM BOM
            if self.bill_of_material:
                self.sync_material_lines()

            # AUTOMATED PRODUCTION ORDER QUANTITY AND STATUS SYNC
            # Set a transient flag so that ProductionOrder.save() does NOT cascade
            # back into process_inventory(). During admin saves, save_related() is
            # the canonical call site for process_inventory() after inlines commit.
            if self.pk:
                self._skip_po_inventory_sync = True
                from .models import ProductionOrder
                linked_pos = ProductionOrder.objects.filter(work_order=self)
                print(f"[PRODUCTION ORDER SYNC] Found {linked_pos.count()} linked Production Order(s).", flush=True)
                for po in linked_pos:
                    po_status = (po.status or '').upper().strip()
                    update_fields = []
                    if is_completed and po_status != 'COMPLETED':
                        po.status = 'COMPLETED'
                        po.completed_at = timezone.now()
                        update_fields.extend(['status', 'completed_at'])
                    if self.quantity_produced and self.quantity_produced > Decimal('0.00') and po.quantity != self.quantity_produced:
                        po.quantity = self.quantity_produced
                        if 'quantity' not in update_fields:
                            update_fields.append('quantity')
                    if update_fields:
                        po.work_order = self  # Ensure PO references this in-memory instance with the flag
                        po.save(update_fields=update_fields)
                        print(f"    Updated ProductionOrder #{po.pk} fields: {update_fields}.", flush=True)
                self._skip_po_inventory_sync = False

            # AUTO-GENERATE DEFAULT PROCESS INSTRUCTIONS IF NONE EXIST
            if self.pk and not self.instructions.exists():
                from .views import generate_work_order_instructions
                generate_work_order_instructions(self)

            # DYNAMIC YIELD AUTO-SCALING TRIGGER FOR PARENT BULK RUNS
            if is_completed and self.product and self.product.product_type == 'INTERMEDIATE':
                self.sync_child_packaging_expectations()

        print("[WORK ORDER SAVE END]", flush=True)
        print("==================================================\n", flush=True)
                
        
    class Meta:
        permissions = [
            ('can_start_production', 'Can start production on work orders'),
            ('can_resolve_shortage', 'Can resolve shortage on work orders'),
        ]

    def __str__(self):
        return f"Work Order {self.work_order_id} — {self.product.name}"


class BillOfMaterial(models.Model):
    bom_id = models.AutoField(primary_key=True)
    product = models.ForeignKey('Product', on_delete=models.CASCADE, related_name='boms', limit_choices_to={'product_type__in': ['FINISHED', 'INTERMEDIATE']}, help_text="Finished or Intermediate product this build recipe belongs to.")
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
        return self.name or f"BOM #{self.bom_id} ({self.product.name})"

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
    quantity_expected = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="Planned / expected component requirement for this line.")
    quantity_actual = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))], help_text="The actual physical quantity consumed during this run.")   
    deducted_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), editable=False, help_text="Tracks quantity already deducted from inventory.")
    
    @property
    def quantity_deducted(self):
        """Alias property for deducted_quantity."""
        return self.deducted_quantity

    @quantity_deducted.setter
    def quantity_deducted(self, value):
        self.deducted_quantity = value

    @property
    def quantity_allocated(self):
        """
        Calculates planned quantity allocated for this material line based on BOM requirement
        multiplied by ProductionOrder target quantity.
        """
        if not self.work_order or not self.work_order.bill_of_material:
            return Decimal('0.00')

        bom_item = self.work_order.bill_of_material.items.filter(component=self.component).first()
        if not bom_item:
            return Decimal('0.00')

        target_qty = self.work_order.target_quantity
        return (bom_item.quantity_required * target_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    class Meta:
        # Prevents adding the same raw material/component to the same work order twice
        unique_together = ('work_order', 'component')
        verbose_name = "Work Order Material Line"
        verbose_name_plural = "Work Order Material Lines"

    def clean(self):
        super().clean()
        if self.quantity_actual is not None and self.quantity_actual < Decimal('0.00'):
            raise ValidationError({
                'quantity_actual': "Actual quantity consumed cannot be a negative number."
            })

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        MaterialVarianceRecord.sync_from_material_line(self)

    def __str__(self):
        return f"{self.component.name} ({self.quantity_actual} consumed) for Work Order #{self.work_order.work_order_id}"

class MaterialVarianceRecord(models.Model):
    VARIANCE_CLASSIFICATION_CHOICES = [
        ('FAVOURABLE', 'Favourable (Material Efficiency / Saved)'),
        ('UNFAVOURABLE', 'Unfavourable (Waste / Over-consumption / Scrap)'),
        ('EXACT', 'Exact Match / Zero Variance'),
    ]

    variance_id = models.AutoField(primary_key=True)
    variance_code = models.CharField(max_length=20, unique=True, editable=False, blank=True, null=True, help_text="System-generated unique material variance code.")
    work_order_material_line = models.OneToOneField(
        'WorkOrderMaterialLine',
        on_delete=models.CASCADE,
        related_name='variance_record',
        null=True,
        blank=True,
        help_text="The work order material line this material variance originates from."
    )
    work_order = models.ForeignKey(
        'WorkOrder',
        on_delete=models.CASCADE,
        related_name='variance_records',
        null=True,
        blank=True,
        help_text="Parent Work Order."
    )
    product = models.ForeignKey(
        'Product',
        on_delete=models.PROTECT,
        related_name='variance_records',
        help_text="Component product associated with this variance record."
    )
    quantity_expected = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Planned / allocated BOM quantity required for this production run."
    )
    quantity_actual = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Actual physical quantity consumed during production."
    )
    quantity_variance = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Quantity variance (quantity_actual - quantity_expected)."
    )
    unit_cost = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Unit cost of component at time of variance calculation."
    )
    financial_impact = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Calculated financial impact (quantity_variance * unit_cost)."
    )
    variance_percentage = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Percentage usage variance relative to expected."
    )
    efficiency_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('100.00'),
        help_text="Material utilization efficiency rate percentage."
    )
    variance_classification = models.CharField(
        max_length=20, choices=VARIANCE_CLASSIFICATION_CHOICES, default='EXACT', db_index=True
    )
    recorded_at = models.DateTimeField(auto_now=True, db_index=True)
    notes = models.TextField(blank=True, help_text="Audit notes or breakdown for this variance record.")

    def save(self, *args, **kwargs):
        if not self.variance_code:
            prefix = "MVR"
            last_rec = MaterialVarianceRecord.objects.filter(
                variance_code__startswith=prefix
            ).order_by('variance_id').last()

            if last_rec and last_rec.variance_code:
                try:
                    last_seq = int(last_rec.variance_code.split('-')[-1])
                    new_seq = last_seq + 1
                except (ValueError, IndexError):
                    new_seq = 1
            else:
                new_seq = 1

            self.variance_code = f"{prefix}-{new_seq:04d}"

        super().save(*args, **kwargs)

    @classmethod
    def sync_from_material_line(cls, line):
        if not line or not line.pk:
            return None

        actual = line.quantity_actual or Decimal('0.00')

        expected = Decimal('0.00')
        if line.work_order and line.work_order.bill_of_material:
            bom_item = line.work_order.bill_of_material.items.filter(component=line.component).first()
            if bom_item:
                target_qty = line.work_order.target_quantity
                expected = (bom_item.quantity_required * target_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        qty_var = (actual - expected).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        unit_cost = Decimal('0.00')
        if line.component:
            inv = line.component.stock.first()
            if inv and inv.unit_cost:
                unit_cost = inv.unit_cost

        cost_impact = (qty_var * unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if expected > Decimal('0.00'):
            pct = (qty_var / expected) * Decimal('100.00')
            pct = pct.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            pct = Decimal('0.00')

        if actual > Decimal('0.00'):
            eff = (expected / actual) * Decimal('100.00')
            eff = eff.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            eff = Decimal('100.00')

        if qty_var > Decimal('0.00'):
            v_class = 'UNFAVOURABLE'
        elif qty_var < Decimal('0.00'):
            v_class = 'FAVOURABLE'
        else:
            v_class = 'EXACT'

        rec, created = cls.objects.get_or_create(
            work_order_material_line=line,
            defaults={
                'work_order': line.work_order,
                'product': line.component,
                'quantity_expected': expected,
                'quantity_actual': actual,
                'quantity_variance': qty_var,
                'unit_cost': unit_cost,
                'financial_impact': cost_impact,
                'variance_percentage': pct,
                'efficiency_rate': eff,
                'variance_classification': v_class,
                'notes': f"Auto-calculated material variance for WO #{line.work_order_id} ({line.component.name})"
            }
        )

        if not created:
            rec.work_order = line.work_order
            rec.product = line.component
            rec.quantity_expected = expected
            rec.quantity_actual = actual
            rec.quantity_variance = qty_var
            rec.unit_cost = unit_cost
            rec.financial_impact = cost_impact
            rec.variance_percentage = pct
            rec.efficiency_rate = eff
            rec.variance_classification = v_class
            rec.notes = f"Auto-calculated material variance for WO #{line.work_order_id} ({line.component.name})"
            rec.save()

        return rec

    @property
    def production_run_type(self):
        """Returns the production run type (PRODUCTION or PACKAGING) from the linked Work Order."""
        if self.work_order and self.work_order.category:
            return self.work_order.category
        if self.work_order_material_line and self.work_order_material_line.work_order:
            return self.work_order_material_line.work_order.category
        return "UNKNOWN"

    def __str__(self):
        code = self.variance_code or f"MVR-{self.variance_id:04d}"
        sign = "+" if self.quantity_variance > 0 else ""
        run_type = self.production_run_type
        return f"{code} [{run_type}] — {self.product.name} ({sign}{self.quantity_variance:.2f} units, ${self.financial_impact:.2f})"

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
    work_order = models.ForeignKey('WorkOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='production_runs', help_text="Linked Work Order blueprint for this production run. Optional — link a Work Order to sync recipe and material allocations.")
    employee = models.ManyToManyField('Employee', blank=True, related_name='production_runs', help_text="Employees assigned to this production run.")
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.01'))], help_text="Quantity to be produced in this specific run.")
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, validators=[MinValueValidator(Decimal('0.00'))], help_text="Manufacturing cost per unit for this specific batch.")
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='IN_PROGRESS', db_index=True)
    notes = models.TextField(blank=True, null=True, help_text="Any issues or notes during this production run.")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when the production run was marked COMPLETED.")

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
            try:
                self.product = self.work_order.product
            except ObjectDoesNotExist:
                pass

        # Normalize status string for case-insensitive checks
        current_status = (self.status or '').upper().strip()

        # PRODUCT & WORK ORDER CONSTRAINTS
        if self.product and self.product.product_type not in ['Finished Goods', 'FINISHED', 'INTERMEDIATE']:
            raise ValidationError({
                'product': "Only products designated as 'Finished Goods' or 'Intermediate' can be selected for a production run."
            })

        if self.product and self.work_order_id:
            try:
                wo = self.work_order
                if wo and wo.product != self.product:
                    raise ValidationError({
                        'work_order': (
                            f"Conflict: The selected Work Order ({wo}) is for '{wo.product}', "
                            f"but this Production Order is set to produce '{self.product}'."
                        )
                    })
            except ObjectDoesNotExist:
                pass

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
            if self.work_order_id:
                try:
                    if self.work_order and self.work_order.bill_of_material:
                        bom = self.work_order.bill_of_material
                except ObjectDoesNotExist:
                    pass
            if not bom and self.product:
                bom = self.product.boms.filter(is_active=True).first()

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

        # Sets Timestamps based on status
        if self.status == 'COMPLETED':
            if not self.completed_at:
                self.completed_at = timezone.now()
        else:
            self.completed_at = None

        with transaction.atomic():
            super().save(*args, **kwargs)

            # Assign M2M Employees (Requires self.pk to exist)
            if is_new and self.work_order_id:
                try:
                    wo = self.work_order
                    if wo and hasattr(wo, 'employee'):
                        self.employee.set(wo.employee.all())
                except ObjectDoesNotExist:
                    pass

            # Sync WorkOrder inventory processing from ProductionOrder target quantity.
            # GUARD: Only call process_inventory() when a ProductionOrder is saved
            # OUTSIDE the admin WorkOrder save flow. During admin saves, the
            # WorkOrderAdmin.save_related() method is the canonical call site and
            # handles process_inventory() after inlines are committed.
            # We detect the admin flow by checking for the _skip_po_inventory_sync
            # flag set by WorkOrder.save().
            if self.work_order_id:
                try:
                    wo = self.work_order
                    if wo and not getattr(wo, '_skip_po_inventory_sync', False):
                        wo.refresh_from_db()
                        wo.process_inventory()
                except ObjectDoesNotExist:
                    pass

            # Non-inventory completion logic (ensure this method does NOT update stock!)
            if is_transitioning_to_completed:
                self.complete_production()
                
    def __str__(self):
        code = self.production_order_code or f"POC-{self.production_order_id:04d}"
        wo_code = "Unlinked (No WO)"
        if self.work_order_id:
            try:
                wo = self.work_order
                if wo:
                    wo_code = getattr(wo, 'work_order_code', f"WO-{self.work_order_id}")
            except ObjectDoesNotExist:
                wo_code = f"WO-{self.work_order_id}"
        return f"{code} ({self.get_status_display()}) - Blueprint: {wo_code}"            
class DocumentSequence(models.Model):
    document_type = models.CharField(max_length=32, unique=True, help_text="Unique key for the document type (e.g. 'SALES_INVOICE', 'CREDIT_NOTE', 'SALES_ORDER').")
    prefix = models.CharField(max_length=16, help_text="Prefix used in generated codes (e.g. 'SINV', 'CN', 'SO').")
    last_sequence = models.PositiveIntegerField(default=0, help_text="Monotonically increasing sequence counter.")

    @classmethod
    def get_next_number(cls, doc_type: str, default_prefix: str) -> str:
        """
        Uses select_for_update() inside an atomic transaction to increment and
        return formatted sequential strings: f"{prefix}-{timezone.now().strftime('%Y%m')}-{seq:04d}".
        """
        with transaction.atomic():
            seq_obj, _ = cls.objects.select_for_update().get_or_create(
                document_type=doc_type,
                defaults={'prefix': default_prefix, 'last_sequence': 0}
            )
            seq_obj.last_sequence += 1
            seq_obj.save(update_fields=['last_sequence'])
            
            year_month = timezone.now().strftime('%Y%m')
            prefix = seq_obj.prefix or default_prefix
            return f"{prefix}-{year_month}-{seq_obj.last_sequence:04d}"

    def __str__(self):
        return f"Sequence: {self.document_type} ({self.prefix}) - Last: {self.last_sequence}"

class Customer(models.Model):
    customer_id = models.AutoField(primary_key=True)    
    customer_name = models.CharField(max_length=255)
    contact_info = models.TextField()
    shipping_address = models.TextField()

    @property
    def invoices(self):
        return self.sales_invoices

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
    INVOICING_POLICY_CHOICES = [
        ('ORDER_BASED', 'Invoice on Order Confirmation (Advance/Upfront)'),
        ('DELIVERY_BASED', 'Invoice on Goods Dispatch (Post-Shipment)'),
    ]
    
    order_number = models.CharField(max_length=50, unique=True, editable=False, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    invoicing_policy = models.CharField(
        max_length=20,
        choices=INVOICING_POLICY_CHOICES,
        default='ORDER_BASED',
        help_text="Controls whether invoices are generated upon order confirmation or upon physical dispatch."
    )
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default='draft', editable=False, db_index=True, help_text="Automated state machine based on items and dispatch progress.")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
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
        # Auto-generate order number atomically via DocumentSequence
        if not self.order_number:
            self.order_number = DocumentSequence.get_next_number('SALES_ORDER', 'SO')

        super().save(*args, **kwargs)

        if self.pk and self.status != 'cancelled':
            self.update_status(save=True)

    def confirm_and_generate_invoice(self):
        """
        Confirms the Sales Order (transitions to 'approved' if currently 'draft')
        and generates an associated SalesInvoice with itemized lines if invoicing_policy is 'ORDER_BASED'.
        If 'DELIVERY_BASED', confirms the order without generating an upfront invoice.
        Idempotent: Re-calling on an already invoiced order returns the existing invoice without duplicate creations.
        """
        if self.status == 'cancelled':
            raise ValidationError("Cannot confirm a cancelled Sales Order.")

        if not self.items.exists():
            raise ValidationError("Cannot confirm a Sales Order with no items.")

        with transaction.atomic():
            SalesOrder.objects.select_for_update().get(pk=self.pk)

            # Idempotency Guard: Return existing active invoice if already created
            existing_invoice = self.invoices.filter(
                status__in=['DRAFT', 'POSTED', 'PARTIALLY_PAID', 'PAID']
            ).first()
            if existing_invoice:
                if self.status != 'approved':
                    self.status = 'approved'
                    SalesOrder.objects.filter(pk=self.pk).update(status='approved')
                return existing_invoice

            if self.status == 'draft':
                self.status = 'approved'
                SalesOrder.objects.filter(pk=self.pk).update(status='approved')

            if self.invoicing_policy == 'DELIVERY_BASED':
                return None

            invoice = SalesInvoice.objects.create(
                sales_order=self,
                customer=self.customer,
                invoice_date=timezone.now().date(),
                status='POSTED'
            )

            for item in self.items.all():
                SalesInvoiceLine.objects.create(
                    invoice=invoice,
                    sales_order_item=item,
                    product=item.product,
                    quantity=item.quantity_ordered,
                    unit_price=item.unit_price,
                    tax_rate=Decimal('0.00'),
                    tax_amount=Decimal('0.00'),
                    subtotal=item.total_price,
                    total_price=item.total_price
                )

            invoice.recalculate_totals(save=True)
            invoice._sync_finance_entry()

        return invoice

    class Meta:
        permissions = [
            ('can_confirm_sales_order', 'Can confirm sales orders and generate invoices'),
        ]

    def __str__(self):
        return f"{self.order_number} - {self.customer.customer_name} ({self.get_status_display()})"

class SalesOrderItem(models.Model):
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('Product', on_delete=models.PROTECT, limit_choices_to={'product_type__in': ['FINISHED', 'INTERMEDIATE']})  
    quantity_ordered = models.DecimalField(max_digits=10, decimal_places=2)
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Frozen transaction unit price at order creation. Does not mutate with catalog price updates."
    )
    quantity_dispatched = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    @property
    def quantity(self):
        return self.quantity_ordered

    @property
    def total_price(self):
        qty = Decimal(str(self.quantity_ordered or '0.00'))
        price = Decimal(str(self.unit_price or '0.00'))
        return (qty * price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    def update_dispatched_quantity(self):
        """Recalculates the exact sum of all related shipped/delivered dispatches from the ground truth."""
        total = self.dispatch_records.filter(status__in=['shipped', 'delivered']).aggregate(
            total_dispatched=Sum('quantity_dispatched')
        )['total_dispatched'] or Decimal('0.00')
        
        if self.quantity_dispatched != total:
            self.quantity_dispatched = total
            self.save(update_fields=['quantity_dispatched'])

    def save(self, *args, **kwargs):
        # Auto-freeze unit_price from catalog selling price on creation if not explicitly set
        if (self.unit_price == Decimal('0.00') or self.unit_price is None) and self.product and self.product.selling_price is not None:
            self.unit_price = self.product.selling_price

        super().save(*args, **kwargs)
        if self.sales_order_id:
            self.sales_order.update_status(save=True)

    def delete(self, *args, **kwargs):
        so = self.sales_order
        super().delete(*args, **kwargs)
        if so and so.pk:
            so.update_status(save=True)

    def __str__(self):
        return f"Item: {self.product.name} ({self.quantity_dispatched}/{self.quantity_ordered} Dispatched @ ${self.unit_price})"

class DispatchRecord(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending / Preparing'),
        ('shipped', 'Shipped / In Transit'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
    ]
    dispatch_id = models.AutoField(primary_key=True)  
    dispatch_code = models.CharField(max_length=30, unique=True, editable=False, blank=True, null=True, help_text="System-generated unique dispatch code (e.g. DISP-0001).")
    customer = models.ForeignKey('Customer', on_delete=models.PROTECT, related_name='dispatch_records', null=True, blank=True, help_text="Customer receiving this dispatch (must match Sales Order customer).")
    sales_order_item = models.ForeignKey('SalesOrderItem', on_delete=models.PROTECT, related_name='dispatch_records', limit_choices_to={'sales_order__status__in': ['draft', 'approved', 'partially_dispatched']}, help_text="Only active, non-completed sales order items can be selected for dispatch.")
    product = models.ForeignKey('Product', on_delete=models.PROTECT, related_name='dispatches', limit_choices_to={'product_type': 'FINISHED'}, help_text="Only finished goods can be selected for dispatch.")  
    quantity_dispatched = models.DecimalField(max_digits=10, decimal_places=2)
    dispatch_date = models.DateField(default=timezone.now, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    delivery_date = models.DateField(blank=True, null=True, editable=False)
    is_stock_deducted = models.BooleanField(default=False, editable=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._orig_quantity = self.quantity_dispatched if self.pk else Decimal('0.00')
        self._orig_status = self.status if self.pk else None
    
    def clean(self):
        super().clean()

        if self.sales_order_item:
            so = self.sales_order_item.sales_order
            expected_product = self.sales_order_item.product

            if so:
                # 1. Disallow dispatching to completed or cancelled sales orders for new dispatches
                if not self.pk and so.status in ['completed', 'cancelled']:
                    raise ValidationError({
                        'sales_order_item': f"Cannot create dispatch for Sales Order #{so.order_number} because it has already been {so.get_status_display()}."
                    })

                # 2. Prevent adding a customer that does not match the sales order customer
                if self.customer and self.customer != so.customer:
                    raise ValidationError({
                        'customer': f"Customer '{self.customer.customer_name}' does not match Customer '{so.customer.customer_name}' assigned to Sales Order #{so.order_number}."
                    })

            # 3. Prevent adding a product that does not match the sales order item product & suggest matching product
            if self.product and expected_product and self.product != expected_product:
                raise ValidationError({
                    'product': f"Product Mismatch: The selected product '{self.product.name}' (SKU: {self.product.sku}) "
                               f"does not match the product in Sales Order #{so.order_number if so else ''}. "
                               f"Suggested matching product: '{expected_product.name}' (SKU: {expected_product.sku})."
                })
        
        if not self.product or not self.quantity_dispatched or not self.sales_order_item:
            return

        was_deducted_before = self._orig_status in ['shipped', 'delivered']
        is_deducting_now = self.status in ['shipped', 'delivered']
        
        if is_deducting_now:
            if was_deducted_before:
                diff = self.quantity_dispatched - self._orig_quantity
            else:
                diff = self.quantity_dispatched
            
            if diff > Decimal('0.00') and self.product:
                inventory = Inventory.objects.filter(product=self.product).first()
                current_available = inventory.quantity_available if inventory else Decimal('0.00')
                
                if diff > current_available:
                    raise ValidationError({
                        'quantity_dispatched': f"Insufficient stock available! You requested {self.quantity_dispatched} "
                                               f"(Net addition of +{diff}), but only {current_available} units of "
                                               f"'{self.product.name}' are currently available in inventory."
                    })

    def save(self, *args, **kwargs):
        # Auto-populate customer from Sales Order if missing
        if self.sales_order_item and self.sales_order_item.sales_order and not self.customer:
            self.customer = self.sales_order_item.sales_order.customer

        # Auto-populate product from Sales Order Item if missing
        if self.sales_order_item and self.sales_order_item.product and not self.product:
            self.product = self.sales_order_item.product
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
        was_deducted_before = self._orig_status in ['shipped', 'delivered']
        is_deducting_now = self.status in ['shipped', 'delivered']

        becoming_deducted = is_deducting_now and not was_deducted_before
        leaving_deducted = not is_deducting_now and was_deducted_before
        quantity_changed = is_deducting_now and was_deducted_before and (self.quantity_dispatched != self._orig_quantity)

        if is_deducting_now:
            self.is_stock_deducted = True
            if self.status == 'delivered' and not self.delivery_date:
                self.delivery_date = timezone.now().date()
            elif self.status != 'delivered':
                self.delivery_date = None
        else:
            self.is_stock_deducted = False
            self.delivery_date = None

        self.full_clean()

        with transaction.atomic():
            super().save(*args, **kwargs)

            if becoming_deducted:
                self._apply_stock_change(self.quantity_dispatched)
            elif leaving_deducted:
                self._apply_stock_change(-self._orig_quantity)
            elif quantity_changed:
                diff = self.quantity_dispatched - self._orig_quantity
                self._apply_stock_change(diff)

            self._sync_parent_order_status()

            # Delivery-Based Invoicing on Dispatch Fulfillment
            if is_deducting_now and self.sales_order_item and self.sales_order_item.sales_order:
                so = self.sales_order_item.sales_order
                if so.invoicing_policy == 'DELIVERY_BASED':
                    existing_inv = SalesInvoice.objects.filter(dispatch=self).first()
                    if not existing_inv:
                        invoice = SalesInvoice.objects.create(
                            sales_order=so,
                            dispatch=self,
                            customer=self.customer or so.customer,
                            invoice_date=timezone.now().date(),
                            status='POSTED'
                        )
                        SalesInvoiceLine.objects.create(
                            invoice=invoice,
                            sales_order_item=self.sales_order_item,
                            product=self.product,
                            quantity=self.quantity_dispatched,
                            unit_price=self.sales_order_item.unit_price,
                            tax_rate=Decimal('0.00'),
                            tax_amount=Decimal('0.00'),
                            subtotal=self.quantity_dispatched * self.sales_order_item.unit_price,
                            total_price=self.quantity_dispatched * self.sales_order_item.unit_price
                        )
                        invoice.recalculate_totals(save=True)
                        invoice._sync_finance_entry()

            # Refresh original tracked values for instance reuse
            self._orig_quantity = self.quantity_dispatched
            self._orig_status = self.status

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
            status__in=['shipped', 'delivered']
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
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('POSTED', 'Posted/Issued'),
        ('PARTIALLY_PAID', 'Partially Paid'),
        ('PAID', 'Paid'),
        ('CREDITED', 'Credited'),
        ('CANCELLED', 'Cancelled'),
    ]
    invoice_id = models.AutoField(primary_key=True)
    invoice_number = models.CharField(max_length=255, unique=True, editable=False, blank=True, help_text="Unique identifier for the invoice. Auto-generated.")
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.PROTECT, null=True, blank=True, related_name='invoices', help_text="Originating sales order.")
    customer = models.ForeignKey('Customer', on_delete=models.PROTECT, null=True, blank=True, related_name='sales_invoices')
    dispatch = models.ForeignKey('DispatchRecord', on_delete=models.PROTECT, null=True, blank=True, related_name='sales_invoices')
    invoice_date = models.DateField(default=timezone.now, db_index=True)
    due_date = models.DateField(null=True, blank=True, help_text="Payment due date.")
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), help_text="Net invoice amount before taxes.")
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), help_text="Total tax assessed on this invoice.")
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, editable=False, default=Decimal('0.00'), help_text="Grand total (Subtotal + Tax).")
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='DRAFT', db_index=True)

    def clean(self):  
        super().clean()     
        if self.subtotal is not None:
            self.subtotal = Decimal(str(self.subtotal)).quantize(
                Decimal('0.01'), 
                rounding=ROUND_HALF_UP
            )
        if self.tax_amount is not None:
            self.tax_amount = Decimal(str(self.tax_amount)).quantize(
                Decimal('0.01'), 
                rounding=ROUND_HALF_UP
            )
        if self.total_amount is not None:
            self.total_amount = Decimal(str(self.total_amount)).quantize(
                Decimal('0.01'), 
                rounding=ROUND_HALF_UP
            )
        if self.dispatch and self.dispatch.dispatch_date and self.invoice_date and self.invoice_date < self.dispatch.dispatch_date:
            raise ValidationError({'invoice_date': 'Invoice date cannot be earlier than the physical dispatch date.'}) 

    def save(self, *args, **kwargs):
        # Auto-calculate total amount based on dispatch volume and selling price if dispatch present and no lines exist
        if self.dispatch and self.dispatch.product and not self.pk:
            price = self.dispatch.product.selling_price or Decimal('0.00')
            if price == Decimal('0.00') and hasattr(self.dispatch.product, 'stock'):
                inv = self.dispatch.product.stock.first()
                if inv and inv.unit_cost:
                    price = inv.unit_cost
            self.subtotal = (self.dispatch.quantity_dispatched or Decimal('0.00')) * price
            self.total_amount = self.subtotal

        if self.subtotal is not None:
            self.subtotal = Decimal(str(self.subtotal)).quantize(
                Decimal('0.01'), 
                rounding=ROUND_HALF_UP
            )
        if self.tax_amount is not None:
            self.tax_amount = Decimal(str(self.tax_amount)).quantize(
                Decimal('0.01'), 
                rounding=ROUND_HALF_UP
            )
        if self.total_amount is not None:
            self.total_amount = Decimal(str(self.total_amount)).quantize(
                Decimal('0.01'), 
                rounding=ROUND_HALF_UP
            )
       
        if not self.invoice_number:
            self.invoice_number = DocumentSequence.get_next_number('SALES_INVOICE', 'SINV')

        self.full_clean()    
        super().save(*args, **kwargs)
        if self.pk:
            self._sync_finance_entry()

    def _sync_finance_entry(self):
        """Auto-posts/updates revenue entry in General Ledger (FinanceEntry) for posted invoices."""
        if self.status in ['POSTED', 'PAID', 'PARTIALLY_PAID'] and self.total_amount and self.total_amount > Decimal('0.00'):
            entry_date = self.invoice_date or timezone.now().date()
            FinanceEntry.objects.update_or_create(
                sales_invoice=self,
                category='SALES',
                entry_type='REVENUE',
                defaults={
                    'amount': self.total_amount,
                    'entry_date': entry_date
                }
            )

    @property
    def payments(self):
        return self.sales_payments

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
            new_status = 'PAID'
        elif paid > Decimal('0.00'):
            new_status = 'PARTIALLY_PAID'
        else:
            new_status = 'DRAFT' if self.status in ['DRAFT', 'Unpaid', 'POSTED'] else self.status

        if self.status != new_status:
            self.status = new_status
            if save and self.pk:
                SalesInvoice.objects.filter(pk=self.pk).update(status=new_status)
        return new_status

    def recalculate_totals(self, save=True):
        """Recalculates subtotal, tax_amount, and total_amount based on child SalesInvoiceLine items."""
        lines = list(self.lines.all())
        if lines:
            self.subtotal = sum(Decimal(str(l.subtotal or '0.00')) for l in lines).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            self.tax_amount = sum(Decimal(str(l.tax_amount or '0.00')) for l in lines).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            self.total_amount = sum(Decimal(str(l.total_price or '0.00')) for l in lines).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if save and self.pk:
            SalesInvoice.objects.filter(pk=self.pk).update(
                subtotal=self.subtotal,
                tax_amount=self.tax_amount,
                total_amount=self.total_amount
            )
            self._sync_finance_entry()

    def __str__(self):
        return f"Sales Invoice #{self.invoice_number} — ${self.total_amount:.2f} ({self.customer.customer_name if self.customer else 'N/A'}) [{self.get_status_display()}]"

class SalesInvoiceLine(models.Model):
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name='lines')
    sales_order_item = models.ForeignKey(SalesOrderItem, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoice_lines')
    product = models.ForeignKey('Product', on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.00'))])
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), help_text="Tax percentage (e.g. 16.00 for 16% VAT).")
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    def save(self, *args, **kwargs):
        qty = Decimal(str(self.quantity or '0.00'))
        price = Decimal(str(self.unit_price or '0.00'))
        rate = Decimal(str(self.tax_rate or '0.00'))

        self.subtotal = (qty * price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        self.tax_amount = (self.subtotal * (rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        self.total_price = (self.subtotal + self.tax_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        self.full_clean()
        super().save(*args, **kwargs)
        if self.invoice_id:
            self.invoice.recalculate_totals(save=True)

    def __str__(self):
        return f"Invoice Line: {self.product.name} ({self.quantity} @ ${self.unit_price})"

class CreditNote(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('POSTED', 'Posted'),
        ('REFUNDED', 'Refunded'),
    ]
    credit_note_id = models.AutoField(primary_key=True)
    credit_note_number = models.CharField(max_length=64, unique=True, editable=False, blank=True)
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.PROTECT, related_name='credit_notes')
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='credit_notes', null=True, blank=True)
    issue_date = models.DateField(default=timezone.now, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT', db_index=True)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    reason = models.CharField(max_length=255, blank=True, default='')

    def save(self, *args, **kwargs):
        if not self.credit_note_number:
            self.credit_note_number = DocumentSequence.get_next_number('CREDIT_NOTE', 'CN')
        if not self.customer and self.invoice and self.invoice.customer:
            self.customer = self.invoice.customer
        self.full_clean()
        super().save(*args, **kwargs)
        if self.pk:
            self._sync_finance_entry()

    def _sync_finance_entry(self):
        """Auto-posts/updates refund expense entry in General Ledger (FinanceEntry) for posted credit notes."""
        if self.status in ['POSTED', 'REFUNDED'] and self.total_amount and self.total_amount > Decimal('0.00') and self.invoice_id:
            entry_date = self.issue_date or timezone.now().date()
            FinanceEntry.objects.update_or_create(
                sales_invoice=self.invoice,
                category='CUSTOMER_REFUND',
                entry_type='EXPENSE',
                defaults={
                    'amount': self.total_amount,
                    'entry_date': entry_date
                }
            )

    def recalculate_totals(self, save=True):
        lines = list(self.lines.all())
        if lines:
            self.subtotal = sum(Decimal(str(l.subtotal or '0.00')) for l in lines).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            self.tax_amount = sum(Decimal(str(l.tax_amount or '0.00')) for l in lines).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            self.total_amount = sum(Decimal(str(l.total_price or '0.00')) for l in lines).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            self.subtotal = Decimal('0.00')
            self.tax_amount = Decimal('0.00')
            self.total_amount = Decimal('0.00')
        if save and self.pk:
            CreditNote.objects.filter(pk=self.pk).update(
                subtotal=self.subtotal,
                tax_amount=self.tax_amount,
                total_amount=self.total_amount
            )
            self._sync_finance_entry()

    def __str__(self):
        return f"Credit Note #{self.credit_note_number} — ${self.total_amount:.2f} ({self.get_status_display()})"

class CreditNoteLine(models.Model):
    credit_note = models.ForeignKey(CreditNote, on_delete=models.CASCADE, related_name='lines')
    product = models.ForeignKey('Product', on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.00'))])
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), help_text="Tax percentage.")
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    def save(self, *args, **kwargs):
        qty = Decimal(str(self.quantity or '0.00'))
        price = Decimal(str(self.unit_price or '0.00'))
        rate = Decimal(str(self.tax_rate or '0.00'))

        self.subtotal = (qty * price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        self.tax_amount = (self.subtotal * (rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        self.total_price = (self.subtotal + self.tax_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        self.full_clean()
        super().save(*args, **kwargs)
        if self.credit_note_id:
            self.credit_note.recalculate_totals(save=True)

    def __str__(self):
        return f"CN Line: {self.product.name} ({self.quantity} @ ${self.unit_price})"

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
                # Auto-post payment finance entry
                payment_date = self.paid_at.date() if (self.paid_at and hasattr(self.paid_at, 'date')) else timezone.now().date()
                FinanceEntry.objects.create(
                    sales_invoice=self.invoice,
                    entry_type='REVENUE',
                    category='SALES',
                    amount=self.amount,
                    entry_date=payment_date
                )

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
    invoice_date = models.DateField(default=timezone.now, db_index=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False, default=Decimal('0.00'))

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='UNPAID', db_index=True)
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
        total = self.payments.aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')
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
                latest = self.payments.order_by('-paid_at').first()
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
    PAYMENT_METHOD_CHOICES = [
        ('CASH', 'Cash'),
        ('TRANSFER', 'Bank Transfer'),
        ('CHEQUE', 'Cheque'),
    ]

    payment_id = models.AutoField(primary_key=True)
    purchase_invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default='TRANSFER')
    paid_at = models.DateTimeField(default=timezone.now)
    reference_number = models.CharField(max_length=100, blank=True, null=True, default='')

    def clean(self):
        super().clean()
        if self.purchase_invoice_id and self.amount:
            already_paid = self.purchase_invoice.payments.exclude(pk=self.pk).aggregate(
                total=models.Sum('amount')
            )['total'] or Decimal('0.00')
            remaining = (self.purchase_invoice.total_amount or Decimal('0.00')) - already_paid
            if self.amount > remaining:
                raise ValidationError({
                    'amount': f"Payment of ${self.amount} exceeds remaining bill balance of ${remaining:.2f}."
                })

        if self.payment_method in ['TRANSFER', 'CHEQUE'] and not self.reference_number:
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

    def save(self, *args, **kwargs):
        # 1. Run model clean data validations
        self.full_clean()

        # 2. Detect historical state to prevent duplicate stock updates on multiple edits
        previously_approved = False
        if self.pk:
            previously_approved = Return.objects.filter(pk=self.pk, quality_control_status='APPROVED').exists()

        # 3. Execute database operations in a single, safe atomic pass
        with transaction.atomic():
            super().save(*args, **kwargs)

            # If return is approved, adjust inventory and issue CreditNote
            if self.quality_control_status == 'APPROVED' and not previously_approved:
                product = self.dispatch.product if self.dispatch else None

                # Locate the originating billed SalesInvoice
                invoice_record = None
                if self.dispatch:
                    invoice_record = SalesInvoice.objects.filter(dispatch=self.dispatch).first()
                    if not invoice_record and self.dispatch.sales_order_item and self.dispatch.sales_order_item.sales_order:
                        invoice_record = self.dispatch.sales_order_item.sales_order.invoices.first()

                # Determine unit price billed (customer selling price snapshot, not factory cost)
                unit_price_billed = Decimal('0.00')
                if self.dispatch and self.dispatch.sales_order_item and self.dispatch.sales_order_item.unit_price:
                    unit_price_billed = self.dispatch.sales_order_item.unit_price
                elif product and product.selling_price:
                    unit_price_billed = product.selling_price

                # Step A: Issue CreditNote and CreditNoteLine
                if invoice_record and product:
                    credit_note = CreditNote.objects.create(
                        invoice=invoice_record,
                        customer=self.customer or invoice_record.customer,
                        issue_date=timezone.now().date(),
                        status='POSTED',
                        reason=self.reason_for_return or 'Customer RMA Return'
                    )
                    CreditNoteLine.objects.create(
                        credit_note=credit_note,
                        product=product,
                        quantity=self.quantity_returned,
                        unit_price=unit_price_billed,
                        tax_rate=Decimal('0.00'),
                        tax_amount=Decimal('0.00'),
                        subtotal=self.quantity_returned * unit_price_billed,
                        total_price=self.quantity_returned * unit_price_billed
                    )
                    credit_note.recalculate_totals(save=True)
                    credit_note._sync_finance_entry()

                # Step B: Return physical items back into warehouse inventory and log transaction
                if product:
                    inventory_item, created = Inventory.objects.get_or_create(
                        product=product,
                        location=self.return_warehouse_location or 'Main Warehouse',
                        defaults={'quantity_available': Decimal('0.00')}
                    )
                    inventory_item.quantity_available += self.quantity_returned
                    inventory_item.save(update_fields=['quantity_available'])

                    StockTransaction.objects.create(
                        product=product,
                        dispatch_record=self.dispatch,
                        quantity=self.quantity_returned,
                        transaction_type='ADJUSTMENT',
                        notes=f"RMA Return #{self.return_id} Restock: {self.reason_for_return}"
                    )

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
    entry_type = models.CharField(max_length=10, choices=ENTRY_TYPE_CHOICES, default='EXPENSE', db_index=True)
    category = models.CharField(max_length=20, choices=ENTRY_CATEGORY_CHOICES, default='SALES', db_index=True)
    procurement_order = models.ForeignKey('ProcurementOrder', on_delete=models.PROTECT, null=True, blank=True, related_name='financial_entries')
    sales_invoice = models.ForeignKey('SalesInvoice', on_delete=models.PROTECT, null=True, blank=True, related_name='financial_entries')
    material_variance = models.ForeignKey('MaterialVarianceRecord', on_delete=models.PROTECT, null=True, blank=True, related_name='financial_entries')
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.00'))], help_text="Amount must be a positive amount greater than zero.")
    entry_date = models.DateField(db_index=True)

    def clean(self):
        # 1. Prevent negative entries from skewing totals
        if self.entry_type == 'REVENUE' and self.category in ['PROCUREMENT', 'LOSS', 'LABOR', 'OVERHEAD']:
            raise ValidationError(f"A Revenue entry cannot be categorized under {self.get_category_display()}.")
        if self.entry_type == 'EXPENSE' and self.category == 'SALES':
            raise ValidationError("An Expense entry cannot be categorized under Sales Revenue.")
        if self.material_variance and (self.entry_type != 'EXPENSE' and self.category != 'LOSS'):
            raise ValidationError("Entries tied to a Material Variance Record must be set as an Expense under the Loss category.")
    
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"[{self.get_entry_type_display()}] ${self.amount} — {self.get_category_display()} ({self.entry_date})"
