from django.apps import AppConfig
import logging

logger = logging.getLogger(__name__)


class LamoappConfig(AppConfig):
    name = 'lamoapp'

    def ready(self):
        """Relax two Django database feature checks.

        `check_database_version_supported` rejects MySQL/MariaDB builds older
        than the minimum Django supports, and `can_return_columns_from_insert`
        forces Django 4.0+ "RETURNING"-style inserts. The development server
        for this project runs against a MariaDB build that trips both, so the
        checks are disabled to keep existing environments working.

        NOTE: this is a compatibility shim, not a fix. Upgrading MySQL/MariaDB
        (or the Django version in requirements.txt) is the real solution and
        then this method can be deleted.
        """
        try:
            from django.db.backends.base.base import BaseDatabaseWrapper
            from django.db.backends.mysql.features import DatabaseFeatures

            BaseDatabaseWrapper.check_database_version_supported = lambda self: None
            DatabaseFeatures.can_return_columns_from_insert = property(lambda self: False)
        except Exception as exc:
            logger.warning(
                'Could not apply MySQL compatibility shim exc_type=%s',
                type(exc).__name__,
            )

