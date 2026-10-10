"""Link already-published worker-created services to their requesting worker.

Until this fix, ``service_request_publish`` created the ``Service`` row but
never set ``WorkerProfile.service`` for the worker who requested it. The
service therefore showed up in the public service list while every worker
query (``service_workers``, ``build_worker_recommendations``, ``book_service``)
still filtered on the authoritative ``service`` foreign key - so customers saw
"No workers found" for their own custom service.

This data migration repairs the rows that already exist by backfilling the
missing relation for every PUBLISHED request. Rows where the worker has since
been linked to a service through their profile are left untouched, and the
migration is idempotent (re-running it changes nothing).
"""

from django.db import migrations


def link_published_services_to_workers(apps, schema_editor):
    ServiceRequest = apps.get_model('lamoapp', 'ServiceRequest')
    WorkerProfile = apps.get_model('lamoapp', 'WorkerProfile')

    published = ServiceRequest.objects.filter(
        status='PUBLISHED',
        published_service__isnull=False,
    ).select_related('published_service')

    linked = 0
    for service_request in published:
        linked += WorkerProfile.objects.filter(
            user_id=service_request.requested_by_id,
            service__isnull=True,
        ).update(service=service_request.published_service)

    if linked:
        print(f'  Linked {linked} worker profile(s) to their published service.')


def unlink_published_services_from_workers(apps, schema_editor):
    """No-op: the link is the correct state, reversing it would re-break search."""


class Migration(migrations.Migration):

    dependencies = [
        ('lamoapp', '0031_customuser_password_reset_token_hash'),
    ]

    operations = [
        migrations.RunPython(
            link_published_services_to_workers,
            unlink_published_services_from_workers,
        ),
    ]
