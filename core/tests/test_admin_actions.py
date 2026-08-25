"""
Test suite for Extended Admin UI & Action Endpoints in core/admin.py.

Domain Reference: Glass Putty Manufacturing
- Bulk Stage: 'Bulk Putty Base' (Calcium Carbonate + Raw Linseed Oil)
- Packaging Stage: 'Glass Putty 5kg Tin' (Bulk Base + 5kg Tins + Lids + Labels)
"""
from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth.models import User
from core.models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory,
    WorkOrder, ProductionOrder, Customer, SalesOrder, SalesOrderItem,
    SalesInvoice
)


class WorkOrderAdminActionViewsTestCase(TestCase):
    def setUp(self):
        # Admin user and client
        self.admin_user = User.objects.create_superuser(
            username='admin_tester',
            password='password123',
            email='admin@glassputty.test'
        )
        self.client = Client()
        self.client.login(username='admin_tester', password='password123')

        # Supplier
        self.supplier = Supplier.objects.create(name='Industrial Minerals Ltd', contact_info='minerals@test.com')

        # Raw Materials
        self.caco3 = Product.objects.create(
            name='Calcium Carbonate',
            product_type='RAW',
            category='Powder',
            unit_of_measurement='kg',
            supplier=self.supplier
        )
        self.linseed_oil = Product.objects.create(
            name='Raw Linseed Oil',
            product_type='RAW',
            category='Liquid',
            unit_of_measurement='kg',
            supplier=self.supplier
        )

        # Packaging Materials
        self.tin = Product.objects.create(
            name='Empty 5kg Tin',
            product_type='RAW',
            category='Packaging',
            unit_of_measurement='pcs',
            supplier=self.supplier
        )
        self.lid = Product.objects.create(
            name='5kg Tin Lid',
            product_type='RAW',
            category='Packaging',
            unit_of_measurement='pcs',
            supplier=self.supplier
        )
        self.label = Product.objects.create(
            name='5kg Product Label',
            product_type='RAW',
            category='Packaging',
            unit_of_measurement='pcs',
            supplier=self.supplier
        )

        # Intermediate & Finished Goods
        self.bulk_putty = Product.objects.create(
            name='Bulk Putty Base',
            product_type='INTERMEDIATE',
            category='Bulk',
            unit_of_measurement='kg'
        )
        self.glass_putty_tin = Product.objects.create(
            name='Glass Putty 5kg Tin',
            product_type='FINISHED',
            category='Finished Goods',
            unit_of_measurement='pcs',
            selling_price=Decimal('25.00')
        )

        # Stage 1 Bulk BOM: 0.80 kg CaCO3 + 0.15 kg Oil per 1 kg Bulk Putty
        self.bulk_bom = BillOfMaterial.objects.create(product=self.bulk_putty, is_active=True)
        BOMItem.objects.create(bom=self.bulk_bom, component=self.caco3, quantity_required=Decimal('0.80'))
        BOMItem.objects.create(bom=self.bulk_bom, component=self.linseed_oil, quantity_required=Decimal('0.15'))

        # Stage 2 Packaging BOM: 5 kg Bulk Putty + 1 Tin + 1 Lid + 1 Label per 1 Tin
        self.pkg_bom = BillOfMaterial.objects.create(product=self.glass_putty_tin, is_active=True)
        BOMItem.objects.create(bom=self.pkg_bom, component=self.bulk_putty, quantity_required=Decimal('5.00'))
        BOMItem.objects.create(bom=self.pkg_bom, component=self.tin, quantity_required=Decimal('1.00'))
        BOMItem.objects.create(bom=self.pkg_bom, component=self.lid, quantity_required=Decimal('1.00'))
        BOMItem.objects.create(bom=self.pkg_bom, component=self.label, quantity_required=Decimal('1.00'))

        # Seed Warehouses
        self.caco3_inv, _ = Inventory.objects.get_or_create(product=self.caco3, defaults={'quantity_available': Decimal('500.00')})
        self.caco3_inv.quantity_available = Decimal('500.00')
        self.caco3_inv.save()

        self.oil_inv, _ = Inventory.objects.get_or_create(product=self.linseed_oil, defaults={'quantity_available': Decimal('100.00')})
        self.oil_inv.quantity_available = Decimal('100.00')
        self.oil_inv.save()

        self.tin_inv, _ = Inventory.objects.get_or_create(product=self.tin, defaults={'quantity_available': Decimal('100.00')})
        self.tin_inv.quantity_available = Decimal('100.00')
        self.tin_inv.save()

        self.lid_inv, _ = Inventory.objects.get_or_create(product=self.lid, defaults={'quantity_available': Decimal('100.00')})
        self.lid_inv.quantity_available = Decimal('100.00')
        self.lid_inv.save()

        self.label_inv, _ = Inventory.objects.get_or_create(product=self.label, defaults={'quantity_available': Decimal('100.00')})
        self.label_inv.quantity_available = Decimal('100.00')
        self.label_inv.save()

        self.bulk_inv, _ = Inventory.objects.get_or_create(product=self.bulk_putty, defaults={'quantity_available': Decimal('0.00')})
        self.bulk_inv.quantity_available = Decimal('0.00')
        self.bulk_inv.save()

        self.fg_inv, _ = Inventory.objects.get_or_create(product=self.glass_putty_tin, defaults={'quantity_available': Decimal('0.00')})
        self.fg_inv.quantity_available = Decimal('0.00')
        self.fg_inv.save()

    def test_admin_start_production_transitions_draft_to_in_progress(self):
        """Clicking start-production admin endpoint on a valid DRAFT bulk order moves it to IN_PROGRESS."""
        wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        ProductionOrder.objects.create(
            product=self.bulk_putty,
            work_order=wo,
            quantity=Decimal('100.00'),
            status='DRAFT'
        )

        url = reverse('admin:workorder-start-production', args=[wo.pk])
        response = self.client.get(url, follow=True)

        self.assertEqual(response.status_code, 200)
        wo.refresh_from_db()
        self.assertEqual(wo.status, 'IN_PROGRESS')
        self.assertTrue(wo.is_inventory_allocated)

        # Inventory reserved: 80 kg CaCO3, 15 kg Oil
        self.caco3_inv.refresh_from_db()
        self.assertEqual(self.caco3_inv.quantity_available, Decimal('420.00'))
        self.assertEqual(self.caco3_inv.quantity_allocated, Decimal('80.0000'))

    def test_admin_start_production_detects_shortage_and_moves_to_awaiting_resolution(self):
        """Starting a packaging order with zero bulk putty moves it to AWAITING_RESOLUTION."""
        pkg_wo = WorkOrder.objects.create(
            product=self.glass_putty_tin,
            bill_of_material=self.pkg_bom,
            quantity_produced=Decimal('20.00'),  # requires 100 kg Bulk Base
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        ProductionOrder.objects.create(
            product=self.glass_putty_tin,
            work_order=pkg_wo,
            quantity=Decimal('20.00'),
            status='DRAFT'
        )

        url = reverse('admin:workorder-start-production', args=[pkg_wo.pk])
        response = self.client.get(url, follow=True)

        self.assertEqual(response.status_code, 200)
        pkg_wo.refresh_from_db()
        self.assertEqual(pkg_wo.status, 'AWAITING_RESOLUTION')

    def test_admin_resolve_shortage_downscale_target(self):
        """Clicking resolve-shortage/DOWNSCALE_TARGET/ scales target batch and moves order to IN_PROGRESS."""
        # 50.00 kg bulk available (can produce 10 tins of 5kg each)
        self.bulk_inv.quantity_available = Decimal('50.00')
        self.bulk_inv.save(update_fields=['quantity_available'])

        pkg_wo = WorkOrder.objects.create(
            product=self.glass_putty_tin,
            bill_of_material=self.pkg_bom,
            quantity_produced=Decimal('20.00'),  # requested 20 tins (100 kg bulk)
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )
        ProductionOrder.objects.create(
            product=self.glass_putty_tin,
            work_order=pkg_wo,
            quantity=Decimal('20.00'),
            status='ON_HOLD_SHORTAGE'
        )

        url = reverse('admin:workorder-resolve-shortage', args=[pkg_wo.pk, 'DOWNSCALE_TARGET'])
        response = self.client.get(url)

        # Assert 302 redirect
        self.assertEqual(response.status_code, 302)

        pkg_wo.refresh_from_db()
        self.assertEqual(pkg_wo.status, 'IN_PROGRESS')
        self.assertEqual(pkg_wo.quantity_produced, Decimal('10.00'))  # scaled down to 10 tins
        self.assertTrue(pkg_wo.is_inventory_allocated)

        # Verify bulk inventory allocated: 50.00 kg
        self.bulk_inv.refresh_from_db()
        self.assertEqual(self.bulk_inv.quantity_available, Decimal('0.00'))
        self.assertEqual(self.bulk_inv.quantity_allocated, Decimal('50.0000'))

    def test_admin_resolve_shortage_top_up_bulk(self):
        """Clicking resolve-shortage/TOP_UP_BULK/ spawns bulk parent order and holds packaging order."""
        pkg_wo = WorkOrder.objects.create(
            product=self.glass_putty_tin,
            bill_of_material=self.pkg_bom,
            quantity_produced=Decimal('20.00'),  # requires 100 kg Bulk Base (shortfall 100 kg)
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )
        ProductionOrder.objects.create(
            product=self.glass_putty_tin,
            work_order=pkg_wo,
            quantity=Decimal('20.00'),
            status='ON_HOLD_SHORTAGE'
        )

        url = reverse('admin:workorder-resolve-shortage', args=[pkg_wo.pk, 'TOP_UP_BULK'])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 302)

        pkg_wo.refresh_from_db()
        self.assertEqual(pkg_wo.status, 'ON_HOLD_SHORTAGE')
        self.assertIsNotNone(pkg_wo.parent_work_order)
        self.assertEqual(pkg_wo.parent_work_order.product, self.bulk_putty)
        self.assertEqual(pkg_wo.parent_work_order.quantity_produced, Decimal('100.00'))
        self.assertEqual(pkg_wo.parent_work_order.status, 'IN_PROGRESS')

    def test_admin_sales_order_confirm_and_generate_invoice(self):
        """Clicking confirm-order on SalesOrderAdmin confirms order and generates SalesInvoice."""
        customer = Customer.objects.create(
            customer_name='Acme Glazing Co',
            contact_info='acme@test.com',
            shipping_address='123 Glazing Way'
        )
        so = SalesOrder.objects.create(customer=customer, status='draft')
        SalesOrderItem.objects.create(
            sales_order=so,
            product=self.glass_putty_tin,
            quantity_ordered=Decimal('40.00')  # 40 tins * $25.00 = $1,000.00
        )

        url = reverse('admin:salesorder-confirm-order', args=[so.pk])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 302)

        so.refresh_from_db()
        self.assertEqual(so.status, 'approved')

        # Check SalesInvoice generated
        invoice = SalesInvoice.objects.filter(customer=customer).first()
        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.total_amount, Decimal('1000.00'))
        self.assertEqual(invoice.status, 'POSTED')

    def test_admin_change_form_renders_action_buttons(self):
        """Verifies change_form.html renders Start Production button on DRAFT and shortage pathways on AWAITING_RESOLUTION."""
        # 1. Draft Order
        draft_wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('50.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        change_url = reverse('admin:core_workorder_change', args=[draft_wo.pk])
        response = self.client.get(change_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Start Production')

        # 2. Awaiting Resolution Order
        shortage_wo = WorkOrder.objects.create(
            product=self.glass_putty_tin,
            bill_of_material=self.pkg_bom,
            quantity_produced=Decimal('20.00'),
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )
        change_url_shortage = reverse('admin:core_workorder_change', args=[shortage_wo.pk])
        response_shortage = self.client.get(change_url_shortage)
        self.assertEqual(response_shortage.status_code, 200)
        self.assertContains(response_shortage, 'Intermediate Component Shortage Detected')
        self.assertContains(response_shortage, 'Build Sub-Assembly (Top-Up)')
        self.assertContains(response_shortage, 'Downscale Batch to Available Stock')

    def test_permission_denied_start_production_without_perm(self):
        """A staff user without can_start_production permission is denied access to start-production endpoint."""
        # Create a non-superuser staff user with NO custom permissions
        staff_user = User.objects.create_user(
            username='limited_staff',
            password='staffpass123',
            email='staff@glassputty.test',
            is_staff=True,
        )
        limited_client = Client()
        limited_client.login(username='limited_staff', password='staffpass123')

        wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('50.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )

        url = reverse('admin:workorder-start-production', args=[wo.pk])
        response = limited_client.get(url)
        self.assertEqual(response.status_code, 403)

        # Verify work order status was NOT changed
        wo.refresh_from_db()
        self.assertEqual(wo.status, 'DRAFT')

    def test_permission_denied_resolve_shortage_without_perm(self):
        """A staff user without can_resolve_shortage permission is denied access to resolve-shortage endpoint."""
        staff_user = User.objects.create_user(
            username='limited_staff2',
            password='staffpass456',
            email='staff2@glassputty.test',
            is_staff=True,
        )
        limited_client = Client()
        limited_client.login(username='limited_staff2', password='staffpass456')

        pkg_wo = WorkOrder.objects.create(
            product=self.glass_putty_tin,
            bill_of_material=self.pkg_bom,
            quantity_produced=Decimal('20.00'),
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )

        url = reverse('admin:workorder-resolve-shortage', args=[pkg_wo.pk, 'DOWNSCALE_TARGET'])
        response = limited_client.get(url)
        self.assertEqual(response.status_code, 403)

        # Verify work order status was NOT changed
        pkg_wo.refresh_from_db()
        self.assertEqual(pkg_wo.status, 'AWAITING_RESOLUTION')

    def test_resolve_shortage_invalid_choice_returns_error(self):
        """Calling resolve-shortage with an invalid choice parameter returns a redirect with error message."""
        pkg_wo = WorkOrder.objects.create(
            product=self.glass_putty_tin,
            bill_of_material=self.pkg_bom,
            quantity_produced=Decimal('20.00'),
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )

        url = reverse('admin:workorder-resolve-shortage', args=[pkg_wo.pk, 'INVALID_CHOICE'])
        response = self.client.get(url, follow=True)

        self.assertEqual(response.status_code, 200)
        # Verify error message was emitted
        response_messages = list(response.context['messages'])
        self.assertTrue(
            any('Invalid resolution choice' in str(m) for m in response_messages),
            f"Expected 'Invalid resolution choice' error message, got: {[str(m) for m in response_messages]}"
        )

        # Verify work order status was NOT changed
        pkg_wo.refresh_from_db()
        self.assertEqual(pkg_wo.status, 'AWAITING_RESOLUTION')

    def test_hold_for_existing_via_post_with_bulk_wo_id(self):
        """HOLD_FOR_EXISTING pathway via POST with a bulk_wo_id sets parent and moves to ON_HOLD_SHORTAGE."""
        # Create an active bulk order to link
        active_bulk_wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )

        pkg_wo = WorkOrder.objects.create(
            product=self.glass_putty_tin,
            bill_of_material=self.pkg_bom,
            quantity_produced=Decimal('20.00'),
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )
        ProductionOrder.objects.create(
            product=self.glass_putty_tin,
            work_order=pkg_wo,
            quantity=Decimal('20.00'),
            status='ON_HOLD_SHORTAGE'
        )

        url = reverse('admin:workorder-resolve-shortage', args=[pkg_wo.pk, 'HOLD_FOR_EXISTING'])
        response = self.client.post(url, data={'bulk_wo_id': str(active_bulk_wo.pk)})

        self.assertEqual(response.status_code, 302)

        pkg_wo.refresh_from_db()
        self.assertEqual(pkg_wo.status, 'ON_HOLD_SHORTAGE')
        self.assertEqual(pkg_wo.parent_work_order_id, active_bulk_wo.pk)
