#!/usr/bin/env python3
"""Load only the cached Mistral 4 embedding onto a selected 2-D mesh."""

import argparse
import os
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sp", type=int, required=True)
    parser.add_argument("--tp", type=int, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()

    os.environ["MISTRAL4_HF_MODEL"] = str(args.model.resolve())
    os.environ["PREFILL_HF_MODEL"] = str(args.model.resolve())
    os.environ["PREFILL_TTNN_CACHE"] = str(args.cache_root.resolve())
    os.environ.setdefault("PREFILL_FABRIC_MODE", "2d")

    import ttnn
    from models.demos.common.prefill.adapter import get_adapter
    from models.demos.common.prefill.runners.runner_utils import open_mesh_device
    from models.demos.deepseek_v3_d_p.tt.tt_parallel_embedding import TtParallelEmbedding

    adapter = get_adapter("mistral_small_4")
    config = adapter.load_hf_config()
    shape = (args.sp, args.tp)
    mesh = None
    try:
        mesh = open_mesh_device(shape, adapter.model_config, l1_small_size=adapter.l1_small_size)
        cache_path = adapter.weight_cache_path(shape)
        start = time.perf_counter()
        print(f"EMBED LOAD START shape={shape} cache={cache_path}", flush=True)
        embedding = TtParallelEmbedding(
            mesh_device=mesh,
            vocab_size=config.vocab_size,
            emb_dim=config.hidden_size,
            torch_weight=None,
            sp_axis=0,
            tp_axis=1,
            weight_cache_path=cache_path,
        )
        print(f"EMBED LOAD COMPLETE seconds={time.perf_counter() - start:.3f}", flush=True)
        del embedding
    finally:
        if mesh is not None:
            ttnn.set_fabric_config(ttnn.FabricConfig.DISABLED)
            ttnn.close_mesh_device(mesh)


if __name__ == "__main__":
    main()
