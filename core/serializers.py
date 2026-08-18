"""
CORE SERIALIZERS MODULE

Provides object-to-dictionary serialization, data formatting, and input 
validation/deserialization for core ERP domain models.
"""

from decimal import Decimal
from django.core.exceptions import ValidationError
from .models import (
    Product, Inventory, WorkOrder, WorkOrderMaterialLine, WorkOrderInstruction,
    ProductionOrder, SalesOrder, SalesOrderItem, ProcurementOrder, PurchaseOrder,
    DispatchRecord, SalesInvoice, FinanceEntry, MaterialVarianceRecord, StockTransaction, Supplier, Customer
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


class SalesOrderSerializer(BaseSerializer):
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
            "status": obj.status,
            "created_at": obj.created_at.isoformat() if obj.created_at else None,
            "order_total": float(order_total),
            "items": [
                {
                    "item_id": item.pk,
                    "product_id": item.product_id,
                    "product_name": item.product.name if item.product else "",
                    "quantity_ordered": float(item.quantity_ordered or 0.0),
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
