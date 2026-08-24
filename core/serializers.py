"""
CORE SERIALIZERS MODULE

Provides object-to-dictionary serialization, data formatting, and input 
validation/deserialization for core ERP domain models.
"""

from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .models import (
    Product, Inventory, WorkOrder, WorkOrderMaterialLine, WorkOrderInstruction,
    ProductionOrder, SalesOrder, SalesOrderItem, ProcurementOrder, PurchaseOrder,
    DispatchRecord, SalesInvoice, FinanceEntry, MaterialVarianceRecord, StockTransaction,
    Supplier, Customer, DocumentSequence, SalesInvoiceLine, CreditNote, CreditNoteLine
)


class BaseSerializer:
    """
    Base Serializer class providing generic collection serialization 
    and serialization helper methods.
    """
    @classmethod
    def serialize(cls, instance):
        if instance is None:
            return None
        return cls._serialize_instance(instance)

    @classmethod
    def serialize_queryset(cls, queryset):
        return [cls._serialize_instance(obj) for obj in queryset]


class ProductSerializer(BaseSerializer):
    """Serializes Product instances and validates creation payloads."""

    @classmethod
    def _serialize_instance(cls, obj: Product):
        stock_item = obj.stock.first()
        return {
            "product_id": obj.product_id,
            "sku": obj.sku,
            "name": obj.name,
            "product_type": obj.product_type,
            "category": obj.category,
            "unit_of_measurement": obj.unit_of_measurement,
            "selling_price": float(obj.selling_price) if obj.selling_price is not None else None,
            "quantity_available": float(stock_item.quantity_available) if stock_item else 0.0,
            "unit_cost": float(stock_item.unit_cost) if stock_item else 0.0,
            "supplier_id": obj.supplier_id,
            "supplier_name": obj.supplier.name if obj.supplier else "N/A"
        }

    @classmethod
    def validate_and_deserialize(cls, data):
        """Validates incoming product dictionary payload."""
        errors = {}
        name = data.get("name", "").strip()
        product_type = data.get("product_type", "FINISHED").upper()
        category = data.get("category", "General").strip()
        unit_of_measurement = data.get("unit_of_measurement", "pcs").strip()
        supplier_id = data.get("supplier_id")
        selling_price = data.get("selling_price")

        if not name:
            errors["name"] = ["Product name cannot be blank."]

        if product_type not in ['RAW', 'INTERMEDIATE', 'FINISHED']:
            errors["product_type"] = ["Invalid product type classification."]

        if product_type in ['FINISHED', 'INTERMEDIATE'] and supplier_id:
            errors["supplier_id"] = ["Finished and intermediate goods cannot have an external supplier."]

        if selling_price is not None:
            try:
                price = Decimal(str(selling_price))
                if price < Decimal('0.00'):
                    errors["selling_price"] = ["Selling price cannot be negative."]
            except Exception:
                errors["selling_price"] = ["Invalid numerical value for selling price."]

        if errors:
            raise ValidationError(errors)

        return {
            "name": name,
            "product_type": product_type,
            "category": category or "General",
            "unit_of_measurement": unit_of_measurement or "pcs",
            "supplier_id": supplier_id if product_type == 'RAW' else None,
            "selling_price": Decimal(str(selling_price)) if (selling_price is not None and product_type in ['FINISHED', 'INTERMEDIATE']) else None
        }


class InventorySerializer(BaseSerializer):
    """Serializes Warehouse Stock Inventory levels."""

    @classmethod
    def _serialize_instance(cls, obj: Inventory):
        return {
            "inventory_id": obj.Inventory_id,
            "product_id": obj.product_id,
            "product_name": obj.product.name if obj.product else "",
            "sku": obj.product.sku if obj.product else "",
            "unit_cost": float(obj.unit_cost or 0.0),
            "quantity_available": float(obj.quantity_available or 0.0),
            "quantity_allocated": float(obj.quantity_allocated or 0.0),
            "location": obj.location,
            "total_valuation": float(obj.total_valuation or 0.0),
            "is_low_stock": obj.quantity_available <= Decimal('10.00'),
            "last_updated": obj.last_updated.isoformat() if obj.last_updated else None
        }


