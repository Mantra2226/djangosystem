"""
Test suite for Step 3: Role-Based Authorization & Permissions in core/.

Validates role-based access control and lifecycle guards for the Glass Putty manufacturing domain:
- 'Shop-Floor Operator': restricted operational access, header immutability, no delete.
- 'Production Supervisor': full production execution and shortage resolution, no delete.
- 'Plant Admin' (Superuser): unconstrained administrative & delete capabilities.
- Lifecycle Immutability: completed work orders lock all fields for non-superusers.
"""
from decimal import Decimal
from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User, Group
from core.models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory,
    WorkOrder, ProductionOrder
)
from core.admin import WorkOrderAdmin


class RoleBasedAuthorizationTestCase(TestCase):
    def setUp(self):
        # Request factory & site for direct ModelAdmin method tests
        self.factory = RequestFactory()
        self.site = AdminSite()
        self.admin = WorkOrderAdmin(WorkOrder, self.site)

        # Retrieve seeded groups from migration 0038
        self.operator_group = Group.objects.get(name='Shop-Floor Operator')
        self.supervisor_group = Group.objects.get(name='Production Supervisor')

        # 1. Shop-Floor Operator User
        self.operator_user = User.objects.create_user(
            username='operator_john',
            password='password123',
            email='john@glassputty.test',
            is_staff=True
        )
        self.operator_user.groups.add(self.operator_group)
        self.operator_client = Client()
        self.operator_client.login(username='operator_john', password='password123')

        # 2. Production Supervisor User
        self.supervisor_user = User.objects.create_user(
            username='supervisor_sarah',
            password='password123',
            email='sarah@glassputty.test',
            is_staff=True
        )
        self.supervisor_user.groups.add(self.supervisor_group)
        self.supervisor_client = Client()
        self.supervisor_client.login(username='supervisor_sarah', password='password123')

        # 3. Superuser (Plant Admin)
        self.admin_user = User.objects.create_superuser(
            username='admin_plant',
            password='password123',
            email='admin@glassputty.test'
        )
        self.admin_client = Client()
        self.admin_client.login(username='admin_plant', password='password123')

        # Domain Setup: Glass Putty Manufacturing
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

        # Seed Inventory
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

    def test_operator_cannot_start_production_via_admin_view(self):
        """Shop-Floor Operator lacks 'can_start_production' permission and receives 403 Forbidden."""
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
        response = self.operator_client.get(url)
        self.assertEqual(response.status_code, 403)

        wo.refresh_from_db()
        self.assertEqual(wo.status, 'DRAFT')
        self.assertFalse(wo.is_inventory_allocated)

    def test_supervisor_can_start_production_via_admin_view(self):
        """Production Supervisor has 'can_start_production' permission and successfully triggers production."""
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
        response = self.supervisor_client.get(url)
        self.assertEqual(response.status_code, 302)

        wo.refresh_from_db()
        self.assertEqual(wo.status, 'IN_PROGRESS')
        self.assertTrue(wo.is_inventory_allocated)

        # Verify stock reserved: 80 kg CaCO3, 15 kg Oil
        self.caco3_inv.refresh_from_db()
        self.assertEqual(self.caco3_inv.quantity_available, Decimal('420.00'))
        self.assertEqual(self.caco3_inv.quantity_allocated, Decimal('80.0000'))

    def test_operator_cannot_delete_work_orders(self):
        """Operators are strictly barred from deleting Work Orders (403 Forbidden)."""
        wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('50.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        url = reverse('admin:core_workorder_delete', args=[wo.pk])
        response = self.operator_client.post(url, data={'post': 'yes'})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(WorkOrder.objects.filter(pk=wo.pk).exists())

    def test_supervisor_cannot_delete_work_orders_policy(self):
        """Production Supervisors are also barred from deleting Work Orders (Audit Trail Policy)."""
        wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('50.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        url = reverse('admin:core_workorder_delete', args=[wo.pk])
        response = self.supervisor_client.post(url, data={'post': 'yes'})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(WorkOrder.objects.filter(pk=wo.pk).exists())

    def test_superuser_can_delete_work_orders(self):
        """Superusers (Plant Admins) retain delete permission."""
        wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('50.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        url = reverse('admin:core_workorder_delete', args=[wo.pk])
        response = self.admin_client.post(url, data={'post': 'yes'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(WorkOrder.objects.filter(pk=wo.pk).exists())

    def test_operator_field_level_restrictions(self):
        """Operators have header/specification fields locked as readonly on active orders."""
        wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('50.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        request = self.factory.get('/')
        request.user = self.operator_user

        readonly_fields = self.admin.get_readonly_fields(request, wo)
        expected_operator_readonly = [
            'order_code', 'product', 'target_quantity', 'status',
            'category', 'parent_work_order', 'bill_of_material'
        ]
        for field in expected_operator_readonly:
            self.assertIn(field, readonly_fields)

        # Supervisor on the same DRAFT order has standard readonly fields (header fields are editable)
        sup_request = self.factory.get('/')
        sup_request.user = self.supervisor_user
        sup_readonly = self.admin.get_readonly_fields(sup_request, wo)
        self.assertNotIn('product', sup_readonly)
        self.assertNotIn('bill_of_material', sup_readonly)

    def test_completed_work_order_is_fully_immutable(self):
        """COMPLETED Work Orders have all model fields locked as readonly for non-superusers."""
        wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('50.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        wo.instructions.update(status='COMPLETED')
        wo.status = 'COMPLETED'
        wo.save(update_fields=['status'])
        wo.refresh_from_db()
        self.assertEqual(wo.status, 'COMPLETED')

        all_model_fields = [f.name for f in WorkOrder._meta.fields]

        # 1. Operator on COMPLETED order -> all fields readonly
        op_request = self.factory.get('/')
        op_request.user = self.operator_user
        op_readonly = self.admin.get_readonly_fields(op_request, wo)
        self.assertEqual(set(op_readonly), set(all_model_fields))

        # 2. Supervisor on COMPLETED order -> all fields readonly
        sup_request = self.factory.get('/')
        sup_request.user = self.supervisor_user
        sup_readonly = self.admin.get_readonly_fields(sup_request, wo)
        self.assertEqual(set(sup_readonly), set(all_model_fields))

        # 3. Superuser on COMPLETED order -> standard readonly fields (fallback)
        admin_request = self.factory.get('/')
        admin_request.user = self.admin_user
        admin_readonly = self.admin.get_readonly_fields(admin_request, wo)
        self.assertNotEqual(set(admin_readonly), set(all_model_fields))
