from django.db import migrations, models


def backfill_log_codes(apps, schema_editor):
    ProcessExecutionLog = apps.get_model('core', 'ProcessExecutionLog')
    for log in ProcessExecutionLog.objects.all().order_by('log_id'):
        if not log.log_code:
            log.log_code = f"PEL-{log.log_id:05d}"
            log.save(update_fields=['log_code'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0051_alter_processexecutionlog_level'),
    ]

    operations = [
        migrations.AlterField(
            model_name='processexecutionlog',
            name='process_type',
            field=models.CharField(
                choices=[
                    ('STOCK_ALLOCATION', 'Stock Allocation & Component Reservation'),
                    ('MRP_EVALUATION', 'MRP Shortage Evaluation'),
                    ('PO_DRAFT', 'Auto-Draft Purchase Order'),
                    ('BATCH_DOWNSCALE', 'Batch Downscale Target'),
                    ('AUTO_RESUME', 'Auto-Resume On-Hold Order'),
                    ('RECONCILIATION', 'Stock & Consumption Reconciliation'),
                    ('ORDER_SYNC', 'Database & Order Synchronization'),
                    ('AVCO_RECALCULATION', 'AVCO Recalculation'),
                ],
                db_index=True,
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name='processexecutionlog',
            name='event_title',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Concise operational title for stepper and timeline display.',
                max_length=128,
            ),
        ),
        migrations.AddField(
            model_name='processexecutionlog',
            name='log_code',
            field=models.CharField(
                blank=True,
                db_index=True,
                editable=False,
                help_text='Unique business identifier (e.g. PEL-00001).',
                max_length=32,
                null=True,
            ),
        ),
        migrations.RunPython(backfill_log_codes, reverse_code=migrations.RunPython.noop),
        migrations.AlterField(
            model_name='processexecutionlog',
            name='log_code',
            field=models.CharField(
                blank=True,
                db_index=True,
                editable=False,
                help_text='Unique business identifier (e.g. PEL-00001).',
                max_length=32,
                null=True,
                unique=True,
            ),
        ),
    ]
