"""Copy the standalone project, deny FinAgent imports/network, run real CPU plumbing."""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys
from uuid import uuid4
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-dir", type=Path, default=root.parent / "training/macbert_nlu",
                        help="Standalone project directory; defaults to ../training/macbert_nlu")
    source = parser.parse_args().training_dir.resolve()
    if not (source / "pyproject.toml").is_file():
        parser.error("训练工程不存在，请使用 --training-dir 指定包含 pyproject.toml 的目录")
    audit = root / ".finagent/nlu_validation" / uuid4().hex
    checkout = audit / "standalone"
    checkout.parent.mkdir(parents=True)
    shutil.copytree(source, checkout, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "outputs", "base_model", "delivery", "build", "dist", "private"))
    code = r'''
import sys,importlib.abc,socket,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/"src"))
class DenyHost(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,*args):
  if fullname=="finagent" or fullname.startswith("finagent."):raise ImportError("Standalone check forbids FinAgent")
sys.meta_path.insert(0,DenyHost())
def no_network(*args,**kwargs):raise RuntimeError("Offline check forbids network")
socket.socket.connect=no_network
from finagent_macbert_nlu.smoke import create
from finagent_macbert_nlu.dataset import load_config,load_data
from finagent_macbert_nlu.train import run,resolve_checkpoint
from finagent_macbert_nlu.calibrate import calibrate
from finagent_macbert_nlu.evaluate import evaluate
from finagent_macbert_nlu.export import export
from finagent_macbert_nlu.package import package
from finagent_macbert_nlu.predict import Predictor
from finagent_macbert_nlu.artifacts import verify_bundle
from finagent_macbert_nlu.contracts import write_json,file_hash
config=load_config(create("outputs/smoke"))
splits,data=load_data(config)
summary=run(config)
best=resolve_checkpoint(Path(config["output_dir"])/"best")
calibrate(best);report=evaluate(best)
export(best,"outputs/export")
delivery=package("outputs/export","outputs/delivery")
p=Predictor("outputs/export",production=False)
result=p.predict("分析甲公司新闻")
denied=False
try:verify_bundle("outputs/export",production=True)
except ValueError:denied=True
assert denied
write_json("standalone_result.json",dict(standalone_import=True,network_calls=0,smoke=True,steps=summary["global_step"],data=data["counts"],head_updates=summary["head_updates"],production_denied=denied,evaluation=report,delivery=str(delivery.resolve()),prediction_status=result["input_status"]))
'''
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONUTF8": "1"}
    result = subprocess.run([sys.executable, "-c", code], cwd=checkout, env=env, capture_output=True, text=True)
    (audit / "standalone.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    result.check_returncode()
    delivery = checkout / "outputs/delivery"
    wheel = next(delivery.glob("*.whl"))
    installed = audit / "wheel-install"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    model = next(p for p in delivery.iterdir() if p.is_dir())
    probe = f'''import sys,socket,json
sys.path.insert(0,{str(installed)!r})
def deny(*a,**kw):raise RuntimeError("Network forbidden")
socket.socket.connect=deny
import finagent_macbert_nlu
from finagent_macbert_nlu.predict import Predictor
assert {str(installed)!r} in finagent_macbert_nlu.__file__
p=Predictor({str(model)!r},production=False)
assert p.predict("分析甲公司新闻")["input_status"]=="ok"
print(json.dumps({{"module":finagent_macbert_nlu.__file__,"network_calls":0,"loaded_from_wheel":True}}))
'''
    checked = subprocess.run([sys.executable, "-c", probe], cwd=installed, env=env, capture_output=True, text=True, check=True)
    body = json.loads((checkout / "standalone_result.json").read_text(encoding="utf-8"))
    body.update(wheel_check=json.loads(checked.stdout), audit_dir=str(audit))
    # Import the verified copy, so this developer script does not depend on an
    # editable installation pointing at a particular training source location.
    sys.path.insert(0, str(installed))
    from finagent_macbert_nlu.contracts import file_hash, write_json
    destination = root / ".finagent/dist/nlu"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(wheel, destination / wheel.name)
    training_zip = destination / "macbert_nlu_training.zip"
    with zipfile.ZipFile(training_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(checkout.rglob("*")):
            relative = path.relative_to(checkout)
            if any(part in {"outputs", "__pycache__", "build", "dist"} or part.endswith(".egg-info") for part in relative.parts):
                continue
            if path.is_file() and path.name != "standalone_result.json":
                archive.write(path, Path("training/macbert_nlu") / relative)
        if (source.parent / "README.md").is_file():
            archive.write(source.parent / "README.md", "training/README.md")
    body["training_archive"] = {"path": str(training_zip), "sha256": file_hash(training_zip)}
    write_json(root / ".finagent/nlu_validation/package_check.json", body)
    print(json.dumps({k: v for k, v in body.items() if k != "evaluation"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
