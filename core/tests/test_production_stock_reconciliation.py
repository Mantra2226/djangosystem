"""
TESTS: Production Reconciliation Invariant Engine & Centralized Stock Consumption Service.

Verifies:
1. Multi-material atomic stock deductions & finished goods ledger creation.
2. All-or-nothing rollback on partial failure (completeness invariant).
3. Release of unused allocations back to available inventory on under-consumption.
4. Secondary packaging order compatibility (bulk intermediate + tins/lids/packaging).
5. Scrap variance incorporation into weighted average unit cost (AVCO).
6. Idempotency safety gate preventing duplicate ledger transactions.
7. Admin action trigger for production stock reconciliation.
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory
from django.utils import timezone

from core.models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory,
    WorkOrder, WorkOrderMaterialLine, ProductionOrder, StockTransaction
)
from core.services.production_reconciliation import (
    ProductionReconciliationEngine,
    ProductionReconciliationError
)
from core.admin import WorkOrderAdmin
from django.contrib.admin.sites import AdminSite

User = get_user_model()


class ProductionStockReconciliationTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_superuser(
            username='admin_supervisor',
            email='admin@factory.internal',
            password='AdminPassword123!'
        )

        # 1. Supplier
        self.supplier = Supplier.objects.create(
            name="Apex Raw Materials Ltd.",
            contact_info="orders@apexmaterials.com"
        )

        # 2. Raw Materials
        self.calcium_carbonate = Product.objects.create(
            name="Calcium Carbonate Pure",
            sku="RM-CC-RECON",
            product_type="RAW",
            category="Minerals",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.linseed_oil = Product.objects.create(
            name="Raw Linseed Oil Grade A",
            sku="RM-OIL-RECON",
            product_type="RAW",
            category="Oils",
            unit_of_measurement="Liters",
            supplier=self.supplier
        )

        # 3. Finished Product: Glass Putty 1000kg
        self.finished_putty = Product.objects.create(
            name="Industrial Glass Putty 1000kg",
            sku="FG-PUTTY-RECON",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="kg",
            selling_price=Decimal("30.00")
        )

        # 4. BOM: 1kg putty -> 0.8kg CC ($1.00/kg), 0.2L Linseed Oil ($3.00/L)
        self.bom = BillOfMaterial.objects.create(
            product=self.finished_putty,
            name="Standard Putty Recipe",
            is_active=True
        )
        BOMItem.objects.create(bom=self.bom, component=self.calcium_carbonate, quantity_required=Decimal("0.8000"))
        BOMItem.objects.create(bom=self.bom, component=self.linseed_oil, quantity_required=Decimal("0.2000"))

        # 5. Inventory Setup
        self.cc_inv, _ = Inventory.objects.update_or_create(
            product=self.calcium_carbonate,
            defaults={'quantity_available': Decimal('1000.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('1.00')}
        )
        self.oil_inv, _ = Inventory.objects.update_or_create(
            product=self.linseed_oil,
            defaults={'quantity_available': Decimal('400.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('3.00')}
        )
        self.putty_inv, _ = Inventory.objects.update_or_create(
            product=self.finished_putty,
            defaults={'quantity_available': Decimal('0.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('0.00')}
        )

    def test_atomic_multi_material_deduction_and_finished_output(self):
        """Batch of 1000kg putty: Calcium Carbonate (800kg) and Oil (200L) deducted atomically with finished output posted."""
        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("1000.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal("1000.00"),
            status='DRAFT'
        )

        # Start production -> Allocates stock
        wo.start_production()
        wo.refresh_from_db()
        self.assertEqual(wo.status, 'IN_PROGRESS')
        self.assertTrue(wo.is_inventory_allocated)

        # Log actual material consumption
        cc_line = wo.material_lines.get(component=self.calcium_carbonate)
        cc_line.quantity_actual = Decimal('800.00')
        cc_line.save(update_fields=['quantity_actual'])

        oil_line = wo.material_lines.get(component=self.linseed_oil)
        oil_line.quantity_actual = Decimal('200.00')
        oil_line.save(update_fields=['quantity_actual'])

        # Complete Work Order
        wo.status = 'COMPLETED'
        wo.save()
        wo.process_inventory()

        # Check Invariant Results
        wo.refresh_from_db()
        self.assertTrue(wo.is_inventory_updated)

        self.cc_inv.refresh_from_db()
        self.oil_inv.refresh_from_db()
        self.putty_inv.refresh_from_db()

        # Available stock deducted: 1000 - 800 = 200kg CC, 400 - 200 = 200L Oil
        self.assertEqual(self.cc_inv.quantity_available, Decimal('200.00'))
        self.assertEqual(self.cc_inv.quantity_allocated, Decimal('0.00'))

        self.assertEqual(self.oil_inv.quantity_available, Decimal('200.00'))
        self.assertEqual(self.oil_inv.quantity_allocated, Decimal('0.00'))

        # Finished goods credited: +1000kg
        self.assertEqual(self.putty_inv.quantity_available, Decimal('1000.00'))

        # Check StockTransaction Ledger
        cc_trans = StockTransaction.objects.filter(product=self.calcium_carbonate, work_order=wo, transaction_type='PRODUCTION_CONSUMPTION').first()
        self.assertIsNotNone(cc_trans)
        self.assertEqual(cc_trans.quantity, Decimal('-800.00'))

        oil_trans = StockTransaction.objects.filter(product=self.linseed_oil, work_order=wo, transaction_type='PRODUCTION_CONSUMPTION').first()
        self.assertIsNotNone(oil_trans)
        self.assertEqual(oil_trans.quantity, Decimal('-200.00'))

        output_trans = StockTransaction.objects.filter(product=self.finished_putty, work_order=wo, transaction_type='PRODUCTION_OUTPUT').first()
        self.assertIsNotNone(output_trans)
        self.assertEqual(output_trans.quantity, Decimal('1000.00'))

        po.refresh_from_db()
        self.assertEqual(po.status, 'COMPLETED')

    def test_partial_failure_rolls_back_entire_reconciliation_transaction(self):
        """If a required component is missing or fails verification, the entire reconciliation is rolled back."""
        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("500.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal("500.00"),
            status='IN_PROGRESS'
        )

        wo.process_inventory()

        # Simulate database lock / failure during stock transaction creation
        from unittest.mock import patch
        with patch('core.models.StockTransaction.objects.create', side_effect=RuntimeError("Database lock failure during ledger commit")):
            with self.assertRaises(RuntimeError):
                ProductionReconciliationEngine.reconcile_work_order_completion(wo)

        # Verify rollback: is_inventory_updated remains False and no output transactions created
        wo.refresh_from_db()
        self.assertFalse(wo.is_inventory_updated)
        self.assertFalse(StockTransaction.objects.filter(work_order=wo, transaction_type='PRODUCTION_OUTPUT').exists())

    def test_under_consumption_releases_unconsumed_allocations_back_to_available(self):
        """Planned: 800kg CC, 200L oil. Actual consumed: 750kg CC, 180L oil. Residual is released to available pool."""
        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("1000.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal("1000.00"),
            status='IN_PROGRESS'
        )

        wo.process_inventory()

        # Set under-consumption actuals
        cc_line = wo.material_lines.get(component=self.calcium_carbonate)
        cc_line.quantity_actual = Decimal('750.00')
        cc_line.save()

        oil_line = wo.material_lines.get(component=self.linseed_oil)
        oil_line.quantity_actual = Decimal('180.00')
        oil_line.save()

        # Reconcile completion
        wo.status = 'COMPLETED'
        ProductionReconciliationEngine.reconcile_work_order_completion(wo)

        self.cc_inv.refresh_from_db()
        self.oil_inv.refresh_from_db()

        # Total available: Started 1000 - consumed 750 = 250 available (allocated 0)
        self.assertEqual(self.cc_inv.quantity_available, Decimal('250.00'))
        self.assertEqual(self.cc_inv.quantity_allocated, Decimal('0.00'))

        # Total available: Started 400 - consumed 180 = 220 available (allocated 0)
        self.assertEqual(self.oil_inv.quantity_available, Decimal('220.00'))
        self.assertEqual(self.oil_inv.quantity_allocated, Decimal('0.00'))

    def test_packaging_order_compatibility_reconciles_intermediate_and_packaging_materials(self):
        """Packaging Run converting bulk intermediate putty + empty tins + lids into 5kg packaged putty."""
        # 1. Bulk Putty (Intermediate)
        bulk_putty = Product.objects.create(
            name="Unpackaged Bulk Putty",
            sku="INT-BULK-PUTTY",
            product_type="INTERMEDIATE",
            category="Putty",
            unit_of_measurement="kg"
        )
        # 2. Packaging Tin (Raw/Packaging)
        metal_tin = Product.objects.create(
            name="5kg Metal Tin with Handle",
            sku="PKG-TIN-5KG",
            product_type="RAW",
            category="Packaging",
            unit_of_measurement="Units",
            supplier=self.supplier
        )
        # 3. Packaged Finished Product
        packaged_putty = Product.objects.create(
            name="Glass Putty 5kg Retail Tin",
            sku="FG-PUTTY-5KG",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="Tins",
            selling_price=Decimal("15.00")
        )

        # 4. Packaging BOM: 1 Tin requires 5kg Bulk Putty + 1 Metal Tin
        pkg_bom = BillOfMaterial.objects.create(
            product=packaged_putty,
            name="5kg Tin Packaging Recipe",
            is_active=True
        )
        BOMItem.objects.create(bom=pkg_bom, component=bulk_putty, quantity_required=Decimal("5.0000"))
        BOMItem.objects.create(bom=pkg_bom, component=metal_tin, quantity_required=Decimal("1.0000"))

        # Inventory
        Inventory.objects.update_or_create(
            product=bulk_putty,
            defaults={'quantity_available': Decimal('500.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('1.40')}
        )
        Inventory.objects.update_or_create(
            product=metal_tin,
            defaults={'quantity_available': Decimal('100.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('0.50')}
        )
        pkg_inv, _ = Inventory.objects.update_or_create(
            product=packaged_putty,
            defaults={'quantity_available': Decimal('0.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('0.00')}
        )

        # Packaging Work Order for 50 Tins (requires 250kg bulk putty + 50 tins)
        wo = WorkOrder.objects.create(
            product=packaged_putty,
            bill_of_material=pkg_bom,
            category='PACKAGING',
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        po = ProductionOrder.objects.create(
            product=packaged_putty,
            work_order=wo,
            quantity=Decimal("50.00"),
            status='IN_PROGRESS'
        )
        wo.process_inventory()

        # Set consumption actuals
        bulk_line = wo.material_lines.get(component=bulk_putty)
        bulk_line.quantity_actual = Decimal('250.00')
        bulk_line.save(update_fields=['quantity_actual'])

        tin_line = wo.material_lines.get(component=metal_tin)
        tin_line.quantity_actual = Decimal('50.00')
        tin_line.save(update_fields=['quantity_actual'])

        # Reconcile completion
        wo.status = 'COMPLETED'
        ProductionReconciliationEngine.reconcile_work_order_completion(wo)

        # Assertions
        bulk_inv = Inventory.objects.get(product=bulk_putty)
        tin_inv = Inventory.objects.get(product=metal_tin)
        pkg_inv.refresh_from_db()

        self.assertEqual(bulk_inv.quantity_available, Decimal('250.00'))  # 500 - 250
        self.assertEqual(tin_inv.quantity_available, Decimal('50.00'))   # 100 - 50
        self.assertEqual(pkg_inv.quantity_available, Decimal('50.00'))   # +50 finished tins

        self.assertTrue(StockTransaction.objects.filter(product=bulk_putty, work_order=wo, quantity=Decimal('-250.00')).exists())
        self.assertTrue(StockTransaction.objects.filter(product=metal_tin, work_order=wo, quantity=Decimal('-50.00')).exists())
        self.assertTrue(StockTransaction.objects.filter(product=packaged_putty, work_order=wo, quantity=Decimal('50.00')).exists())

    def test_scrap_adjusted_avco_calculation(self):
        """Verifies that scrap variance (yielding less good output for consumed materials) elevates unit cost correctly."""
        # 1000kg batch of materials consumed: 800kg CC @ $1.00 ($800) + 200L oil @ $3.00 ($600) = $1,400 total material cost
        # But actual good output produced is only 700kg due to scrap.
        # Batch unit cost should be $1,400 / 700 = $2.00/kg
        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("1000.00"),
            actual_quantity_produced=Decimal("700.00"),  # Scrap variance
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal("1000.00"),
            status='IN_PROGRESS'
        )
        wo.process_inventory()

        # Set consumption actuals
        cc_line = wo.material_lines.get(component=self.calcium_carbonate)
        cc_line.quantity_actual = Decimal('800.00')
        cc_line.save(update_fields=['quantity_actual'])

        oil_line = wo.material_lines.get(component=self.linseed_oil)
        oil_line.quantity_actual = Decimal('200.00')
        oil_line.save(update_fields=['quantity_actual'])

        wo.status = 'COMPLETED'
        summary = ProductionReconciliationEngine.reconcile_work_order_completion(wo)

        self.putty_inv.refresh_from_db()
        self.assertEqual(self.putty_inv.quantity_available, Decimal('700.00'))
        self.assertEqual(self.putty_inv.unit_cost, Decimal('2.00'))

    def test_idempotent_reconciliation_prevents_duplicate_ledger_transactions(self):
        """Calling reconcile_work_order_completion multiple times on a completed order skips safely."""
        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("100.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal("100.00"),
            status='IN_PROGRESS'
        )
        wo.process_inventory()

        # Set consumption actuals
        cc_line = wo.material_lines.get(component=self.calcium_carbonate)
        cc_line.quantity_actual = Decimal('80.00')
        cc_line.save(update_fields=['quantity_actual'])

        oil_line = wo.material_lines.get(component=self.linseed_oil)
        oil_line.quantity_actual = Decimal('20.00')
        oil_line.save(update_fields=['quantity_actual'])

        wo.status = 'COMPLETED'
        res1 = ProductionReconciliationEngine.reconcile_work_order_completion(wo)
        self.assertFalse(res1.get('skipped', False))

        initial_tx_count = StockTransaction.objects.filter(work_order=wo).count()

        # Second call
        res2 = ProductionReconciliationEngine.reconcile_work_order_completion(wo)
        self.assertTrue(res2.get('skipped', False))

        # Assert no duplicate transactions created
        final_tx_count = StockTransaction.objects.filter(work_order=wo).count()
        self.assertEqual(initial_tx_count, final_tx_count)

    def test_admin_action_triggers_reconciliation_service(self):
        """Tests triggering action_reconcile_production_stock from WorkOrderAdmin."""
        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("100.00"),
            production_start_date=timezone.now().date(),
            status='COMPLETED'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal("100.00"),
            status='COMPLETED'
        )

        # Set consumption actuals
        cc_line = wo.material_lines.get(component=self.calcium_carbonate)
        cc_line.quantity_actual = Decimal('80.00')
        cc_line.save(update_fields=['quantity_actual'])

        oil_line = wo.material_lines.get(component=self.linseed_oil)
        oil_line.quantity_actual = Decimal('20.00')
        oil_line.save(update_fields=['quantity_actual'])

        admin_instance = WorkOrderAdmin(WorkOrder, AdminSite())
        factory = RequestFactory()
        request = factory.post('/admin/core/workorder/')
        request.user = self.supervisor
        # Setup message framework support on request
        from django.contrib.messages.storage.fallback import FallbackStorage
        setattr(request, 'session', {})
        setattr(request, '_messages', FallbackStorage(request))

        admin_instance.action_reconcile_production_stock(request, WorkOrder.objects.filter(pk=wo.pk))

        wo.refresh_from_db()
        self.assertTrue(wo.is_inventory_updated)
        self.assertTrue(StockTransaction.objects.filter(work_order=wo, transaction_type='PRODUCTION_OUTPUT').exists())
