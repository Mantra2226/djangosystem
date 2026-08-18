from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from core.models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory,
    WorkOrder, StockTransaction
)


class StandalonePackagingIdempotencyTestCase(TestCase):
    """
    Automated test suite verifying independent packaging runs pulling from warehouse inventory,
    including standard lifecycle idempotency, scrap/overage delta tracking, and shortage resolution pathways.
    """

    def setUp(self):
        self.supplier = Supplier.objects.create(
            name="Packaging Supplies Co",
            contact_info="orders@packagingsupplies.com"
        )

        # 1. Raw Materials (Packaging components & Raw ingredients for bulk BOM)
        self.calcium_carb = Product.objects.create(
            name="Calcium Carbonate",
            product_type="RAW",
            category="Powders",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.linseed_oil = Product.objects.create(
            name="Raw Linseed Oil",
            product_type="RAW",
            category="Liquids",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.empty_tin = Product.objects.create(
            name="Empty 5kg Tin",
            product_type="RAW",
            category="Packaging",
            unit_of_measurement="pcs",
            supplier=self.supplier
        )
        self.tin_lid = Product.objects.create(
            name="5kg Tin Lid",
            product_type="RAW",
            category="Packaging",
            unit_of_measurement="pcs",
            supplier=self.supplier
        )
        self.product_label = Product.objects.create(
            name="5kg Product Label",
            product_type="RAW",
            category="Packaging",
            unit_of_measurement="pcs",
            supplier=self.supplier
        )

        # 2. Intermediate Good
        self.bulk_putty = Product.objects.create(
            name="Bulk Putty Base",
            product_type="INTERMEDIATE",
            category="Putty",
            unit_of_measurement="kg"
        )

        # 3. Finished Good
        self.finished_putty_tin = Product.objects.create(
            name="Glass Putty 5kg Tin",
            product_type="FINISHED",
            category="Retail Tins",
            unit_of_measurement="pcs",
            selling_price=Decimal("15.00")
        )

        # 4. Inventory Seed Stock
        self.inv_calcium = Inventory.objects.create(
            product=self.calcium_carb,
            quantity_available=Decimal("1000.00"),
            quantity_allocated=Decimal("0.00"),
            location="Raw Material Bay"
        )
        self.inv_oil = Inventory.objects.create(
            product=self.linseed_oil,
            quantity_available=Decimal("200.00"),
            quantity_allocated=Decimal("0.00"),
            location="Liquid Storage"
        )
        self.inv_bulk = Inventory.objects.create(
            product=self.bulk_putty,
            quantity_available=Decimal("250.00"),
            quantity_allocated=Decimal("0.00"),
            location="Intermediate Store"
        )
        self.inv_tin = Inventory.objects.create(
            product=self.empty_tin,
            quantity_available=Decimal("100.00"),
            quantity_allocated=Decimal("0.00"),
            location="Packaging Bay"
        )
        self.inv_lid = Inventory.objects.create(
            product=self.tin_lid,
            quantity_available=Decimal("100.00"),
            quantity_allocated=Decimal("0.00"),
            location="Packaging Bay"
        )
        self.inv_label = Inventory.objects.create(
            product=self.product_label,
            quantity_available=Decimal("100.00"),
            quantity_allocated=Decimal("0.00"),
            location="Packaging Bay"
        )
        self.inv_finished, _ = Inventory.objects.get_or_create(
            product=self.finished_putty_tin,
            defaults={'quantity_available': Decimal('0.00'), 'quantity_allocated': Decimal('0.00')}
        )
        self.inv_finished.quantity_available = Decimal('0.00')
        self.inv_finished.quantity_allocated = Decimal('0.00')
        self.inv_finished.save()

        # 5. BOMs:
        # Bulk Putty BOM (per 1.00 kg bulk): 0.80 kg Calcium Carbonate + 0.15 kg Linseed Oil
        self.bulk_bom = BillOfMaterial.objects.create(
            product=self.bulk_putty,
            name="Bulk Putty Formula",
            is_active=True
        )
        BOMItem.objects.create(
            bom=self.bulk_bom,
            component=self.calcium_carb,
            quantity_required=Decimal("0.8000")
        )
        BOMItem.objects.create(
            bom=self.bulk_bom,
            component=self.linseed_oil,
            quantity_required=Decimal("0.1500")
        )

        # Packaging BOM (per 1 Tin): 5.00 kg Bulk Putty Base + 1 pc Empty 5kg Tin + 1 pc 5kg Tin Lid + 1 pc 5kg Product Label
        self.pack_bom = BillOfMaterial.objects.create(
            product=self.finished_putty_tin,
            name="5kg Tin Packaging Specification",
            is_active=True
        )
        BOMItem.objects.create(
            bom=self.pack_bom,
            component=self.bulk_putty,
            quantity_required=Decimal("5.0000")
        )
        BOMItem.objects.create(
            bom=self.pack_bom,
            component=self.empty_tin,
            quantity_required=Decimal("1.0000")
        )
        BOMItem.objects.create(
            bom=self.pack_bom,
            component=self.tin_lid,
            quantity_required=Decimal("1.0000")
        )
        BOMItem.objects.create(
            bom=self.pack_bom,
            component=self.product_label,
            quantity_required=Decimal("1.0000")
        )

    # =========================================================================
    # A. Standard Lifecycle & Delta Idempotency
    # =========================================================================

    def test_standalone_packaging_allocation_idempotent(self):
        """
        A1: Standalone Packaging Allocation & Idempotency.
        - Create Standalone Packaging WO: category='PACKAGING', parent_work_order=None, target_quantity=20.00 tins.
        - Call wo.start_production() and invoke wo.process_inventory() 3 times consecutively.
        - Assert: Bulk Putty Base allocated = 100.00 kg (Available: 150.00 kg).
        - Assert: Empty Tins allocated = 20 pcs (Available: 80 pcs).
        - Assert: Tin Lids allocated = 20 pcs (Available: 80 pcs).
        - Assert: Labels allocated = 20 pcs (Available: 80 pcs).
        - Assert: is_inventory_allocated=True and repeated passes produce 0 duplicate deductions.
        """
        wo = WorkOrder.objects.create(
            product=self.finished_putty_tin,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("20.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )

        self.assertEqual(wo.category, 'PACKAGING')
        self.assertIsNone(wo.parent_work_order)
        self.assertEqual(wo.target_quantity, Decimal("20.00"))

        # Start production
        success, msg = wo.start_production()
        self.assertTrue(success)
        self.assertEqual(wo.status, 'IN_PROGRESS')

        # Invoke process_inventory() 3 times consecutively
        for _ in range(3):
            wo.process_inventory()

        # Refresh all component inventories
        self.inv_bulk.refresh_from_db()
        self.inv_tin.refresh_from_db()
        self.inv_lid.refresh_from_db()
        self.inv_label.refresh_from_db()
        wo.refresh_from_db()

        self.assertTrue(wo.is_inventory_allocated)

        # 20 tins * 5.00 kg = 100.00 kg Bulk Putty Base
        self.assertEqual(self.inv_bulk.quantity_allocated, Decimal("100.00"))
        self.assertEqual(self.inv_bulk.quantity_available, Decimal("150.00")) # 250 - 100

        # 20 tins * 1 pc = 20 pcs Empty Tins
        self.assertEqual(self.inv_tin.quantity_allocated, Decimal("20.00"))
        self.assertEqual(self.inv_tin.quantity_available, Decimal("80.00")) # 100 - 20

        # 20 tins * 1 pc = 20 pcs Tin Lids
        self.assertEqual(self.inv_lid.quantity_allocated, Decimal("20.00"))
        self.assertEqual(self.inv_lid.quantity_available, Decimal("80.00")) # 100 - 20

        # 20 tins * 1 pc = 20 pcs Labels
        self.assertEqual(self.inv_label.quantity_allocated, Decimal("20.00"))
        self.assertEqual(self.inv_label.quantity_available, Decimal("80.00")) # 100 - 20

        # No consumption transactions logged during Phase 1
        self.assertEqual(
            StockTransaction.objects.filter(work_order=wo, transaction_type='PRODUCTION_CONSUMPTION').count(),
            0
        )

    def test_standalone_packaging_scrap_and_delta_idempotent(self):
        """
        A2: Packaging Scrap, Actual Overages & Delta Idempotency.
        - On the active 20-tin packaging order, log actual material consumption with scrap:
          - Bulk Putty Base actual = 105.00 kg (5.00 kg process overage).
          - Empty Tins actual = 22 pcs (2 damaged during filling).
          - Tin Lids actual = 21 pcs (1 dented during crimping).
          - Labels actual = 20 pcs.
        - Run wo.process_inventory() 3 times.
        - Assert: quantity_deducted on each material line updates to 105.00, 22.00, 21.00, and 20.00 respectively.
        - Assert: Warehouse available stock covers the overage once, and subsequent saves calculate Delta = 0.00 without further stock deductions.
        """
        wo = WorkOrder.objects.create(
            product=self.finished_putty_tin,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("20.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        wo.start_production()

        line_bulk = wo.material_lines.get(component=self.bulk_putty)
        line_tin = wo.material_lines.get(component=self.empty_tin)
        line_lid = wo.material_lines.get(component=self.tin_lid)
        line_label = wo.material_lines.get(component=self.product_label)

        line_bulk.quantity_actual = Decimal("105.00")
        line_bulk.save()

        line_tin.quantity_actual = Decimal("22.00")
        line_tin.save()

        line_lid.quantity_actual = Decimal("21.00")
        line_lid.save()

        line_label.quantity_actual = Decimal("20.00")
        line_label.save()

        # Run process_inventory() 3 times consecutively
        for _ in range(3):
            wo.process_inventory()

        line_bulk.refresh_from_db()
        line_tin.refresh_from_db()
        line_lid.refresh_from_db()
        line_label.refresh_from_db()

        self.inv_bulk.refresh_from_db()
        self.inv_tin.refresh_from_db()
        self.inv_lid.refresh_from_db()
        self.inv_label.refresh_from_db()

        # Check line deducted quantities
        self.assertEqual(line_bulk.deducted_quantity, Decimal("105.00"))
        self.assertEqual(line_tin.deducted_quantity, Decimal("22.00"))
        self.assertEqual(line_lid.deducted_quantity, Decimal("21.00"))
        self.assertEqual(line_label.deducted_quantity, Decimal("20.00"))

        # Bulk Putty: 100.00 allocated consumed (allocated = 0.00); 5.00 excess deducted from available (150 - 5 = 145.00)
        self.assertEqual(self.inv_bulk.quantity_allocated, Decimal("0.00"))
        self.assertEqual(self.inv_bulk.quantity_available, Decimal("145.00"))

        # Empty Tins: 20.00 allocated consumed (allocated = 0.00); 2.00 excess deducted from available (80 - 2 = 78.00)
        self.assertEqual(self.inv_tin.quantity_allocated, Decimal("0.00"))
        self.assertEqual(self.inv_tin.quantity_available, Decimal("78.00"))

        # Tin Lids: 20.00 allocated consumed (allocated = 0.00); 1.00 excess deducted from available (80 - 1 = 79.00)
        self.assertEqual(self.inv_lid.quantity_allocated, Decimal("0.00"))
        self.assertEqual(self.inv_lid.quantity_available, Decimal("79.00"))

        # Labels: 20.00 allocated consumed (allocated = 0.00); 0.00 excess (available = 80.00)
        self.assertEqual(self.inv_label.quantity_allocated, Decimal("0.00"))
        self.assertEqual(self.inv_label.quantity_available, Decimal("80.00"))

        # 4 consumption transactions logged (1 per material line)
        tx_count = StockTransaction.objects.filter(
            work_order=wo,
            transaction_type='PRODUCTION_CONSUMPTION'
        ).count()
        self.assertEqual(tx_count, 4, "Exactly 4 consumption transactions should be recorded without duplication")

    def test_standalone_packaging_completion_and_output_idempotent(self):
        """
        A3: Packaging Order Completion & Finished Goods Output Idempotency.
        - Complete the packaging WO (status='COMPLETED', actual_quantity_produced=20.00).
        - Call wo.process_inventory() twice.
        - Assert: Finished good ('Glass Putty 5kg Tin') stock increases by exactly +20.00 pcs (1x, never +40.00).
        - Assert: is_inventory_updated=True skips the second execution pass cleanly.
        """
        wo = WorkOrder.objects.create(
            product=self.finished_putty_tin,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("20.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        wo.start_production()

        # Complete instructions and set COMPLETED
        wo.instructions.update(status='COMPLETED')
        wo.recalculate_status()
        wo.status = 'COMPLETED'
        wo.actual_quantity_produced = Decimal("20.00")
        wo.save(update_fields=['status', 'actual_quantity_produced'])

        # First pass
        wo.process_inventory()

        # Second pass (duplicate call)
        wo.process_inventory()

        self.inv_finished.refresh_from_db()
        wo.refresh_from_db()

        self.assertTrue(wo.is_inventory_updated)
        self.assertEqual(
            self.inv_finished.quantity_available,
            Decimal("20.00"),
            "Finished goods inventory must increase by exactly 20.00 pcs (1x) and never double"
        )

        output_tx_count = StockTransaction.objects.filter(
            work_order=wo,
            transaction_type='PRODUCTION_OUTPUT'
        ).count()
        self.assertEqual(output_tx_count, 1, "Exactly one PRODUCTION_OUTPUT transaction should be created")

    # =========================================================================
    # B. Bulk Putty Base Shortage & Resolution Pathways
    # =========================================================================

    def test_standalone_packaging_shortage_detection_gate(self):
        """
        B1: Bulk Putty Shortage Detection & Safety Gate.
        - Set 'Bulk Putty Base' warehouse inventory to only 50.00 kg available.
        - Create Packaging WO targeting 30 tins (requires 150.00 kg bulk base; 100.00 kg shortfall).
        - Call wo.start_production().
        - Assert: start_production() returns (False, ...) and transitions status to AWAITING_RESOLUTION.
        - Assert: is_inventory_allocated=False and zero stock (bulk, tins, lids, labels) is reserved.
        """
        self.inv_bulk.quantity_available = Decimal("50.00")
        self.inv_bulk.save()

        wo = WorkOrder.objects.create(
            product=self.finished_putty_tin,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("30.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )

        success, msg = wo.start_production()
        self.assertFalse(success)
        self.assertIn("Bulk shortage detected", msg)

        wo.refresh_from_db()
        self.assertEqual(wo.status, 'AWAITING_RESOLUTION')
        self.assertFalse(wo.is_inventory_allocated)

        # Verify zero stock was reserved across any components
        self.inv_bulk.refresh_from_db()
        self.inv_tin.refresh_from_db()
        self.inv_lid.refresh_from_db()
        self.inv_label.refresh_from_db()

        self.assertEqual(self.inv_bulk.quantity_available, Decimal("50.00"))
        self.assertEqual(self.inv_bulk.quantity_allocated, Decimal("0.00"))
        self.assertEqual(self.inv_tin.quantity_available, Decimal("100.00"))
        self.assertEqual(self.inv_tin.quantity_allocated, Decimal("0.00"))
        self.assertEqual(self.inv_lid.quantity_available, Decimal("100.00"))
        self.assertEqual(self.inv_lid.quantity_allocated, Decimal("0.00"))
        self.assertEqual(self.inv_label.quantity_available, Decimal("100.00"))
        self.assertEqual(self.inv_label.quantity_allocated, Decimal("0.00"))

    def test_shortage_resolution_top_up_bulk_idempotent(self):
        """
        B2: TOP_UP_BULK Shortage Resolution & Duplicate Execution Spam Protection.
        - From the AWAITING_RESOLUTION order (100.00 kg shortfall), call wo.resolve_bulk_shortage('TOP_UP_BULK') twice.
        - Assert: Exactly ONE parent Bulk WO is spawned with target_quantity=100.00 kg and status IN_PROGRESS.
        - Assert: Packaging WO links to the new bulk WO as parent_work_order and status is set to ON_HOLD_SHORTAGE.
        - Assert: The second resolution attempt raises a ValidationError and does not spawn a second parent bulk order.
        """
        self.inv_bulk.quantity_available = Decimal("50.00")
        self.inv_bulk.save()

        wo = WorkOrder.objects.create(
            product=self.finished_putty_tin,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("30.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        wo.start_production()

        # First resolution call: TOP_UP_BULK
        wo.resolve_bulk_shortage('TOP_UP_BULK')
        wo.refresh_from_db()

        self.assertEqual(wo.status, 'ON_HOLD_SHORTAGE')
        self.assertIsNotNone(wo.parent_work_order)

        parent_wo = wo.parent_work_order
        self.assertEqual(parent_wo.product, self.bulk_putty)
        self.assertEqual(parent_wo.quantity_produced, Decimal("100.00"))
        self.assertEqual(parent_wo.status, 'IN_PROGRESS')
        self.assertTrue(parent_wo.is_inventory_allocated)

        # Second resolution call: duplicate execution attempt on the same on-hold WorkOrder
        with self.assertRaises(ValidationError) as ctx:
            wo.resolve_bulk_shortage('TOP_UP_BULK')

        self.assertIn("AWAITING_RESOLUTION", str(ctx.exception))

        # Assert exactly ONE parent Bulk WorkOrder exists in total
        bulk_orders = WorkOrder.objects.filter(product=self.bulk_putty)
        self.assertEqual(bulk_orders.count(), 1, "Exactly one parent Bulk WorkOrder must be created")
        self.assertEqual(bulk_orders.first().pk, parent_wo.pk)

    def test_shortage_resolution_downscale_target_idempotent(self):
        """
        B3: DOWNSCALE_TARGET Shortage Resolution & Inventory Allocation Idempotency.
        - From the AWAITING_RESOLUTION order (50.00 kg bulk available, 5.00 kg per tin), call wo.resolve_bulk_shortage('DOWNSCALE_TARGET').
        - Assert: wo.target_quantity is recalculated to 10.00 tins.
        - Assert: wo.parent_work_order remains None.
        - Assert: wo.status flips directly to IN_PROGRESS and is_inventory_allocated=True.
        - Assert: Exactly 50.00 kg Bulk Putty, 10 Tins, 10 Lids, and 10 Labels are allocated.
        - Assert: Subsequent process_inventory() calls do not alter stock balances.
        """
        self.inv_bulk.quantity_available = Decimal("50.00")
        self.inv_bulk.save()

        wo = WorkOrder.objects.create(
            product=self.finished_putty_tin,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("30.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        wo.start_production()

        # Execute DOWNSCALE_TARGET
        wo.resolve_bulk_shortage('DOWNSCALE_TARGET')
        wo.refresh_from_db()

        self.assertEqual(wo.quantity_produced, Decimal("10.00")) # 50 kg available / 5 kg/tin = 10 tins
        self.assertEqual(wo.target_quantity, Decimal("10.00"))
        self.assertIsNone(wo.parent_work_order)
        self.assertEqual(wo.status, 'IN_PROGRESS')
        self.assertTrue(wo.is_inventory_allocated)

        # Check warehouse allocations for downscaled target (10 tins)
        self.inv_bulk.refresh_from_db()
        self.inv_tin.refresh_from_db()
        self.inv_lid.refresh_from_db()
        self.inv_label.refresh_from_db()

        # 10 * 5.00 = 50.00 kg Bulk Putty
        self.assertEqual(self.inv_bulk.quantity_allocated, Decimal("50.00"))
        self.assertEqual(self.inv_bulk.quantity_available, Decimal("0.00")) # 50 - 50

        # 10 * 1 pc = 10 pcs Tins, Lids, Labels
        self.assertEqual(self.inv_tin.quantity_allocated, Decimal("10.00"))
        self.assertEqual(self.inv_tin.quantity_available, Decimal("90.00"))

        self.assertEqual(self.inv_lid.quantity_allocated, Decimal("10.00"))
        self.assertEqual(self.inv_lid.quantity_available, Decimal("90.00"))

        self.assertEqual(self.inv_label.quantity_allocated, Decimal("10.00"))
        self.assertEqual(self.inv_label.quantity_available, Decimal("90.00"))

        # Subsequent process_inventory() calls do not alter balances
        wo.process_inventory()
        self.inv_bulk.refresh_from_db()
        self.assertEqual(self.inv_bulk.quantity_allocated, Decimal("50.00"))
        self.assertEqual(self.inv_bulk.quantity_available, Decimal("0.00"))

    def test_shortage_resolution_hold_for_existing_idempotent(self):
        """
        B4: HOLD_FOR_EXISTING Shortage Resolution & Link Idempotency.
        - Create an independent active Stage 1 Bulk WO (IN_PROGRESS, target: 100.00 kg).
        - On the packaging order in AWAITING_RESOLUTION, call wo.resolve_bulk_shortage('HOLD_FOR_EXISTING', existing_wo_id=bulk_wo.id) twice.
        - Assert: Packaging WO attaches bulk_wo as parent_work_order and transitions to ON_HOLD_SHORTAGE.
        - Assert: The second call raises a ValidationError and does not alter the link or state.
        """
        self.inv_bulk.quantity_available = Decimal("50.00")
        self.inv_bulk.save()

        # Independent active bulk run on the floor
        bulk_wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("100.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )

        pack_wo = WorkOrder.objects.create(
            product=self.finished_putty_tin,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("30.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        pack_wo.start_production()

        # First call: HOLD_FOR_EXISTING with bulk_wo
        pack_wo.resolve_bulk_shortage('HOLD_FOR_EXISTING', existing_wo_id=bulk_wo.pk)
        pack_wo.refresh_from_db()

        self.assertEqual(pack_wo.status, 'ON_HOLD_SHORTAGE')
        self.assertEqual(pack_wo.parent_work_order, bulk_wo)

        # Second call: Duplicate attempt
        with self.assertRaises(ValidationError) as ctx:
            pack_wo.resolve_bulk_shortage('HOLD_FOR_EXISTING', existing_wo_id=bulk_wo.pk)

        self.assertIn("AWAITING_RESOLUTION", str(ctx.exception))
        pack_wo.refresh_from_db()
        self.assertEqual(pack_wo.status, 'ON_HOLD_SHORTAGE')
        self.assertEqual(pack_wo.parent_work_order, bulk_wo)
