"""
TESTS: Warehouse Inventory Filtering by Product Type & Unfold Sidebar Shortcuts
Verifies:
1. InventoryProductTypeFilter lookups and query logic.
2. Filtering for RAW_CHEMICALS (excluding packaging components).
3. Filtering for PACKAGING (containers, tins, lids, labels).
4. Filtering for INTERMEDIATE (WIP bulk base putty).
5. Filtering for FINISHED (finished packaged goods).
6. Filtering for ALL_RAW (raw chemicals + packaging).
7. Admin changelist HTTP GET requests with query filters.
8. Product type badges and category display helpers in InventoryAdmin.
9. Sidebar navigation shortcut URLs in UNFOLD settings.
"""

from decimal import Decimal
from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import TestCase, Client, RequestFactory
from django.urls import reverse

from core.models import Product, Supplier, Inventory
from core.admin import InventoryAdmin, InventoryProductTypeFilter

User = get_user_model()


class InventoryProductTypeFilterTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.factory = RequestFactory()
        self.superuser = User.objects.create_superuser(
            username='admin_inventory',
            email='inventory@example.com',
            password='Password123!'
        )
        self.client.force_login(self.superuser)

        self.supplier = Supplier.objects.create(
            name="Apex Industrial Chemicals",
            contact_info="orders@apexchemicals.com"
        )
        self.pkg_supplier = Supplier.objects.create(
            name="Crown Packaging Ltd",
            contact_info="sales@crownpackaging.com"
        )

        # 1. Raw Chemicals (product_type='RAW', category not packaging)
        self.calcium_carbonate = Product.objects.create(
            name="Calcium Carbonate Pure 100-Mesh",
            sku="RAW-CACO3-100M",
            product_type="RAW",
            category="Minerals & Powders",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.linseed_oil = Product.objects.create(
            name="Raw Linseed Oil Grade A",
            sku="RAW-LSO-GRA",
            product_type="RAW",
            category="Raw Chemical Oils",
            unit_of_measurement="L",
            supplier=self.supplier
        )

        # 2. Packaging Materials (product_type='RAW', category or name = packaging/tins/lids/labels)
        self.empty_tin = Product.objects.create(
            name="Empty 5kg Metal Tin",
            sku="PKG-TIN-5KG",
            product_type="RAW",
            category="Packaging",
            unit_of_measurement="Unit",
            supplier=self.pkg_supplier
        )
        self.tin_lid = Product.objects.create(
            name="5kg Tin Sealing Lid",
            sku="PKG-LID-5KG",
            product_type="RAW",
            category="Packaging",
            unit_of_measurement="Unit",
            supplier=self.pkg_supplier
        )
        self.product_label = Product.objects.create(
            name="5kg Putty Product Label",
            sku="PKG-LBL-5KG",
            product_type="RAW",
            category="Packaging",
            unit_of_measurement="Unit",
            supplier=self.pkg_supplier
        )

        # 3. Intermediate / WIP Bulk Putty Base (product_type='INTERMEDIATE')
        self.bulk_putty = Product.objects.create(
            name="Bulk Putty Base Compound",
            sku="WIP-PUTTY-BASE",
            product_type="INTERMEDIATE",
            category="Bulk Intermediate",
            unit_of_measurement="kg",
            selling_price=Decimal("120.00")
        )

        # 4. Finished Packaged Goods (product_type='FINISHED')
        self.finished_putty_tin = Product.objects.create(
            name="5kg Glass Putty Tin (Retail)",
            sku="FG-PUTTY-5KG-TIN",
            product_type="FINISHED",
            category="Retail Finished Goods",
            unit_of_measurement="Tin",
            selling_price=Decimal("750.00")
        )

        # Seed Inventory records
        self.inv_caco3 = Inventory.objects.create(
            product=self.calcium_carbonate,
            quantity_available=Decimal('1000.00'),
            unit_cost=Decimal('15.00'),
            location='Chemical Warehouse Bay A'
        )
        self.inv_oil = Inventory.objects.create(
            product=self.linseed_oil,
            quantity_available=Decimal('500.00'),
            unit_cost=Decimal('85.00'),
            location='Chemical Warehouse Bay B'
        )
        self.inv_tin = Inventory.objects.create(
            product=self.empty_tin,
            quantity_available=Decimal('300.00'),
            unit_cost=Decimal('45.00'),
            location='Packaging Store 1'
        )
        self.inv_lid = Inventory.objects.create(
            product=self.tin_lid,
            quantity_available=Decimal('300.00'),
            unit_cost=Decimal('12.00'),
            location='Packaging Store 1'
        )
        self.inv_label = Inventory.objects.create(
            product=self.product_label,
            quantity_available=Decimal('500.00'),
            unit_cost=Decimal('3.50'),
            location='Packaging Store 2'
        )
        self.inv_bulk = Inventory.objects.create(
            product=self.bulk_putty,
            quantity_available=Decimal('450.00'),
            unit_cost=Decimal('65.00'),
            location='Shop Floor Holding Tank 1'
        )
        self.inv_fg, _ = Inventory.objects.get_or_create(
            product=self.finished_putty_tin,
            defaults={
                'quantity_available': Decimal('150.00'),
                'unit_cost': Decimal('420.00'),
                'location': 'Finished Goods Pallet Racks'
            }
        )
        self.inv_fg.quantity_available = Decimal('150.00')
        self.inv_fg.unit_cost = Decimal('420.00')
        self.inv_fg.location = 'Finished Goods Pallet Racks'
        self.inv_fg.save()

        self.site = AdminSite()
        self.admin = InventoryAdmin(Inventory, self.site)

    def test_filter_lookups(self):
        """Verify lookups provide all 5 key warehouse product categories."""
        request = self.factory.get('/')
        filter_inst = InventoryProductTypeFilter(request, request.GET.copy(), Inventory, self.admin)
        lookups = filter_inst.lookups(request, self.admin)
        lookup_keys = [k for k, v in lookups]
        self.assertIn('RAW_CHEMICALS', lookup_keys)
        self.assertIn('PACKAGING', lookup_keys)
        self.assertIn('INTERMEDIATE', lookup_keys)
        self.assertIn('FINISHED', lookup_keys)
        self.assertIn('ALL_RAW', lookup_keys)

    def test_filter_raw_chemicals(self):
        """Filtering by RAW_CHEMICALS includes raw minerals/oils and excludes packaging/tins/labels."""
        request = self.factory.get('/?product_type=RAW_CHEMICALS')
        filter_inst = InventoryProductTypeFilter(request, request.GET.copy(), Inventory, self.admin)
        qs = filter_inst.queryset(request, Inventory.objects.all())

        product_pks = list(qs.values_list('product_id', flat=True))
        self.assertIn(self.calcium_carbonate.pk, product_pks)
        self.assertIn(self.linseed_oil.pk, product_pks)
        self.assertNotIn(self.empty_tin.pk, product_pks)
        self.assertNotIn(self.tin_lid.pk, product_pks)
        self.assertNotIn(self.product_label.pk, product_pks)
        self.assertNotIn(self.bulk_putty.pk, product_pks)
        self.assertNotIn(self.finished_putty_tin.pk, product_pks)
        self.assertEqual(qs.count(), 2)

    def test_filter_packaging_materials(self):
        """Filtering by PACKAGING returns empty tins, lids, and product labels."""
        request = self.factory.get('/?product_type=PACKAGING')
        filter_inst = InventoryProductTypeFilter(request, request.GET.copy(), Inventory, self.admin)
        qs = filter_inst.queryset(request, Inventory.objects.all())

        product_pks = list(qs.values_list('product_id', flat=True))
        self.assertIn(self.empty_tin.pk, product_pks)
        self.assertIn(self.tin_lid.pk, product_pks)
        self.assertIn(self.product_label.pk, product_pks)
        self.assertNotIn(self.calcium_carbonate.pk, product_pks)
        self.assertNotIn(self.linseed_oil.pk, product_pks)
        self.assertNotIn(self.bulk_putty.pk, product_pks)
        self.assertNotIn(self.finished_putty_tin.pk, product_pks)
        self.assertEqual(qs.count(), 3)

    def test_filter_intermediate_wip_bulk_base(self):
        """Filtering by INTERMEDIATE returns WIP bulk putty base only."""
        request = self.factory.get('/?product_type=INTERMEDIATE')
        filter_inst = InventoryProductTypeFilter(request, request.GET.copy(), Inventory, self.admin)
        qs = filter_inst.queryset(request, Inventory.objects.all())

        product_pks = list(qs.values_list('product_id', flat=True))
        self.assertIn(self.bulk_putty.pk, product_pks)
        self.assertNotIn(self.calcium_carbonate.pk, product_pks)
        self.assertNotIn(self.finished_putty_tin.pk, product_pks)
        self.assertEqual(qs.count(), 1)

    def test_filter_finished_goods(self):
        """Filtering by FINISHED returns retail finished goods only."""
        request = self.factory.get('/?product_type=FINISHED')
        filter_inst = InventoryProductTypeFilter(request, request.GET.copy(), Inventory, self.admin)
        qs = filter_inst.queryset(request, Inventory.objects.all())

        product_pks = list(qs.values_list('product_id', flat=True))
        self.assertIn(self.finished_putty_tin.pk, product_pks)
        self.assertNotIn(self.bulk_putty.pk, product_pks)
        self.assertNotIn(self.calcium_carbonate.pk, product_pks)
        self.assertEqual(qs.count(), 1)

    def test_filter_all_raw(self):
        """Filtering by ALL_RAW returns both raw chemicals and packaging (all product_type='RAW')."""
        request = self.factory.get('/?product_type=ALL_RAW')
        filter_inst = InventoryProductTypeFilter(request, request.GET.copy(), Inventory, self.admin)
        qs = filter_inst.queryset(request, Inventory.objects.all())

        product_pks = list(qs.values_list('product_id', flat=True))
        self.assertIn(self.calcium_carbonate.pk, product_pks)
        self.assertIn(self.linseed_oil.pk, product_pks)
        self.assertIn(self.empty_tin.pk, product_pks)
        self.assertIn(self.tin_lid.pk, product_pks)
        self.assertIn(self.product_label.pk, product_pks)
        self.assertNotIn(self.bulk_putty.pk, product_pks)
        self.assertNotIn(self.finished_putty_tin.pk, product_pks)
        self.assertEqual(qs.count(), 5)

    def test_admin_changelist_get_queries(self):
        """Admin changelist HTTP GET requests with query parameters filter items correctly."""
        base_url = reverse('admin:core_inventory_changelist')

        # 1. Raw Chemicals
        resp_raw = self.client.get(f"{base_url}?product_type=RAW_CHEMICALS")
        self.assertEqual(resp_raw.status_code, 200)
        self.assertContains(resp_raw, "Calcium Carbonate Pure 100-Mesh")
        self.assertContains(resp_raw, "Raw Linseed Oil Grade A")
        self.assertNotContains(resp_raw, "Empty 5kg Metal Tin")
        self.assertNotContains(resp_raw, "Bulk Putty Base Compound")
        self.assertNotContains(resp_raw, "5kg Glass Putty Tin (Retail)")

        # 2. Packaging
        resp_pkg = self.client.get(f"{base_url}?product_type=PACKAGING")
        self.assertEqual(resp_pkg.status_code, 200)
        self.assertContains(resp_pkg, "Empty 5kg Metal Tin")
        self.assertContains(resp_pkg, "5kg Tin Sealing Lid")
        self.assertNotContains(resp_pkg, "Calcium Carbonate Pure 100-Mesh")

        # 3. Intermediate
        resp_wip = self.client.get(f"{base_url}?product_type=INTERMEDIATE")
        self.assertEqual(resp_wip.status_code, 200)
        self.assertContains(resp_wip, "Bulk Putty Base Compound")
        self.assertNotContains(resp_wip, "5kg Glass Putty Tin (Retail)")

        # 4. Finished Goods
        resp_fg = self.client.get(f"{base_url}?product_type=FINISHED")
        self.assertEqual(resp_fg.status_code, 200)
        self.assertContains(resp_fg, "5kg Glass Putty Tin (Retail)")
        self.assertNotContains(resp_fg, "Bulk Putty Base Compound")

    def test_admin_product_type_badge_and_category_display(self):
        """InventoryAdmin renders accurate HTML color badges and category labels with no leaked color names."""
        # Raw Chemical
        caco3_badge = self.admin.product_type_badge(self.inv_caco3)
        self.assertIn("Raw Material", str(caco3_badge))
        self.assertIn("#0284c7", str(caco3_badge))
        self.assertNotIn("info", str(caco3_badge).lower())
        self.assertEqual(self.admin.product_category_display(self.inv_caco3), "Minerals & Powders")

        # Packaging
        pkg_badge = self.admin.product_type_badge(self.inv_tin)
        self.assertIn("Packaging", str(pkg_badge))
        self.assertIn("#f59e0b", str(pkg_badge))
        self.assertNotIn("warning", str(pkg_badge).lower())

        # Intermediate
        wip_badge = self.admin.product_type_badge(self.inv_bulk)
        self.assertIn("component / sub-assembly", str(wip_badge))
        self.assertIn("#2563eb", str(wip_badge))
        self.assertNotIn("primary", str(wip_badge).lower())

        # Finished Good
        fg_badge = self.admin.product_type_badge(self.inv_fg)
        self.assertIn("Finished Good", str(fg_badge))
        self.assertIn("#10b981", str(fg_badge))
        self.assertNotIn("success", str(fg_badge).lower())

    def test_sidebar_navigation_shortcuts_in_settings(self):
        """Verify UNFOLD sidebar navigation contains direct shortcuts for warehouse stock categories."""
        unfold_nav = settings.UNFOLD.get("SIDEBAR", {}).get("navigation", [])
        warehouse_section = next((sec for sec in unfold_nav if sec.get("title") == "Warehouse & Inventory"), None)
        self.assertIsNotNone(warehouse_section, "Warehouse & Inventory section must exist in sidebar.")

        items = warehouse_section.get("items", [])
        item_titles = [it.get("title") for it in items]

        self.assertIn("All Warehouse Stock", item_titles)
        self.assertIn("Raw Chemicals Stock", item_titles)
        self.assertIn("Packaging Stock", item_titles)
        self.assertIn("WIP Bulk Base Putty", item_titles)
        self.assertIn("Finished Goods Stock", item_titles)

        # Verify dynamic link evaluation for callables and expected targets
        request = self.factory.get('/admin/')
        for it in items:
            title = it.get("title")
            link = it.get("link")
            if callable(link):
                evaluated_link = link(request)
            else:
                evaluated_link = str(link)

            if title in [
                "All Warehouse Stock", "Raw Chemicals Stock", "Packaging Stock",
                "WIP Bulk Base Putty", "Finished Goods Stock"
            ]:
                self.assertTrue(evaluated_link.startswith('/admin/core/inventory/'), f"Link {evaluated_link} must target inventory changelist.")

            if title == "Raw Chemicals Stock":
                self.assertIn("product_type=RAW_CHEMICALS", evaluated_link)
            elif title == "Packaging Stock":
                self.assertIn("product_type=PACKAGING", evaluated_link)
            elif title == "WIP Bulk Base Putty":
                self.assertIn("product_type=INTERMEDIATE", evaluated_link)
            elif title == "Finished Goods Stock":
                self.assertIn("product_type=FINISHED", evaluated_link)

