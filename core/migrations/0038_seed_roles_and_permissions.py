from django.db import migrations
from django.core.management.sql import emit_post_migrate_signal


def seed_groups_and_permissions(apps, schema_editor):
    """
    Seeds 'Shop-Floor Operator' and 'Production Supervisor' groups with precise
    least-privilege permissions for manufacturing MES/ERP operations.
    """
    db_alias = schema_editor.connection.alias

    # Rule 3: Emit post-migrate signal to ensure custom permissions declared in 0037 are created in DB
    try:
        emit_post_migrate_signal(2, False, db_alias)
    except Exception:
        try:
            emit_post_migrate_signal(2, False, 'default')
        except Exception:
            pass

    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    operator_permissions = [
        # WorkOrder operational lines
        'view_workorder',
        'view_workordermaterialline',
        'change_workordermaterialline',
        # Warehouse & Catalog Visibility
        'view_inventory',
        'view_stocktransaction',
        'view_product',
        'view_billofmaterial',
        'view_bomitem',
    ]

    supervisor_permissions = [
        # WorkOrder management & execution actions
        'view_workorder',
        'add_workorder',
        'change_workorder',
        'can_start_production',
        'can_resolve_shortage',
        'view_workordermaterialline',
        'add_workordermaterialline',
        'change_workordermaterialline',
        'delete_workordermaterialline',
        # Warehouse & Catalog management
        'view_inventory',
        'change_inventory',
        'view_stocktransaction',
        'view_product',
        'view_billofmaterial',
        'view_bomitem',
    ]

    # Seed Shop-Floor Operator
    operator_group, _ = Group.objects.using(db_alias).get_or_create(name='Shop-Floor Operator')
    op_perms = Permission.objects.using(db_alias).filter(codename__in=operator_permissions)
    operator_group.permissions.set(op_perms)

    # Seed Production Supervisor
    supervisor_group, _ = Group.objects.using(db_alias).get_or_create(name='Production Supervisor')
    sup_perms = Permission.objects.using(db_alias).filter(codename__in=supervisor_permissions)
    supervisor_group.permissions.set(sup_perms)


def remove_groups_and_permissions(apps, schema_editor):
    """
    Rollback handler: removes seeded groups cleanly.
    """
    db_alias = schema_editor.connection.alias
    Group = apps.get_model('auth', 'Group')
    Group.objects.using(db_alias).filter(
        name__in=['Shop-Floor Operator', 'Production Supervisor']
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        # Rule 1: Linear Intra-App Chain
        ('core', '0037_alter_salesorder_options_alter_workorder_options'),
        # Rule 2: Cross-App Framework Pinning
        ('auth', '0012_alter_user_first_name_max_length'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.RunPython(seed_groups_and_permissions, remove_groups_and_permissions),
    ]
