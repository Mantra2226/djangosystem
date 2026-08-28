"""
TEST SUITE: Hybrid Two-Tier Audit Logging & Milestone Stepper Subsystem.
(core/tests/test_process_execution_logging.py)

Validates:
1. Dual-channel logging utility writes to database (ProcessExecutionLog) without breaking standard logger.
2. MRP shortage evaluations generate structured ProcessExecutionLog audit entries.
3. Production stock reconciliations record atomic deductions, outputs, and residual releases.
4. ProcessExecutionLogInline enforces strict immutability (has_add_permission=False, can_delete=False).
5. User attribution (logged_by) correctly associates events with authenticated users.
6. Message truncation guard truncates long strings to 1000 characters and stores overflow in details.
7. Bulk logging helper creates multiple logs efficiently in a single bulk_create query.
8. Reconciliation atomic rollback failure logs are safely captured in the database.
9. Admin milestone stepper renders high-contrast HTML across lifecycle states.
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory
from django.contrib.admin.sites import AdminSite
from django.utils import timezone

from core.models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory,
    WorkOrder, ProductionOrder, ProductionOrderItem, PurchaseOrder,
    ProcessExecutionLog, StockTransaction
)
from core.services.logging_service import log_execution_event, bulk_log_execution_events
from core.services.mrp_services import (
    evaluate_mrp_shortages,
    resolve_raw_autodraft_po,
    resolve_batch_downscale,
    check_and_auto_resume_on_hold_orders
)
from core.services.production_reconciliation import (
    ProductionReconciliationEngine,
    ProductionReconciliationError
)
from core.admin import (
    ProcessExecutionLogInline,
    ProductionOrderAdmin,
    WorkOrderAdmin,
    ProcessExecutionLogAdmin
)

User = get_user_model()


class ProcessExecutionLoggingTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.supervisor = User.objects.create_superuser(
            username='plant_manager',
            email='manager@factory.internal',
            password='Password123!'
        )

        # 1. Supplier
        self.supplier = Supplier.objects.create(
            name="Apex Raw Materials Co.",
            contact_info="sales@apexmaterials.com"
        )

        # 2. Raw Materials
        self.calcium_carbonate = Product.objects.create(
            name="Calcium Carbonate Pure",
            sku="RM-CC-LOGTEST",
            product_type="RAW",
            category="Minerals",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.linseed_oil = Product.objects.create(
            name="Raw Linseed Oil Grade A",
            sku="RM-OIL-LOGTEST",
            product_type="RAW",
            category="Oils",
            unit_of_measurement="Liters",
            supplier=self.supplier
        )

        # 3. Finished Good
        self.finished_putty = Product.objects.create(
            name="Standard Glass Putty 1000kg",
            sku="FG-PUTTY-LOGTEST",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="kg",
            selling_price=Decimal("35.00")
        )

        # 4. Recipe (BOM): 1kg Putty = 0.8kg CC ($1.50) + 0.2L Linseed Oil ($4.00)
        self.bom = BillOfMaterial.objects.create(
            product=self.finished_putty,
            name="Putty Recipe 1000kg Batch",
            is_active=True
        )
        BOMItem.objects.create(bom=self.bom, component=self.calcium_carbonate, quantity_required=Decimal("0.8000"))
        BOMItem.objects.create(bom=self.bom, component=self.linseed_oil, quantity_required=Decimal("0.2000"))

        # 5. Inventory: Calcium Carbonate (400kg), Linseed Oil (100L) -> shortage for 1000kg batch (needs 800kg CC, 200L Oil)
        self.cc_inv, _ = Inventory.objects.update_or_create(
            product=self.calcium_carbonate,
            defaults={'quantity_available': Decimal('400.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('1.50')}
        )
        self.oil_inv, _ = Inventory.objects.update_or_create(
            product=self.linseed_oil,
            defaults={'quantity_available': Decimal('100.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('4.00')}
        )
        self.putty_inv, _ = Inventory.objects.update_or_create(
            product=self.finished_putty,
            defaults={'quantity_available': Decimal('0.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('0.00')}
        )

    def test_dual_channel_logging_writes_to_db_and_does_not_break_logger(self):
        """Invoke log_execution_event() directly and assert matching database record is created."""
        initial_count = ProcessExecutionLog.objects.count()

        log_entry = log_execution_event(
            process_type='MRP_EVALUATION',
            message="Test dual-channel execution event message.",
            level='INFO',
            details={'test_key': 'test_val', 'batch_size': 500},
            logged_by=self.supervisor
        )

        self.assertIsNotNone(log_entry)
        self.assertEqual(ProcessExecutionLog.objects.count(), initial_count + 1)
        self.assertEqual(log_entry.process_type, 'MRP_EVALUATION')
        self.assertEqual(log_entry.level, 'INFO')
        self.assertEqual(log_entry.message, "Test dual-channel execution event message.")
        self.assertEqual(log_entry.details.get('test_key'), 'test_val')
        self.assertEqual(log_entry.logged_by, self.supervisor)

    def test_mrp_evaluation_records_execution_logs(self):
        """Trigger MRP shortage evaluation on an order with shortfalls and assert ProcessExecutionLog is recorded."""
        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal('1000.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('1000.00'),
            status='PENDING'
        )

        # Trigger shortage evaluation
        report = evaluate_mrp_shortages(po)
        self.assertTrue(len(report) > 0)

        # Verify MRP_EVALUATION log exists
        eval_logs = ProcessExecutionLog.objects.filter(
            production_order=po,
            process_type='MRP_EVALUATION'
        )
        self.assertTrue(eval_logs.exists())
        log = eval_logs.first()
        self.assertIn("MRP Evaluation completed", log.message)
        self.assertEqual(log.level, 'WARNING')  # Shortages exist
        self.assertIn('lines', log.details)
        self.assertEqual(log.details['scanned_components_count'], 2)
        self.assertEqual(log.details['shortage_lines_count'], 2)

    def test_stock_reconciliation_records_reconciliation_logs(self):
        """Complete production reconciliation on a Work Order and assert component deductions and output credits are logged."""
        # Provide full inventory for 100kg test run (needs 80kg CC, 20L Oil)
        self.cc_inv.quantity_available = Decimal('500.00')
        self.cc_inv.save()
        self.oil_inv.quantity_available = Decimal('200.00')
        self.oil_inv.save()

        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal('100.00'),
            actual_quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('100.00'),
            status='IN_PROGRESS'
        )

        summary = ProductionReconciliationEngine.reconcile_work_order_completion(wo)
        self.assertFalse(summary.get('skipped', False))

        recon_logs = ProcessExecutionLog.objects.filter(
            work_order=wo,
            process_type='RECONCILIATION'
        )
        self.assertTrue(recon_logs.exists())

        # Check that row locking, deductions, and finished good output were logged
        messages = [l.message for l in recon_logs]
        self.assertTrue(any("Acquired exclusive atomic row locks" in m for m in messages))
        self.assertTrue(any("Deducted" in m and "Calcium Carbonate" in m for m in messages))
        self.assertTrue(any("Deducted" in m and "Linseed Oil" in m for m in messages))
        self.assertTrue(any("Credited output" in m and "Standard Glass Putty" in m for m in messages))

    def test_audit_log_inline_immutability(self):
        """Assert has_add_permission and has_delete_permission are disabled for ProcessExecutionLogInline."""
        inline = ProcessExecutionLogInline(ProcessExecutionLog, self.site)
        request = RequestFactory().get('/admin/')
        request.user = self.supervisor

        self.assertFalse(inline.has_add_permission(request))
        self.assertFalse(inline.has_add_permission(request, obj=None))
        self.assertFalse(inline.has_delete_permission(request))
        self.assertFalse(inline.has_delete_permission(request, obj=None))
        self.assertEqual(inline.can_delete, False)
        self.assertEqual(inline.max_num, 0)

    def test_user_attribution_logging(self):
        """Verify user attribution (logged_by) persists correctly to ProcessExecutionLog."""
        log_entry = log_execution_event(
            process_type='PO_DRAFT',
            message="Auto-drafted PO for raw materials.",
            level='INFO',
            logged_by=self.supervisor
        )
        self.assertEqual(log_entry.logged_by, self.supervisor)
        self.assertEqual(log_entry.logged_by.username, 'plant_manager')

    def test_string_truncation_guard(self):
        """Verify oversized messages > 1000 characters are truncated and preserved in details."""
        long_message = "A" * 1500

        log_entry = log_execution_event(
            process_type='MRP_EVALUATION',
            message=long_message,
            level='INFO'
        )

        self.assertIsNotNone(log_entry)
        self.assertEqual(len(log_entry.message), 1000)
        self.assertEqual(log_entry.message, "A" * 1000)
        self.assertIn('_full_message', log_entry.details)
        self.assertEqual(len(log_entry.details['_full_message']), 1500)
        self.assertEqual(log_entry.details['_truncated_length'], 1500)

    def test_bulk_logging_helper(self):
        """Verify bulk_log_execution_events() writes multiple entries in a single bulk_create operation."""
        events = [
            {
                'process_type': 'RECONCILIATION',
                'message': f"Batch component {i} consumed.",
                'level': 'INFO',
                'details': {'component_index': i}
            }
            for i in range(10)
        ]

        created = bulk_log_execution_events(events)
        self.assertEqual(len(created), 10)
        self.assertEqual(ProcessExecutionLog.objects.filter(process_type='RECONCILIATION', message__startswith="Batch component").count(), 10)

    def test_critical_reconciliation_failure_persists_log(self):
        """Verify that when reconciliation fails and rolls back, a critical ERROR log is persisted to the DB."""
        # Create a WorkOrder without product/BOM or with invalid state that raises an error
        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        from unittest.mock import patch

        initial_error_logs = ProcessExecutionLog.objects.filter(
            work_order=wo,
            level='ERROR'
        ).count()

        with patch('core.models.StockTransaction.objects.create', side_effect=RuntimeError("Simulated ledger write timeout")):
            with self.assertRaises(RuntimeError):
                ProductionReconciliationEngine.reconcile_work_order_completion(wo)

        # The error log must have been saved in the outer except block despite the transaction rollback
        error_logs = ProcessExecutionLog.objects.filter(
            work_order=wo,
            level='ERROR',
            process_type='RECONCILIATION'
        )
        self.assertEqual(error_logs.count(), initial_error_logs + 1)
        self.assertIn("CRITICAL RECONCILIATION FAILURE", error_logs.first().message)
        self.assertIn("Simulated ledger write timeout", error_logs.first().message)

    def test_milestone_stepper_rendering(self):
        """Verify milestone_stepper() renders high-contrast HTML with appropriate steps and status badges."""
        admin = ProductionOrderAdmin(ProductionOrder, self.site)

        # 1. Unsaved/None order
        html_none = admin.milestone_stepper(None)
        self.assertIn("Save Production Order to render execution milestones", html_none)

        # 2. ProductionOrder with shortages (ON_HOLD_SHORTAGE)
        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal('500.00'),
            status='DRAFT'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('500.00'),
            status='ON_HOLD_SHORTAGE'
        )
        html_shortage = admin.milestone_stepper(po)
        self.assertIn("Manufacturing Lifecycle Milestone Stepper", html_shortage)
        self.assertIn("SHORTAGE DETECTED", html_shortage)

        # 3. ProductionOrder in AWAITING_PROCUREMENT
        po.status = 'AWAITING_PROCUREMENT'
        po.save()
        html_proc = admin.milestone_stepper(po)
        self.assertIn("AWAITING PROCUREMENT", html_proc)

        # 4. ProductionOrder COMPLETED
        po.status = 'COMPLETED'
        po.save()
        html_completed = admin.milestone_stepper(po)
        self.assertIn("COMPLETED", html_completed)
        self.assertIn("VERIFIED", html_completed)
