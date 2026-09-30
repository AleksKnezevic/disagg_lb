#!/usr/bin/env python3
"""Populate the Mistral Small 4 SP2xTP4 prefill TTNN weight cache."""

import argparse
import os
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--mesh-descriptor", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    os.environ["MISTRAL4_HF_MODEL"] = str(args.model.resolve())
    os.environ["PREFILL_HF_MODEL"] = str(args.model.resolve())
    os.environ["PREFILL_TTNN_CACHE"] = str(args.cache_root.resolve())
    os.environ["PREFILL_MAX_SEQ_LEN"] = "5120"
    os.environ["PREFILL_FABRIC_MODE"] = "2d"
    os.environ["TT_MESH_GRAPH_DESC_PATH"] = str(args.mesh_descriptor.resolve())

    import torch
    import ttnn
    from models.demos.common.prefill.adapter import get_adapter
    from models.demos.common.prefill.runners.runner_utils import open_mesh_device
    from models.demos.deepseek_v3_d_p.tt.moe.tt_moe_gate_prefill import GateComputeMode
    from models.demos.deepseek_v3_d_p.tt.tt_ccl import per_axis_topology
    from models.demos.deepseek_v3_d_p.tt.tt_prefill_transformer import TtPrefillTransformer
    from models.demos.deepseek_v3_d_p.utils.transformer_helpers import load_and_compute_layer_by_layer

    mesh = None
    try:
        adapter = get_adapter("mistral_small_4")
        config = adapter.load_hf_config()
        config.max_seq_len = 5120
        mesh = open_mesh_device((2, 4), adapter.model_config, l1_small_size=adapter.l1_small_size)
        cache_path = adapter.weight_cache_path((2, 4))
        print(f"Populating cache at {cache_path}", flush=True)
        load_and_compute_layer_by_layer(
            variant=adapter,
            model_path=args.model.resolve(),
            config=config,
            num_layers=36,
            attention_mask=torch.ones((1, 5120), dtype=torch.int64),
            compute_reference=False,
            build_ttnn_cache=True,
            weight_cache_path=cache_path,
            mesh_device=mesh,
            seq_len=5120,
            num_links=2,
            topology=per_axis_topology(),
            sp_axis=0,
            tp_axis=1,
            gate_fallback_mode=GateComputeMode.GPT_DEVICE,
        )
        complete = TtPrefillTransformer.check_cache_complete(
            cache_path,
            36,
            adapter.model_config.NUM_ROUTED_EXPERTS // 8,
            first_k_dense=adapter.model_config.NUM_DENSE_LAYERS,
        )
        if not complete:
            raise RuntimeError(f"cache population finished but completeness check failed: {cache_path}")
        print(f"CACHE COMPLETE: {cache_path}", flush=True)
    finally:
        if mesh is not None:
            ttnn.set_fabric_config(ttnn.FabricConfig.DISABLED)
            ttnn.close_mesh_device(mesh)


if __name__ == "__main__":
    main()
