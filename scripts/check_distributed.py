#!/usr/bin/env python3
"""Run with torchrun to verify BF16 compute and NCCL before loading 9B weights."""
import datetime
import json
import os

import torch
import torch.distributed as dist


def main() -> None:
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    dist.init_process_group("nccl", timeout=datetime.timedelta(seconds=90))
    try:
        value = torch.tensor([dist.get_rank() + 1.0], device="cuda")
        dist.all_reduce(value)
        expected = dist.get_world_size() * (dist.get_world_size() + 1) / 2
        assert value.item() == expected, (value.item(), expected)
        inputs = torch.randn(256, 256, device="cuda", dtype=torch.bfloat16, requires_grad=True)
        loss = (inputs @ inputs).float().square().mean()
        loss.backward()
        assert bool(torch.isfinite(inputs.grad).all())
        torch.cuda.synchronize()
        print(json.dumps({"status": "ok", "rank": dist.get_rank(),
                          "world_size": dist.get_world_size(), "device": local_rank,
                          "gpu": torch.cuda.get_device_name(local_rank),
                          "capability": torch.cuda.get_device_capability(local_rank),
                          "torch": torch.__version__, "cuda_runtime": torch.version.cuda,
                          "nccl": torch.cuda.nccl.version(), "all_reduce": value.item(),
                          "bf16_backward_finite": True,
                          "nccl_environment": {key: os.environ.get(key) for key in
                                               ["NCCL_P2P_DISABLE", "NCCL_IB_DISABLE", "NCCL_SOCKET_IFNAME"]}}),
              flush=True)
        dist.barrier()
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
