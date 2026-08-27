"""
MRP SERVICES MODULE (core/services/mrp_services.py)
Handles BOM explosion, shortage evaluation, auto-drafting POs, and MRP state management.
"""

from collections import defaultdict
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from core.models import (
    Product, BillOfMaterial, BOMItem, Inventory, ProductionOrder, ProductionOrderItem,
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
    Inspects ProductionOrderItem.resolution_status to preserve saved resolutions.
    """
    if not production_order or not getattr(production_order, 'work_order', None):
        return []

    work_order = production_order.work_order
    if getattr(work_order, 'status', '').upper() in ['IN_PROGRESS', 'COMPLETED']:
        return []

    bom = work_order.bill_of_material
    if not bom:
        return []

    target_qty = getattr(production_order, 'quantity', Decimal('0.00')) or Decimal('0.00')
    shortage_report = []

    # Map existing ProductionOrderItem records by raw_material_id
    saved_items = {
        item.raw_material_id: item
        for item in production_order.items.select_related('raw_material', 'linked_purchase_order').all()
    } if production_order.pk else {}

    for item in bom.items.select_related('component'):
        component = item.component
        item_req = item.quantity_required or Decimal('0.00')
        total_needed = (item_req * target_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        total_available = Inventory.objects.filter(
            product=component
        ).aggregate(
            total=Sum('quantity_available')
        )['total'] or Decimal('0.00')

        shortfall = max(Decimal('0.00'), total_needed - total_available)

        saved_item = saved_items.get(component.pk)
        is_resolved = False
        resolution_status = 'UNRESOLVED'
        linked_po = None

        if saved_item:
            resolution_status = saved_item.resolution_status
            linked_po = saved_item.linked_purchase_order
            if resolution_status in ['PO_DRAFTED', 'OVERRIDDEN', 'DOWNSCALED', 'RESOLVED', 'NO_SHORTAGE']:
                is_resolved = True

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
            'has_shortage': shortfall > Decimal('0.00') and not is_resolved,
            'is_resolved': is_resolved,
            'resolution_status': resolution_status,
            'linked_po': linked_po,
            'inbound_po_qty': inbound_po_qty,
            'active_run_qty': active_run_qty,
            'max_producible': max_producible,
            'supplier': getattr(component, 'supplier', None),
        })

    return shortage_report


# =========================================================================
# RESOLUTION PATHWAY HANDLERS FOR RAW MATERIALS
# =========================================================================

def resolve_raw_autodraft_po(production_order, component_id, shortfall_qty):
    """
    OPTION 1: Auto-Draft PO
    Looks up Product.supplier and appends shortfall to an open PurchaseOrder (or creates a new draft).
    Updates the corresponding ProductionOrderItem with PO_DRAFTED.
    """
    component = Product.objects.get(pk=component_id)
    if not component.supplier:
        raise ValidationError(f"Cannot auto-draft Purchase Order: Product '{component.name}' has no assigned supplier.")

    shortfall = Decimal(str(shortfall_qty))
    with transaction.atomic():
        po = PurchaseOrder.objects.filter(
            supplier=component.supplier,
            status='DRAFT'
        ).first()

        if not po:
            po = PurchaseOrder.objects.create(
                supplier=component.supplier,
                status='DRAFT',
                notes=f"Auto-drafted by MRP Trigger Engine for Production Order #{production_order.pk}."
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
            po_item.save()

        # Guarantee PO status remains DRAFT
        if po.status != 'DRAFT':
            po.status = 'DRAFT'
            po.save(update_fields=['status'])

        if production_order and production_order.pk:
            order_item, _ = ProductionOrderItem.objects.get_or_create(
                production_order=production_order,
                raw_material=component,
                defaults={
                    'planned_quantity': shortfall,
                    'shortage_quantity': shortfall,
                    'resolution_status': 'UNRESOLVED'
                }
            )
            order_item.resolve_with_po(purchase_order=po)

            note_msg = f"[MRP RESOLVED] Appended {shortfall} units of {component.name} to Draft PO #{po.po_number}."
            production_order.notes = f"{production_order.notes or ''}\n{note_msg}".strip()
            production_order.save(update_fields=['notes'])

    po.refresh_from_db()
    return po


def resolve_raw_hold_inbound(production_order, component_id, inbound_po=None):
    """
    OPTION 2: Hold for Inbound Stock
    Binds the shortage item to an open incoming Purchase Order already in transit / drafted.
    """
    component = Product.objects.get(pk=component_id)
    
    if isinstance(inbound_po, (int, str)):
        inbound_po = PurchaseOrder.objects.filter(pk=inbound_po).first()

    if not inbound_po:
        inbound_po = PurchaseOrder.objects.filter(
            items__product=component,
            status__in=['SENT', 'PARTIAL', 'DRAFT']
        ).first()

    if not inbound_po:
        raise ValidationError(
            f"No active inbound Purchase Orders (Sent, Partial, Draft) found for '{component.name}'. "
            f"Please draft a new Purchase Order instead."
        )

    with transaction.atomic():
        order_item, _ = ProductionOrderItem.objects.get_or_create(
            production_order=production_order,
            raw_material=component,
            defaults={
                'planned_quantity': Decimal('0.00'),
                'shortage_quantity': Decimal('0.00'),
                'resolution_status': 'UNRESOLVED'
            }
        )

        order_item.resolve_with_po(
            purchase_order=inbound_po,
            notes=f"Holding for inbound delivery on PO #{inbound_po.po_number or inbound_po.pk}."
        )
        msg = f"[MRP HELD] Awaiting inbound stock for {component.name} from PO #{inbound_po.po_number or inbound_po.pk}."

        production_order.notes = f"{production_order.notes or ''}\n{msg}".strip()
        production_order.save(update_fields=['notes'])

    return production_order


def resolve_raw_direct_procurement(production_order, component_id, shortfall_qty):
    """
    OPTION 3: Direct Fast-Track Procurement
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
            order_item, _ = ProductionOrderItem.objects.get_or_create(
                production_order=production_order,
                raw_material=component,
                defaults={
                    'planned_quantity': shortfall,
                    'shortage_quantity': shortfall,
                    'resolution_status': 'UNRESOLVED'
                }
            )
            order_item.resolution_status = 'RESOLVED'
            order_item.resolution_notes = f"Fast-Track Procurement Order #{procurement.procurement_order_id} ({shortfall} units)."
            order_item.resolved_at = timezone.now()
            order_item.save()
            production_order.update_mrp_resolution_state()

            note_msg = f"[MRP RESOLVED] Spawned Fast-Track Procurement Order #{procurement.procurement_order_id} ({shortfall} units)."
            production_order.notes = f"{production_order.notes or ''}\n{note_msg}".strip()
            production_order.save(update_fields=['notes'])

    return procurement


# =========================================================================
# UNIVERSAL BATCH DOWNSCALE & SUPERVISOR OVERRIDE
# =========================================================================

def resolve_batch_downscale(production_order, bottleneck_component_id):
    """
    Calculates maximum producible yield based on available stock of the bottleneck component,
    scales down work_order target quantity and production_order.quantity, recalculates all
    ProductionOrderItem.planned_quantity rows, and marks the bottleneck item as DOWNSCALED.
    """
    from decimal import ROUND_HALF_UP
    component = Product.objects.get(pk=bottleneck_component_id)
    avail = Inventory.objects.filter(product=component).aggregate(
        total=Sum('quantity_available')
    )['total'] or Decimal('0.00')

    bom = None
    if production_order.work_order and production_order.work_order.bill_of_material:
        bom = production_order.work_order.bill_of_material
    elif production_order.product:
        bom = production_order.product.boms.filter(is_active=True).first()

    req_per_unit = Decimal('1.00')
    if bom:
        bom_item = bom.items.filter(component=component).first()
        if bom_item and bom_item.quantity_required and bom_item.quantity_required > Decimal('0.00'):
            req_per_unit = bom_item.quantity_required

    max_producible = (avail / req_per_unit).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if max_producible <= Decimal('0.00'):
        raise ValidationError(f"Cannot downscale batch: Available inventory for '{component.name}' is zero.")

    with transaction.atomic():
        production_order.quantity = max_producible
        note_msg = f"[MRP RESOLVED] Down-scaled production batch target to {max_producible} units based on available stock of {component.name}."
        production_order.notes = f"{production_order.notes or ''}\n{note_msg}".strip()
        production_order.save(update_fields=['quantity', 'notes'])

        if production_order.work_order_id and getattr(production_order, 'work_order', None):
            wo = production_order.work_order
            wo.quantity_produced = max_producible
            wo.save(update_fields=['quantity_produced'])

        # Recalculate MRP for all components under new batch size
        production_order.evaluate_mrp()

        # Flag the bottleneck component specifically as DOWNSCALED
        b_item = ProductionOrderItem.objects.filter(
            production_order=production_order,
            raw_material=component
        ).first()
        if b_item:
            b_item.resolution_status = 'DOWNSCALED'
            b_item.shortage_quantity = Decimal('0.00')
            b_item.resolution_notes = f"Batch target downscaled to {max_producible} units based on available stock ({avail:.2f} available)."
            b_item.resolved_at = timezone.now()
            b_item.save(update_fields=['resolution_status', 'shortage_quantity', 'resolution_notes', 'resolved_at'])

        production_order.update_mrp_resolution_state()
        production_order.refresh_from_db()

    return production_order


def resolve_intermediate_partial_batch(production_order, max_producible_qty):
    """Backwards-compatible alias for partial batch run."""
    new_qty = Decimal(str(max_producible_qty)).quantize(Decimal('0.01'))
    if new_qty <= Decimal('0.00'):
        raise ValidationError("Cannot adjust batch size: Available component inventory is zero.")

    with transaction.atomic():
        production_order.quantity = new_qty
        note_msg = f"[MRP RESOLVED] Down-scaled production batch target to {new_qty} units based on available component stock."
        production_order.notes = f"{production_order.notes or ''}\n{note_msg}".strip()
        production_order.save(update_fields=['quantity', 'notes'])

        if production_order.work_order_id and getattr(production_order, 'work_order', None):
            production_order.work_order.quantity_produced = new_qty
            production_order.work_order.save(update_fields=['quantity_produced'])

        for item in production_order.items.all():
            if item.resolution_status == 'UNRESOLVED':
                item.resolution_status = 'DOWNSCALED'
                item.resolution_notes = f"Downscaled production batch target to {new_qty} units."
                item.resolved_at = timezone.now()
                item.save(update_fields=['resolution_status', 'resolution_notes', 'resolved_at'])

        production_order.evaluate_mrp()

    return production_order


def resolve_item_override(production_order, component_id, user=None, notes=""):
    """Authorizes supervisor override for the shortage item."""
    component = Product.objects.get(pk=component_id)
    order_item = ProductionOrderItem.objects.filter(
        production_order=production_order,
        raw_material=component
    ).first()
    if not order_item:
        order_item = ProductionOrderItem.objects.create(
            production_order=production_order,
            raw_material=component,
            planned_quantity=Decimal('0.00'),
            shortage_quantity=Decimal('0.00'),
            resolution_status='UNRESOLVED'
        )
    return order_item.resolve_with_override(user=user, notes=notes)


# =========================================================================
# RESOLUTION PATHWAY HANDLERS FOR INTERMEDIATE SUB-ASSEMBLIES
# =========================================================================

def resolve_intermediate_build(production_order, component_id, shortfall_qty):
    """
    OPTION 1: Build Sub-Assembly
    Spawns a child WorkOrder and ProductionOrder with quantity = shortfall.
    Sets po_item.resolution_status = 'CHILD_WO_CREATED'.
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
            status='PENDING'
        )

        child_po = ProductionOrder.objects.create(
            product=component,
            work_order=child_wo,
            quantity=shortfall,
            status='PENDING',
            notes=f"Auto-generated sub-assembly run for parent Production Order #{production_order.pk}."
        )

        if production_order and production_order.pk:
            order_item, _ = ProductionOrderItem.objects.get_or_create(
                production_order=production_order,
                raw_material=component,
                defaults={
                    'planned_quantity': shortfall,
                    'shortage_quantity': shortfall,
                    'resolution_status': 'UNRESOLVED'
                }
            )
            order_item.resolution_status = 'CHILD_WO_CREATED'
            order_item.resolution_notes = f"Spawned child Sub-Assembly Run #{child_po.pk} (WO-{child_wo.pk}) for {shortfall} units of {component.name}."
            order_item.resolved_at = timezone.now()
            order_item.save(update_fields=['resolution_status', 'resolution_notes', 'resolved_at'])
            production_order.update_mrp_resolution_state()

            note_msg = f"[MRP RESOLVED] Spawned child Sub-Assembly Run #{child_po.pk} (WO-{child_wo.pk}) for {shortfall} units of {component.name}."
            production_order.notes = f"{production_order.notes or ''}\n{note_msg}".strip()
            production_order.save(update_fields=['notes'])

    return child_wo, child_po


def resolve_intermediate_hold_active(production_order, component_id, active_po=None):
    """
    OPTION 2: Hold for Active Run
    Links the parent order to an active intermediate run currently IN_PROGRESS on the floor.
    Sets po_item.resolution_status = 'HOLD_ACTIVE_RUN'.
    """
    component = Product.objects.get(pk=component_id)
    if isinstance(active_po, (int, str)):
        active_po = ProductionOrder.objects.filter(pk=active_po).first()

    if not active_po:
        active_po = ProductionOrder.objects.filter(
            product=component,
            status='IN_PROGRESS'
        ).exclude(pk=production_order.pk if production_order else None).first()

    if not active_po:
        raise ValidationError(
            f"No active shop floor runs (IN_PROGRESS) found for '{component.name}'. "
            f"Please trigger a Child Work Order instead."
        )

    run_code = active_po.production_order_code or f"#{active_po.pk}"
    msg = f"[MRP HELD] Linked to active shop floor run {run_code} for {component.name}."

    with transaction.atomic():
        if production_order and production_order.pk:
            order_item, _ = ProductionOrderItem.objects.get_or_create(
                production_order=production_order,
                raw_material=component,
                defaults={
                    'planned_quantity': Decimal('0.00'),
                    'shortage_quantity': Decimal('0.00'),
                    'resolution_status': 'UNRESOLVED'
                }
            )
            order_item.resolution_status = 'HOLD_ACTIVE_RUN'
            order_item.resolution_notes = msg
            order_item.resolved_at = timezone.now()
            order_item.save(update_fields=['resolution_status', 'resolution_notes', 'resolved_at'])
            production_order.update_mrp_resolution_state()

        production_order.notes = f"{production_order.notes or ''}\n{msg}".strip()
        production_order.save(update_fields=['notes'])

    return production_order


# =========================================================================
# EVENT-DRIVEN AUTO-RESUME SIGNALS
# =========================================================================

def check_and_auto_resume_on_hold_orders(product=None):
    """
    Scans ProductionOrders currently in ON_HOLD_SHORTAGE, PARTIALLY_RESOLVED, or AWAITING_PROCUREMENT.
    Re-evaluates physical inventory against planned quantities for PO_DRAFTED items.
    If physical unallocated inventory is now sufficient, updates item resolution_status = 'NO_SHORTAGE'.
    If all items are satisfied, order status automatically transitions to READY_TO_START / MRP_RESOLVED.
    """
    target_orders = ProductionOrder.objects.filter(
        status__in=['ON_HOLD_SHORTAGE', 'PARTIALLY_RESOLVED', 'AWAITING_PROCUREMENT']
    )
    if product:
        target_orders = target_orders.filter(
            work_order__bill_of_material__items__component=product
        ).distinct()

    resumed_orders = []
    for po in target_orders:
        with transaction.atomic():
            # Check PO_DRAFTED items for physical goods arrival
            for item in po.items.filter(resolution_status='PO_DRAFTED'):
                available_unallocated = Inventory.objects.filter(
                    product=item.raw_material
                ).aggregate(total=Sum('quantity_available'))['total'] or Decimal('0.00')
                if available_unallocated >= item.planned_quantity:
                    item.resolution_status = 'NO_SHORTAGE'
                    item.shortage_quantity = Decimal('0.00')
                    item.save(update_fields=['resolution_status', 'shortage_quantity'])

            # Re-evaluate MRP shortages (preserves saved resolutions)
            po.evaluate_mrp()
            po.update_mrp_resolution_state()
            po.refresh_from_db()

            if po.status in ['READY_TO_START', 'MRP_RESOLVED'] and not po.has_unresolved_shortages:
                resume_note = f"[MRP AUTO-RESUMED] Stock sufficiency verified. Order status transitioned to {po.get_status_display()}."
                po.notes = f"{po.notes or ''}\n{resume_note}".strip()
                po.save(update_fields=['notes'])
                resumed_orders.append(po)

    return resumed_orders