class WorkOrderSerializer(BaseSerializer):
    """Serializes Work Order operational blueprints, execution status, and material lines."""

    @classmethod
    def _serialize_instance(cls, obj: WorkOrder):
        return {
            "work_order_id": obj.work_order_id,
            "work_order_code": obj.work_order_code,
            "category": obj.category,
            "product_id": obj.product_id,
            "product_name": obj.product.name if obj.product else "",
            "product_sku": obj.product.sku if obj.product else "",
            "bill_of_material_id": obj.bill_of_material_id,
            "parent_work_order_id": obj.parent_work_order_id,
            "status": obj.status,
            "target_quantity": float(obj.target_quantity),
            "quantity_produced": float(obj.quantity_produced) if obj.quantity_produced is not None else None,
            "actual_quantity_produced": float(obj.actual_quantity_produced) if obj.actual_quantity_produced is not None else None,
            "production_start_date": str(obj.production_start_date) if obj.production_start_date else None,
            "production_end_date": obj.production_end_date.isoformat() if obj.production_end_date else None,
            "assigned_crew": [emp.employee_name for emp in obj.employee.all()],
            "material_lines": [
                {
                    "material_line_id": line.material_line_id,
                    "component_id": line.component_id,
                    "component_name": line.component.name if line.component else "",
                    "quantity_expected": float(line.quantity_expected or 0.0),
                    "quantity_actual": float(line.quantity_actual or 0.0)
                } for line in obj.material_lines.all()
            ]
        }


class ProductionOrderSerializer(BaseSerializer):
    """Serializes Production Run Orders."""

    @classmethod
    def _serialize_instance(cls, obj: ProductionOrder):
        return {
            "production_order_id": obj.production_order_id,
            "production_order_code": obj.production_order_code,
            "product_id": obj.product_id,
            "product_name": obj.product.name if obj.product else "",
            "work_order_id": obj.work_order_id,
            "work_order_code": obj.work_order.work_order_code if (obj.work_order_id and getattr(obj, 'work_order', None)) else "",
            "quantity": float(obj.quantity or 0.0),
            "unit_cost": float(obj.unit_cost or 0.0),
            "status": obj.status,
            "created_at": obj.created_at.isoformat() if obj.created_at else None,
            "completed_at": obj.completed_at.isoformat() if obj.completed_at else None
        }


class ProcurementOrderSerializer(BaseSerializer):
    """Serializes Procurement Inbound Delivery Orders."""

    @classmethod
    def _serialize_instance(cls, obj: ProcurementOrder):
        return {
            "procurement_order_id": obj.procurement_order_id,
            "purchase_order_id": obj.purchase_order_id,
            "po_number": obj.purchase_order.po_number if obj.purchase_order else "Direct",
            "product_id": obj.product_id,
            "product_name": obj.product.name if obj.product else "",
            "quantity": float(obj.quantity or 0.0),
            "price_per_unit": float(obj.price_per_unit or 0.0),
            "total_cost": float(obj.total_cost or 0.0),
            "order_date": str(obj.order_date) if obj.order_date else None,
            "delivery_date": obj.delivery_date.isoformat() if obj.delivery_date else None,
            "status": obj.status,
            "delivery_location": obj.delivery_location
        }


class LegacySalesOrderSerializer(BaseSerializer):
    """Serializes Customer Sales Orders and line items."""

    @classmethod
    def _serialize_instance(cls, obj: SalesOrder):
        items = obj.items.all()
        order_total = sum((item.total_price for item in items), Decimal('0.00'))
        return {
            "sales_order_id": obj.pk,
            "order_number": obj.order_number,
            "customer_id": obj.customer_id,
            "customer_name": obj.customer.customer_name if obj.customer else "",
            "invoicing_policy": obj.invoicing_policy,
            "status": obj.status,
            "created_at": obj.created_at.isoformat() if obj.created_at else None,
            "order_total": float(order_total),
            "items": [
                {
                    "item_id": item.pk,
                    "product_id": item.product_id,
                    "product_name": item.product.name if item.product else "",
                    "quantity_ordered": float(item.quantity_ordered or 0.0),
                    "unit_price": float(item.unit_price or 0.0),
                    "total_price": float(item.total_price or 0.0)
                } for item in items
            ]
        }


