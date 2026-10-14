"""Compatibility shim for TorchCodec 0.2 and the local ms-swift checkout.

The local ms-swift video wrapper turns a local path into ``io.BytesIO`` before
calling qwen-vl-utils. TorchCodec 0.2 accepts raw ``bytes`` but not ``BytesIO``.
This external plugin preserves ms-swift's URL/base64 behavior while converting
that one representation. It can be removed after upstream accepts BytesIO or
the local wrapper passes bytes itself.
"""

from __future__ import annotations

from io import BytesIO


def apply_patch() -> None:
    from swift.model.models import qwen

    current = qwen._get_new_read_video_func
    if getattr(current, "_kvc_torchcodec_compat", False):
        return
    original_factory = current

    def compatible_factory(read_video_func, read_backend):
        if read_backend != "torchcodec":
            return original_factory(read_video_func, read_backend)

        def wrapped(ele: dict):
            from swift.template import load_file

            copied = dict(ele)
            source = load_file(copied["video"])
            if isinstance(source, BytesIO):
                source = source.getvalue()
            copied["video"] = source
            return read_video_func(copied)

        return wrapped

    compatible_factory._kvc_torchcodec_compat = True
    qwen._get_new_read_video_func = compatible_factory


apply_patch()

