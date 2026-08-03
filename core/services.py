from collections import defaultdict
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from .models import (
    Product, BillOfMaterial, BOMItem, Inventory, ProductionOrder, 
    WorkOrder, PurchaseOrder, PurchaseOrderItem, ProcurementOrder
)

def explode_material_requirements(product, target_quantity=Decimal('1.0000'), visited=None):
    """
    Recursively flattens a multi-level Bill of Materials down to its 
    foundational raw materials and unconfigured sub-assemblies.
    """
    if visited is None:
        visited = set()
    
    # Emergency Loop Break: Safely halts if a circular loop slips through admin validation
    if product.pk in visited:
        raise ValidationError(
            f"Infinite Loop Recurrence Error: Circular dependency detected at '{product.name}'."
        )
    
    requirements = defaultdict(Decimal)
    
    # Grab the active recipe for this current item depth level
    active_bom = product.boms.filter(is_active=True).first()
    
    # Base Case: If it's a RAW item or has no active recipe, it's a foundational material
    if not active_bom or product.product_type == 'RAW':
        requirements[product] += Decimal(target_quantity)
        return requirements
    
    visited.add(product.pk)
    
    # Recursive Step: Dig through nested blueprints
    for item in active_bom.items.select_related('component'):
        sub_component = item.component
        extended_quantity = item.quantity_required * Decimal(target_quantity)
        
        # Keep digging down the branch
        sub_requirements = explode_material_requirements(
            product=sub_component, 
            target_quantity=extended_quantity, 
            visited=visited.copy()
        )
        
        # Roll the child totals back up into our local accumulator
        for component_product, accumulated_qty in sub_requirements.items():
            requirements[component_product] += accumulated_qty
            
    return requirements


def evaluate_mrp_shortages(production_order):
    """
    Evaluates material line requirements for a ProductionOrder or WorkOrder.
    Returns a list of shortage analysis dicts per component line.
    """
    if not production_order or not getattr(production_order, 'work_order', None):
        return []

    work_order = production_order.work_order
    bom = work_order.bill_of_material
    if not bom:
        return []

    target_qty = getattr(production_order, 'quantity', Decimal('0.00')) or Decimal('0.00')
    shortage_report = []

    for item in bom.items.select_related('component'):
        component = item.component
        item_req = item.quantity_required or Decimal('0.00')
        total_needed = item_req * target_qty

        total_available = Inventory.objects.filter(
            product=component
        ).aggregate(
            total=Sum('quantity_available')
        )['total'] or Decimal('0.00')

        shortfall = max(Decimal('0.00'), total_needed - total_available)

        # Check for open inbound PurchaseOrders for Raw Materials
        inbound_po_qty = Decimal('0.00')
        if component.product_type == 'RAW':
            inbound_po_qty = PurchaseOrderItem.objects.filter(
                product=component,
                purchase_order__status__in=['SENT', 'PARTIAL']
            ).aggregate(total=Sum('quantity_ordered'))['total'] or Decimal('0.00')

        # Check for active ProductionOrders for Intermediate Goods
        active_run_qty = Decimal('0.00')
        if component.product_type == 'INTERMEDIATE':
            active_run_qty = ProductionOrder.objects.filter(
                product=component,
                status='IN_PROGRESS'
            ).aggregate(total=Sum('quantity'))['total'] or Decimal('0.00')

        # Calculate max producible units of parent good with currently available component stock
        max_producible = (total_available / item_req) if item_req > 0 else target_qty

        shortage_report.append({
            'component': component,
            'required_qty': total_needed,
            'available_qty': total_available,
            'shortfall_qty': shortfall,
            'product_type': component.product_type,
            'has_shortage': shortfall > Decimal('0.00'),
            'inbound_po_qty': inbound_po_qty,
            'active_run_qty': active_run_qty,
            'max_producible': max_producible,
            'supplier': getattr(component, 'supplier', None),
        })

    return shortage_report


