"""
TEST SUITE FOR DJANGO UNFOLD MODERNIZATION (core/tests/test_admin_unfold.py)

Validates Unfold ModelAdmin registrations, dashboard KPI callbacks,
tabbed fieldset configurations, native action buttons, and security immutability guards.
"""

from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase, Client, RequestFactory
from django.contrib.admin.sites import site
from django.contrib.auth.models import User, Group, Permission
from django.urls import reverse
from django.utils import timezone

from unfold.admin import ModelAdmin, TabularInline

from core.models import (
    Supplier, Product, PurchaseOrder, PurchaseOrderItem, ProcurementOrder, Inventory,
    StockTransaction, Employee, ProductionOrder, Customer, SalesOrder, SalesOrderItem,
    DispatchRecord, SalesInvoice, Return, MaterialVarianceRecord, FinanceEntry, WorkOrder,
    BillOfMaterial, BOMItem, SalesInvoicePayments, PurchasePayment, WorkOrderMaterialLine,
    DocumentSequence, SalesInvoiceLine, CreditNote, CreditNoteLine
)
from core.dashboard import dashboard_callback
from core.admin import (
    SalesOrderAdmin, SalesInvoiceAdmin, CreditNoteAdmin, WorkOrderAdmin,
    ProductionOrderAdmin, InventoryAdmin
)


