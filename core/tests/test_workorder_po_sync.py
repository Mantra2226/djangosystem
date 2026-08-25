"""
TESTS: Work Order & Production Order Status Synchronization, Action Visibility, and MRP Freezing.

Verifies:
1. WorkOrder start_production() transitions state to IN_PROGRESS and prevents double-initiation.
2. Starting production locks the linked ProductionOrder's MRP resolution (is_mrp_resolved=True, resolution_applied='INITIAL_STOCK_ALLOCATION').
3. Completing a WorkOrder automatically synchronizes the linked ProductionOrder status to COMPLETED.
4. Admin action detail filters dynamically hide Start Production buttons on active and completed orders.
"""

from decimal import Decimal
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.utils import timezone

from core.admin import WorkOrderAdmin
from core.models import (
    Product, BillOfMaterial, BOMItem, Inventory,
    WorkOrder, WorkOrderInstruction, ProductionOrder
)
from core.services import evaluate_mrp_shortages

User = get_user_model()


class WorkOrderProductionOrderSyncTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username='mes_admin',
            email='admin@example.com',
            password='Password123!'
        )

        # 0. Supplier
        from core.models import Supplier
        self.supplier = Supplier.objects.create(
            name="Apex Minerals Inc.",
            contact_info="apex@example.com"
        )

        # 1. Base Materials
        self.linseed_oil = Product.objects.create(
            name="Raw Linseed Oil",
            sku="RM-OIL-001",
            product_type="RAW",
            category="Raw Oils",
            unit_of_measurement="Liters",
            supplier=self.supplier
        )
        self.calcium_carbonate = Product.objects.create(
            name="Calcium Carbonate",
            sku="RM-CC-001",
            product_type="RAW",
            category="Minerals",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.bulk_putty = Product.objects.create(
            name="Bulk Putty Paste",
            sku="INT-PUTTY-001",
            product_type="INTERMEDIATE",
            category="Bulk Paste",
            unit_of_measurement="kg",
            selling_price=Decimal("15.00")
        )

        # 2. Stock Inventory
        Inventory.objects.update_or_create(
            product=self.linseed_oil,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('500.00')}
        )
        Inventory.objects.update_or_create(
            product=self.calcium_carbonate,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('2000.00')}
        )

        # 3. BOM Recipe for Bulk Putty
        self.bom = BillOfMaterial.objects.create(
            product=self.bulk_putty,
            name="Standard Putty 100kg Recipe",
            is_active=True
        )
        BOMItem.objects.create(bom=self.bom, component=self.linseed_oil, quantity_required=Decimal("0.20"))
        BOMItem.objects.create(bom=self.bom, component=self.calcium_carbonate, quantity_required=Decimal("0.80"))

        # 4. WorkOrder & ProductionOrder
        self.wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bom,
            category='PRODUCTION',
            status='DRAFT',
            quantity_produced=Decimal("100.00"),
            production_start_date=timezone.now().date()
        )
        self.po = ProductionOrder.objects.create(
            product=self.bulk_putty,
            work_order=self.wo,
            quantity=Decimal("100.00"),
            status='IN_PROGRESS'
        )

        self.site = AdminSite()
        self.wo_admin = WorkOrderAdmin(WorkOrder, self.site)
        self.factory = RequestFactory()

    def test_start_production_hides_action_buttons_and_locks_status(self):
        """Start a work order, verify status is IN_PROGRESS, and assert double-initiation raises ValidationError."""
        self.wo.status = 'DRAFT'
        self.wo.save()

        success, msg = self.wo.start_production()
        self.assertTrue(success)

        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, 'IN_PROGRESS')

        # Assert double-initiation is rejected
        with self.assertRaises(ValidationError):
            self.wo.start_production()

    def test_start_production_locks_production_order_mrp_resolution(self):
        """Starting a work order locks linked ProductionOrder MRP resolution and freezes evaluation."""
        self.wo.status = 'DRAFT'
        self.wo.save()
        self.po.is_mrp_resolved = False
        self.po.save()

        self.wo.start_production()

        self.po.refresh_from_db()
        self.assertTrue(self.po.is_mrp_resolved)
        self.assertEqual(self.po.resolution_applied, 'INITIAL_STOCK_ALLOCATION')
        self.assertIsNotNone(self.po.resolved_at)

        # Ensure evaluate_mrp_shortages returns empty / frozen report
        report = evaluate_mrp_shortages(self.po)
        self.assertEqual(report, [])

    def test_work_order_completion_syncs_production_order_status(self):
        """Completing a WorkOrder automatically marks linked ProductionOrders as COMPLETED."""
        self.wo.status = 'DRAFT'
        self.wo.save()
        self.wo.start_production()

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'IN_PROGRESS')

        # Execute completion
        self.wo.complete_work_order()

        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, 'COMPLETED')

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'COMPLETED')
        self.assertIsNotNone(self.po.completed_at)

    def test_admin_action_predicates_prevent_execution_on_active_orders(self):
        """Verify get_actions_detail hides Start Production button when WorkOrder is IN_PROGRESS or COMPLETED."""
        request = self.factory.get(f'/admin/core/workorder/{self.wo.pk}/change/')
        request.user = self.superuser

        # 1. When DRAFT: Start Production action should be visible
        self.wo.status = 'DRAFT'
        self.wo.save()
        actions_draft = self.wo_admin.get_actions_detail(request, self.wo.pk)
        action_names_draft = [a.action_name for a in actions_draft]
        self.assertTrue(any('action_start_production_button' in name for name in action_names_draft))

        # 2. When IN_PROGRESS: Start Production action should be hidden
        self.wo.status = 'IN_PROGRESS'
        self.wo.save()
        actions_in_prog = self.wo_admin.get_actions_detail(request, self.wo.pk)
        action_names_in_prog = [a.action_name for a in actions_in_prog]
        self.assertFalse(any('action_start_production_button' in name for name in action_names_in_prog))

        # 3. When COMPLETED: Start Production action should be hidden
        self.wo.status = 'COMPLETED'
        self.wo.save()
        actions_completed = self.wo_admin.get_actions_detail(request, self.wo.pk)
        action_names_completed = [a.action_name for a in actions_completed]
        self.assertFalse(any('action_start_production_button' in name for name in action_names_completed))
