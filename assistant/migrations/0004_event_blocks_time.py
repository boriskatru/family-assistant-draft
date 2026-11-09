from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('assistant', '0003_address_book')]

    operations = [
        migrations.AddField(
            model_name='event', name='blocks_time',
            field=models.BooleanField(default=True),
        ),
    ]
