# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

from typing import Any, List, TypeVar

import torch


def get_xformers_operator(name: str):
    def no_such_operator(*args, **kwargs):
        raise RuntimeError(
            f"No such operator xformers::{name} - did you forget to build xformers with `python setup.py develop`?"
        )

    try:
        return getattr(torch.ops.xformers, name)
    except (RuntimeError, AttributeError):
        return no_such_operator


OPERATORS_REGISTRY: List[Any] = []

ClsT = TypeVar("ClsT")


def register_operator(cls: ClsT) -> ClsT:
    global OPERATORS_REGISTRY
    OPERATORS_REGISTRY.append(cls)
    return cls
