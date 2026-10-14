#!/usr/bin/env python3
"""Exercise the actual BF16 FLA/Causal Conv1d forward/backward CUDA kernels."""
import argparse
import importlib.metadata
import json
from pathlib import Path

import torch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-causal-conv1d", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError("A CUDA GPU with BF16 support is required")
    torch.manual_seed(42)
    from fla.ops.gated_delta_rule import chunk_gated_delta_rule

    tensors = [torch.randn(1, 256, 4, 128, dtype=torch.bfloat16,
                           device="cuda", requires_grad=True) for _ in range(3)]
    gate = (-torch.rand(1, 256, 4, device="cuda")).requires_grad_()
    beta = torch.rand(1, 256, 4, device="cuda", requires_grad=True)
    result, _ = chunk_gated_delta_rule(*tensors, g=gate, beta=beta,
                                      use_qk_l2norm_in_kernel=True)
    loss = result.float().square().mean()
    loss.backward()
    torch.cuda.synchronize()
    assert torch.isfinite(loss).item()
    assert all(t.grad is not None and torch.isfinite(t.grad).all().item()
               for t in [*tensors, gate, beta])
    report = {"status": "ok", "gpu": torch.cuda.get_device_name(),
              "torch": torch.__version__, "cuda_runtime": torch.version.cuda,
              "fla_version": importlib.metadata.version("flash-linear-attention"),
              "fla_bf16_forward_backward": True, "fla_loss": loss.item()}
    if not args.skip_causal_conv1d:
        from causal_conv1d import causal_conv1d_fn

        inputs = torch.randn(1, 128, 256, device="cuda", dtype=torch.bfloat16,
                             requires_grad=True)
        weights = torch.randn(128, 4, device="cuda", dtype=torch.bfloat16,
                              requires_grad=True)
        conv = causal_conv1d_fn(inputs, weights, activation="silu")
        conv.float().square().mean().backward()
        torch.cuda.synchronize()
        assert all(t.grad is not None and torch.isfinite(t.grad).all().item()
                   for t in [inputs, weights])
        report["causal_conv1d_version"] = importlib.metadata.version("causal-conv1d")
        report["causal_conv1d_bf16_forward_backward"] = True
    else:
        report["causal_conv1d_bf16_forward_backward"] = "not_tested"
    content = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(content, encoding="utf-8")
    print(content)


if __name__ == "__main__":
    main()