class UnfoldAdminIntegrationTests(TestCase):
    """
    Validates that the Glass Putty ERP admin is fully integrated with django-unfold.
    """

    def setUp(self):
        self.client = Client()
        self.factory = RequestFactory()

        # Superuser
        self.superuser = User.objects.create_superuser(
            username='admin_superuser',
            email='admin@glassputty.com',
            password='Password123!'
        )

        # Staff user with sales/billing permissions
        self.billing_user = User.objects.create_user(
            username='billing_officer',
            email='billing@glassputty.com',
            password='Password123!',
            is_staff=True
        )
        self.billing_group, _ = Group.objects.get_or_create(name='Billing Officer')
        self.billing_user.groups.add(self.billing_group)

        # Standard operator
        self.operator_user = User.objects.create_user(
            username='floor_operator',
            email='operator@glassputty.com',
            password='Password123!',
            is_staff=True
        )
        self.operator_group, _ = Group.objects.get_or_create(name='Shop-Floor Operator')
        self.operator_user.groups.add(self.operator_group)

        # Setup standard catalog items
        self.supplier = Supplier.objects.create(
            name="Apex Raw Chemicals Ltd",
            contact_info="orders@apexchemicals.com"
        )
        self.raw_putty = Product.objects.create(
            name="Bulk Putty Base",
            sku="RAW-PUTTY-001",
            product_type="INTERMEDIATE",
            category="Putty",
            unit_of_measurement="kg"
        )
        self.tin_pkg = Product.objects.create(
            name="Empty 5kg Tin",
            sku="PKG-TIN-5KG",
            product_type="RAW",
            category="Packaging",
            unit_of_measurement="tin",
            supplier=self.supplier
        )
        self.finished_putty = Product.objects.create(
            name="Glass Putty 5kg Tin",
            sku="FG-PUTTY-5KG",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="tin",
            selling_price=Decimal("1500.00")
        )

        # Setup Customer
        self.customer = Customer.objects.create(
            customer_name="Nairobi Glass Builders",
            contact_info="procurement@nairobiglass.co.ke",
            shipping_address="Industrial Area, Road C, Nairobi"
        )

    def test_all_core_models_registered_with_unfold_modeladmin(self):
        """Verify that all core ERP models are registered using unfold.admin.ModelAdmin."""
        core_models = [
            Supplier, Product, PurchaseOrder, ProcurementOrder, Inventory,
            StockTransaction, Employee, ProductionOrder, Customer, SalesOrder,
            DispatchRecord, SalesInvoice, Return, MaterialVarianceRecord,
            FinanceEntry, WorkOrder, BillOfMaterial, DocumentSequence, CreditNote
        ]
        for model in core_models:
            self.assertIn(model, site._registry, f"{model.__name__} is not registered in Django admin.")
            admin_instance = site._registry[model]
            self.assertIsInstance(
                admin_instance,
                ModelAdmin,
                f"{model.__name__} admin class {admin_instance.__class__.__name__} does not inherit from unfold.admin.ModelAdmin."
            )

    def test_dashboard_callback_returns_expected_kpi_cards(self):
        """Verify that core.dashboard.dashboard_callback calculates accurate live KPI metrics."""
        # Create test inventory
        Inventory.objects.create(
            product=self.finished_putty,
            quantity_available=Decimal("100.00"),
            unit_cost=Decimal("800.00"),
            location="Warehouse A"
        )
        # Create test SalesInvoice
        inv = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_number="SINV-TEST-001",
            subtotal=Decimal("30000.00"),
            tax_amount=Decimal("4800.00"),
            total_amount=Decimal("34800.00"),
            status="POSTED"
        )
        # Create test FinanceEntry
        FinanceEntry.objects.create(
            entry_type='REVENUE',
            category='SALES',
            amount=Decimal("34800.00"),
            entry_date=timezone.now().date(),
            sales_invoice=inv
        )
        # Create test WorkOrder
        WorkOrder.objects.create(
            product=self.finished_putty,
            quantity_produced=Decimal("20.00"),
            status='IN_PROGRESS'
        )

        request = self.factory.get('/admin/')
        request.user = self.superuser
        context = {}

        result_context = dashboard_callback(request, context)

        self.assertIn("kpi_cards", result_context)
        cards = result_context["kpi_cards"]
        self.assertEqual(len(cards), 6)

        self.assertEqual(result_context["active_work_orders_count"], 1)
        self.assertGreaterEqual(result_context["total_warehouse_valuation"], Decimal("80000.00"))
        self.assertGreaterEqual(result_context["total_receivables"], Decimal("34800.00"))
        self.assertGreaterEqual(result_context["mtd_revenue"], Decimal("34800.00"))

    def test_sales_order_confirm_action_topbar(self):
        """Verify the SalesOrder Confirm Order action executes cleanly."""
        order = SalesOrder.objects.create(
            customer=self.customer,
            order_number="SO-202608-TEST",
            invoicing_policy="ORDER_BASED",
            status="draft"
        )
        SalesOrderItem.objects.create(
            sales_order=order,
            product=self.finished_putty,
            quantity_ordered=Decimal("10.00"),
            unit_price=Decimal("1500.00")
        )

        self.client.force_login(self.superuser)

        # Trigger confirm action via detail action / custom URL
        response = self.client.get(f'/admin/core/salesorder/{order.pk}/confirm-order/', follow=True)
        self.assertEqual(response.status_code, 200)

        order.refresh_from_db()
        self.assertEqual(order.status, 'approved')
        self.assertTrue(SalesInvoice.objects.filter(sales_order=order).exists())

    def test_sales_invoice_pdf_action_button(self):
        """Verify that the Sales Invoice PDF action streams a valid PDF file."""
        invoice = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_number="SINV-PDF-001",
            subtotal=Decimal("15000.00"),
            tax_amount=Decimal("2400.00"),
            total_amount=Decimal("17400.00"),
            status="POSTED"
        )
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.finished_putty,
            quantity=Decimal("10.00"),
            unit_price=Decimal("1500.00"),
            subtotal=Decimal("15000.00"),
            tax_amount=Decimal("2400.00"),
            total_price=Decimal("17400.00")
        )

        admin_instance = site._registry[SalesInvoice]
        request = self.factory.get(f'/admin/core/salesinvoice/{invoice.pk}/download-pdf/')
        request.user = self.superuser

        response = admin_instance.action_download_pdf(request, invoice.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn(b'%PDF', response.content[:10])

    def test_credit_note_pdf_action_button(self):
        """Verify that the Credit Note PDF action streams a valid PDF file."""
        invoice = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_number="SINV-CN-PDF-001",
            subtotal=Decimal("15000.00"),
            tax_amount=Decimal("2400.00"),
            total_amount=Decimal("17400.00"),
            status="POSTED"
        )
        credit_note = CreditNote.objects.create(
            invoice=invoice,
            customer=self.customer,
            credit_note_number="CN-PDF-001",
            subtotal=Decimal("3000.00"),
            tax_amount=Decimal("480.00"),
            total_amount=Decimal("3480.00"),
            status="POSTED",
            reason="Commercial return - defective packaging"
        )
        CreditNoteLine.objects.create(
            credit_note=credit_note,
            product=self.finished_putty,
            quantity=Decimal("2.00"),
            unit_price=Decimal("1500.00"),
            subtotal=Decimal("3000.00"),
            tax_amount=Decimal("480.00"),
            total_price=Decimal("3480.00")
        )

        admin_instance = site._registry[CreditNote]
        request = self.factory.get(f'/admin/core/creditnote/{credit_note.pk}/download-pdf/')
        request.user = self.superuser

        response = admin_instance.action_download_pdf(request, credit_note.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn(b'%PDF', response.content[:10])

    def test_sales_invoice_immutability_guards_preserved(self):
        """Verify non-superusers cannot delete or mutate posted invoices."""
        invoice = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_number="SINV-LOCK-001",
            subtotal=Decimal("15000.00"),
            tax_amount=Decimal("2400.00"),
            total_amount=Decimal("17400.00"),
            status="POSTED"
        )
        admin_instance = site._registry[SalesInvoice]

        request = self.factory.get(f'/admin/core/salesinvoice/{invoice.pk}/change/')
        request.user = self.billing_user

        # Non-superusers have no delete permission
        self.assertFalse(admin_instance.has_delete_permission(request, invoice))

        # All fields are read-only for posted invoice when accessed by non-superusers
        readonly = admin_instance.get_readonly_fields(request, invoice)
        self.assertIn('total_amount', readonly)
        self.assertIn('status', readonly)
        self.assertIn('customer', readonly)

    def test_admin_index_and_changelist_render_without_errors(self):
        """Ensure Unfold admin index and changelists render cleanly with HTTP 200."""
        self.client.force_login(self.superuser)

        endpoints = [
            '/admin/',
            '/admin/reports-dashboard/',
            '/reports/',
            '/admin/core/workorder/',
            '/admin/core/productionorder/',
            '/admin/core/billofmaterial/',
            '/admin/core/materialvariancerecord/',
            '/admin/core/product/',
            '/admin/core/inventory/',
            '/admin/core/stocktransaction/',
            '/admin/core/dispatchrecord/',
            '/admin/core/return/',
            '/admin/core/salesorder/',
            '/admin/core/salesinvoice/',
            '/admin/core/creditnote/',
            '/admin/core/customer/',
            '/admin/core/documentsequence/',
            '/admin/core/purchaseorder/',
            '/admin/core/procurementorder/',
            '/admin/core/purchaseinvoice/',
            '/admin/core/supplier/',
            '/admin/core/employee/',
            '/admin/core/financeentry/',
            '/admin/auth/user/',
            '/admin/auth/group/',
        ]

        for url in endpoints:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, f"Endpoint {url} failed with status {response.status_code}")

    def test_prevent_linking_completed_work_order_to_new_production_order(self):
        """Verify that ProductionOrder model validation prevents linking a COMPLETED WorkOrder."""
        completed_wo = WorkOrder.objects.create(
            product=self.finished_putty,
            category='PRODUCTION',
            quantity_produced=Decimal('50.00')
        )
        WorkOrder.objects.filter(pk=completed_wo.pk).update(status='COMPLETED')
        completed_wo.refresh_from_db()

        po = ProductionOrder(
            product=self.finished_putty,
            work_order=completed_wo,
            quantity=Decimal('50.00'),
            status='IN_PROGRESS'
        )

        with self.assertRaises(ValidationError) as ctx:
            po.clean()

        self.assertIn('work_order', ctx.exception.message_dict)
        error_msg = ctx.exception.message_dict['work_order'][0]
        self.assertIn("cannot be linked because it is already COMPLETED", error_msg)

    def test_allow_linking_active_work_order_to_production_order(self):
        """Verify that linking an active (DRAFT or IN_PROGRESS) WorkOrder succeeds cleanly."""
        active_wo = WorkOrder.objects.create(
            product=self.finished_putty,
            category='PRODUCTION',
            status='DRAFT',
            quantity_produced=Decimal('50.00')
        )

        po = ProductionOrder(
            product=self.finished_putty,
            work_order=active_wo,
            quantity=Decimal('50.00'),
            status='DRAFT'
        )
        # clean() should pass without raising ValidationError
        po.clean()

    def test_admin_form_completed_work_order_validation(self):
        """Verify that ProductionOrderAdminForm rejects completed work orders on form submission."""
        self.client.force_login(self.superuser)

        completed_wo = WorkOrder.objects.create(
            product=self.finished_putty,
            category='PRODUCTION',
            quantity_produced=Decimal('50.00')
        )
        WorkOrder.objects.filter(pk=completed_wo.pk).update(status='COMPLETED')
        completed_wo.refresh_from_db()

        post_data = {
            'product': self.finished_putty.pk,
            'work_order': completed_wo.pk,
            'quantity': '50.00',
            'status': 'DRAFT',
        }

        response = self.client.post('/admin/core/productionorder/add/', post_data)
        self.assertEqual(response.status_code, 200)  # Re-renders form with error
        form_errors = response.context['adminform'].form.errors
        self.assertIn('work_order', form_errors)
        self.assertIn("cannot be linked because it is already COMPLETED", str(form_errors['work_order']))
        self.assertFalse(ProductionOrder.objects.filter(work_order=completed_wo).exists())

    def test_mrp_resolution_pathways_viewer_renders_action_links(self):
        """Verify that mrp_resolution_pathways_viewer renders direct clickable action links without nested forms."""
        self.client.force_login(self.superuser)

        bom = BillOfMaterial.objects.create(product=self.finished_putty, is_active=True)
        BOMItem.objects.create(bom=bom, component=self.tin_pkg, quantity_required=Decimal('1.00'))

        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=bom,
            category='PRODUCTION',
            quantity_produced=Decimal('50.00')
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('50.00'),
            status='DRAFT'
        )

        response = self.client.get(f'/admin/core/productionorder/{po.pk}/change/')
        self.assertEqual(response.status_code, 200)
        # Verify action links are present in HTML
        self.assertContains(response, '/mrp_resolve_action/?production_order_id=')
        self.assertContains(response, 'Execute Option 1: Auto-Draft &amp; Open Purchase Order')

    def test_mrp_resolve_action_raw_autodraft_po_redirects_to_purchase_order(self):
        """Verify executing raw_autodraft_po generates PO and redirects directly to its change form without ProcurementOrder."""
        self.client.force_login(self.superuser)

        bom = BillOfMaterial.objects.create(product=self.finished_putty, is_active=True)
        BOMItem.objects.create(bom=bom, component=self.tin_pkg, quantity_required=Decimal('1.00'))

        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=bom,
            category='PRODUCTION',
            quantity_produced=Decimal('50.00')
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('50.00'),
            status='DRAFT'
        )

        url = f'/mrp_resolve_action/?production_order_id={po.pk}&component_id={self.tin_pkg.pk}&shortfall_qty=50.00&resolution_action=raw_autodraft_po'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

        draft_po = PurchaseOrder.objects.filter(supplier=self.supplier).order_by('-pk').first()
        self.assertIsNotNone(draft_po)
        self.assertEqual(draft_po.status, 'DRAFT')
        self.assertEqual(response.url, f'/admin/core/purchaseorder/{draft_po.pk}/change/')
        # Ensure no ProcurementOrder was created by Option 1
        self.assertFalse(ProcurementOrder.objects.filter(product=self.tin_pkg).exists())

        # Follow redirect and verify change form displays with DRAFT status
        follow_resp = self.client.get(response.url)
        self.assertEqual(follow_resp.status_code, 200)
        self.assertContains(follow_resp, f"Auto-drafted Purchase Order #{draft_po.po_number}")

        # Simulate operator reviewing and clicking Save on the change form
        item = draft_po.items.first()
        post_data = {
            'supplier': self.supplier.pk,
            'notes': 'Reviewed and confirmed for delivery',
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '1',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-item_id': item.pk,
            'items-0-purchase_order': draft_po.pk,
            'items-0-product': self.tin_pkg.pk,
            'items-0-quantity_ordered': '50.00',
            'items-0-price_per_unit': '12.50',
        }
        save_resp = self.client.post(f'/admin/core/purchaseorder/{draft_po.pk}/change/', post_data)
        self.assertEqual(save_resp.status_code, 302)
        draft_po.refresh_from_db()
        self.assertEqual(draft_po.status, 'SENT')

    def test_mrp_resolve_action_intermediate_build_redirects_to_work_order(self):
        """Verify executing intermediate_build generates child WorkOrder and redirects directly to its change form."""
        self.client.force_login(self.superuser)

        # Seed raw material inventory so intermediate work order can start cleanly
        tin_inv, _ = Inventory.objects.get_or_create(product=self.tin_pkg)
        tin_inv.quantity_available = Decimal("100.00")
        tin_inv.save()

        bom_bulk = BillOfMaterial.objects.create(product=self.raw_putty, is_active=True)
        BOMItem.objects.create(bom=bom_bulk, component=self.tin_pkg, quantity_required=Decimal('0.10'))

        bom_fg = BillOfMaterial.objects.create(product=self.finished_putty, is_active=True)
        BOMItem.objects.create(bom=bom_fg, component=self.raw_putty, quantity_required=Decimal('1.00'))

        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=bom_fg,
            category='PRODUCTION',
            quantity_produced=Decimal('20.00')
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('20.00'),
            status='DRAFT'
        )

        url = f'/mrp_resolve_action/?production_order_id={po.pk}&component_id={self.raw_putty.pk}&shortfall_qty=20.00&resolution_action=intermediate_build'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

        child_wo = WorkOrder.objects.filter(product=self.raw_putty).order_by('-pk').first()
        self.assertIsNotNone(child_wo)
        self.assertEqual(response.url, f'/admin/core/workorder/{child_wo.pk}/change/')

    def test_work_order_admin_hides_raw_id_and_uses_code_display(self):
        """Verify WorkOrderAdmin hides raw work_order_id from list_display and links on work_order_code."""
        admin_instance = site._registry[WorkOrder]
        self.assertNotIn('work_order_id', admin_instance.list_display)
        self.assertNotIn('id', admin_instance.list_display)
        self.assertIn('work_order_code', admin_instance.list_display)
        self.assertEqual(admin_instance.list_display_links, ('work_order_code',))

        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            category='PRODUCTION',
            quantity_produced=Decimal('50.00')
        )
        self.client.force_login(self.superuser)
        response = self.client.get('/admin/core/workorder/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, wo.work_order_code)

    def test_production_order_admin_work_order_link_column(self):
        """Verify ProductionOrderAdmin renders clickable Work Order link with format {code} — {product}."""
        admin_instance = site._registry[ProductionOrder]
        self.assertIn('work_order_link', admin_instance.list_display)
        self.assertNotIn('work_order', admin_instance.list_display)

        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            category='PRODUCTION',
            quantity_produced=Decimal('35.00')
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('35.00'),
            status='DRAFT'
        )

        self.client.force_login(self.superuser)
        response = self.client.get('/admin/core/productionorder/')
        self.assertEqual(response.status_code, 200)

        expected_link = f'/admin/core/workorder/{wo.pk}/change/'
        expected_text = f"{wo.work_order_code} — {self.finished_putty.name}"
        self.assertContains(response, expected_link)
        self.assertContains(response, expected_text)

    def test_work_order_category_auto_assigned_and_readonly(self):
        """Verify WorkOrder.category is read-only in admin and automatically assigned based on product type."""
        admin_instance = site._registry[WorkOrder]
        self.assertIn('category', admin_instance.readonly_fields)

        # 1. Product type INTERMEDIATE -> auto-assigned PRODUCTION
        wo_bulk = WorkOrder.objects.create(
            product=self.raw_putty,
            quantity_produced=Decimal('500.00')
        )
        self.assertEqual(wo_bulk.category, 'PRODUCTION')

        # 2. Product type FINISHED -> auto-assigned PACKAGING
        wo_pack = WorkOrder.objects.create(
            product=self.finished_putty,
            quantity_produced=Decimal('100.00')
        )
        self.assertEqual(wo_pack.category, 'PACKAGING')


