# Generated manually for Section 12 Database Hardening

from django.db import migrations, models


def convert_empty_reference_ids_to_null(apps, schema_editor):
    """
    Data migration: Convert any empty string reference_id values to NULL.
    In MySQL UNIQUE indexes, multiple NULLs are permitted, but empty strings count as duplicates.
    """
    WalletTransaction = apps.get_model('lamoapp', 'WalletTransaction')
    WalletTransaction.objects.filter(reference_id='').update(reference_id=None)


def convert_null_reference_ids_to_empty(apps, schema_editor):
    """
    Reversible operation: Convert NULL reference_ids back to empty string if rolled back.
    """
    WalletTransaction = apps.get_model('lamoapp', 'WalletTransaction')
    WalletTransaction.objects.filter(reference_id__isnull=True).update(reference_id='')


class Migration(migrations.Migration):

    dependencies = [
        ('lamoapp', '0042_booking_proposed_date_booking_proposed_time_and_more'),
    ]

    operations = [
        migrations.RunPython(
            convert_empty_reference_ids_to_null,
            reverse_code=convert_null_reference_ids_to_empty,
        ),
        migrations.AlterField(
            model_name='wallettransaction',
            name='reference_id',
            field=models.CharField(
                blank=True,
                default=None,
                help_text='Bank transaction reference / UTR number for manual recharges (unique when present)',
                max_length=100,
                null=True,
                unique=True,
            ),
        ),
    ]

