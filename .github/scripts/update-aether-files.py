#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Intel Corporation
#
# Updates aether-onramp configuration files for CI environment
# and configures sd-core values to use local registry image for testing

import argparse
import re
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path
from typing import Optional, Tuple

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with: sudo apt install python3-yaml", file=sys.stderr)
    sys.exit(1)


def run_command(cmd: list, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command and return the result."""
    if capture:
        result = subprocess.run(cmd, capture_output=True, text=True, check=check)
    else:
        result = subprocess.run(cmd, check=check)
    return result


def get_network_info() -> Tuple[str, str]:
    """Detect the default network interface and IP address."""
    try:
        # Get default interface
        result = run_command(['ip', 'route'])
        for line in result.stdout.splitlines():
            if 'default' in line:
                parts = line.split()
                interface = parts[parts.index('dev') + 1]
                break
        else:
            raise RuntimeError("Could not find default network interface")

        # Get IP address for that interface
        result = run_command(['ip', '-4', 'addr', 'show', interface])
        ip_match = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)', result.stdout)
        if not ip_match:
            raise RuntimeError(f"Could not find IP address for interface {interface}")

        ip_addr = ip_match.group(1)
        return interface, ip_addr

    except Exception as e:
        print(f"ERROR: Failed to detect network info: {e}", file=sys.stderr)
        sys.exit(1)


def update_hosts_ini(aether_dir: Path) -> None:
    """Generate a localhost-only hosts.ini for CI, avoiding SSH transport."""
    hosts_file = aether_dir / 'hosts.ini'

    import os
    ansible_user = os.environ.get('USER', 'runner')

    content = (
        "[all]\n"
        f"localhost ansible_connection=local ansible_user={ansible_user} "
        "ansible_python_interpreter=/usr/bin/python3\n"
        "\n"
        "[master_nodes]\n"
        "localhost\n"
        "\n"
        "[worker_nodes]\n"
        "#node2\n"
        "\n"
        "[gnbsim_nodes]\n"
        "localhost\n"
    )

    hosts_file.write_text(content)
    print(f"Generated local-only {hosts_file} for user: {ansible_user}")


def update_nested_keys(data: object, key_name: str, value: str) -> int:
    """Recursively update all matching keys in a nested YAML structure."""
    updates = 0

    if isinstance(data, dict):
        for key, nested_value in data.items():
            if key == key_name:
                data[key] = value
                updates += 1
            else:
                updates += update_nested_keys(nested_value, key_name, value)
    elif isinstance(data, list):
        for item in data:
            updates += update_nested_keys(item, key_name, value)

    return updates


def update_vars_main(aether_dir: Path, interface: str, ip_addr: str) -> None:
    """Update vars/main.yml with detected interface and IP."""
    vars_file = aether_dir / 'vars' / 'main.yml'
    with open(vars_file, 'r') as f:
        vars_data = yaml.safe_load(f) or {}

    interface_updates = update_nested_keys(vars_data, 'data_iface', interface)
    if interface_updates == 0:
        raise RuntimeError(
            f"Expected at least one 'data_iface' entry in {vars_file}"
        )

    core_config = vars_data.get('core')
    if not isinstance(core_config, dict):
        raise RuntimeError(f"Expected top-level 'core' mapping in {vars_file}")

    amf_config = core_config.get('amf')
    if not isinstance(amf_config, dict) or 'ip' not in amf_config:
        raise RuntimeError(f"Expected path 'core.amf.ip' in {vars_file}")

    amf_config['ip'] = ip_addr

    with open(vars_file, 'w') as f:
        yaml.dump(vars_data, f, default_flow_style=False, sort_keys=False)

    print(f"Updated {vars_file}")


def replace_aether_templates_with_placeholders(content: str) -> str:
    """Replace Aether template expressions and control lines with YAML-safe placeholders."""
    expression_pattern = re.compile(r'\{\{[^}]+\}\}')
    placeholder_index = 0

    def line_indent(line: str) -> str:
        return line[: len(line) - len(line.lstrip(' \t'))]

    def replace_control(template_text: str, indent: str) -> str:
        nonlocal placeholder_index
        placeholder = f"AETHER_CONTROL_PLACEHOLDER_{placeholder_index}"
        placeholder_index += 1
        return f'{indent}{placeholder}: "{placeholder}"'

    def replace_expression(match: re.Match[str]) -> str:
        nonlocal placeholder_index
        placeholder = f"AETHER_EXPR_PLACEHOLDER_{placeholder_index}"
        placeholder_index += 1
        # Quote the placeholder so YAML parsers treat it as a string.
        return f'"{placeholder}"'

    processed_lines = []
    original_lines = content.splitlines()

    for index, line in enumerate(original_lines):
        stripped_line = line.strip()
        if stripped_line.startswith('{%') and stripped_line.endswith('%}'):
            indent = line_indent(line)

            if not indent:
                for next_line in original_lines[index + 1:]:
                    if next_line.strip():
                        indent = line_indent(next_line)
                        if indent:
                            break

            if not indent and processed_lines:
                previous_line = processed_lines[-1]
                if previous_line.strip():
                    indent = line_indent(previous_line)

            processed_lines.append(replace_control(stripped_line, indent))
            continue

        processed_lines.append(line)

    modified_content = '\n'.join(processed_lines)
    modified_content = expression_pattern.sub(replace_expression, modified_content)
    return modified_content


def get_chart_info(aether_dir: Path) -> Tuple[str, str]:
    """Extract Helm chart reference and version from vars/main.yml."""
    vars_file = aether_dir / 'vars' / 'main.yml'

    with open(vars_file, 'r') as f:
        vars_data = yaml.safe_load(f)

    chart_ref = vars_data.get('core', {}).get('helm', {}).get('chart_ref')
    chart_version = vars_data.get('core', {}).get('helm', {}).get('chart_version')

    if not chart_ref or not chart_version:
        print("ERROR: Failed to extract chart information from vars/main.yml", file=sys.stderr)
        print("Expected path: .core.helm.chart_ref and .core.helm.chart_version", file=sys.stderr)
        sys.exit(1)

    return chart_ref, chart_version


def find_single_directory(base_dir: Path, name: str) -> Optional[Path]:
    """Find a single directory with the given name, error if multiple or none found."""
    matches = list(base_dir.rglob(name))
    matches = [m for m in matches if m.is_dir()]

    if len(matches) == 0:
        return None
    elif len(matches) > 1:
        print(f"ERROR: Found multiple {name} directories:", file=sys.stderr)
        for m in matches:
            print(f"  {m}", file=sys.stderr)
        sys.exit(1)

    return matches[0]


def get_enabled_sections(base_values_file: Path) -> list:
    """Extract enabled sections from sdcore-5g-values.yaml."""
    with open(base_values_file, 'r') as f:
        values = yaml.safe_load(f)

    enabled_sections = []
    if values:
        for section_name, section_config in values.items():
            if isinstance(section_config, dict):
                # Check for various enable flags at the top level
                if (section_config.get('enable') is True or
                    section_config.get('enable5G') is True or
                    section_config.get('enable4G') is True):
                    enabled_sections.append(section_name)
                    print(f"Found enabled section: {section_name}")

    return enabled_sections


def build_image_overrides(
    chart_dir: Path,
    base_values_file: Path,
    image_name: str,
    local_image_name: str,
    registry_prefix: str
) -> dict:
    """Build the image override structure for sd-core values based on enabled sections."""
    overrides = {}

    # Get enabled sections from the base values file
    enabled_sections = get_enabled_sections(base_values_file)

    if not enabled_sections:
        print("WARNING: No enabled sections found in values file")
        return overrides

    for section_name in enabled_sections:
        # Try to find corresponding chart directory
        section_dir = find_single_directory(chart_dir, section_name)

        # Some charts might have different names, try common alternatives
        if not section_dir and section_name == 'omec-user-plane':
            section_dir = find_single_directory(chart_dir, 'bess-upf')

        if not section_dir or not (section_dir / 'values.yaml').exists():
            continue

        with open(section_dir / 'values.yaml', 'r') as f:
            chart_values = yaml.safe_load(f)

        # Check if this chart has images to override
        if not chart_values.get('images', {}).get('tags'):
            continue

        tags = {}
        for tag_name, tag_value in chart_values.get('images', {}).get('tags', {}).items():
            if tag_name == image_name:
                tags[tag_name] = local_image_name
            else:
                tags[tag_name] = f"{registry_prefix}{tag_value}"

        # Build override structure
        override_struct = {
            'images': {
                'repository': '',
                'tags': tags
            }
        }

        overrides[section_name] = override_struct

    return overrides


def render_section_images_block(images_config: dict) -> list[str]:
    """Render an images block with the indentation expected in sd-core values."""
    rendered = yaml.dump(
        {'images': images_config},
        default_flow_style=False,
        sort_keys=False,
    ).splitlines()
    return [f"  {line}\n" for line in rendered]


def apply_image_overrides_to_content(content: str, overrides: dict) -> str:
    """Apply image overrides by rewriting only the relevant section blocks."""
    lines = content.splitlines(keepends=True)

    for section_name, override_struct in overrides.items():
        section_header = f"{section_name}:"
        section_start = None

        for index, line in enumerate(lines):
            if line.rstrip('\n') == section_header:
                section_start = index
                break

        if section_start is None:
            print(f"WARNING: Section not found in values file: {section_name}", file=sys.stderr)
            continue

        section_end = len(lines)
        for index in range(section_start + 1, len(lines)):
            line = lines[index]
            stripped = line.strip()
            if not stripped:
                continue
            if line.startswith(' ') or line.startswith('\t'):
                continue
            if stripped.startswith('#') or stripped in ('---', '...'):
                continue
            section_end = index
            break

        images_block = render_section_images_block(override_struct['images'])

        existing_start = None
        existing_end = None
        insert_at = None

        for index in range(section_start + 1, section_end):
            line = lines[index]
            stripped = line.strip()

            if line.startswith('  images:'):
                existing_start = index
                existing_end = section_end
                for sibling_index in range(index + 1, section_end):
                    sibling_line = lines[sibling_index]
                    if sibling_line.startswith('  ') and not sibling_line.startswith('    '):
                        existing_end = sibling_index
                        break
                break

            if insert_at is None and stripped == 'config:':
                insert_at = index

        if existing_start is not None and existing_end is not None:
            lines[existing_start:existing_end] = images_block
            delta = len(images_block) - (existing_end - existing_start)
            section_end += delta
        else:
            if insert_at is None:
                insert_at = section_end
            lines[insert_at:insert_at] = images_block

    return ''.join(lines)


def configure_sdcore_images(
    aether_dir: Path,
    image_name: str,
    local_image_name: str
) -> None:
    """Configure sd-core values to use local registry image for testing."""
    print(f"\n=== Configuring {image_name} to use local image ===")

    base_values_file = aether_dir / 'deps' / '5gc' / 'roles' / 'core' / 'templates' / 'sdcore-5g-values.yaml'
    registry_prefix = 'ghcr.io/omec-project/'

    if not base_values_file.exists():
        print(f"ERROR: Values file does not exist: {base_values_file}", file=sys.stderr)
        sys.exit(1)

    # Read original content
    original_content = base_values_file.read_text()

    # Replace Aether templates with placeholders so YAML parsing can inspect enabled sections.
    print("Replacing Aether templates with placeholders...")
    modified_content = replace_aether_templates_with_placeholders(original_content)

    # Write modified content to temp file for YAML processing
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as temp_base:
        temp_base.write(modified_content)
        temp_base_path = Path(temp_base.name)

    try:
        # Get chart info and pull chart
        chart_ref, chart_version = get_chart_info(aether_dir)
        print(f"Chart: {chart_ref} version {chart_version}")

        # Create temp directory for chart
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_chart_dir = Path(temp_dir)

            # Pull the Helm chart into temp directory
            print(f"Pulling Helm chart...")
            run_command(['helm', 'pull', chart_ref, '--version', chart_version, '--untar', '--destination', str(temp_chart_dir)],
                       check=True, capture=False)

            # Find the pulled chart directory (should be in temp directory)
            chart_dirs = [d for d in temp_chart_dir.iterdir() if d.is_dir() and d.name.startswith('sd-core')]
            if not chart_dirs:
                print("ERROR: Could not find pulled chart directory", file=sys.stderr)
                sys.exit(1)
            if len(chart_dirs) > 1:
                print(f"ERROR: Found multiple sd-core directories in temp directory: {[d.name for d in chart_dirs]}", file=sys.stderr)
                sys.exit(1)

            pulled_chart_dir = chart_dirs[0]

            # Build image overrides
            print("\n=== Extracting image tags from Helm chart values ===")
            overrides = build_image_overrides(
                pulled_chart_dir,
                temp_base_path,
                image_name,
                local_image_name,
                registry_prefix
            )

            # Update only the images blocks so templated YAML structure is preserved.
            final_content = apply_image_overrides_to_content(original_content, overrides)

            # Write final content back to original file
            base_values_file.write_text(final_content)

            print(f"\n=== Image overrides merged into: {base_values_file} ===")
            print(f"Local image ({image_name}): {local_image_name}")
            print(f"Other images: {registry_prefix}<image from chart>")

            # Clean up pulled chart
            shutil.rmtree(pulled_chart_dir, ignore_errors=True)

    finally:
        temp_base_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(
        description='Updates aether-onramp configuration files for CI environment'
    )
    parser.add_argument(
        'aether_onramp_dir',
        type=Path,
        help='Path to aether-onramp directory'
    )
    parser.add_argument(
        'image_name',
        nargs='?',
        help='(optional) Name of image to override in sd-core values'
    )
    parser.add_argument(
        'local_image_name',
        nargs='?',
        help='(optional) Local image tag to use for testing'
    )

    args = parser.parse_args()

    if not args.aether_onramp_dir.exists():
        print(f"ERROR: Directory does not exist: {args.aether_onramp_dir}", file=sys.stderr)
        sys.exit(1)

    # Detect network interface and IP
    interface, ip_addr = get_network_info()
    print(f"Extracted IP: {ip_addr}")
    print(f"Interface: {interface}")

    # Update basic aether-onramp configuration
    update_hosts_ini(args.aether_onramp_dir)
    update_vars_main(args.aether_onramp_dir, interface, ip_addr)
    print("\nUpdated aether-onramp configuration files")

    # Configure sd-core images if parameters provided
    if args.image_name and args.local_image_name:
        configure_sdcore_images(
            args.aether_onramp_dir,
            args.image_name,
            args.local_image_name
        )
    else:
        print("\n=== Skipping sd-core values configuration (IMAGE_NAME and LOCAL_IMAGE_NAME not provided) ===")


if __name__ == '__main__':
    main()