# =========================================================================
# RESOLUTION PATHWAY HANDLERS FOR RAW MATERIALS (3 OPTIONS)
# =========================================================================

def resolve_raw_autodraft_po(production_order, component_id, shortfall_qty):
    """
    OPTION 1: Auto-Draft PO
    Looks up Product.supplier and appends shortfall to an open DRAFT PurchaseOrder (or creates a new draft).
    """
    component = Product.objects.get(pk=component_id)
    if not component.supplier:
        raise ValidationError(f"Cannot auto-draft Purchase Order: Product '{component.name}' has no assigned supplier.")

    shortfall = Decimal(str(shortfall_qty))
    with transaction.atomic():
        po, created = PurchaseOrder.objects.get_or_create(
            supplier=component.supplier,
            status='DRAFT',
            defaults={
                'notes': f"Auto-drafted by MRP Trigger Engine for Production Order #{production_order.pk}."
            }
        )

        po_item, item_created = PurchaseOrderItem.objects.get_or_create(
            purchase_order=po,
            product=component,
            defaults={
                'quantity_ordered': shortfall,
                'price_per_unit': Decimal('0.00')
            }
        )

        if not item_created:
            po_item.quantity_ordered += shortfall
            po_item.save(update_fields=['quantity_ordered'])

        if production_order and production_order.pk:
            note_msg = f"[MRP RESOLVED] Appended {shortfall} units of {component.name} to DRAFT PO #{po.po_number}."
            production_order.notes = f"{production_order.notes or ''}\n{note_msg}".strip()
            production_order.save(update_fields=['notes'])

    return po


def resolve_raw_direct_procurement(production_order, component_id, shortfall_qty):
    """
    OPTION 2: Direct Procurement
    Spawns a ProcurementOrder set to PENDING for fast-tracking delivery.
    """
    component = Product.objects.get(pk=component_id)
    shortfall = Decimal(str(shortfall_qty))
    with transaction.atomic():
        procurement = ProcurementOrder.objects.create(
            product=component,
            quantity=shortfall,
            price_per_unit=Decimal('0.00'),
            order_date=timezone.now().date(),
            status='PENDING',
            delivery_location='Main Warehouse'
        )

        if production_order and production_order.pk:
            note_msg = f"[MRP RESOLVED] Spawned Fast-Track Procurement Order #{procurement.procurement_order_id} ({shortfall} units)."
            production_order.notes = f"{production_order.notes or ''}\n{note_msg}".strip()
            production_order.save(update_fields=['notes'])

    return procurement


def resolve_raw_hold_inbound(production_order, component_id):
    """
    OPTION 3: Hold for Inbound Stock
    Keeps order ON_HOLD_SHORTAGE to consume stock from an existing open PO already in transit.
    """
    component = Product.objects.get(pk=component_id)
    open_pos = PurchaseOrder.objects.filter(
        items__product=component,
        status__in=['SENT', 'PARTIAL']
    ).distinct()

    po_numbers = [po.po_number for po in open_pos]
    msg = f"[MRP HELD] Awaiting inbound stock for {component.name} from open POs: {', '.join(po_numbers) if po_numbers else 'None in transit'}."

    with transaction.atomic():
        production_order.status = 'ON_HOLD_SHORTAGE'
        production_order.notes = f"{production_order.notes or ''}\n{msg}".strip()
        production_order.save(update_fields=['status', 'notes'])

    return production_order


# =========================================================================
# RESOLUTION PATHWAY HANDLERS FOR INTERMEDIATE SUB-ASSEMBLIES (3 OPTIONS)
# =========================================================================

