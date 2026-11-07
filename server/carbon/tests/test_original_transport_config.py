"""Check that the optional original-host transport follows server settings."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from carbon.original_host.stack import make_children


class TransportConfigurationTests(unittest.TestCase):
    def test_stack_passes_listener_ports_to_transport_without_ipc_secret(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'server').mkdir()
            (root / 'runtime').mkdir()
            config = root / 'server.toml'
            config.write_text('[server]\nPUBLIC_HOST="127.0.0.1"\n'
                              'LOCAL_ADVERTISE_HOST="127.0.0.1"\n'
                              'IPC_SECRET="test-only-test-only-test-only-test-only"\n'
                              'MESSENGER_LISTEN="127.0.0.1:13635"\n'
                              '[carbon]\nFESL_LISTEN="127.0.0.1:18330"\n'
                              'THEATER_LISTEN="127.0.0.1:18335"\n'
                              'ORIGINAL_HOST_ACCOUNT="Host"\nORIGINAL_SEED_ACCOUNT="Seed"\n'
                              'ORIGINAL_RESTART_MARKER="runtime/original.restart"\n')
            host = root / 'host.cfg'
            host.write_text('SERVER.LOGIN_NAME: "Host"\nSERVER.SUBACCOUNT: "Host"\n')
            with patch('carbon.original_host.stack.read_secret'), patch.dict(os.environ, {'NFS_IPC_SECRET': 'private-test-secret-at-least-32-bytes'}):
                children = make_children(release=root, config=config, host_config=host,
                                         prefix=root, wine=root / 'wine')
            transport = next(child for child in children if child.name == 'transport')
            self.assertIn('carbon.transport.web_app:app', transport.command)
            expected = {'FESL': '18330', 'THEATER': '18335', 'MESSENGER': '13635', 'RACE': '19141'}
            for name, port in expected.items():
                self.assertEqual(transport.env[f'CARBON_TRANSPORT_{name}_PORT'], port)
            self.assertNotIn('NFS_IPC_SECRET', transport.env)
            self.assertEqual(next(c for c in children if c.name == 'carbon').env['NFS_IPC_SECRET'], 'private-test-secret-at-least-32-bytes')

    @unittest.skipUnless(importlib.util.find_spec("starlette"), "Starlette transport dependency is optional")
    def test_transport_reads_ports_and_rejects_invalid_configuration(self):
        release = Path(__file__).resolve().parents[3]
        env = os.environ.copy()
        env['PYTHONPATH'] = os.pathsep.join((str(release / 'server'), str(release), env.get('PYTHONPATH', '')))
        env.update({f'CARBON_TRANSPORT_{name}_PORT': str(port) for name, port in
                    [('FESL', 18330), ('THEATER', 18335), ('MESSENGER', 13635), ('RACE', 19141)]})
        code = 'import json; from carbon.transport.web_tunnel import PORTS; print(json.dumps(PORTS))'
        result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {'fesl': 18330, 'theater': 18335, 'messenger': 13635, 'race': 19141})
        for invalid in ('0', '65536', 'not-a-port'):
            env['CARBON_TRANSPORT_FESL_PORT'] = invalid
            result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('CARBON_TRANSPORT_FESL_PORT', result.stderr)


if __name__ == '__main__':
    unittest.main()
