"""Fetch/import the reviewed public sample manifest. No credentials or scraping.

Run from the repository root after installing finagent[pdf,calendar]. Files stay
in data_dir/public_samples; only metadata/annotations are distributed here.
"""
import argparse
import json
from pathlib import Path

import httpx

from finagent.config import load_settings
from finagent.data.evidence import EvidenceStore, write_json
from finagent.experiments.legacy_library import DocumentImport, KnowledgeBase
from finagent.runtime import now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="finagent.toml")
    parser.add_argument("--manifest", default="doc/public_samples.json")
    parser.add_argument("--offline", action="store_true", help="仅导入已下载原文")
    args = parser.parse_args()
    settings = load_settings(args.config)
    store = EvidenceStore(settings.data_dir)
    directory = settings.data_dir / "public_samples"
    directory.mkdir(parents=True, exist_ok=True)
    results = []
    for item in json.loads(Path(args.manifest).read_text(encoding="utf-8")):
        spec = DocumentImport.model_validate(item["metadata"])
        filename = item["filename"]
        if Path(filename).name != filename:
            raise ValueError("sample filename must be a basename")
        path = directory / filename
        try:
            if not path.is_file() and not args.offline:
                with httpx.Client(follow_redirects=False, timeout=20) as client:
                    with client.stream("GET", spec.source_url) as response:
                        response.raise_for_status()
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 50 * 1024 * 1024:
                                raise ValueError("sample exceeds 50MiB")
                            chunks.append(chunk)
                payload = b"".join(chunks)
                if not payload.startswith(b"%PDF"):
                    raise ValueError("not a PDF")
                path.write_bytes(payload)
            print("Importing " + filename, flush=True)
            doc = KnowledgeBase(store).ingest(path, spec)
            record = {"filename": filename, "document_id": doc["document_id"], "snapshot_id": doc["snapshot_id"],
                      "source_url": spec.source_url, "parsed_blocks": len(doc["blocks"]), "facts": len(doc["facts"]),
                      "pending": doc["pending"], "status": doc["quality"], "synthetic": False}
        except Exception as exc:
            record = {"filename": filename, "status": "failed", "error_type": type(exc).__name__}
        results.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        write_json(settings.data_dir / "checks/p0_public_materials.json", {"checked_at": now().isoformat(), "samples": results})
    return 0 if all(r["status"] != "failed" and not r.get("pending") for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