def resolve_intermediate_build(production_order, component_id, shortfall_qty):
    """
    OPTION 1: Build Sub-Assembly
    Spawns a child WorkOrder and ProductionOrder with quantity = shortfall.
    """
    component = Product.objects.get(pk=component_id)
    active_bom = component.boms.filter(is_active=True).first()
    if not active_bom:
        raise ValidationError(f"Cannot spawn sub-assembly: Product '{component.name}' has no active BOM recipe.")

    shortfall = Decimal(str(shortfall_qty))
    with transaction.atomic():
        child_wo = WorkOrder.objects.create(
            product=component,
            bill_of_material=active_bom,
            quantity_produced=shortfall,
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )

        child_po = ProductionOrder.objects.create(
            product=component,
            work_order=child_wo,
            quantity=shortfall,
            status='IN_PROGRESS',
            notes=f"Auto-generated sub-assembly run for parent Production Order #{production_order.pk}."
        )

        if production_order and production_order.pk:
            note_msg = f"[MRP RESOLVED] Spawned child Sub-Assembly Run #{child_po.pk} (WO-{child_wo.pk}) for {shortfall} units of {component.name}."
            production_order.notes = f"{production_order.notes or ''}\n{note_msg}".strip()
            production_order.save(update_fields=['notes'])

    return child_wo, child_po


def resolve_intermediate_hold_active(production_order, component_id):
    """
    OPTION 2: Hold for Active Run
    Links the parent order to an active intermediate run currently IN_PROGRESS on the floor.
    """
    component = Product.objects.get(pk=component_id)
    active_runs = ProductionOrder.objects.filter(
        product=component,
        status='IN_PROGRESS'
    ).exclude(pk=production_order.pk if production_order else None)

    run_ids = [f"#{po.pk}" for po in active_runs]
    msg = f"[MRP HELD] Linked to active shop floor runs for {component.name}: {', '.join(run_ids) if run_ids else 'No active runs'}."

    with transaction.atomic():
        production_order.status = 'ON_HOLD_SHORTAGE'
        production_order.notes = f"{production_order.notes or ''}\n{msg}".strip()
        production_order.save(update_fields=['status', 'notes'])

    return production_order


def resolve_intermediate_partial_batch(production_order, max_producible_qty):
    """
    OPTION 3: Partial Batch Run
    Adjusts parent production target down to match currently available stock.
    """
    new_qty = Decimal(str(max_producible_qty)).quantize(Decimal('0.01'))
    if new_qty <= Decimal('0.00'):
        raise ValidationError("Cannot adjust batch size: Available component inventory is zero.")

    with transaction.atomic():
        production_order.quantity = new_qty
        production_order.status = 'IN_PROGRESS'

        note_msg = f"[MRP RESOLVED] Down-scaled production batch target to {new_qty} units based on available component stock."
        production_order.notes = f"{production_order.notes or ''}\n{note_msg}".strip()
        production_order.save(update_fields=['quantity', 'status', 'notes'])

        if production_order.work_order:
            production_order.work_order.quantity_produced = new_qty
            production_order.work_order.save(update_fields=['quantity_produced'])

    return production_order


# =========================================================================
# EVENT-DRIVEN AUTO-RESUME SIGNALS
# =========================================================================

def check_and_auto_resume_on_hold_orders(product=None):
    """
    Scans all ProductionOrders currently ON_HOLD_SHORTAGE.
    If inventory for all required BOM material lines is now sufficient, auto-resumes to IN_PROGRESS.
    """
    on_hold_orders = ProductionOrder.objects.filter(status='ON_HOLD_SHORTAGE')
    if product:
        on_hold_orders = on_hold_orders.filter(work_order__bill_of_material__items__component=product).distinct()

    resumed_orders = []
    for po in on_hold_orders:
        report = evaluate_mrp_shortages(po)
        has_any_shortage = any(item['has_shortage'] for item in report)

        if not has_any_shortage and report:
            with transaction.atomic():
                po.status = 'IN_PROGRESS'
                resume_note = f"[MRP AUTO-RESUMED] Stock sufficiency restored. Order auto-resumed to IN_PROGRESS."
                po.notes = f"{po.notes or ''}\n{resume_note}".strip()
                po.save(update_fields=['status', 'notes'])
                resumed_orders.append(po)

    return resumed_orders