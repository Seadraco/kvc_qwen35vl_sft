"""ms-swift plugin: rank-local runtime evidence without altering the SFT loss."""

from __future__ import annotations

import json
from pathlib import Path

import torch
from swift.callbacks import TrainerCallback, callbacks_map


class KvcRuntimeCallback(TrainerCallback):
    def on_train_begin(self, args, state, control, model=None, **kwargs):
        self.trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
        self.total = sum(p.numel() for p in model.parameters())
        self.visual_trainable = sum(
            p.numel() for name, p in model.named_parameters()
            if p.requires_grad and ".visual." in name
        )
        try:
            self.dataset_rows = len(self.trainer.train_dataset)
        except TypeError:
            self.dataset_rows = None
        # Include loaded model memory and subsequent optimizer, training and
        # checkpoint-save allocations. Each rank owns a distinct report file.
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        self._write(args, state, "started")

    def _write(self, args, state, status):
        rank = int(args.process_index)
        world = int(args.world_size)
        payload = {
            "status": status,
            "rank": rank,
            "world_size": world,
            "global_step": state.global_step,
            "per_device_train_batch_size": args.per_device_train_batch_size,
            "gradient_accumulation_steps": args.gradient_accumulation_steps,
            "global_batch_size": world * args.per_device_train_batch_size * args.gradient_accumulation_steps,
            "global_batch_size_is_configured": True,
            "dataset_rows": self.dataset_rows,
            "dataset_rows_divisible_by_global_batch": (
                self.dataset_rows % (world * args.per_device_train_batch_size * args.gradient_accumulation_steps) == 0
                if self.dataset_rows is not None else None
            ),
            "trainable_parameters": self.trainable,
            "total_parameters": self.total,
            "trainable_fraction": self.trainable / self.total,
            "trainable_visual_parameters": self.visual_trainable,
            "cuda_device": str(args.device),
            "gpu_name": torch.cuda.get_device_name() if torch.cuda.is_available() else None,
            "peak_memory_allocated_bytes": torch.cuda.max_memory_allocated() if torch.cuda.is_available() else 0,
            "peak_memory_reserved_bytes": torch.cuda.max_memory_reserved() if torch.cuda.is_available() else 0,
        }
        output = Path(args.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        target = output / f"runtime_rank_{rank}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

    def on_save(self, args, state, control, **kwargs):
        self._write(args, state, "checkpoint_saved")

    def on_train_end(self, args, state, control, **kwargs):
        self._write(args, state, "completed")


callbacks_map["kvc_runtime"] = KvcRuntimeCallback
