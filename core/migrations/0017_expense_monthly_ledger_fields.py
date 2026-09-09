from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0016_replace_applied_status_with_not_established'),
    ]

    operations = [
        migrations.AddField(
            model_name='expenseimport',
            name='original_filename',
            field=models.CharField(blank=True, max_length=255, verbose_name='原始文件名'),
        ),
        migrations.AddField(
            model_name='expenseimport',
            name='file_sha256',
            field=models.CharField(blank=True, db_index=True, max_length=64, verbose_name='文件哈希'),
        ),
        migrations.AddField(
            model_name='expenseimport',
            name='format_version',
            field=models.CharField(db_index=True, default='legacy', max_length=50, verbose_name='数据口径版本'),
        ),
        migrations.AlterField(
            model_name='expensesnapshot',
            name='total_expense',
            field=models.DecimalField(decimal_places=4, default=0, max_digits=18, verbose_name='累计支出'),
        ),
        migrations.AddConstraint(
            model_name='expenseimport',
            constraint=models.UniqueConstraint(
                condition=~models.Q(file_sha256=''),
                fields=('format_version', 'file_sha256'),
                name='unique_expense_import_file_hash',
            ),
        ),
    ]
