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
from django.urls import reverse

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
        """Complete production reconciliation on a Work Order and assert consolidated Phase 3 SUCCESS completion log."""
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

        summary = ProductionReconciliationEngine.reconcile_work_order_completion(wo, user=self.supervisor)
        self.assertFalse(summary.get('skipped', False))

        recon_logs = ProcessExecutionLog.objects.filter(
            work_order=wo,
            process_type='RECONCILIATION'
        )
        # Exactly ONE unified Phase 3 completion record must be created
        self.assertEqual(recon_logs.count(), 1)
        log = recon_logs.first()
        self.assertEqual(log.level, 'SUCCESS')
        self.assertEqual(log.logged_by, self.supervisor)
        self.assertIn("Phase 3 Reconciliation Complete", log.message)
        self.assertIn("Produced 100.00 kg", log.message)
        self.assertIn("Standard Glass Putty 1000kg", log.message)
        self.assertEqual(log.details['phase'], 'PHASE_3_RECONCILIATION_COMPLETION')
        self.assertEqual(log.details['output_quantity'], 100.0)
        self.assertEqual(log.details['inventory_shift']['initial'], 0.0)
        self.assertEqual(log.details['inventory_shift']['final'], 100.0)
        self.assertIn('residuals_released', log.details)
        self.assertIn('avco_shift', log.details)

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

    def test_phase2_incremental_consumption_single_aggregated_log(self):
        """Verify Phase 2 incremental material consumption produces a single aggregated log with full component details."""
        self.cc_inv.quantity_available = Decimal('500.00')
        self.cc_inv.save()
        self.oil_inv.quantity_available = Decimal('200.00')
        self.oil_inv.save()

        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('100.00'),
            status='IN_PROGRESS'
        )

        # Allocate stock first (Phase 1)
        wo.process_inventory()

        initial_log_count = ProcessExecutionLog.objects.filter(work_order=wo).count()

        # Update actuals with over-consumption on Calcium Carbonate (85kg vs 80kg planned)
        lines = list(wo.material_lines.all())
        for line in lines:
            if line.component == self.calcium_carbonate:
                line.quantity_actual = Decimal('85.00')
            else:
                line.quantity_actual = Decimal('20.00')
            line.save(update_fields=['quantity_actual'])

        # Trigger Phase 2 deduction pass
        wo.process_inventory()

        # Assert exactly ONE consolidated log was created
        phase2_logs = ProcessExecutionLog.objects.filter(
            work_order=wo,
            process_type='RECONCILIATION',
            message__startswith="Phase 2 Material Consumption"
        )
        self.assertEqual(phase2_logs.count(), 1)
        log = phase2_logs.first()
        self.assertEqual(log.level, 'WARNING')  # Overconsumption detected (85 > 80)
        self.assertIn("Processed 2 component(s)", log.message)
        self.assertIn("Net Delta: +105.00 units", log.message)

        # Assert structured details
        details = log.details
        self.assertEqual(details['phase'], 'PHASE_2_CONSUMPTION')
        self.assertEqual(details['component_count'], 2)
        self.assertTrue(details['has_overconsumption'])
        self.assertEqual(len(details['components']), 2)

        # Verify component breakdown
        comp_names = [c['material_name'] for c in details['components']]
        self.assertIn("Calcium Carbonate Pure", comp_names)
        self.assertIn("Raw Linseed Oil Grade A", comp_names)

        # Idempotency check: A second pass with no new deltas must create zero additional logs
        wo.process_inventory()
        self.assertEqual(phase2_logs.count(), 1)

    def test_phase3_consolidated_reconciliation_output_shift_and_avco(self):
        """Verify Phase 3 completion produces a single SUCCESS record with dynamic currency and complete JSON shift metrics."""
        from django.conf import settings
        currency_sym = getattr(settings, 'CURRENCY_SYMBOL', 'KSh')

        self.cc_inv.quantity_available = Decimal('1000.00')
        self.cc_inv.unit_cost = Decimal('2.00')
        self.cc_inv.save()
        self.oil_inv.quantity_available = Decimal('500.00')
        self.oil_inv.unit_cost = Decimal('5.00')
        self.oil_inv.save()

        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal('200.00'),
            actual_quantity_produced=Decimal('200.00'),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('200.00'),
            status='IN_PROGRESS'
        )

        summary = ProductionReconciliationEngine.reconcile_work_order_completion(wo, user=self.supervisor)
        self.assertFalse(summary.get('skipped', False))

        phase3_logs = ProcessExecutionLog.objects.filter(
            work_order=wo,
            process_type='RECONCILIATION',
            level='SUCCESS'
        )
        self.assertEqual(phase3_logs.count(), 1)
        log = phase3_logs.first()
        self.assertEqual(log.logged_by, self.supervisor)
        self.assertIn(currency_sym, log.message)
        self.assertIn("Phase 3 Reconciliation Complete", log.message)
        self.assertIn("Produced 200.00 kg", log.message)

        details = log.details
        self.assertEqual(details['phase'], 'PHASE_3_RECONCILIATION_COMPLETION')
        self.assertEqual(details['output_product']['name'], self.finished_putty.name)
        self.assertEqual(details['output_quantity'], 200.0)
        self.assertEqual(details['inventory_shift']['initial'], 0.0)
        self.assertEqual(details['inventory_shift']['final'], 200.0)
        self.assertEqual(details['inventory_shift']['net_shift'], 200.0)
        self.assertIn('avco_shift', details)
        self.assertIn('residuals_released', details)
        self.assertIsNotNone(details['transaction_id'])

    def test_cascade_and_idempotency_deduplication(self):
        """Verify duplicate ORDER_SYNC events and redundant reconciliation calls are safely suppressed."""
        # 1. Unchanged MRP resolution state must not log duplicate ORDER_SYNC
        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal('100.00'),
            status='DRAFT'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('100.00'),
            status='PENDING'
        )
        evaluate_mrp_shortages(po)

        # Baseline count of ORDER_SYNC logs
        sync_logs_before = ProcessExecutionLog.objects.filter(production_order=po, process_type='ORDER_SYNC').count()

        # Trigger update_mrp_resolution_state() repeatedly with no state change
        po.update_mrp_resolution_state()
        po.update_mrp_resolution_state()
        sync_logs_after = ProcessExecutionLog.objects.filter(production_order=po, process_type='ORDER_SYNC').count()
        self.assertEqual(sync_logs_before, sync_logs_after)

        # 2. sync_child_packaging_expectations() with 0 child packaging orders must emit 0 ORDER_SYNC logs
        wo.sync_child_packaging_expectations()
        child_sync_logs = ProcessExecutionLog.objects.filter(work_order=wo, process_type='ORDER_SYNC')
        self.assertEqual(child_sync_logs.count(), 0)

        # 3. Repeated reconciliation on completed order must skip and emit 0 logs
        self.cc_inv.quantity_available = Decimal('500.00')
        self.cc_inv.save()
        self.oil_inv.quantity_available = Decimal('200.00')
        self.oil_inv.save()
        wo.status = 'IN_PROGRESS'
        wo.actual_quantity_produced = Decimal('100.00')
        wo.save()

        res1 = ProductionReconciliationEngine.reconcile_work_order_completion(wo)
        self.assertFalse(res1.get('skipped', False))
        log_count_completed = ProcessExecutionLog.objects.filter(work_order=wo, level='SUCCESS').count()
        self.assertEqual(log_count_completed, 1)

        # Second and third calls must skip safely
        res2 = ProductionReconciliationEngine.reconcile_work_order_completion(wo)
        res3 = ProductionReconciliationEngine.reconcile_work_order_completion(wo)
        self.assertTrue(res2.get('skipped', False))
        self.assertTrue(res3.get('skipped', False))
        self.assertEqual(ProcessExecutionLog.objects.filter(work_order=wo, level='SUCCESS').count(), 1)

    def test_contextvar_user_attribution_and_token_reset(self):
        """Verify ContextVar user attribution works seamlessly and token reset prevents leakage."""
        from core.services.logging_service import (
            set_current_authenticated_user,
            reset_current_authenticated_user,
            get_current_authenticated_user
        )

        # Context starts as None
        self.assertIsNone(get_current_authenticated_user())

        # Set user in context
        token = set_current_authenticated_user(self.supervisor)
        try:
            self.assertEqual(get_current_authenticated_user(), self.supervisor)
            # Log event without explicit logged_by parameter
            log_entry = log_execution_event(
                process_type='ORDER_SYNC',
                message="ContextVar attribution test event."
            )
            self.assertIsNotNone(log_entry)
            self.assertEqual(log_entry.logged_by, self.supervisor)
        finally:
            reset_current_authenticated_user(token)

        # After reset, context returns to None
        self.assertIsNone(get_current_authenticated_user())

    def test_deterministic_log_code_generation(self):
        """Verify log_code is deterministically derived from log_id as PEL-XXXXX with unique constraint."""
        log1 = log_execution_event(
            process_type='MRP_EVALUATION',
            message="Test deterministic log code 1."
        )
        log2 = log_execution_event(
            process_type='ORDER_SYNC',
            message="Test deterministic log code 2."
        )
        self.assertIsNotNone(log1)
        self.assertIsNotNone(log2)

        expected_code1 = f"PEL-{log1.log_id:05d}"
        expected_code2 = f"PEL-{log2.log_id:05d}"
        self.assertEqual(log1.log_code, expected_code1)
        self.assertEqual(log2.log_code, expected_code2)
        self.assertIn(expected_code1, str(log1))

        # Test querying by log_code
        queried_log = ProcessExecutionLog.objects.filter(log_code=expected_code1).first()
        self.assertEqual(queried_log, log1)

    def test_phase1_stock_allocation_logging_and_competing_runs(self):
        """Verify Phase 1 stock allocation logs shifts, captures competing runs, and is strictly idempotent."""
        # Top up stock so both work orders can allocate
        self.cc_inv.quantity_available = Decimal('1000.00')
        self.cc_inv.save()
        self.oil_inv.quantity_available = Decimal('500.00')
        self.oil_inv.save()

        # 1. Start WorkOrder 1 (holds 80kg CC and 20L Oil)
        wo1 = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        po1 = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo1,
            quantity=Decimal('100.00'),
            status='IN_PROGRESS'
        )
        wo1.process_inventory()
        self.assertTrue(wo1.is_inventory_allocated)

        wo1_alloc_log = ProcessExecutionLog.objects.filter(
            work_order=wo1,
            process_type='STOCK_ALLOCATION'
        ).first()
        self.assertIsNotNone(wo1_alloc_log)
        self.assertEqual(wo1_alloc_log.level, 'SUCCESS')
        self.assertEqual(wo1_alloc_log.event_title, 'Stock Allocation & Component Reservation')
        self.assertEqual(wo1_alloc_log.details['phase'], 'PHASE_1_STOCK_ALLOCATION')
        self.assertEqual(wo1_alloc_log.details['target_batch_quantity'], 100.0)

        # 2. Start WorkOrder 2 (target 50 units -> needs 40kg CC and 10L Oil)
        wo2 = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal('50.00'),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        po2 = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo2,
            quantity=Decimal('50.00'),
            status='IN_PROGRESS'
        )
        wo2.process_inventory()
        self.assertTrue(wo2.is_inventory_allocated)

        wo2_alloc_logs = ProcessExecutionLog.objects.filter(
            work_order=wo2,
            process_type='STOCK_ALLOCATION'
        )
        self.assertEqual(wo2_alloc_logs.count(), 1)
        wo2_log = wo2_alloc_logs.first()
        self.assertEqual(wo2_log.level, 'SUCCESS')
        self.assertIn("Active competing run(s): 1", wo2_log.message)

        # Inspect details payload structure
        details = wo2_log.details
        self.assertEqual(details['phase'], 'PHASE_1_STOCK_ALLOCATION')
        self.assertEqual(details['target_batch_quantity'], 50.0)
        self.assertEqual(len(details['allocated_components']), 2)

        cc_entry = next(c for c in details['allocated_components'] if c['component_name'] == "Calcium Carbonate Pure")
        self.assertEqual(cc_entry['unit_req'], 0.8)
        self.assertEqual(cc_entry['allocated_qty'], 40.0)
        self.assertEqual(len(cc_entry['competing_allocations']), 1)
        self.assertEqual(cc_entry['competing_allocations'][0]['order_code'], wo1.work_order_code)
        self.assertEqual(cc_entry['competing_allocations'][0]['held_qty'], 80.0)

        # 3. Idempotent Transition Guard: Subsequent saves must not duplicate STOCK_ALLOCATION log
        wo2.process_inventory()
        wo2.save()
        self.assertEqual(wo2_alloc_logs.count(), 1)

    def test_work_order_execution_history_view(self):
        """Verify the dedicated execution history admin route renders HTTP 200 with timeline context."""
        self.cc_inv.quantity_available = Decimal('500.00')
        self.cc_inv.save()
        self.oil_inv.quantity_available = Decimal('200.00')
        self.oil_inv.save()

        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('100.00'),
            status='IN_PROGRESS'
        )
        wo.process_inventory()

        self.client.force_login(self.supervisor)
        url = reverse('admin:core_workorder_execution_history', args=[wo.pk])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertIn('work_order', response.context)
        self.assertIn('logs', response.context)
        self.assertIn('milestones', response.context)
        self.assertIn('competing_runs', response.context)

        content = response.content.decode('utf-8')
        self.assertIn(wo.work_order_code, content)
        self.assertIn("Execution Pipeline Status", content)
        self.assertIn("Phase 1: Allocation", content)
        self.assertIn("Phase 1 Stock Allocation Complete", content)

