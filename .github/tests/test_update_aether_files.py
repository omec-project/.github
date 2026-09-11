# SPDX-FileCopyrightText: 2026 The Linux Foundation
# SPDX-License-Identifier: Apache-2.0

"""Fixture tests for the E2E image-list configuration helper."""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'update-aether-files.py'
FIXTURES = Path(__file__).parent / 'fixtures'

spec = importlib.util.spec_from_file_location('update_aether_files', SCRIPT)
update_aether_files = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(update_aether_files)


class ImageListTests(unittest.TestCase):
    registry_prefix = 'ghcr.io/omec-project/'

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.chart_dir = root / 'chart'
        values_dir = self.chart_dir / 'bess-upf'
        values_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / 'user-plane-values.yaml', values_dir / 'values.yaml')
        self.base_values = root / 'base-values.yaml'
        shutil.copy(FIXTURES / 'base-values.yaml', self.base_values)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_legacy_single_image_override_is_unchanged(self):
        overrides = update_aether_files.build_image_overrides(
            self.chart_dir,
            self.base_values,
            {'bess': 'localhost:5000/bess:testing'},
            self.registry_prefix,
        )

        tags = overrides['omec-user-plane']['images']['tags']
        self.assertEqual(tags['bess'], 'localhost:5000/bess:testing')
        self.assertEqual(tags['pfcp'], 'ghcr.io/omec-project/upf-pfcp:rel-2.6.2')

    def test_upf_image_list_overrides_both_images(self):
        image_overrides = update_aether_files.image_list_to_overrides(['bess', 'pfcp'])
        overrides = update_aether_files.build_image_overrides(
            self.chart_dir,
            self.base_values,
            image_overrides,
            self.registry_prefix,
        )

        content = (FIXTURES / 'aether-values-template.yaml').read_text()
        rendered = update_aether_files.apply_image_overrides_to_content(content, overrides)

        self.assertIn('bess: localhost:5000/bess:testing', rendered)
        self.assertIn('pfcp: localhost:5000/pfcp:testing', rendered)
        self.assertIn('sriov: ghcr.io/omec-project/aether-cni:rel-1.4.1', rendered)

    def test_rejects_duplicate_image_names(self):
        with self.assertRaisesRegex(ValueError, 'duplicates'):
            update_aether_files.image_list_to_overrides(['bess', 'bess'])

    def test_rejects_empty_image_list(self):
        with self.assertRaisesRegex(ValueError, 'must not be empty'):
            update_aether_files.image_list_to_overrides([])