class DispatchRecordSerializer(BaseSerializer):
    """Serializes Finished Goods Dispatch Shipment Records."""

    @classmethod
    def _serialize_instance(cls, obj: DispatchRecord):
        return {
            "dispatch_id": obj.dispatch_id,
            "dispatch_code": obj.dispatch_code,
            "customer_name": obj.customer.customer_name if obj.customer else "",
            "product_name": obj.product.name if obj.product else "",
            "quantity_dispatched": float(obj.quantity_dispatched or 0.0),
            "dispatch_date": str(obj.dispatch_date) if obj.dispatch_date else None,
            "status": obj.status,
            "delivery_date": str(obj.delivery_date) if obj.delivery_date else None,
            "is_stock_deducted": obj.is_stock_deducted
        }


class MaterialVarianceRecordSerializer(BaseSerializer):
    """Serializes Material Variance Records including production run type."""

    @classmethod
    def _serialize_instance(cls, obj: MaterialVarianceRecord):
        return {
            "variance_id": obj.variance_id,
            "variance_code": obj.variance_code,
            "production_run_type": obj.production_run_type,
            "work_order_id": obj.work_order_id,
            "work_order_code": obj.work_order.work_order_code if obj.work_order else "",
            "product_id": obj.product_id,
            "product_name": obj.product.name if obj.product else "",
            "quantity_expected": float(obj.quantity_expected or 0.0),
            "quantity_actual": float(obj.quantity_actual or 0.0),
            "quantity_variance": float(obj.quantity_variance or 0.0),
            "unit_cost": float(obj.unit_cost or 0.0),
            "financial_impact": float(obj.financial_impact or 0.0),
            "variance_percentage": float(obj.variance_percentage or 0.0),
            "efficiency_rate": float(obj.efficiency_rate or 0.0),
            "variance_classification": obj.variance_classification,
            "recorded_at": obj.recorded_at.isoformat() if obj.recorded_at else None,
            "notes": obj.notes or ""
        }


class StockTransactionSerializer(BaseSerializer):
    """Serializes Inventory Stock Transactions including work order code."""

    @classmethod
    def _serialize_instance(cls, obj: StockTransaction):
        return {
            "transaction_id": obj.transaction_id,
            "product_id": obj.product_id,
            "product_sku": obj.product.sku if obj.product else "",
            "product_name": obj.product.name if obj.product else "",
            "work_order_id": obj.work_order_id,
            "work_order_code": obj.work_order_code,
            "dispatch_record_id": obj.dispatch_record_id,
            "quantity": float(obj.quantity or 0.0),
            "transaction_type": obj.transaction_type,
            "transaction_type_display": obj.get_transaction_type_display(),
            "notes": obj.notes or "",
            "created_at": obj.created_at.isoformat() if obj.created_at else None
        }


# =============================================================================
# DJANGO REST FRAMEWORK (DRF) SERIALIZERS
# =============================================================================

from rest_framework import serializers


class MaterialLogSerializer(serializers.Serializer):
    """
    Validates shop-floor operator real-time material consumption logging payloads.
    """
    component_id = serializers.IntegerField(required=True)
    quantity_actual = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=True,
        min_value=Decimal('0.00')
    )


class WorkOrderCompletionSerializer(serializers.Serializer):
    """
    Validates work order completion and scrap logging payloads.
    """
    actual_quantity_produced = serializers.DecimalField(
        required=True,
        min_value=Decimal('0.00'),
        max_digits=12,
        decimal_places=2
    )
    scrap_quantity = serializers.DecimalField(
        required=False,
        default=Decimal('0.00'),
        min_value=Decimal('0.00'),
        max_digits=12,
        decimal_places=2
    )
    scrap_reason = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
        default=''
    )


