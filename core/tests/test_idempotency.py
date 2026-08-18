from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from core.models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory,
    WorkOrder, WorkOrderMaterialLine, ProductionOrder, StockTransaction
)


class WorkOrderInventoryIdempotencyTestCase(TestCase):
    """
    Automated test suite verifying that all WorkOrder inventory mutations
    and state transitions across Phase 1, Phase 2, Phase 3, and Shortage Resolution
    are strictly idempotent and immune to duplicate execution / spamming.
    """

    def setUp(self):
        self.supplier = Supplier.objects.create(name="Idempotency Supplier", contact_info="idem@test.com")

        self.raw_polymer = Product.objects.create(
            name="Raw Industrial Polymer",
            product_type="RAW",
            category="Plastics",
            unit_of_measurement="kg",
            supplier=self.supplier
        )

        self.bulk_compound = Product.objects.create(
            name="Bulk Polymer Compound",
            product_type="INTERMEDIATE",
            category="Compounds",
            unit_of_measurement="kg"
        )

        self.finished_container = Product.objects.create(
            name="Finished Polymer Container",
            product_type="FINISHED",
            category="Containers",
            unit_of_measurement="units",
            selling_price=Decimal("40.00")
        )

        # BOM for Intermediate Bulk Compound: 1 kg bulk requires 2.0 kg raw polymer
        self.bulk_bom = BillOfMaterial.objects.create(
            product=self.bulk_compound,
            name="Bulk Compound BOM",
            is_active=True
        )
        self.bulk_bom_item = BOMItem.objects.create(
            bom=self.bulk_bom,
            component=self.raw_polymer,
            quantity_required=Decimal("2.0000")
        )

        # BOM for Finished Goods Container: 1 container requires 0.5 kg bulk compound
        self.finished_bom = BillOfMaterial.objects.create(
            product=self.finished_container,
            name="Finished Container BOM",
            is_active=True
        )
        self.finished_bom_item = BOMItem.objects.create(
            bom=self.finished_bom,
            component=self.bulk_compound,
            quantity_required=Decimal("0.5000")
        )

    def test_double_phase_1_allocation_is_idempotent(self):
        """
        Test 1: Double Phase 1 Allocation
        - Call wo.start_production() and wo.process_inventory() back-to-back.
        - Assert that raw material quantity_allocated matches the exact single-batch requirement and is never doubled.
        """
        raw_inv, _ = Inventory.objects.get_or_create(
            product=self.raw_polymer,
            defaults={'quantity_available': Decimal('100.00'), 'quantity_allocated': Decimal('0.00')}
        )
        raw_inv.quantity_available = Decimal('100.00')
        raw_inv.quantity_allocated = Decimal('0.00')
        raw_inv.save()

        wo = WorkOrder.objects.create(
            product=self.bulk_compound,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("10.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        po = ProductionOrder.objects.create(
            product=self.bulk_compound,
            work_order=wo,
            quantity=Decimal("10.00"),
            status='IN_PROGRESS'
        )

        # 1. Start production (which triggers Phase 1 stock allocation once)
        success, msg = wo.start_production()
        self.assertTrue(success)
        self.assertEqual(wo.status, 'IN_PROGRESS')

        # 2. Call process_inventory() immediately back-to-back
        wo.process_inventory()

        # 3. Verify that raw material allocation is EXACTLY single-batch (10 units * 2.0 kg = 20.00 kg)
        raw_inv.refresh_from_db()
        wo.refresh_from_db()

        self.assertTrue(wo.is_inventory_allocated)
        self.assertEqual(raw_inv.quantity_allocated, Decimal("20.00"))
        self.assertEqual(raw_inv.quantity_available, Decimal("80.00"))

        # 4. Call process_inventory() a third time to ensure absolute idempotence
        wo.process_inventory()
        raw_inv.refresh_from_db()

        self.assertEqual(raw_inv.quantity_allocated, Decimal("20.00"), "quantity_allocated must not double on duplicate calls")
        self.assertEqual(raw_inv.quantity_available, Decimal("80.00"), "quantity_available must not decrease on duplicate calls")

    def test_consecutive_unchanged_saves_phase_2_is_idempotent(self):
        """
        Test 2: Consecutive Unchanged Saves (Phase 2)
        - Set quantity_actual = Decimal('25.00') on a material line and run wo.process_inventory().
        - Call wo.process_inventory() 3 more times without changing quantity_actual.
        - Assert that quantity_deducted remains 25.00 and warehouse stock is deducted exactly once.
        """
        raw_inv, _ = Inventory.objects.get_or_create(
            product=self.raw_polymer,
            defaults={'quantity_available': Decimal('100.00'), 'quantity_allocated': Decimal('0.00')}
        )
        raw_inv.quantity_available = Decimal('100.00')
        raw_inv.quantity_allocated = Decimal('0.00')
        raw_inv.save()

        wo = WorkOrder.objects.create(
            product=self.bulk_compound,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("10.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        ProductionOrder.objects.create(
            product=self.bulk_compound,
            work_order=wo,
            quantity=Decimal("10.00"),
            status='IN_PROGRESS'
        )

        wo.start_production()
        raw_inv.refresh_from_db()
        # Allocated 20.00, Available 80.00

        mat_line = wo.material_lines.get(component=self.raw_polymer)

        # Set quantity_actual = 25.00 (consuming all 20.00 allocated + 5.00 excess from available)
        mat_line.quantity_actual = Decimal("25.00")
        mat_line.save()
        wo.process_inventory()

        mat_line.refresh_from_db()
        raw_inv.refresh_from_db()

        self.assertEqual(mat_line.deducted_quantity, Decimal("25.00"))
        self.assertEqual(raw_inv.quantity_allocated, Decimal("0.00"))
        self.assertEqual(raw_inv.quantity_available, Decimal("75.00"))

        initial_tx_count = StockTransaction.objects.filter(work_order=wo).count()

        # Call process_inventory() 3 more times without changing quantity_actual
        for i in range(3):
            wo.process_inventory()

        mat_line.refresh_from_db()
        raw_inv.refresh_from_db()
        final_tx_count = StockTransaction.objects.filter(work_order=wo).count()

        self.assertEqual(mat_line.deducted_quantity, Decimal("25.00"), "deducted_quantity must remain 25.00")
        self.assertEqual(raw_inv.quantity_allocated, Decimal("0.00"), "quantity_allocated must not change on duplicate saves")
        self.assertEqual(raw_inv.quantity_available, Decimal("75.00"), "quantity_available must not change on duplicate saves")
        self.assertEqual(final_tx_count, initial_tx_count, "No duplicate StockTransaction rows should be created on unchanged saves")

    def test_duplicate_phase_3_finished_goods_posting_is_idempotent(self):
        """
        Test 3: Duplicate Phase 3 Finished Goods Posting
        - Transition WO to COMPLETED and call wo.process_inventory() twice.
        - Assert that finished good inventory count increases by exactly target_quantity (1x), guarded by is_inventory_updated.
        """
        # Prepare warehouse raw stock
        raw_inv, _ = Inventory.objects.get_or_create(
            product=self.raw_polymer,
            defaults={'quantity_available': Decimal('100.00'), 'quantity_allocated': Decimal('0.00')}
        )
        raw_inv.quantity_available = Decimal('100.00')
        raw_inv.quantity_allocated = Decimal('0.00')
        raw_inv.save()

        finished_inv, _ = Inventory.objects.get_or_create(
            product=self.bulk_compound,
            defaults={'quantity_available': Decimal('0.00')}
        )
        finished_inv.quantity_available = Decimal('0.00')
        finished_inv.save()

        wo = WorkOrder.objects.create(
            product=self.bulk_compound,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("10.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        ProductionOrder.objects.create(
            product=self.bulk_compound,
            work_order=wo,
            quantity=Decimal("10.00"),
            status='IN_PROGRESS'
        )

        wo.start_production()

        # Mark all instructions completed and set status to COMPLETED
        wo.instructions.update(status='COMPLETED')
        wo.recalculate_status()
        wo.status = 'COMPLETED'
        wo.save(update_fields=['status'])

        # First call to process_inventory()
        wo.process_inventory()

        wo.refresh_from_db()
        finished_inv.refresh_from_db()

        self.assertTrue(wo.is_inventory_updated)
        self.assertEqual(finished_inv.quantity_available, Decimal("10.00"))

        # Second call to process_inventory() (duplicate execution)
        wo.process_inventory()

        wo.refresh_from_db()
        finished_inv.refresh_from_db()

        self.assertEqual(
            finished_inv.quantity_available,
            Decimal("10.00"),
            "Finished good inventory must not increase twice when process_inventory() is called multiple times"
        )

        output_tx_count = StockTransaction.objects.filter(
            work_order=wo,
            transaction_type='PRODUCTION_OUTPUT'
        ).count()
        self.assertEqual(output_tx_count, 1, "Exactly one PRODUCTION_OUTPUT transaction must be recorded")

    def test_shortage_resolution_spam_protection_is_idempotent(self):
        """
        Test 4: Shortage Resolution Spam Protection
        - On an order in AWAITING_RESOLUTION, call resolve_bulk_shortage('TOP_UP_BULK') twice.
        - Assert that exactly ONE supplemental child Bulk Work Order is created and the second call is blocked cleanly.
        """
        # Packaging 50 units of finished_container requires 25.00 kg of bulk_compound (50 * 0.5 kg)
        # Warehouse only has 5.00 kg bulk compound -> Shortfall = 20.00 kg
        Inventory.objects.get_or_create(
            product=self.raw_polymer,
            defaults={'quantity_available': Decimal('500.00')}
        )

        bulk_inv, _ = Inventory.objects.get_or_create(
            product=self.bulk_compound,
            defaults={'quantity_available': Decimal('5.00')}
        )
        bulk_inv.quantity_available = Decimal('5.00')
        bulk_inv.save()

        pack_wo = WorkOrder.objects.create(
            product=self.finished_container,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        ProductionOrder.objects.create(
            product=self.finished_container,
            work_order=pack_wo,
            quantity=Decimal("50.00"),
            status='IN_PROGRESS'
        )

        # start_production detects shortfall -> moves to AWAITING_RESOLUTION
        success, msg = pack_wo.start_production()
        self.assertFalse(success)
        pack_wo.refresh_from_db()
        self.assertEqual(pack_wo.status, 'AWAITING_RESOLUTION')

        # First call: TOP_UP_BULK
        pack_wo.resolve_bulk_shortage('TOP_UP_BULK')
        pack_wo.refresh_from_db()

        self.assertEqual(pack_wo.status, 'ON_HOLD_SHORTAGE')
        self.assertIsNotNone(pack_wo.parent_work_order)
        self.assertEqual(pack_wo.parent_work_order.quantity_produced, Decimal("20.00"))

        parent_wo_pk = pack_wo.parent_work_order.pk

        # Second call: Duplicate TOP_UP_BULK attempt on the same WorkOrder
        with self.assertRaises(ValidationError) as ctx:
            pack_wo.resolve_bulk_shortage('TOP_UP_BULK')

        self.assertIn("AWAITING_RESOLUTION", str(ctx.exception))

        # Assert exactly ONE supplemental Bulk Work Order was created for this product
        bulk_work_orders = WorkOrder.objects.filter(product=self.bulk_compound)
        self.assertEqual(bulk_work_orders.count(), 1, "Exactly one supplemental Bulk WorkOrder must be created")
        self.assertEqual(bulk_work_orders.first().pk, parent_wo_pk)
