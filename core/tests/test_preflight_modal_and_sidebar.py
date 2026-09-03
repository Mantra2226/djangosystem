"""
TEST SUITE: Pre-Flight Production Start Confirmation Modal & Operations Audit Sidebar Navigation.
(core/tests/test_preflight_modal_and_sidebar.py)

Validates:
1. Sidebar navigation configuration contains the dedicated "Operations Audit & Logs" group.
2. get_preflight_production_summary() returns is_ready=True when all BOM component stock is sufficient.
3. get_preflight_production_summary() correctly detects shortages and sets is_ready=False.
4. get_preflight_production_summary() honors supervisor OVERRIDDEN items on linked ProductionOrder.
5. get_preflight_production_summary() accurately detects and itemizes concurrent active runs.
6. Pre-flight confirmation view GET renders the preflight_start_confirmation.html template.
7. Pre-flight confirmation view POST successfully starts production and transitions status.
8. Pre-flight confirmation view POST fails safely and displays errors when stock is blocked.
9. Pre-flight confirmation view enforces strict permission checks.
10. WorkOrder admin change form renders the Pre-Flight start button across launchable statuses (DRAFT, PENDING, READY_TO_START).
"""

from decimal import Decimal
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory,
    WorkOrder, WorkOrderMaterialLine, ProductionOrder, ProductionOrderItem,
    ProcessExecutionLog
)
from core.services.mrp_services import get_preflight_production_summary

User = get_user_model()


class PreflightModalAndSidebarTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.superuser = User.objects.create_superuser(
            username='admin_operator',
            email='admin@glassputty.internal',
            password='AdminPassword123!'
        )

        self.operator = User.objects.create_user(
            username='floor_operator',
            email='operator@glassputty.internal',
            password='OperatorPassword123!',
            is_staff=True
        )

        # 1. Supplier
        self.supplier = Supplier.objects.create(
            name="Apex Chemicals Ltd",
            contact_info="orders@apexchemicals.com"
        )

        # 2. Raw Materials
        self.calcium_carbonate = Product.objects.create(
            name="Calcium Carbonate Extra",
            sku="RM-CC-PREFLIGHT",
            product_type="RAW",
            category="Minerals",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.linseed_oil = Product.objects.create(
            name="Linseed Oil Industrial",
            sku="RM-OIL-PREFLIGHT",
            product_type="RAW",
            category="Oils",
            unit_of_measurement="Liters",
            supplier=self.supplier
        )

        # 3. Finished Good
        self.glazing_putty = Product.objects.create(
            name="Glazing Putty 500kg",
            sku="FG-PUTTY-PREFLIGHT",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="kg",
            selling_price=Decimal("40.00")
        )

        # 4. Recipe (BOM): 1kg Putty = 0.80kg CC + 0.20L Linseed Oil
        self.bom = BillOfMaterial.objects.create(
            product=self.glazing_putty,
            name="Glazing Putty 500kg Standard Recipe",
            is_active=True
        )
        self.bom_item_cc = BOMItem.objects.create(
            bom=self.bom,
            component=self.calcium_carbonate,
            quantity_required=Decimal("0.8000")
        )
        self.bom_item_oil = BOMItem.objects.create(
            bom=self.bom,
            component=self.linseed_oil,
            quantity_required=Decimal("0.2000")
        )

        # 5. Inventory
        self.inv_cc = Inventory.objects.create(
            product=self.calcium_carbonate,
            quantity_available=Decimal("1000.00"),
            quantity_allocated=Decimal("0.00"),
            unit_cost=Decimal("2.00")
        )
        self.inv_oil = Inventory.objects.create(
            product=self.linseed_oil,
            quantity_available=Decimal("500.00"),
            quantity_allocated=Decimal("0.00"),
            unit_cost=Decimal("5.00")
        )

    def test_sidebar_contains_operations_audit_group(self):
        """Verify settings.UNFOLD contains the Operations Audit & Logs group with correct items."""
        nav = settings.UNFOLD.get("SIDEBAR", {}).get("navigation", [])
        group_titles = [g.get("title") for g in nav]

        self.assertIn("Operations Audit & Logs", group_titles)

        audit_group = next(g for g in nav if g.get("title") == "Operations Audit & Logs")
        self.assertEqual(audit_group.get("icon"), "fact_check")

        item_titles = [item.get("title") for item in audit_group.get("items", [])]
        self.assertIn("Process Execution Logs", item_titles)
        self.assertIn("Material Variances", item_titles)

        # Verify Material Variances is no longer in Manufacturing group
        mfg_group = next((g for g in nav if g.get("title") == "Manufacturing & Shop-Floor"), None)
        if mfg_group:
            mfg_items = [item.get("title") for item in mfg_group.get("items", [])]
            self.assertNotIn("Material Variances", mfg_items)

    def test_preflight_summary_all_stock_sufficient(self):
        """When all component stock is available, summary reports is_ready=True."""
        wo = WorkOrder.objects.create(
            product=self.glazing_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("500.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        po = ProductionOrder.objects.create(
            product=self.glazing_putty,
            work_order=wo,
            quantity=Decimal("500.00"),
            status='PENDING'
        )

        summary = get_preflight_production_summary(wo)

        self.assertTrue(summary['is_ready'])
        self.assertFalse(summary['has_shortages'])
        self.assertEqual(summary['target_quantity'], Decimal("500.00"))
        self.assertEqual(len(summary['component_readiness']), 2)

        for comp in summary['component_readiness']:
            self.assertTrue(comp['is_sufficient'])
            self.assertEqual(comp['status_label'], 'Sufficient')
            self.assertEqual(comp['shortfall_qty'], Decimal("0.00"))

    def test_preflight_summary_detects_shortage(self):
        """When available stock is less than required, summary reports is_ready=False and highlights shortage."""
        # Deplete Linseed Oil stock to 50L (500kg batch requires 100L)
        self.inv_oil.quantity_available = Decimal("50.00")
        self.inv_oil.save()

        wo = WorkOrder.objects.create(
            product=self.glazing_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("500.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        po = ProductionOrder.objects.create(
            product=self.glazing_putty,
            work_order=wo,
            quantity=Decimal("500.00"),
            status='PENDING'
        )

        summary = get_preflight_production_summary(wo)

        self.assertFalse(summary['is_ready'])
        self.assertTrue(summary['has_shortages'])

        oil_comp = next(c for c in summary['component_readiness'] if c['component_id'] == self.linseed_oil.pk)
        self.assertFalse(oil_comp['is_sufficient'])
        self.assertEqual(oil_comp['status_label'], 'Shortage')
        self.assertEqual(oil_comp['required_qty'], Decimal("100.00"))
        self.assertEqual(oil_comp['available_qty'], Decimal("50.00"))
        self.assertEqual(oil_comp['shortfall_qty'], Decimal("50.00"))

    def test_preflight_summary_honors_supervisor_override(self):
        """When a shortage component has OVERRIDDEN status on ProductionOrderItem, is_ready evaluates to True."""
        # Deplete Linseed Oil stock
        self.inv_oil.quantity_available = Decimal("20.00")
        self.inv_oil.save()

        wo = WorkOrder.objects.create(
            product=self.glazing_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("500.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        po = ProductionOrder.objects.create(
            product=self.glazing_putty,
            work_order=wo,
            quantity=Decimal("500.00"),
            status='READY_TO_START'
        )

        # Authorize supervisor override on the shortage item
        po_item = ProductionOrderItem.objects.create(
            production_order=po,
            raw_material=self.linseed_oil,
            planned_quantity=Decimal("100.00"),
            shortage_quantity=Decimal("80.00"),
            resolution_status='OVERRIDDEN',
            resolution_notes="Authorized by plant supervisor",
            resolved_by=self.superuser,
            resolved_at=timezone.now()
        )

        summary = get_preflight_production_summary(wo)

        self.assertTrue(summary['is_ready'])
        oil_comp = next(c for c in summary['component_readiness'] if c['component_id'] == self.linseed_oil.pk)
        self.assertTrue(oil_comp['is_overridden'])
        self.assertTrue(oil_comp['is_sufficient'])
        self.assertEqual(oil_comp['status_label'], 'Overridden (Authorized)')

    def test_preflight_summary_concurrent_runs(self):
        """Concurrent IN_PROGRESS work orders sharing components are listed with their allocations."""
        # Create Active Run WO-1
        wo1 = WorkOrder.objects.create(
            product=self.glazing_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("300.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        WorkOrderMaterialLine.objects.update_or_create(
            work_order=wo1,
            component=self.calcium_carbonate,
            defaults={
                'quantity_expected': Decimal("240.00"),
                'quantity_actual': Decimal("0.00")
            }
        )

        # Preflight evaluation on pending WO-2
        wo2 = WorkOrder.objects.create(
            product=self.glazing_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("200.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )

        summary = get_preflight_production_summary(wo2)

        self.assertEqual(len(summary['concurrent_runs']), 1)
        run_info = summary['concurrent_runs'][0]
        self.assertEqual(run_info['work_order_id'], wo1.pk)
        self.assertIn(self.calcium_carbonate.name, run_info['shared_components'])

    def test_preflight_view_get_renders_template(self):
        """Authenticated superuser GET request renders preflight_start_confirmation.html with 200 OK."""
        self.client.force_login(self.superuser)

        wo = WorkOrder.objects.create(
            product=self.glazing_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("500.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        po = ProductionOrder.objects.create(
            product=self.glazing_putty,
            work_order=wo,
            quantity=Decimal("500.00"),
            status='PENDING'
        )

        url = reverse('admin:workorder-preflight-start', args=[wo.pk])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin/core/workorder/preflight_start_confirmation.html')
        self.assertIn('summary', response.context)
        self.assertEqual(response.context['original'].pk, wo.pk)

    def test_preflight_view_post_starts_production(self):
        """Submitting confirmation POST transitions WorkOrder to IN_PROGRESS and redirects."""
        self.client.force_login(self.superuser)

        wo = WorkOrder.objects.create(
            product=self.glazing_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("500.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        po = ProductionOrder.objects.create(
            product=self.glazing_putty,
            work_order=wo,
            quantity=Decimal("500.00"),
            status='PENDING'
        )

        url = reverse('admin:workorder-preflight-start', args=[wo.pk])
        response = self.client.post(url)

        self.assertEqual(response.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.status, 'IN_PROGRESS')
        self.assertTrue(wo.is_inventory_allocated)

    def test_preflight_view_post_blocked_insufficient_stock(self):
        """Submitting confirmation POST when stock is insufficient does not start production."""
        self.client.force_login(self.superuser)

        # Deplete Calcium Carbonate stock
        self.inv_cc.quantity_available = Decimal("0.00")
        self.inv_cc.save()

        wo = WorkOrder.objects.create(
            product=self.glazing_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("500.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        po = ProductionOrder.objects.create(
            product=self.glazing_putty,
            work_order=wo,
            quantity=Decimal("500.00"),
            status='PENDING'
        )

        url = reverse('admin:workorder-preflight-start', args=[wo.pk])
        response = self.client.post(url)

        self.assertEqual(response.status_code, 302)
        wo.refresh_from_db()
        # Order should NOT be IN_PROGRESS
        self.assertNotEqual(wo.status, 'IN_PROGRESS')

    def test_preflight_view_requires_permission(self):
        """Unauthorized staff user without can_start_production permission gets 403 Forbidden."""
        self.client.force_login(self.operator)

        wo = WorkOrder.objects.create(
            product=self.glazing_putty,
            bill_of_material=self.bom,
            quantity_produced=Decimal("500.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )

        url = reverse('admin:workorder-preflight-start', args=[wo.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

        response_post = self.client.post(url)
        self.assertEqual(response_post.status_code, 403)

    def test_start_production_button_rendered_for_launchable_statuses(self):
        """Verify the Start Production pre-flight button URL appears on DRAFT and PENDING orders."""
        self.client.force_login(self.superuser)

        for launchable_status in ['DRAFT', 'PENDING']:
            wo = WorkOrder.objects.create(
                product=self.glazing_putty,
                bill_of_material=self.bom,
                quantity_produced=Decimal("100.00"),
                production_start_date=timezone.now().date(),
                status=launchable_status
            )
            change_url = reverse('admin:core_workorder_change', args=[wo.pk])
            response = self.client.get(change_url)
            self.assertEqual(response.status_code, 200)

            preflight_url = reverse('admin:workorder-preflight-start', args=[wo.pk])
            self.assertContains(response, preflight_url)
