"""Verify prompt/data alignment and run the training archive outside the source tree."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
from uuid import uuid4


def main():
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--training-dir', type=Path, default=project.parent / 'training/qwen3_intent')
    parser.add_argument('--output', type=Path, default=project / '.finagent/nlu_sft2_validation')
    args = parser.parse_args()
    source = args.training_dir.resolve(); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    for asset, runtime in (('schema/semantic.json', 'nlu_sft2_schema.json'), ('prompts/system.txt', 'nlu_sft2_system.txt')):
        if (source / asset).read_bytes() != (project / 'src/finagent/resources' / runtime).read_bytes():
            raise RuntimeError('Training and production contract differ: ' + asset)
    archive = output / 'qwen3_intent_server_v2.zip'
    excluded = {'.venv', '.git', '__pycache__', 'outputs', 'base_model', 'vendor'}
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(source.rglob('*')):
            relative = path.relative_to(source)
            if path.is_file() and not excluded.intersection(relative.parts):
                bundle.write(path, Path('qwen3_intent') / relative)
    destination = output / ('training_install_' + uuid4().hex)
    with zipfile.ZipFile(archive) as bundle:
        for name in bundle.namelist():
            if not (destination / name).resolve().is_relative_to(destination.resolve()):
                raise RuntimeError('Unsafe training archive entry')
        bundle.extractall(destination)
    environment = {**os.environ, 'PYTHONPATH': '', 'PYTHONUTF8': '1'}
    test = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
             cwd=destination / 'qwen3_intent', env=environment, capture_output=True, text=True, encoding='utf-8')
    (output / 'training_archive_tests.log').write_text(test.stdout + test.stderr, encoding='utf-8')
    if test.returncode:
        raise RuntimeError('Standalone training archive tests failed; see log')
    report = {'training_archive': str(archive), 'sha256': sha256(archive.read_bytes()).hexdigest(),
              'standalone_tests_passed': True, 'contract_snapshots_match': True,
              'finagent_source_required_for_training': False, 'real_gpu_training': False}
    (output / 'training_package_check.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
