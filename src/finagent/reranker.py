"""Local Qwen3 yes/no-logit scoring service; model dependencies are optional."""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import hmac
import math
from pathlib import Path
from threading import BoundedSemaphore
from time import monotonic
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .harness.memory_search import QWEN3_MEMORY_INSTRUCTION


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    query: str = Field(min_length=1, max_length=4000)
    documents: list[Annotated[str, Field(min_length=1, max_length=12000)]] = Field(min_length=1, max_length=100)
    top_n: int | None = Field(None, ge=1, le=100)


class QwenReranker:
    """Use the official Qwen3 pair template and P(yes | yes,no), without generation."""
    def __init__(self, model_path, *, device="cuda:0", quantization="4bit", max_length=8192, batch_size=1):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable; select --device cpu --quantization none explicitly")
        if quantization == "4bit" and not device.startswith("cuda"):
            raise ValueError("4-bit serving requires a CUDA device")
        self.torch, self.device = torch, device
        self.max_length, self.batch_size = max_length, batch_size
        self.quantization = quantization
        dtype = (torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16) if device.startswith("cuda") else torch.float32
        options = {"device_map": {"": device}, "torch_dtype": dtype,
                   "local_files_only": True, "trust_remote_code": False, "attn_implementation": "sdpa"}
        if quantization == "4bit":
            options["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True,
                bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=dtype)
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, padding_side="left",
            local_files_only=True, trust_remote_code=False)
        self.model = AutoModelForCausalLM.from_pretrained(model_path, **options).eval()
        tokens = [self.tokenizer.encode(word, add_special_tokens=False) for word in ("no", "yes")]
        if any(len(value) != 1 for value in tokens):
            raise ValueError("The tokenizer must encode yes/no as single tokens")
        self.no, self.yes = tokens[0][0], tokens[1][0]
        prefix = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query '
                  'and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        self.prefix = self.tokenizer.encode(prefix, add_special_tokens=False)
        self.suffix = self.tokenizer.encode(suffix, add_special_tokens=False)

    def score(self, query, documents, *, deadline):
        torch, scores, prompt_tokens = self.torch, [], 0
        for start in range(0, len(documents), self.batch_size):
            if monotonic() >= deadline:
                raise TimeoutError("reranking deadline exceeded")
            pairs = [f"<Instruct>: {QWEN3_MEMORY_INSTRUCTION}\n<Query>: {query}\n<Document>: {doc}"
                     for doc in documents[start:start + self.batch_size]]
            encoded = self.tokenizer(pairs, padding=False, add_special_tokens=False, truncation=True,
                max_length=self.max_length - len(self.prefix) - len(self.suffix), return_attention_mask=False)
            rows = [{"input_ids": self.prefix + row + self.suffix} for row in encoded["input_ids"]]
            inputs = self.tokenizer.pad(rows, padding=True, return_tensors="pt").to(self.device)
            prompt_tokens += int(inputs["attention_mask"].sum().item())
            with torch.inference_mode():
                logits = self.model(**inputs, use_cache=False, logits_to_keep=1).logits[:, -1, :]
                values = torch.softmax(logits[:, [self.no, self.yes]].float(), dim=-1)[:, 1]
                scores.extend(values.cpu().tolist())
        return scores, prompt_tokens


def create_app(loader, *, model_name="Qwen/Qwen3-Reranker-8B", api_key="", request_seconds=25):
    @asynccontextmanager
    async def lifespan(app):
        app.state.scorer = loader()
        yield
        app.state.scorer = None

    app = FastAPI(title="FinSight memory reranker", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    gate = BoundedSemaphore(1)

    @app.get("/health")
    def health():
        scorer = app.state.scorer
        return {"status": "ok", "model": model_name, "device": scorer.device,
                "quantization": scorer.quantization, "max_length": scorer.max_length}

    @app.post("/v1/rerank")
    def rerank(body: RerankRequest, authorization: str = Header("")):
        if api_key and not hmac.compare_digest(authorization.encode(), ("Bearer " + api_key).encode()):
            raise HTTPException(401, "Invalid reranker credential")
        if body.model != model_name:
            raise HTTPException(422, "Requested reranker model is not served here")
        if len(body.query) + sum(len(doc) for doc in body.documents) > 30000:
            raise HTTPException(413, "Reranker shortlist exceeds the character budget")
        if not gate.acquire(blocking=False):
            raise HTTPException(503, "Reranker is busy", headers={"Retry-After": "1"})
        try:
            scores, tokens = app.state.scorer.score(body.query, body.documents, deadline=monotonic() + request_seconds)
            if len(scores) != len(body.documents) or any(
                    isinstance(score, bool) or not isinstance(score, (int, float))
                    or not math.isfinite(score) or not 0 <= score <= 1 for score in scores):
                raise ValueError("Invalid scorer output")
            ranked = sorted(enumerate(scores), key=lambda item: (-item[1], item[0]))
            return {"model": model_name, "results": [
                {"index": index, "relevance_score": score} for index, score in ranked[:body.top_n]],
                "usage": {"prompt_tokens": tokens, "completion_tokens": 0}}
        except (RuntimeError, ValueError, TimeoutError):
            # Neither prompts nor upstream exception text are returned or logged.
            raise HTTPException(503, "Reranker inference unavailable") from None
        finally:
            gate.release()

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, default=Path(".finagent/models/Qwen3-Reranker-8B"))
    parser.add_argument("--port", type=int, default=11436)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--quantization", choices=("4bit", "none"), default="4bit")
    parser.add_argument("--max-length", type=int, default=8192)
    parser.add_argument("--batch-size", type=int, default=1)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 256 <= args.max_length <= 32768 or not 1 <= args.batch_size <= 8:
        parser.error("port, max-length or batch-size is outside its supported range")
    path = args.model_path.resolve()
    if not (path / "config.json").is_file():
        parser.error("Download the official Qwen/Qwen3-Reranker-8B checkpoint into --model-path first")
    from .config import load_settings
    settings = load_settings()
    app = create_app(lambda: QwenReranker(str(path), device=args.device,
        quantization=args.quantization, max_length=args.max_length, batch_size=args.batch_size),
        api_key=settings.memory.rerank_api_key.get_secret_value())
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
