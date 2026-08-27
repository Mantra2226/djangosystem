from django.db import migrations


def backfill_entry_codes(apps, schema_editor):
    FinanceEntry = apps.get_model('core', 'FinanceEntry')
    DocumentSequence = apps.get_model('core', 'DocumentSequence')
    db_alias = schema_editor.connection.alias

    entries = FinanceEntry.objects.using(db_alias).filter(entry_code__isnull=True).order_by('entry_date', 'finance_entry_id')
    if not entries.exists():
        return

    seq_obj, _ = DocumentSequence.objects.using(db_alias).get_or_create(
        document_type='FINANCE_ENTRY',
        defaults={'prefix': 'FE', 'last_sequence': 0}
    )

    current_prefix = seq_obj.prefix or 'FE'
    used_codes = set(
        FinanceEntry.objects.using(db_alias).exclude(entry_code__isnull=True).values_list('entry_code', flat=True)
    )

    for entry in entries:
        ref_date = getattr(entry, 'timestamp', None) or getattr(entry, 'entry_date', None)
        year_month = ref_date.strftime('%Y%m') if hasattr(ref_date, 'strftime') else '202608'

        while True:
            seq_obj.last_sequence += 1
            candidate = f"{current_prefix}-{year_month}-{seq_obj.last_sequence:04d}"
            if candidate not in used_codes:
                used_codes.add(candidate)
                entry.entry_code = candidate
                if not entry.reference_document:
                    if getattr(entry, 'sales_invoice_id', None):
                        try:
                            inv = entry.sales_invoice
                            if inv and getattr(inv, 'invoice_number', None):
                                entry.reference_document = inv.invoice_number
                        except Exception:
                            pass
                    elif getattr(entry, 'procurement_order_id', None):
                        entry.reference_document = f"PO #{entry.procurement_order_id}"
                entry.save(update_fields=['entry_code', 'reference_document'])
                break

    seq_obj.save(update_fields=['last_sequence'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0044_financeentry_entry_code_fields'),
    ]

    operations = [
        migrations.RunPython(backfill_entry_codes, reverse_code=noop_reverse),
    ]
