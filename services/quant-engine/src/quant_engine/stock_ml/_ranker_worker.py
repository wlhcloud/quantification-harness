"""Disposable LightGBM training worker used to release CUDA/native memory."""
from __future__ import annotations

import json
import pickle
import sys
import traceback
from pathlib import Path


def run_worker(request_path: str, result_path: str) -> None:
    """Multiprocessing entry point; always returns a JSON result envelope."""
    output = Path(result_path)
    try:
        with Path(request_path).open("rb") as fh:
            request = pickle.load(fh)
        with Path(request["frameFile"]).open("rb") as fh:
            frame = pickle.load(fh)

        from ..etf_quant.ranker import train_ranker

        result = train_ranker(frame, request["label"], request["modelConfig"],
                              Path(request["outputDir"]))
        envelope = {"ok": True, "result": result}
    except BaseException as error:
        envelope = {"ok": False, "error": str(error), "traceback": traceback.format_exc()}
    output.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    run_worker(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
