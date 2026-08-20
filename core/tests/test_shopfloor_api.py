"""
Test suite for Step 4: DRF Permission Classes and Real-Time Shop-Floor Logging API in core/.

Validates:
1. Role-based access control via DRF permission classes (IsProductionSupervisor, IsShopFloorOperatorOrSupervisor).
2. Object-level lifecycle guard (IsWorkOrderActiveForLogging).
3. Real-time material logging and Phase 2 incremental inventory delta deduction under row locking.
4. Supervisor state transitions (start-production, resolve-shortage, complete-order) and Phase 3 stock reconciliation.

Domain Context: Glass Putty Manufacturing MES/ERP
- Raw Materials: Calcium Carbonate (kg), Raw Linseed Oil (kg)
- Intermediate: Bulk Putty Base (kg)
- Packaging Components: Empty 5kg Tin (pcs), 5kg Tin Lid (pcs), 5kg Product Label (pcs)
- Finished Good: Glass Putty 5kg Tin (pcs)
"""

from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth.models import User, Group
from rest_framework.test import APIClient
from rest_framework import status

from core.models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory,
    WorkOrder, WorkOrderMaterialLine, ProductionOrder
)


class ShopFloorAPITestCase(TestCase):
    def setUp(self):
        # ---------------------------------------------------------------------
        # 1. User and Role Setup
        # ---------------------------------------------------------------------
        self.operator_group, _ = Group.objects.get_or_create(name='Shop-Floor Operator')
        self.supervisor_group, _ = Group.objects.get_or_create(name='Production Supervisor')

        # Operator User
        self.operator_user = User.objects.create_user(
            username='operator_dave',
            password='password123',
            email='dave@glassputty.test'
        )
        self.operator_user.groups.add(self.operator_group)
        self.operator_client = APIClient()
        self.operator_client.force_authenticate(user=self.operator_user)

        # Supervisor User
        self.supervisor_user = User.objects.create_user(
            username='supervisor_carol',
            password='password123',
            email='carol@glassputty.test'
        )
        self.supervisor_user.groups.add(self.supervisor_group)
        self.supervisor_client = APIClient()
        self.supervisor_client.force_authenticate(user=self.supervisor_user)

        # Unauthorized User (no groups)
        self.unauthorized_user = User.objects.create_user(
            username='unauth_guest',
            password='password123',
            email='guest@glassputty.test'
        )
        self.unauthorized_client = APIClient()
        self.unauthorized_client.force_authenticate(user=self.unauthorized_user)

        # Anonymous Client
        self.anon_client = APIClient()

        # ---------------------------------------------------------------------
        # 2. Domain Entities: Glass Putty Manufacturing Supply Chain
        # ---------------------------------------------------------------------
        self.supplier = Supplier.objects.create(
            name="Industrial Minerals Supply Co",
            contact_info="orders@mineralsupply.test"
        )

        # Raw Materials
        self.calcium_carb = Product.objects.create(
            name='Calcium Carbonate',
            sku='RM-CC-01',
            product_type='RAW',
            category='Powder',
            unit_of_measurement='kg',
            supplier=self.supplier
        )
        self.inv_calcium, _ = Inventory.objects.get_or_create(
            product=self.calcium_carb,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('500.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('0.50')}
        )
        self.inv_calcium.quantity_available = Decimal('500.00')
        self.inv_calcium.quantity_allocated = Decimal('0.00')
        self.inv_calcium.save()

        self.linseed_oil = Product.objects.create(
            name='Raw Linseed Oil',
            sku='RM-RLO-01',
            product_type='RAW',
            category='Liquid',
            unit_of_measurement='kg',
            supplier=self.supplier
        )
        self.inv_oil, _ = Inventory.objects.get_or_create(
            product=self.linseed_oil,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('200.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('2.00')}
        )
        self.inv_oil.quantity_available = Decimal('200.00')
        self.inv_oil.quantity_allocated = Decimal('0.00')
        self.inv_oil.save()

        # Intermediate Good: Bulk Putty Base
        self.bulk_putty = Product.objects.create(
            name='Bulk Putty Base',
            sku='INT-BPB-01',
            product_type='INTERMEDIATE',
            category='Intermediate',
            unit_of_measurement='kg'
        )
        self.inv_bulk, _ = Inventory.objects.get_or_create(
            product=self.bulk_putty,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('0.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('0.85')}
        )
        self.inv_bulk.quantity_available = Decimal('0.00')
        self.inv_bulk.quantity_allocated = Decimal('0.00')
        self.inv_bulk.save()

        # Bulk Recipe BOM: 0.80 kg Calcium Carbonate + 0.15 kg Linseed Oil per 1 kg Bulk Base
        self.bulk_bom = BillOfMaterial.objects.create(
            product=self.bulk_putty,
            name="Bulk Putty Standard Mix Recipe",
            is_active=True
        )
        self.bom_item_cc = BOMItem.objects.create(
            bom=self.bulk_bom,
            component=self.calcium_carb,
            quantity_required=Decimal('0.80')
        )
        self.bom_item_oil = BOMItem.objects.create(
            bom=self.bulk_bom,
            component=self.linseed_oil,
            quantity_required=Decimal('0.15')
        )

        # Packaging Materials
        self.empty_tin = Product.objects.create(
            name='Empty 5kg Tin',
            sku='PKG-TIN-5K',
            product_type='RAW',
            category='Packaging',
            unit_of_measurement='pcs',
            supplier=self.supplier
        )
        self.inv_tin, _ = Inventory.objects.get_or_create(
            product=self.empty_tin,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('100.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('1.00')}
        )
        self.inv_tin.quantity_available = Decimal('100.00')
        self.inv_tin.quantity_allocated = Decimal('0.00')
        self.inv_tin.save()

        self.tin_lid = Product.objects.create(
            name='5kg Tin Lid',
            sku='PKG-LID-5K',
            product_type='RAW',
            category='Packaging',
            unit_of_measurement='pcs',
            supplier=self.supplier
        )
        self.inv_lid, _ = Inventory.objects.get_or_create(
            product=self.tin_lid,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('100.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('0.50')}
        )
        self.inv_lid.quantity_available = Decimal('100.00')
        self.inv_lid.quantity_allocated = Decimal('0.00')
        self.inv_lid.save()

        self.product_label = Product.objects.create(
            name='5kg Product Label',
            sku='PKG-LBL-5K',
            product_type='RAW',
            category='Packaging',
            unit_of_measurement='pcs',
            supplier=self.supplier
        )
        self.inv_label, _ = Inventory.objects.get_or_create(
            product=self.product_label,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('100.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('0.20')}
        )
        self.inv_label.quantity_available = Decimal('100.00')
        self.inv_label.quantity_allocated = Decimal('0.00')
        self.inv_label.save()

        # Finished Good: Glass Putty 5kg Tin
        self.finished_putty = Product.objects.create(
            name='Glass Putty 5kg Tin',
            sku='FG-GP-5KG',
            product_type='FINISHED',
            category='Finished Good',
            unit_of_measurement='pcs',
            selling_price=Decimal('25.00')
        )
        self.inv_finished, _ = Inventory.objects.get_or_create(
            product=self.finished_putty,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('0.00'), 'quantity_allocated': Decimal('0.00'), 'unit_cost': Decimal('7.00')}
        )

        # Packaging BOM: 5.00 kg Bulk Base + 1 pc Tin + 1 pc Lid + 1 pc Label per Tin
        self.pkg_bom = BillOfMaterial.objects.create(
            product=self.finished_putty,
            name="Glass Putty 5kg Packaging Recipe",
            is_active=True
        )
        self.pkg_item_bulk = BOMItem.objects.create(
            bom=self.pkg_bom,
            component=self.bulk_putty,
            quantity_required=Decimal('5.00')
        )
        self.pkg_item_tin = BOMItem.objects.create(
            bom=self.pkg_bom,
            component=self.empty_tin,
            quantity_required=Decimal('1.00')
        )
        self.pkg_item_lid = BOMItem.objects.create(
            bom=self.pkg_bom,
            component=self.tin_lid,
            quantity_required=Decimal('1.00')
        )
        self.pkg_item_label = BOMItem.objects.create(
            bom=self.pkg_bom,
            component=self.product_label,
            quantity_required=Decimal('1.00')
        )

    # =========================================================================
    # Test 1: Anonymous User Authentication Gate
    # =========================================================================
    def test_anonymous_user_cannot_access_api(self):
        """Unauthenticated requests are rejected by global DRF IsAuthenticated gate (401/403)."""
        url = reverse('shopfloor-workorder-list')
        response = self.anon_client.get(url)
        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    # =========================================================================
    # Test 2: Unauthorized User RBAC Gate
    # =========================================================================
    def test_unauthorized_user_cannot_access_api(self):
        """Authenticated users without Operator or Supervisor group receive 403 Forbidden."""
        url = reverse('shopfloor-workorder-list')
        response = self.unauthorized_client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # =========================================================================
    # Test 3: Operator Can List Work Orders
    # =========================================================================
    def test_operator_can_list_work_orders(self):
        """Shop-Floor Operator can query the work order list endpoint (200 OK)."""
        wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        url = reverse('shopfloor-workorder-list')
        response = self.operator_client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 1)

    # =========================================================================
    # Test 4: Operator Can Retrieve Work Order Detail with Nested Material Lines
    # =========================================================================
    def test_operator_can_retrieve_work_order_detail(self):
        """Shop-Floor Operator retrieves work order detail including serialized material lines."""
        wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        url = reverse('shopfloor-workorder-detail', args=[wo.pk])
        response = self.operator_client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['id'], wo.pk)
        self.assertIn('material_lines', response.data)
        self.assertEqual(len(response.data['material_lines']), 2)

    # =========================================================================
    # Test 5: Operator Cannot Start Production
    # =========================================================================
    def test_operator_cannot_start_production(self):
        """Shop-Floor Operator cannot trigger start-production endpoint (403 Forbidden)."""
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
        url = reverse('shopfloor-workorder-start-production', args=[wo.pk])
        response = self.operator_client.post(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        wo.refresh_from_db()
        self.assertEqual(wo.status, 'DRAFT')

    # =========================================================================
    # Test 6: Supervisor Can Start Production and Allocate Stock
    # =========================================================================
    def test_supervisor_can_start_production(self):
        """Supervisor starts production via API: transitions DRAFT -> IN_PROGRESS and reserves stock."""
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
        url = reverse('shopfloor-workorder-start-production', args=[wo.pk])
        response = self.supervisor_client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        wo.refresh_from_db()
        self.assertEqual(wo.status, 'IN_PROGRESS')
        self.assertTrue(wo.is_inventory_allocated)

        # Verify Calcium Carbonate: 500.00 - 80.00 = 420.00 Available, 80.00 Allocated
        self.inv_calcium.refresh_from_db()
        self.assertEqual(self.inv_calcium.quantity_available, Decimal('420.00'))
        self.assertEqual(self.inv_calcium.quantity_allocated, Decimal('80.00'))

        # Verify Linseed Oil: 200.00 - 15.00 = 185.00 Available, 15.00 Allocated
        self.inv_oil.refresh_from_db()
        self.assertEqual(self.inv_oil.quantity_available, Decimal('185.00'))
        self.assertEqual(self.inv_oil.quantity_allocated, Decimal('15.00'))

    # =========================================================================
    # Test 7: Operator Can Log Material on Active IN_PROGRESS Order
    # =========================================================================
    def test_operator_can_log_material_on_active_order(self):
        """Shop-Floor Operator logs consumption on active order: updates quantity_actual and quantity_deducted."""
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
        wo.start_production()

        url = reverse('shopfloor-workorder-log-material', args=[wo.pk])
        payload = {
            'component_id': self.calcium_carb.pk,
            'quantity_actual': '40.00'
        }
        response = self.operator_client.post(url, data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(str(response.data['quantity_actual'])), Decimal('40.00'))
        self.assertEqual(Decimal(str(response.data['quantity_deducted'])), Decimal('40.00'))

        line = wo.material_lines.get(component=self.calcium_carb)
        self.assertEqual(line.quantity_actual, Decimal('40.00'))
        self.assertEqual(line.deducted_quantity, Decimal('40.00'))

    # =========================================================================
    # Test 8: Operator Cannot Log Material on DRAFT Order
    # =========================================================================
    def test_operator_cannot_log_material_on_draft_order(self):
        """Object permission IsWorkOrderActiveForLogging rejects logging on DRAFT orders (403 Forbidden)."""
        wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        url = reverse('shopfloor-workorder-log-material', args=[wo.pk])
        payload = {
            'component_id': self.calcium_carb.pk,
            'quantity_actual': '40.00'
        }
        response = self.operator_client.post(url, data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # =========================================================================
    # Test 9: Operator Cannot Log Material on COMPLETED Order
    # =========================================================================
    def test_operator_cannot_log_material_on_completed_order(self):
        """Object permission IsWorkOrderActiveForLogging rejects logging on COMPLETED orders (403 Forbidden)."""
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
        wo.start_production()
        wo.instructions.all().update(status='COMPLETED')
        wo.status = 'COMPLETED'
        wo.save(update_fields=['status'])

        url = reverse('shopfloor-workorder-log-material', args=[wo.pk])
        payload = {
            'component_id': self.calcium_carb.pk,
            'quantity_actual': '40.00'
        }
        response = self.operator_client.post(url, data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # =========================================================================
    # Test 10: Incremental Delta Deductions Without Double-Deduction
    # =========================================================================
    def test_log_material_incremental_delta_deduction(self):
        """Multiple material logging calls calculate deltas correctly and mutate inventory without double-deduction."""
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
        wo.start_production()

        # Initial allocation: 80.00 kg Calcium Carbonate allocated
        self.inv_calcium.refresh_from_db()
        self.assertEqual(self.inv_calcium.quantity_allocated, Decimal('80.00'))
        self.assertEqual(self.inv_calcium.quantity_available, Decimal('420.00'))

        url = reverse('shopfloor-workorder-log-material', args=[wo.pk])

        # Step 1: Log initial 30.00 kg
        res1 = self.operator_client.post(url, data={'component_id': self.calcium_carb.pk, 'quantity_actual': '30.00'}, format='json')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        self.inv_calcium.refresh_from_db()
        # Allocated: 80.00 - 30.00 = 50.00 kg
        self.assertEqual(self.inv_calcium.quantity_allocated, Decimal('50.00'))
        self.assertEqual(self.inv_calcium.quantity_available, Decimal('420.00'))

        # Step 2: Log updated cumulative actual 50.00 kg (delta = 50 - 30 = 20.00 kg)
        res2 = self.operator_client.post(url, data={'component_id': self.calcium_carb.pk, 'quantity_actual': '50.00'}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)

        self.inv_calcium.refresh_from_db()
        # Allocated: 50.00 - 20.00 = 30.00 kg
        self.assertEqual(self.inv_calcium.quantity_allocated, Decimal('30.00'))
        self.assertEqual(self.inv_calcium.quantity_available, Decimal('420.00'))

        line = wo.material_lines.get(component=self.calcium_carb)
        self.assertEqual(line.quantity_actual, Decimal('50.00'))
        self.assertEqual(line.deducted_quantity, Decimal('50.00'))

    # =========================================================================
    # Test 11: Operator Cannot Resolve Shortage
    # =========================================================================
    def test_operator_cannot_resolve_shortage(self):
        """Shop-Floor Operator cannot trigger shortage resolution actions (403 Forbidden)."""
        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.pkg_bom,
            quantity_produced=Decimal('10.00'),
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )
        url = reverse('shopfloor-workorder-resolve-shortage', args=[wo.pk])
        payload = {'choice': 'DOWNSCALE_TARGET'}
        response = self.operator_client.post(url, data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # =========================================================================
    # Test 12: Supervisor Can Resolve Shortage via Downscale
    # =========================================================================
    def test_supervisor_can_resolve_shortage_downscale(self):
        """Supervisor resolves shortage via DOWNSCALE_TARGET: scales batch to available bulk and moves to IN_PROGRESS."""
        # Add 25.00 kg of bulk putty base to warehouse inventory (enough for 5 tins of 5kg each)
        self.inv_bulk.quantity_available = Decimal('25.00')
        self.inv_bulk.save(update_fields=['quantity_available'])

        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.pkg_bom,
            quantity_produced=Decimal('10.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('10.00'),
            status='DRAFT'
        )
        # Attempting to start production detects shortage (50 kg required, 25 kg available) -> AWAITING_RESOLUTION
        wo.start_production()
        wo.refresh_from_db()
        self.assertEqual(wo.status, 'AWAITING_RESOLUTION')

        url = reverse('shopfloor-workorder-resolve-shortage', args=[wo.pk])
        payload = {'choice': 'DOWNSCALE_TARGET'}
        response = self.supervisor_client.post(url, data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        wo.refresh_from_db()
        self.assertEqual(wo.status, 'IN_PROGRESS')
        self.assertEqual(wo.quantity_produced, Decimal('5.00'))

        # Bulk inventory was allocated: available = 0.00, allocated = 25.00
        self.inv_bulk.refresh_from_db()
        self.assertEqual(self.inv_bulk.quantity_available, Decimal('0.00'))
        self.assertEqual(self.inv_bulk.quantity_allocated, Decimal('25.00'))

    # =========================================================================
    # Test 13: Operator Cannot Complete Order
    # =========================================================================
    def test_operator_cannot_complete_order(self):
        """Shop-Floor Operator cannot trigger complete-order endpoint (403 Forbidden)."""
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
        wo.start_production()

        url = reverse('shopfloor-workorder-complete-order', args=[wo.pk])
        response = self.operator_client.post(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        wo.refresh_from_db()
        self.assertEqual(wo.status, 'IN_PROGRESS')

    # =========================================================================
    # Test 14: Supervisor Can Complete Order and Trigger Reconciliation
    # =========================================================================
    def test_supervisor_can_complete_order(self):
        """Supervisor completes work order: triggers Phase 3 reconciliation and releases unconsumed allocations."""
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
        wo.start_production()

        # Log consumption: 75.00 kg Calcium Carbonate (allocated 80.00) & 14.00 kg Linseed Oil (allocated 15.00)
        url_log = reverse('shopfloor-workorder-log-material', args=[wo.pk])
        self.operator_client.post(url_log, data={'component_id': self.calcium_carb.pk, 'quantity_actual': '75.00'}, format='json')
        self.operator_client.post(url_log, data={'component_id': self.linseed_oil.pk, 'quantity_actual': '14.00'}, format='json')

        # Supervisor completes order
        url_complete = reverse('shopfloor-workorder-complete-order', args=[wo.pk])
        response = self.supervisor_client.post(url_complete, data={'actual_quantity_produced': '100.00'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        wo.refresh_from_db()
        self.assertEqual(wo.status, 'COMPLETED')
        self.assertTrue(wo.is_inventory_updated)

        # Phase 3 Reconciliation Checks:
        # Calcium Carbonate: 5.00 kg unconsumed allocation released back to Available
        # Initial 500.00 - 75.00 actual = 425.00 Available, 0.00 Allocated
        self.inv_calcium.refresh_from_db()
        self.assertEqual(self.inv_calcium.quantity_available, Decimal('425.00'))
        self.assertEqual(self.inv_calcium.quantity_allocated, Decimal('0.00'))

        # Linseed Oil: 1.00 kg unconsumed allocation released back to Available
        # Initial 200.00 - 14.00 actual = 186.00 Available, 0.00 Allocated
        self.inv_oil.refresh_from_db()
        self.assertEqual(self.inv_oil.quantity_available, Decimal('186.00'))
        self.assertEqual(self.inv_oil.quantity_allocated, Decimal('0.00'))

        # Finished Intermediate Output: Bulk Putty Base +100.00 kg Available
        self.inv_bulk.refresh_from_db()
        self.assertEqual(self.inv_bulk.quantity_available, Decimal('100.00'))

    # =========================================================================
    # Test 15: Operator Downward Quantity Logging Rejected
    # =========================================================================
    def test_operator_downward_quantity_logging_rejected(self):
        """Shop-Floor Operator cannot reduce logged material below already deducted stock without supervisor authorization (400 Bad Request)."""
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
        wo.start_production()

        url_log = reverse('shopfloor-workorder-log-material', args=[wo.pk])
        # 1. Operator logs 50.00 kg CaCO3 -> succeeds
        res1 = self.operator_client.post(url_log, data={'component_id': self.calcium_carb.pk, 'quantity_actual': '50.00'}, format='json')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        line = wo.material_lines.get(component=self.calcium_carb)
        self.assertEqual(line.quantity_actual, Decimal('50.00'))
        self.assertEqual(line.quantity_deducted, Decimal('50.00'))

        # 2. Operator attempts to log downward to 40.00 kg -> rejected with 400
        res2 = self.operator_client.post(url_log, data={'component_id': self.calcium_carb.pk, 'quantity_actual': '40.00'}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res2.data.get('detail'), "Decreasing logged quantity below already deducted stock requires supervisor authorization.")

        line.refresh_from_db()
        self.assertEqual(line.quantity_actual, Decimal('50.00'))
        self.assertEqual(line.quantity_deducted, Decimal('50.00'))

    # =========================================================================
    # Test 16: Supervisor Can Override Downward Quantity Logging
    # =========================================================================
    def test_supervisor_can_override_downward_quantity_logging(self):
        """Production Supervisor can decrease logged material quantity and refund stock to available inventory."""
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
        wo.start_production()

        url_log = reverse('shopfloor-workorder-log-material', args=[wo.pk])
        # 1. Operator logs 50.00 kg CaCO3
        self.operator_client.post(url_log, data={'component_id': self.calcium_carb.pk, 'quantity_actual': '50.00'}, format='json')

        # Calcium inventory after Phase 1 (80 allocated, 420 avail) + 50 deducted (allocated 30, avail 420)
        self.inv_calcium.refresh_from_db()
        self.assertEqual(self.inv_calcium.quantity_available, Decimal('420.00'))

        # 2. Supervisor logs downward adjustment to 40.00 kg
        res_sup = self.supervisor_client.post(url_log, data={'component_id': self.calcium_carb.pk, 'quantity_actual': '40.00'}, format='json')
        self.assertEqual(res_sup.status_code, status.HTTP_200_OK)

        line = wo.material_lines.get(component=self.calcium_carb)
        self.assertEqual(line.quantity_actual, Decimal('40.00'))
        self.assertEqual(line.quantity_deducted, Decimal('40.00'))

        # Inventory available should be refunded by 10.00 kg -> 430.00
        self.inv_calcium.refresh_from_db()
        self.assertEqual(self.inv_calcium.quantity_available, Decimal('430.00'))

    # =========================================================================
    # Test 17: Complete Order with Actual Yield, Scrap and Yield Percentage
    # =========================================================================
    def test_complete_order_with_actual_yield_and_scrap(self):
        """Supervisor completes work order specifying actual yield and scrap: finished goods credit actual output and compute yield percentage."""
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
        wo.start_production()

        # Log consumption: 80.00 kg CaCO3 & 15.00 kg Linseed Oil
        url_log = reverse('shopfloor-workorder-log-material', args=[wo.pk])
        self.operator_client.post(url_log, data={'component_id': self.calcium_carb.pk, 'quantity_actual': '80.00'}, format='json')
        self.operator_client.post(url_log, data={'component_id': self.linseed_oil.pk, 'quantity_actual': '15.00'}, format='json')

        # Supervisor completes with 95.00 kg actual yield, 5.00 kg scrap
        url_complete = reverse('shopfloor-workorder-complete-order', args=[wo.pk])
        payload = {
            'actual_quantity_produced': '95.00',
            'scrap_quantity': '5.00',
            'scrap_reason': 'Mixing error'
        }
        response = self.supervisor_client.post(url_complete, data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        wo.refresh_from_db()
        self.assertEqual(wo.status, 'COMPLETED')
        self.assertEqual(wo.actual_quantity_produced, Decimal('95.00'))
        self.assertEqual(wo.scrap_quantity, Decimal('5.00'))
        self.assertEqual(wo.scrap_reason, 'Mixing error')
        self.assertTrue(wo.is_inventory_updated)

        # Serializer exposes yield_percentage: 95.00 / 100.00 * 100 = 95.00%
        self.assertEqual(response.data['work_order']['yield_percentage'], '95.00')
        self.assertEqual(response.data['work_order']['actual_quantity_produced'], '95.00')
        self.assertEqual(response.data['work_order']['scrap_quantity'], '5.00')
        self.assertEqual(response.data['work_order']['scrap_reason'], 'Mixing error')

        # Finished Goods stock reflects actual produced quantity: +95.00
        self.inv_bulk.refresh_from_db()
        self.assertEqual(self.inv_bulk.quantity_available, Decimal('95.00'))

    # =========================================================================
    # Test 18: Work Order List Filtering by Status and Category
    # =========================================================================
    def test_work_order_list_filtering_by_status(self):
        """Work Order list endpoint filters results correctly when status or category query params are passed."""
        # WO 1: DRAFT Bulk Production
        wo1 = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
            category='PRODUCTION'
        )

        # Provide intermediate stock for packaging run: 10 units x 5 kg = 50 kg
        self.inv_bulk.quantity_available = Decimal('50.00')
        self.inv_bulk.save()

        # WO 2: IN_PROGRESS Packaging
        wo2 = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.pkg_bom,
            quantity_produced=Decimal('10.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
            category='PACKAGING'
        )
        ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo2,
            quantity=Decimal('10.00'),
            status='DRAFT'
        )
        wo2.start_production()

        url = reverse('shopfloor-workorder-list')

        # Filter by status=IN_PROGRESS
        res_in_progress = self.operator_client.get(url, {'status': 'IN_PROGRESS'})
        self.assertEqual(res_in_progress.status_code, status.HTTP_200_OK)
        ids_in_progress = [item['id'] for item in res_in_progress.data]
        self.assertIn(wo2.pk, ids_in_progress)
        self.assertNotIn(wo1.pk, ids_in_progress)

        # Filter by status=DRAFT
        res_draft = self.operator_client.get(url, {'status': 'DRAFT'})
        self.assertEqual(res_draft.status_code, status.HTTP_200_OK)
        ids_draft = [item['id'] for item in res_draft.data]
        self.assertIn(wo1.pk, ids_draft)
        self.assertNotIn(wo2.pk, ids_draft)

        # Filter by category=PACKAGING
        res_pkg = self.operator_client.get(url, {'category': 'PACKAGING'})
        self.assertEqual(res_pkg.status_code, status.HTTP_200_OK)
        ids_pkg = [item['id'] for item in res_pkg.data]
        self.assertIn(wo2.pk, ids_pkg)
        self.assertNotIn(wo1.pk, ids_pkg)

