"""Run a local intent Ollama instance using the project's model proxy."""
import argparse
import os
from pathlib import Path

from finagent.config import load_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--port", type=int, default=11435)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    settings = load_settings()
    models = settings.data_dir / "intent-ollama-models"
    models.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "HOME", "USER", "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES"}}
    env.update(OLLAMA_HOST=f"127.0.0.1:{args.port}", OLLAMA_MODELS=str(models),
               OLLAMA_NUM_PARALLEL="1", OLLAMA_MAX_LOADED_MODELS="1", NO_PROXY="127.0.0.1,localhost")
    if settings.model.proxy_url:
        env["HTTPS_PROXY"] = settings.model.proxy_url
    binary = str(Path(args.binary).resolve(strict=True))
    os.execve(binary, [binary, "serve"], env)


if __name__ == "__main__":
    main()
