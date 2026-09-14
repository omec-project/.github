# SPDX-FileCopyrightText: 2026 The Linux Foundation
# SPDX-License-Identifier: Apache-2.0

"""Fixture tests for the E2E image-list configuration helper."""

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


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

        control_plane_dir = self.chart_dir / 'omec-5g-core'
        control_plane_dir.mkdir()
        shutil.copy(FIXTURES / 'control-plane-values.yaml', control_plane_dir / 'values.yaml')
        self.control_plane_base_values = root / 'control-plane-base-values.yaml'
        shutil.copy(FIXTURES / 'control-plane-base-values.yaml', self.control_plane_base_values)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_legacy_single_image_override_is_unchanged(self):
        overrides = update_aether_files.build_image_overrides(
            self.chart_dir,
            self.control_plane_base_values,
            {'amf': 'localhost:5000/amf:testing'},
            self.registry_prefix,
        )

        tags = overrides['omec-5g-core']['images']['tags']
        self.assertEqual(tags['amf'], 'localhost:5000/amf:testing')
        self.assertEqual(tags['nrf'], 'ghcr.io/omec-project/5gc-nrf:rel-generic')

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
        self.assertIn('sriov: ghcr.io/omec-project/aether-cni:rel-generic', rendered)

    def test_gnbsim_image_override_is_unchanged(self):
        aether_dir = self.temp_dir.name
        vars_dir = Path(aether_dir) / 'vars'
        vars_dir.mkdir()
        vars_file = vars_dir / 'main.yml'
        shutil.copy(FIXTURES / 'gnbsim-vars-main.yaml', vars_file)

        update_aether_files.update_vars_main(
            Path(aether_dir),
            'eth0',
            '10.0.0.2',
            'localhost:5000/gnbsim:testing',
        )

        with open(vars_file) as f:
            vars_data = yaml.safe_load(f)

        self.assertEqual(
            vars_data['gnbsim']['docker']['container']['image'],
            'localhost:5000/gnbsim:testing',
        )

    def test_rejects_duplicate_image_names(self):
        with self.assertRaisesRegex(ValueError, 'duplicates'):
            update_aether_files.image_list_to_overrides(['bess', 'bess'])

    def test_rejects_empty_image_list(self):
        with self.assertRaisesRegex(ValueError, 'must not be empty'):
            update_aether_files.image_list_to_overrides([])

    def test_rejects_image_names_with_whitespace(self):
        with self.assertRaisesRegex(ValueError, 'must be non-empty strings'):
            update_aether_files.image_list_to_overrides(['   '])
        with self.assertRaisesRegex(ValueError, 'must not contain whitespace'):
            update_aether_files.image_list_to_overrides(['bess pfcp'])

    def test_rejects_gnbsim_image_with_image_list(self):
        with patch.object(
            sys,
            'argv',
            [
                str(SCRIPT),
                self.temp_dir.name,
                '--gnbsim-image',
                'localhost:5000/gnbsim:testing',
                '--image-list',
                'bess',
            ],
        ):
            with self.assertRaisesRegex(SystemExit, '2'):
                update_aether_files.main()
