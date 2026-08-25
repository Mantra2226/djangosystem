"""
PRODUCTION RECONCILIATION INVARIANT ENGINE (core/services/production_reconciliation.py)

Domain: Glass Putty Manufacturing ERP (MES & Stock Ledger).
Guarantees atomic, all-or-nothing stock deduction for all BOM raw materials/packaging
components and finished goods output creation into the StockTransaction ledger.
"""

from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone


class ProductionReconciliationError(Exception):
    """Raised when production stock reconciliation invariants are violated."""
    pass


class ProductionReconciliationEngine:
    """
    Production Reconciliation Invariant Engine & Centralized Stock Consumption Service.
    
    Invariants Enforced:
    1. Idempotency & Safety Gate: Prevents double-execution and duplicate ledger records.
    2. Deterministic Atomic Locking: Locks all inventory rows in ascending product ID order.
    3. Multi-Material Completeness: Reconciles EVERY required BOM component atomically.
    4. StockTransaction Ledger Integrity: Creates explicit negative consumption transactions.
    5. Residual Allocation Release: Returns unused allocated stock back to available pool.
    6. Finished Goods Output Ledger: Creates explicit positive output transactions.
    7. Scrap-Adjusted AVCO & Cascade: Factors in scrap variance and cascades completion to POs.
    """

    @classmethod
    def reconcile_work_order_completion(cls, work_order):
        """
        Executes atomic Phase 3 reconciliation on WorkOrder completion.
        Returns a structured dictionary summarizing all ledger mutations.
        """
        if not work_order or not work_order.pk:
            raise ProductionReconciliationError("Invalid WorkOrder: Record must be saved in database before reconciliation.")

        # =========================================================================
        # INVARIANT 1: IDEMPOTENCY & SAFETY GATE
        # =========================================================================
        db_flags = work_order.__class__.objects.filter(pk=work_order.pk).values_list(
            'is_inventory_allocated', 'is_inventory_updated', 'status'
        ).first()

        if not db_flags:
            raise ProductionReconciliationError(f"WorkOrder #{work_order.pk} not found in database.")

        is_allocated, is_updated, current_status = db_flags
        if is_updated:
            print(f"[RECONCILIATION ENGINE] WorkOrder #{work_order.pk} already reconciled (is_inventory_updated=True). Skipping safely.", flush=True)
            return {
                'work_order_id': work_order.pk,
                'work_order_code': work_order.work_order_code,
                'status': current_status,
                'skipped': True,
                'message': "Already reconciled. No action taken."
            }

        from core.models import Inventory, StockTransaction, ProductionOrder

        summary = {
            'work_order_id': work_order.pk,
            'work_order_code': work_order.work_order_code,
            'consumed_components': [],
            'released_allocations': [],
            'finished_good_output': None,
            'stock_transactions': [],
            'unit_cost_avco': Decimal('0.00'),
        }

        with transaction.atomic():
            # =========================================================================
            # INVARIANT 2: DETERMINISTIC ATOMIC LOCKING & PACKAGING ORDER COMPATIBILITY
            # =========================================================================
            # Refresh and lock WorkOrder
            locked_wo = work_order.__class__.objects.select_for_update().get(pk=work_order.pk)
            
            # Ensure material lines exist (sync from active BOM if needed)
            locked_wo.sync_material_lines()
            material_lines = list(locked_wo.material_lines.select_related('component').all())

            # Gather all product IDs: all BOM / packaging components + finished product
            product_ids = set()
            for line in material_lines:
                if line.component_id:
                    product_ids.add(line.component_id)

            if locked_wo.product_id:
                product_ids.add(locked_wo.product_id)

            # Sort product IDs ascending to eliminate deadlock risks across concurrent runs
            sorted_product_ids = sorted(product_ids)

            # Acquire exclusive row locks on all required Inventory rows
            locked_inventories = {}
            existing_inventories = Inventory.objects.select_for_update().filter(product_id__in=sorted_product_ids)
            for inv in existing_inventories:
                locked_inventories[inv.product_id] = inv

            # Create inventory records for any missing products
            for pid in sorted_product_ids:
                if pid not in locked_inventories:
                    new_inv = Inventory.objects.create(
                        product_id=pid,
                        quantity_available=Decimal('0.00'),
                        quantity_allocated=Decimal('0.00'),
                    )
                    new_inv = Inventory.objects.select_for_update().get(pk=new_inv.pk)
                    locked_inventories[pid] = new_inv

            # =========================================================================
            # INVARIANT 3: MULTI-MATERIAL COMPLETENESS VERIFICATION
            # =========================================================================
            if locked_wo.bill_of_material:
                expected_bom_components = set(
                    locked_wo.bill_of_material.items.values_list('component_id', flat=True)
                )
                actual_line_components = set(line.component_id for line in material_lines)
                missing_components = expected_bom_components - actual_line_components
                if missing_components:
                    raise ProductionReconciliationError(
                        f"Completeness Invariant Violation: Missing required BOM components {missing_components} on WorkOrder #{locked_wo.pk}."
                    )

            # Total cost accumulator for AVCO calculation
            total_material_cost = Decimal('0.00')

            # =========================================================================
            # INVARIANT 4: ACCURATE CONSUMPTION DEDUCTIONS & STOCK LEDGER
            # =========================================================================
            for line in material_lines:
                comp = line.component
                raw_inv = locked_inventories.get(comp.pk)
                if not raw_inv:
                    raise ProductionReconciliationError(f"Inventory record missing for component '{comp.name}'.")

                actual_qty = line.quantity_actual or Decimal('0.00')
                already_deducted = line.deducted_quantity or Decimal('0.00')
                delta = actual_qty - already_deducted

                if delta > Decimal('0.00'):
                    old_alloc = raw_inv.quantity_allocated
                    old_avail = raw_inv.quantity_available

                    if raw_inv.quantity_allocated >= delta:
                        raw_inv.quantity_allocated -= delta
                    else:
                        excess = delta - raw_inv.quantity_allocated
                        raw_inv.quantity_allocated = Decimal('0.00')
                        raw_inv.quantity_available -= excess

                    raw_inv.save(update_fields=['quantity_available', 'quantity_allocated'])

                    st = StockTransaction.objects.create(
                        product=comp,
                        quantity=-delta,
                        transaction_type='PRODUCTION_CONSUMPTION',
                        work_order=locked_wo,
                        notes=f"Production reconciliation deduction of {delta} {comp.unit_of_measurement or 'units'} for Work Order #{locked_wo.pk} ({comp.name})"
                    )

                    line.deducted_quantity = actual_qty
                    line.save(update_fields=['deducted_quantity'])

                    summary['consumed_components'].append({
                        'component_id': comp.pk,
                        'component_name': comp.name,
                        'consumed_qty': delta,
                        'transaction_id': st.transaction_id,
                    })
                    summary['stock_transactions'].append(st.transaction_id)

                # Material cost calculation for AVCO
                comp_cost = comp.unit_cost if hasattr(comp, 'unit_cost') and comp.unit_cost else (raw_inv.unit_cost or Decimal('0.00'))
                total_material_cost += (actual_qty * comp_cost)

            # =========================================================================
            # INVARIANT 5: RESIDUAL ALLOCATION RELEASE
            # =========================================================================
            for line in material_lines:
                comp = line.component
                raw_inv = locked_inventories.get(comp.pk)
                already_deducted = line.deducted_quantity or Decimal('0.00')
                allocated_qty = line.quantity_allocated or Decimal('0.00')

                residual_allocated = max(Decimal('0.00'), allocated_qty - already_deducted)
                if residual_allocated > Decimal('0.00') and raw_inv:
                    released = min(residual_allocated, raw_inv.quantity_allocated)
                    raw_inv.quantity_allocated -= released
                    raw_inv.quantity_available += released
                    raw_inv.save(update_fields=['quantity_available', 'quantity_allocated'])

                    summary['released_allocations'].append({
                        'component_id': comp.pk,
                        'component_name': comp.name,
                        'released_qty': released,
                    })

            # =========================================================================
            # INVARIANT 6: FINISHED GOODS OUTPUT LEDGER
            # =========================================================================
            effective_qty = locked_wo.actual_quantity_produced if locked_wo.actual_quantity_produced is not None else locked_wo.target_quantity
            finished_qty = effective_qty or Decimal('0.00')

            if finished_qty > Decimal('0.00') and locked_wo.product:
                finished_prod = locked_wo.product
                finished_inv = locked_inventories.get(finished_prod.pk)
                old_qty = finished_inv.quantity_available or Decimal('0.00')
                current_cost = finished_inv.unit_cost or Decimal('0.00')

                # =========================================================================
                # INVARIANT 7: SCRAP-ADJUSTED AVCO UNIT COST & DOWNSTREAM CASCADE
                # =========================================================================
                # Factor in scrap variance: Total material consumed value / effective good yield
                batch_unit_cost = (total_material_cost / finished_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if finished_qty > 0 else Decimal('0.00')

                # Weighted Average Moving Cost (AVCO)
                total_qty_after = old_qty + finished_qty
                if total_qty_after > Decimal('0.00'):
                    new_avco = ((old_qty * current_cost) + (finished_qty * batch_unit_cost)) / total_qty_after
                    finished_inv.unit_cost = new_avco.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                else:
                    finished_inv.unit_cost = batch_unit_cost

                finished_inv.quantity_available = old_qty + finished_qty
                finished_inv.save(update_fields=['quantity_available', 'unit_cost'])

                st_output = StockTransaction.objects.create(
                    product=finished_prod,
                    quantity=finished_qty,
                    transaction_type='PRODUCTION_OUTPUT',
                    work_order=locked_wo,
                    notes=f"Production output of {finished_qty} {finished_prod.unit_of_measurement or 'units'} from Work Order #{locked_wo.pk} (AVCO: ${finished_inv.unit_cost:,.2f})"
                )

                summary['finished_good_output'] = {
                    'product_id': finished_prod.pk,
                    'product_name': finished_prod.name,
                    'output_qty': finished_qty,
                    'transaction_id': st_output.transaction_id,
                }
                summary['stock_transactions'].append(st_output.transaction_id)
                summary['unit_cost_avco'] = finished_inv.unit_cost

            # Update WorkOrder completion flags and timestamp
            locked_wo.is_inventory_updated = True
            if not locked_wo.production_end_date:
                locked_wo.production_end_date = timezone.now().date()
            locked_wo.__class__.objects.filter(pk=locked_wo.pk).update(
                is_inventory_updated=True,
                production_end_date=locked_wo.production_end_date
            )

            # Sync child packaging expectations
            locked_wo.sync_child_packaging_expectations()

            # Cascade completion to linked ProductionOrders
            for po in ProductionOrder.objects.filter(work_order=locked_wo):
                po.status = 'COMPLETED'
                po.completed_at = timezone.now()
                update_fields = ['status', 'completed_at']
                if finished_qty > Decimal('0.00'):
                    po.quantity = finished_qty
                    po.unit_cost = batch_unit_cost
                    update_fields.extend(['quantity', 'unit_cost'])
                po.save(update_fields=update_fields)

        return summary