class MaterialLineDRFSerializer(serializers.ModelSerializer):
    """
    DRF Serializer for WorkOrderMaterialLine instances.
    """
    component = serializers.StringRelatedField(read_only=True)
    component_id = serializers.IntegerField(read_only=True)
    quantity_allocated = serializers.DecimalField(max_digits=12, decimal_places=4, read_only=True)
    quantity_deducted = serializers.DecimalField(source='deducted_quantity', max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = WorkOrderMaterialLine
        fields = [
            'id', 'component', 'component_id', 'quantity_expected',
            'quantity_actual', 'quantity_allocated', 'quantity_deducted'
        ]


class WorkOrderDetailDRFSerializer(serializers.ModelSerializer):
    """
    DRF Serializer for WorkOrder instances with nested material lines.
    """
    id = serializers.IntegerField(source='pk', read_only=True)
    product = serializers.StringRelatedField(read_only=True)
    target_quantity = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    actual_quantity_produced = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    scrap_quantity = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    scrap_reason = serializers.CharField(read_only=True)
    production_end_date = serializers.DateTimeField(read_only=True)
    yield_percentage = serializers.SerializerMethodField()
    material_lines = MaterialLineDRFSerializer(many=True, read_only=True)

    class Meta:
        model = WorkOrder
        fields = [
            'id', 'work_order_code', 'category', 'product', 'status',
            'target_quantity', 'actual_quantity_produced',
            'scrap_quantity', 'scrap_reason', 'yield_percentage',
            'production_start_date', 'production_end_date', 'material_lines'
        ]

    def get_yield_percentage(self, obj):
        target = obj.target_quantity
        actual = obj.actual_quantity_produced
        if target and target > Decimal('0.00') and actual is not None:
            pct = (actual / target) * Decimal('100.00')
            return str(pct.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
        return None


# =============================================================================
# SALES & BILLING SUBSYSTEM DRF SERIALIZERS (MILESTONE 3)
# =============================================================================

class SalesOrderItemSerializer(serializers.ModelSerializer):
    """
    DRF Serializer for SalesOrderItem lines.
    Freezes unit_price from catalog if not explicitly specified.
    """
    id = serializers.IntegerField(source='pk', read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    unit_price = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        min_value=Decimal('0.00')
    )
    total_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = SalesOrderItem
        fields = [
            'id', 'product', 'product_name', 'quantity_ordered',
            'unit_price', 'total_price'
        ]


class SalesInvoiceLineSerializer(serializers.ModelSerializer):
    """
    DRF Serializer for SalesInvoiceLine items.
    """
    id = serializers.IntegerField(source='pk', read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    tax_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    subtotal = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    total_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = SalesInvoiceLine
        fields = [
            'id', 'product', 'product_name', 'quantity', 'unit_price',
            'tax_rate', 'tax_amount', 'subtotal', 'total_price'
        ]


class CreditNoteLineSerializer(serializers.ModelSerializer):
    """
    DRF Serializer for CreditNoteLine items.
    """
    id = serializers.IntegerField(source='pk', read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    tax_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    subtotal = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    total_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = CreditNoteLine
        fields = [
            'id', 'product', 'product_name', 'quantity', 'unit_price',
            'tax_rate', 'tax_amount', 'subtotal', 'total_price'
        ]


class SalesInvoicePaymentPayloadSerializer(serializers.Serializer):
    """
    Validates payment collection payloads for sales invoices.
    """
    amount = serializers.DecimalField(
        required=True,
        min_value=Decimal('0.01'),
        max_digits=12,
        decimal_places=2
    )
    payment_method = serializers.ChoiceField(
        choices=['CASH', 'BANK_TRANSFER', 'CHEQUE', 'CREDIT_CARD', 'CARD', 'TRANSFER'],
        default='BANK_TRANSFER'
    )
    reference = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=128,
        default=''
    )


class CreditNoteCreatePayloadSerializer(serializers.Serializer):
    """
    Validates manual credit note creation payloads.
    """
    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
        default=''
    )
    lines = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        default=list
    )


class SalesOrderSerializer(serializers.ModelSerializer):
    """
    DRF Serializer for SalesOrder instances with nested line items.
    Supports atomic nested creation of lines with catalog price freezing.
    """
    id = serializers.IntegerField(source='pk', read_only=True)
    order_number = serializers.CharField(read_only=True)
    customer_name = serializers.CharField(source='customer.customer_name', read_only=True)
    order_date = serializers.SerializerMethodField(read_only=True)
    status = serializers.CharField(read_only=True)
    total_amount = serializers.SerializerMethodField(read_only=True)
    items = SalesOrderItemSerializer(many=True, required=False)

    class Meta:
        model = SalesOrder
        fields = [
            'id', 'order_number', 'customer', 'customer_name', 'order_date',
            'status', 'invoicing_policy', 'total_amount', 'items'
        ]

    def get_order_date(self, obj):
        return obj.created_at.date() if obj.created_at else None

    def get_total_amount(self, obj):
        items = obj.items.all()
        return sum((item.total_price for item in items), Decimal('0.00'))

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        with transaction.atomic():
            sales_order = SalesOrder.objects.create(**validated_data)
            for item_data in items_data:
                product = item_data.get('product')
                quantity_ordered = item_data.get('quantity_ordered')
                unit_price = item_data.get('unit_price')
                if unit_price is None or unit_price == Decimal('0.00'):
                    unit_price = product.selling_price if product and product.selling_price is not None else Decimal('0.00')
                SalesOrderItem.objects.create(
                    sales_order=sales_order,
                    product=product,
                    quantity_ordered=quantity_ordered,
                    unit_price=unit_price
                )
            sales_order.update_status(save=True)
        return sales_order


class SalesInvoiceSerializer(serializers.ModelSerializer):
    """
    DRF Serializer for SalesInvoice instances with itemized lines.
    """
    id = serializers.IntegerField(source='invoice_id', read_only=True)
    sales_order_number = serializers.CharField(source='sales_order.order_number', read_only=True, default='')
    customer_name = serializers.CharField(source='customer.customer_name', read_only=True, default='')
    lines = SalesInvoiceLineSerializer(many=True, read_only=True)

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_number', 'sales_order', 'sales_order_number',
            'customer', 'customer_name', 'invoice_date', 'due_date',
            'status', 'subtotal', 'tax_amount', 'total_amount', 'lines'
        ]


