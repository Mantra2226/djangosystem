from django.db import migrations, models


def backfill_supplier_codes(apps, schema_editor):
    Supplier = apps.get_model('core', 'Supplier')
    suppliers = Supplier.objects.filter(supplier_code__isnull=True).order_by('supplier_id')
    
    seq = 1
    for s in suppliers:
        while Supplier.objects.filter(supplier_code=f"SUP-{seq:04d}").exists():
            seq += 1
        s.supplier_code = f"SUP-{seq:04d}"
        s.save(update_fields=['supplier_code'])
        seq += 1


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0046_workorder_category_non_editable'),
    ]

    operations = [
        migrations.AddField(
            model_name='supplier',
            name='supplier_code',
            field=models.CharField(blank=True, editable=False, help_text='Unique supplier code (e.g. SUP-0001), auto-generated if left blank.', max_length=20, null=True, unique=True),
        ),
        migrations.RunPython(backfill_supplier_codes, migrations.RunPython.noop),
    ]

