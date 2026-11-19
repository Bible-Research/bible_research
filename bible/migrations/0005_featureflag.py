# Hand-written migration (agents may not run makemigrations).
# A human verifies it matches the model via
# `python manage.py makemigrations --check` before deploy.

from django.db import migrations, models


def seed_sword_search_flag(apps, schema_editor):
    """Create the ``sword_search`` row so the kill switch is
    visible in Django admin from the start."""
    FeatureFlag = apps.get_model('bible', 'FeatureFlag')
    FeatureFlag.objects.get_or_create(
        key='sword_search',
        defaults={
            'enabled': True,
            'description': (
                'Allow search on SWORD filesets '
                '(e.g. LVSGLU8). Disable to stop SWORD scans '
                'without a redeploy.'
            ),
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ('bible', '0004_verse_unique_book_chapter_verse'),
    ]

    operations = [
        migrations.CreateModel(
            name='FeatureFlag',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True,
                    primary_key=True,
                    serialize=False,
                    verbose_name='ID',
                )),
                ('key', models.CharField(
                    max_length=64,
                    unique=True,
                )),
                ('enabled', models.BooleanField(default=True)),
                ('description', models.CharField(
                    blank=True,
                    default='',
                    max_length=255,
                )),
                ('updated_at', models.DateTimeField(
                    auto_now=True,
                )),
            ],
        ),
        migrations.RunPython(
            seed_sword_search_flag,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