class CreditNoteSerializer(serializers.ModelSerializer):
    """
    DRF Serializer for CreditNote instances with itemized lines.
    """
    id = serializers.IntegerField(source='credit_note_id', read_only=True)
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True, default='')
    customer_name = serializers.CharField(source='customer.customer_name', read_only=True, default='')
    lines = CreditNoteLineSerializer(many=True, read_only=True)

    class Meta:
        model = CreditNote
        fields = [
            'id', 'credit_note_number', 'invoice', 'invoice_number',
            'customer', 'customer_name', 'issue_date', 'status',
            'subtotal', 'tax_amount', 'total_amount', 'reason', 'lines'
        ]


class CustomerSerializer(serializers.ModelSerializer):
    """
    DRF Serializer for Customer instances.
    Exposes customer attributes and contact details.
    """
    id = serializers.IntegerField(source='customer_id', read_only=True)
    name = serializers.CharField(source='customer_name', required=False)
    email = serializers.SerializerMethodField()
    phone_number = serializers.SerializerMethodField()
    address = serializers.CharField(source='shipping_address', required=False)
    created_at = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = [
            'id', 'name', 'customer_name', 'email', 'phone_number',
            'contact_info', 'address', 'shipping_address', 'created_at'
        ]

    def get_email(self, obj):
        info = obj.contact_info or ''
        if '@' in info:
            return info
        return ''

    def get_phone_number(self, obj):
        info = obj.contact_info or ''
        if '@' not in info and info:
            return info
        return ''

    def get_created_at(self, obj):
        return None


class BulkPaymentAllocationPayloadSerializer(serializers.Serializer):
    """
    Validates customer lump-sum bulk payment allocation request payloads.
    """
    amount = serializers.DecimalField(
        required=True,
        min_value=Decimal('0.01'),
        max_digits=12,
        decimal_places=2
    )
    payment_method = serializers.ChoiceField(
        choices=['CASH', 'BANK_TRANSFER', 'CHEQUE', 'CREDIT_CARD', 'CARD', 'TRANSFER'],
        default='BANK_TRANSFER'
    )
    reference = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=128,
        default=''
    )
    payment_date = serializers.DateField(
        required=False,
        default=timezone.now
    )


class InvoiceAllocationItemSerializer(serializers.Serializer):
    """
    Serializes individual invoice allocation slice in allocation preview and confirmation results.
    """
    invoice_id = serializers.IntegerField()
    invoice_number = serializers.CharField()
    invoice_date = serializers.DateField()
    total_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    already_paid = serializers.DecimalField(max_digits=12, decimal_places=2)
    balance_before = serializers.DecimalField(max_digits=12, decimal_places=2)
    allocated_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    balance_after = serializers.DecimalField(max_digits=12, decimal_places=2)
    projected_status = serializers.CharField()


class BulkPaymentAllocationResponseSerializer(serializers.Serializer):
    """
    Serializes the aggregated response for customer bulk payment allocation (preview and atomic execution).
    """
    customer_id = serializers.IntegerField()
    customer_name = serializers.CharField()
    total_received = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_allocated = serializers.DecimalField(max_digits=12, decimal_places=2)
    unallocated_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    allocations = InvoiceAllocationItemSerializer(many=True)




