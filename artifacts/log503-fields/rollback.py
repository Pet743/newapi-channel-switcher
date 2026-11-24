"""Run on the deployment host to restore only the switcher's pre-fix image."""
import json
import subprocess
from pathlib import Path


def main():
    release = Path('/opt/newapi-companions/switcher/releases/20261125-log503-fields')
    manifest = json.loads((release / 'manifest.json').read_text())
    compose = Path('/opt/newapi-companions/compose.json')
    config = json.loads(compose.read_text())
    config['services']['switcher']['image'] = manifest['rollback_image']
    temp = compose.with_suffix('.json.rollback.tmp')
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    temp.chmod(compose.stat().st_mode & 0o777)
    temp.replace(compose)
    subprocess.run(['docker', 'compose', '-f', str(compose), 'up', '-d', '--no-deps', '--force-recreate', 'switcher'], check=True)
    print('Switcher restored to', manifest['rollback_image'])


if __name__ == '__main__':
    main()
