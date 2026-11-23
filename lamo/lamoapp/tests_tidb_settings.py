"""Settings tests for the TiDB Cloud move: TLS auto-detection and the optional
isolation level. No database connection is made."""
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from lamo.settings import _system_ca_bundle, resolve_mysql_ssl_options

TIDB_URL = 'mysql://u:p@gateway01.ap-southeast-1.prod.aws.tidbcloud.com:4000/lamo'


def _clean_env(**extra):
    base = {k: v for k, v in os.environ.items() if not k.startswith('DJANGO_DB_') and k != 'DATABASE_URL'}
    base.update(extra)
    return base


class TidbSslTests(SimpleTestCase):
    def test_tidb_host_enables_verify_identity_with_system_ca(self):
        with mock.patch.dict(os.environ, _clean_env(DATABASE_URL=TIDB_URL), clear=True):
            opts = resolve_mysql_ssl_options()
        self.assertEqual(opts['ssl_mode'], 'VERIFY_IDENTITY')
        ca = Path(opts['ssl']['ca'])
        self.assertTrue(ca.is_file())
        # Must NOT be the Aiven CA that ships in the repository.
        repo_ca = Path(settings.BASE_DIR, 'cert', 'ca.pem')
        self.assertNotEqual(ca.resolve(), repo_ca.resolve())

    def test_explicit_ca_still_wins(self):
        explicit = Path(settings.BASE_DIR, 'cert', 'ca.pem')
        if not explicit.is_file():
            self.skipTest('no repo ca.pem to use as an explicit override')
        with mock.patch.dict(os.environ, _clean_env(DATABASE_URL=TIDB_URL, DJANGO_DB_SSL_CA=str(explicit)), clear=True):
            opts = resolve_mysql_ssl_options()
        self.assertEqual(Path(opts['ssl']['ca']).resolve(), explicit.resolve())

    def test_plain_hosts_are_untouched(self):
        with mock.patch.dict(os.environ, _clean_env(DATABASE_URL='mysql://u:p@127.0.0.1:3306/lamo'), clear=True):
            self.assertEqual(resolve_mysql_ssl_options(), {})

    def test_system_ca_bundle_returns_an_existing_file_or_empty(self):
        path = _system_ca_bundle()
        self.assertTrue(path == '' or Path(path).is_file())


class IsolationLevelSettingTests(SimpleTestCase):
    """DJANGO_DB_ISOLATION_LEVEL is opt-in and validated."""

    def _run(self, value):
        code = (
            "import os;os.environ['DJANGO_SETTINGS_MODULE']='lamo.settings';"
            "from django.conf import settings;print(settings.DATABASES['default'].get('OPTIONS',{}).get('isolation_level'))"
        )
        env = _clean_env(
            DJANGO_ENV='development', DJANGO_SECRET_KEY='t' * 60,
            DATABASE_URL='mysql://u:p@127.0.0.1:3306/lamo',
        )
        if value is not None:
            env['DJANGO_DB_ISOLATION_LEVEL'] = value
        return subprocess.run(
            [sys.executable, '-c', code], cwd=str(settings.BASE_DIR), env=env,
            capture_output=True, text=True, timeout=60,
        )

    def test_unset_changes_nothing(self):
        r = self._run(None)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertEqual(r.stdout.strip().splitlines()[-1], 'None')

    def test_read_committed_is_accepted_in_any_spelling(self):
        for spelling in ('read committed', 'READ_COMMITTED', 'read-committed'):
            r = self._run(spelling)
            self.assertEqual(r.returncode, 0, r.stderr[-400:])
            self.assertEqual(r.stdout.strip().splitlines()[-1], 'read committed')

    def test_garbage_is_rejected_without_echoing_the_value(self):
        r = self._run('super-secret-token')
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('super-secret-token', r.stderr)
        self.assertIn('DJANGO_DB_ISOLATION_LEVEL', r.stderr)
