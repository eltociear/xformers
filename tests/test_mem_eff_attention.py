# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

import random
from dataclasses import dataclass
from typing import Any, Sequence, Type

import pytest
import torch
from scipy.stats import binom_test

import xformers.ops
from xformers.ops import fmha

torch.backends.cuda.matmul.allow_tf32 = False
cuda_only = pytest.mark.skipif(not torch.cuda.is_available(), reason="requires CUDA")
_devices = ["cpu", "cuda"] if torch.cuda.is_available() else ["cpu"]


ALL_FW_OPS: Sequence[Type[fmha.common.AttentionFwOpBase]] = [
    fmha.cutlass.FwOp,
    fmha.flash.FwOp,
    fmha.triton.FwOp,
    fmha.small_k.FwOp,
]

ALL_BW_OPS: Sequence[Type[fmha.common.AttentionBwOpBase]] = [
    fmha.cutlass.BwOp,
    fmha.flash.BwOp,
    fmha.triton.BwOp,
    fmha.small_k.BwOp,
]


def sample_random_supported_fw(
    inp: fmha.Inputs, seed: int
) -> Type[fmha.common.AttentionFwOpBase]:
    r = random.Random(seed)
    fw_ops = list(ALL_FW_OPS)
    r.shuffle(fw_ops)
    for op in fw_ops:
        if op.supports(inp):
            return op
    raise NotImplementedError(f"Could not find a FW operator for: {inp}")


def generate_test_shapes_B_Mq_Mkv_H_K_Kv(op):
    shapes = []
    for B in op._TEST_BATCH_SIZES:
        for Mq in [32, 256]:
            for Mkv in [32, 64, 256]:
                for K in op._TEST_K:
                    shapes.append((B, Mq, Mkv, 1, K, K))
        Mq = 256
        Mkv = 128
        K = 32
        H = 1
        # Weird values of parameters
        for M in [2, 3, 15, 31, 32, 34, 68, 72, 90, 132, 136]:
            shapes.append((B, M, Mkv, H, K, K))
            shapes.append((B, Mq, M, H, K, K))
        for _K in [1, 2, 3, 31, 34, 36, 38, 40, 64, 256 + 2, 256 + 8, 512]:
            if _K <= op.SUPPORTED_MAX_K:
                shapes.append((B, Mq, Mkv, H, _K, _K))
        # Different value for K / Kv
        if op.SUPPORTS_DIFFERENT_VALUE_EMBED:
            for _K in [32, 36, 64, 256 + 8]:
                shapes.append((B, Mq, Mkv, H, K, _K))
                shapes.append((B, Mq, Mkv, H, _K, K))
        # Exotic sizes
        for K in op._TEST_K:
            shapes.append((B, 16, 1024, H, K, K))
            shapes.append((B, 1024, 16, H, K, K))
        # Some number of heads
        for H in [3, 5, 12]:
            shapes.append((max(1, B // H), Mq, Mkv, H, K, K))
    # Some strides don't fit on an uint16
    shapes.append((1, 128, 128, 300, 128, 128))
    # TODO: Some strides don't fit on an uint32
    # Crashes on Flash, Errors on Cutlass
    # shapes.append((1, 1, 64000, 300, 128, 128))
    # Add some random shapes
    if op in [
        fmha.cutlass.FwOp,
        fmha.cutlass.BwOp,
        fmha.flash.BwOp,
    ]:
        K_CHOICES = [8 * i for i in range(1, 256 // 8)]
        r = random.Random(0)
        for _ in range(20):
            B = r.randint(1, 400)
            Mq = r.randint(1, 500)
            Mkv = r.randint(1, 500)
            H = r.randint(2, 11)
            B = max(B // H, 1)
            K = r.choice(K_CHOICES)
            Kv = r.choice(K_CHOICES)
            if not op.SUPPORTS_DIFFERENT_VALUE_EMBED:
                Kv = K
            shapes.append((B, Mq, Mkv, H, K, Kv))
    return shapes


def _generate_op_device_dtype_B_Mq_Mkv_H_K_Kv(ops_list, one_shape_per_op: bool = False):
    for op in ops_list:
        for shape in generate_test_shapes_B_Mq_Mkv_H_K_Kv(op):
            has_one = False
            for device in _devices:
                if device not in op.SUPPORTED_DEVICES:
                    continue
                for dtype in op.SUPPORTED_DTYPES:
                    yield (op, device, dtype, *shape)
                    has_one = True
            if has_one and one_shape_per_op:
                break


def _gen_ids(op_device_dtype_B_Mq_Mkv_H_K_Kv):
    return [
        f"{op.NAME}-{device}-{str(dtype)}-{batch_size}-{q_len}-{kv_len}-{h}-{k}-{kv}"
        for (
            op,
            device,
            dtype,
            batch_size,
            q_len,
            kv_len,
            h,
            k,
            kv,
        ) in op_device_dtype_B_Mq_Mkv_H_K_Kv
    ]


_opFW_device_dtype_B_Mq_Mkv_H_K_Kv = list(
    _generate_op_device_dtype_B_Mq_Mkv_H_K_Kv(ALL_FW_OPS)
)
_opFW_device_dtype_B_Mq_Mkv_H_K_Kv_ids = _gen_ids(_opFW_device_dtype_B_Mq_Mkv_H_K_Kv)
_opBW_device_dtype_B_Mq_Mkv_H_K_Kv = list(
    _generate_op_device_dtype_B_Mq_Mkv_H_K_Kv(ALL_BW_OPS)
)
_opBW_device_dtype_B_Mq_Mkv_H_K_Kv_ids = _gen_ids(_opBW_device_dtype_B_Mq_Mkv_H_K_Kv)

_opFW_device_dtype_B_Mq_Mkv_H_K_Kv__xs = list(
    _generate_op_device_dtype_B_Mq_Mkv_H_K_Kv(ALL_FW_OPS, one_shape_per_op=True)
)
_opFW_device_dtype_B_Mq_Mkv_H_K_Kv__xs_ids = _gen_ids(
    _opFW_device_dtype_B_Mq_Mkv_H_K_Kv__xs
)
_opBW_device_dtype_B_Mq_Mkv_H_K_Kv__xs = list(
    _generate_op_device_dtype_B_Mq_Mkv_H_K_Kv(ALL_BW_OPS, one_shape_per_op=True)
)
_opBW_device_dtype_B_Mq_Mkv_H_K_Kv__xs_ids = _gen_ids(
    _opBW_device_dtype_B_Mq_Mkv_H_K_Kv__xs
)


def assert_allclose(
    out: torch.Tensor,
    ref: torch.Tensor,
    msg: str = "failed",
    atol: float = 1e-8,
    rtol: float = 1e-5,
) -> None:
    assert out.shape == ref.shape
    flatten_diff = ((out - ref).abs() - atol - ref.abs() * rtol).flatten()
    max_pos = flatten_diff.argmax()
    max_diff = flatten_diff[max_pos]
    num_different = torch.count_nonzero(flatten_diff > 0)
    percentage = num_different / flatten_diff.numel()
    del flatten_diff
    assert torch.allclose(out, ref, rtol=rtol, atol=atol), (
        f"{msg}: "
        f"out={out.flatten()[max_pos]} and ref={ref.flatten()[max_pos]} (diff={max_diff} > 0)"
        f"/ atol={atol}, rtol={rtol}"
        f"/ total failing elements: {num_different}, percentage={percentage}"
    )


def ref_attention(q, k, v, attn_bias=None, drop_mask=None, p=0.0, scale=None):
    if q.ndim == 4:
        assert p == 0.0
        return ref_attention_bmhk(q, k, v, attn_bias=attn_bias)
    q = q.float()
    k = k.float()
    v = v.float()

    scale = scale if scale is not None else (1 / q.shape[-1] ** 0.5)
    q = q * scale

    attn = q @ k.transpose(-2, -1)
    if attn_bias is not None:
        if isinstance(attn_bias, xformers.ops.AttentionMask):
            attn_bias = attn_bias.to_tensor()
        if attn_bias.shape[0] != attn.shape[0]:
            attn_bias = bmk2bmhk(attn_bias, k.shape[2])
        attn = attn + attn_bias.float()
    attn = attn.softmax(-1)
    if drop_mask is not None:
        attn = attn * (drop_mask / (1 - p))
    return attn @ v


def ref_attention_bmhk(q, k, v, attn_bias, scale=None) -> torch.Tensor:
    assert q.ndim == 4

    def T(t):
        return t.permute((0, 2, 1, 3)).reshape(
            [t.shape[0] * t.shape[2], t.shape[1], t.shape[3]]
        )

    out = ref_attention(T(q), T(k), T(v), attn_bias, scale=scale)
    out = out.reshape([q.shape[0], q.shape[2], q.shape[1], v.shape[3]])
    return out.permute((0, 2, 1, 3))


def create_attn_bias(
    bias_type, batch_size: int, q_len: int, kv_len: int, device, dtype
):
    if bias_type is None:
        return None
    if bias_type is torch.Tensor:
        attn_bias = torch.randn((batch_size, 1, kv_len), device=device, dtype=dtype) * 3
        return attn_bias.expand(batch_size, q_len, kv_len)
    if bias_type is xformers.ops.LowerTriangularMask:
        return bias_type([batch_size, q_len, kv_len], dtype=dtype, device=device)
    assert False, f"Unsupported bias type: {bias_type}"


def create_tensors(
    op,
    device,
    dtype,
    B,
    q_len,
    kv_len,
    h,
    k,
    kv,
    *,
    attn_bias_type=None,
    fmt: str = "BMK",
):
    torch.manual_seed(B * q_len + kv_len * k + kv)
    scale = 3
    if fmt == "BMK":
        query = torch.randn((B * h, q_len, k), device=device, dtype=dtype).mul_(scale)
        key = torch.randn((B * h, kv_len, k), device=device, dtype=dtype).mul_(scale)
        value = torch.randn((B * h, kv_len, kv), device=device, dtype=dtype).mul_(scale)
    else:
        assert fmt == "BMHK"
        query = torch.randn((B, q_len, h, k), device=device, dtype=dtype).mul_(scale)
        key = torch.randn((B, kv_len, h, k), device=device, dtype=dtype).mul_(scale)
        value = torch.randn((B, kv_len, h, kv), device=device, dtype=dtype).mul_(scale)

    attn_bias = None
    if attn_bias_type is not None:
        attn_bias = create_attn_bias(
            attn_bias_type,
            batch_size=B * h,
            q_len=q_len,
            kv_len=kv_len,
            dtype=dtype,
            device=device,
        )

    inputs = fmha.Inputs(query=query, key=key, value=value, attn_bias=attn_bias)
    if not op.supports(inputs):
        err_msg = f"{op.NAME}: unsupported ({inputs})"
        # Ensure we free memory to avoid OOMs
        del query, key, value, attn_bias, inputs
        pytest.skip(err_msg)
    return query, key, value, attn_bias


def bmhk2bmk(tensor) -> torch.Tensor:
    return (
        tensor.permute((0, 2, 1, 3))
        .contiguous()
        .view([tensor.shape[0] * tensor.shape[2], tensor.shape[1], tensor.shape[3]])
    )


def bmk2bmhk(tensor, num_heads: int) -> torch.Tensor:
    return tensor.reshape([-1, num_heads, tensor.shape[1], tensor.shape[2]]).permute(
        (0, 2, 1, 3)
    )


@pytest.mark.parametrize("fmt", ["BMK", "BMHK"])
@pytest.mark.parametrize("packed", [False, True])
@pytest.mark.parametrize(
    "attn_bias_type", [None, xformers.ops.LowerTriangularMask, torch.Tensor]
)
@pytest.mark.parametrize(
    "op_device_dtype_B_Mq_Mkv_H_K_Kv",
    _opFW_device_dtype_B_Mq_Mkv_H_K_Kv,
    ids=_opFW_device_dtype_B_Mq_Mkv_H_K_Kv_ids,
)
def test_forward(
    op_device_dtype_B_Mq_Mkv_H_K_Kv,
    attn_bias_type,
    packed,
    fmt,
):
    (
        op,
        device,
        dtype,
        batch_size,
        q_len,
        kv_len,
        h,
        k,
        kv,
    ) = op_device_dtype_B_Mq_Mkv_H_K_Kv
    if packed and not (k == kv and q_len == kv_len):
        pytest.skip(
            f"packed incompatible with `k ({k}) != kv ({kv})` or `q_len ({q_len}) != kv_len ({kv_len})`"
        )
    query, key, value, attn_bias = create_tensors(
        *op_device_dtype_B_Mq_Mkv_H_K_Kv, attn_bias_type=attn_bias_type, fmt="BMHK"
    )

    if packed:
        c = torch.stack([query, key, value], 2)
        if fmt == "BMK":
            # bm3hk -> 3bhmk -> 3Bmk
            c = c.permute(2, 0, 3, 1, 4).view([3, -1, q_len, k])
            query, key, value = c[0], c[1], c[2]
        else:
            # bm3hk -> 3 x bmhk
            query, key, value = xformers.ops.unbind(c, 2)
        assert not query.is_contiguous()

    out = xformers.ops.memory_efficient_attention_forward(
        query, key, value, attn_bias, op=op
    ).float()
    ref = ref_attention(query, key, value, attn_bias)
    assert out.shape == ref.shape, out.shape

    assert_allclose(
        out,
        ref,
        atol=op.ERROR_ATOL[dtype],
        rtol=op.ERROR_RTOL.get(dtype, 1e-5),
    )


shapes_cu_seqlen = generate_test_shapes_B_Mq_Mkv_H_K_Kv(fmha.cutlass.FwOp)


@dataclass
class CuSeqlenInputs:
    q: torch.Tensor
    k: torch.Tensor
    v: torch.Tensor
    bias: Sequence[Any]

    cu_seqlen_q: torch.Tensor
    max_seqlen_q: int
    cu_seqlen_k: torch.Tensor
    max_seqlen_k: int

    def ref_attention(self) -> torch.Tensor:
        assert self.q.shape[0] == 1
        cu_seqlen_q_cpu = self.cu_seqlen_q.tolist()
        cu_seqlen_k_cpu = self.cu_seqlen_k.tolist()
        outs = []
        for attn_bias, q_start, q_end, k_start, k_end in zip(
            self.bias,
            cu_seqlen_q_cpu,
            cu_seqlen_q_cpu[1:],
            cu_seqlen_k_cpu,
            cu_seqlen_k_cpu[1:],
        ):
            outs.append(
                ref_attention(
                    self.q[:, q_start:q_end],
                    self.k[:, k_start:k_end],
                    self.v[:, k_start:k_end],
                    attn_bias=attn_bias,
                )
            )
        return torch.cat(outs, dim=1)

    @staticmethod
    def generate(
        B_Mq_Mkv_H_K_Kv, attn_bias_type, dtype, device, op
    ) -> "CuSeqlenInputs":
        batch_size, max_q_len, max_kv_len, num_heads, k, kv = B_Mq_Mkv_H_K_Kv
        all_q = []
        all_k = []
        all_v = []
        all_bias = []
        cu_seqlen_q = [0]
        cu_seqlen_k = [0]
        scale = 3
        # Reduce batch size to speedup tests
        batch_size = min(batch_size, 20)
        r = random.Random(max_q_len + k * kv)
        torch.manual_seed(r.randint(0, 128))

        for batch_id in range(batch_size):
            q_len = r.randint(1, max_q_len)
            kv_len = r.randint(1, max_kv_len)

            all_q.append(
                torch.randn((1, q_len, num_heads, k), device=device, dtype=dtype)
                * scale
            )
            all_k.append(
                torch.randn((1, kv_len, num_heads, k), device=device, dtype=dtype)
                * scale
            )
            all_v.append(
                torch.randn((1, kv_len, num_heads, kv), device=device, dtype=dtype)
                * scale
            )

            if batch_id == 0:
                inp = fmha.Inputs(query=all_q[-1], key=all_k[-1], value=all_v[-1])
                if not op.supports(inp):
                    pytest.skip("unsupported configuration")

            cu_seqlen_q += [cu_seqlen_q[-1] + q_len]
            cu_seqlen_k += [cu_seqlen_k[-1] + kv_len]

            attn_bias = None
            if attn_bias_type is not None:
                attn_bias = create_attn_bias(
                    attn_bias_type,
                    batch_size=num_heads,
                    q_len=q_len,
                    kv_len=kv_len,
                    dtype=dtype,
                    device=device,
                )
            all_bias.append(attn_bias)
        return CuSeqlenInputs(
            q=torch.cat(all_q, dim=1),
            k=torch.cat(all_k, dim=1),
            v=torch.cat(all_v, dim=1),
            bias=all_bias,
            cu_seqlen_q=torch.tensor(cu_seqlen_q, dtype=torch.int32, device=device),
            max_seqlen_q=max_q_len,
            cu_seqlen_k=torch.tensor(cu_seqlen_k, dtype=torch.int32, device=device),
            max_seqlen_k=max_kv_len,
        )


@cuda_only
@pytest.mark.parametrize("attn_bias_type", [None, xformers.ops.LowerTriangularMask])
@pytest.mark.parametrize("dtype", list(fmha.cutlass.FwOp.SUPPORTED_DTYPES))
@pytest.mark.parametrize(
    "B_Mq_Mkv_H_K_Kv",
    shapes_cu_seqlen,
    ids=[
        f"{B},{max_q_len},{max_kv_len},{h},{k},{kv}"
        for (B, max_q_len, max_kv_len, h, k, kv) in shapes_cu_seqlen
    ],
)
def test_cu_seqlen_forward(
    B_Mq_Mkv_H_K_Kv,
    attn_bias_type,
    dtype,
):
    device = "cuda"
    op = fmha.cutlass.FwOp

    inputs = CuSeqlenInputs.generate(B_Mq_Mkv_H_K_Kv, attn_bias_type, dtype, device, op)

    out, _ = op.OPERATOR(
        inputs.q,
        inputs.k,
        inputs.v,
        max_seqlen_q=inputs.max_seqlen_q,
        cu_seqlens_q=inputs.cu_seqlen_q,
        cu_seqlens_k=inputs.cu_seqlen_k,
        compute_logsumexp=False,
        causal=attn_bias_type is xformers.ops.LowerTriangularMask,
        scale=None,
    )
    ref = inputs.ref_attention()

    assert_allclose(
        out.float(),
        ref,
        atol=op.ERROR_ATOL[dtype],
        rtol=op.ERROR_RTOL.get(dtype, 1e-5),
    )


@pytest.mark.parametrize("k_len", [5, 6, 32])
@pytest.mark.parametrize("batch_size", [1, 4])
@pytest.mark.parametrize("kv_len", [128, 512])
@pytest.mark.parametrize("q_len", [128, 512])
@pytest.mark.parametrize("device", _devices)
def test_key_query_all_ones(device, q_len, kv_len, batch_size, k_len):
    scale = 3
    query = torch.ones((batch_size, q_len, k_len), device=device)
    key = torch.ones((batch_size, kv_len, k_len), device=device)
    value = torch.randn((batch_size, kv_len, k_len), device=device) * scale

    out = xformers.ops.memory_efficient_attention(query, key, value)
    # this should be equivalent to the average over value
    ref = value.mean(1, keepdim=True).expand_as(query)

    assert_allclose(out, ref, atol=1e-5)


@pytest.mark.parametrize(
    "op_device_dtype_B_Mq_Mkv_H_K_Kv",
    _opFW_device_dtype_B_Mq_Mkv_H_K_Kv,
    ids=_opFW_device_dtype_B_Mq_Mkv_H_K_Kv_ids,
)
def test_logsumexp(op_device_dtype_B_Mq_Mkv_H_K_Kv):
    (
        op,
        device,
        dtype,
        batch_size,
        q_len,
        kv_len,
        h,
        k,
        kv,
    ) = op_device_dtype_B_Mq_Mkv_H_K_Kv
    query, key, value, attn_bias = create_tensors(
        *op_device_dtype_B_Mq_Mkv_H_K_Kv, fmt="BMK"
    )

    _out, lse = xformers.ops.memory_efficient_attention_forward_requires_grad(
        query, key, value
    )
    ref_lse = ((query.float() / k**0.5) @ key.float().transpose(-2, -1)).logsumexp(-1)

    assert_allclose(lse[:, 0, : ref_lse.shape[1]], ref_lse, atol=2e-4)


@pytest.mark.parametrize("fmt", ["BMK", "BMHK"])
@pytest.mark.parametrize(
    "attn_bias_type", [None, xformers.ops.LowerTriangularMask, torch.Tensor]
)
@pytest.mark.parametrize("grad_out_contiguous", [False, True])
@pytest.mark.parametrize(
    "op_device_dtype_B_Mq_Mkv_H_K_Kv",
    _opBW_device_dtype_B_Mq_Mkv_H_K_Kv,
    ids=_opBW_device_dtype_B_Mq_Mkv_H_K_Kv_ids,
)
def test_backward(
    op_device_dtype_B_Mq_Mkv_H_K_Kv,
    grad_out_contiguous,
    attn_bias_type,
    fmt,
):
    (
        op_bw,
        device,
        dtype,
        batch_size,
        q_len,
        kv_len,
        h,
        k,
        kv,
    ) = op_device_dtype_B_Mq_Mkv_H_K_Kv
    query, key, value, attn_bias = create_tensors(
        *op_device_dtype_B_Mq_Mkv_H_K_Kv,
        attn_bias_type=attn_bias_type,
        fmt=fmt,
    )
    op_fw = sample_random_supported_fw(
        fmha.Inputs(query=query, key=key, value=value, attn_bias=attn_bias),
        seed=q_len * kv + kv_len * k,
    )
    qkv = None

    if (
        fmt == "BMHK"
        and query.shape[3] == value.shape[3]
        and query.shape[1] == value.shape[1]
    ):
        qkv = torch.stack([query, key, value], 2)
        qkv.requires_grad_(True)
        # bm3hk -> 3 x bmhk
        query, key, value = xformers.ops.unbind(qkv, 2)
        assert not query.is_contiguous()

    query.requires_grad_(True)
    key.requires_grad_(True)
    value.requires_grad_(True)

    out = xformers.ops.memory_efficient_attention(
        query, key, value, attn_bias, op=(op_fw, op_bw)
    )

    grad_out = torch.ones_like(out)
    if grad_out_contiguous is False:
        grad_out = torch.tensor([1.0], dtype=query.dtype, device=device)[
            None, None, :
        ].expand_as(out)

    out.backward(grad_out)
    del out

    grads = []
    if qkv is None:
        grads = [query.grad, key.grad, value.grad]
        query.grad = None
        key.grad = None
        value.grad = None
    else:
        grads = [qkv.grad]
        qkv.grad = None

    ref = ref_attention(query, key, value, attn_bias)
    ref.backward(grad_out)
    del grad_out
    del ref

    atol = op_bw.ERROR_ATOL[dtype]
    rtol = op_bw.ERROR_RTOL[dtype]

    grads_ref = []
    grads_name = []
    if qkv is None:
        assert isinstance(query.grad, torch.Tensor)
        assert isinstance(key.grad, torch.Tensor)
        assert isinstance(value.grad, torch.Tensor)
        grads_ref = [query.grad, key.grad, value.grad]
        grads_name = ["query", "key", "value"]
    else:
        assert isinstance(qkv.grad, torch.Tensor)
        grads_ref = [qkv.grad]
        grads_name = ["qkv"]
    del query
    del key
    del value
    del qkv

    for name, calc_grad, ref_grad in zip(grads_name, grads, grads_ref):
        assert_allclose(calc_grad, ref_grad, name, atol=atol, rtol=rtol)


def _vec_binom_test(x, n, p):
    """
    vectorized implementation of scipy.stats.binom_test
    this makes our tests much faster
    reference: https://github.com/scipy/scipy/blob/v1.8.0/scipy/stats/_morestats.py#L2609-L2702
    """
    import numpy as np
    from scipy.stats import distributions

    x = np.atleast_1d(x)
    d = distributions.binom.pmf(x, n, p)[:, None]
    rerr = 1 + 1e-7
    # x < p * n case
    i = np.arange(np.ceil(p * n), n + 1)
    y = np.sum(distributions.binom.pmf(i, n, p) <= d * rerr, axis=1)
    pval1 = distributions.binom.cdf(x, n, p) + distributions.binom.sf(n - y, n, p)

    # other case
    i = np.arange(np.floor(p * n) + 1)
    y = np.sum(distributions.binom.pmf(i, n, p) <= d * rerr, axis=1)
    pval2 = distributions.binom.cdf(y - 1, n, p) + distributions.binom.sf(x - 1, n, p)

    pval = np.where(x < p * n, pval1, pval2)
    pval = np.minimum(1.0, pval)
    return pval


@cuda_only
@pytest.mark.parametrize("seed", [42, 124])
@pytest.mark.parametrize("p", [0.3, 0.7])
@pytest.mark.parametrize("k_len", [32])
@pytest.mark.parametrize("batch_size", [1, 2])
@pytest.mark.parametrize("kv_len", [3, 15, 32, 33])
@pytest.mark.parametrize("q_len", [2, 33])
@pytest.mark.parametrize("device", ["cuda"])
def test_dropout(device, q_len, kv_len, batch_size, k_len, p, seed):
    scale = 3
    query = torch.randn((batch_size, q_len, k_len), device=device) * scale
    key = torch.randn((batch_size, kv_len, k_len), device=device) * scale
    value = torch.randn((batch_size, kv_len, k_len), device=device) * scale

    attn_bias = None

    torch.manual_seed(seed)
    out = xformers.ops.memory_efficient_attention(query, key, value, attn_bias, p)

    torch.manual_seed(seed)
    out2 = xformers.ops.memory_efficient_attention(query, key, value, attn_bias, p)

    assert_allclose(out, out2)

    mask = torch.empty((batch_size, q_len, kv_len), device=device)

    torch.manual_seed(seed)
    mask = torch.ops.xformers._temp_dropout(mask, p)

    ref = ref_attention(query, key, value, attn_bias, mask, p)
    assert_allclose(out, ref, atol=2e-4), f"{(out - ref).abs().max()}"

    num_trials = 1000
    p_val_tol = 0.0001
    keep_prob = 1 - p
    masks = []
    for i in range(num_trials):
        mask = torch.ops.xformers._temp_dropout(mask, p)
        masks.append(mask.clone().cpu())
    masks = torch.stack(masks, dim=0)
    p_value = binom_test(masks.sum(), masks.numel(), p=keep_prob)
    assert p_value > p_val_tol, p_value
    masks = masks.sum(0).flatten()
    p_values = _vec_binom_test(masks, num_trials, p=keep_prob)
    assert all(p_values > p_val_tol)


@pytest.mark.skipif(True, reason="Dropout support disabled")
@cuda_only
@pytest.mark.parametrize("p", [0.3, 0.7])
@pytest.mark.parametrize("k_len", [5, 6, 32])
@pytest.mark.parametrize("batch_size", [1, 2])
@pytest.mark.parametrize("kv_len", [3, 15, 32, 33])
@pytest.mark.parametrize("q_len", [2, 33])
@pytest.mark.parametrize("device", ["cuda"])
def test_dropout_backward(device, q_len, kv_len, batch_size, k_len, p):
    scale = 3
    query = torch.randn((batch_size, q_len, k_len), device=device) * scale
    key = torch.randn((batch_size, kv_len, k_len), device=device) * scale
    value = torch.randn((batch_size, kv_len, k_len), device=device) * scale

    query.requires_grad_(True)
    key.requires_grad_(True)
    value.requires_grad_(True)

    grad_out = torch.ones_like(query)

    attn_bias = None

    seed = 42
    torch.manual_seed(seed)
    out = xformers.ops.memory_efficient_attention(query, key, value, attn_bias, p)

    out.backward(grad_out)

    grad_q = query.grad
    grad_k = key.grad
    grad_v = value.grad

    query.grad = None
    key.grad = None
    value.grad = None

    mask = torch.empty((batch_size, q_len, kv_len), device=device)

    torch.manual_seed(seed)
    mask = torch.ops.xformers._temp_dropout(mask, p)

    ref = ref_attention(query, key, value, attn_bias, mask, p)
    ref.backward(grad_out)

    # there is some extra precision loss in the CPU implementation due to an
    # extra accumulation step in grad_q, which is not present in the CUDA
    # implementation
    atol = 5e-4 if device == "cuda" else 6e-4
    assert_allclose(grad_q, query.grad, "grad_q", atol=atol)
    assert_allclose(grad_k, key.grad, "grad_k", atol=atol)
    assert_allclose(grad_v, value.grad, "grad_v", atol=atol)


@pytest.mark.parametrize("k_len", [32])
@pytest.mark.parametrize("batch_size", [1])
@pytest.mark.parametrize("kv_len", [3 * 32])
@pytest.mark.parametrize("q_len", [3 * 32])
@pytest.mark.parametrize("device", _devices)
def test_memory_efficient_attention_full_block_masked(
    device, q_len, kv_len, batch_size, k_len
):
    scale = 3
    query = torch.randn((batch_size, q_len, k_len), device=device) * scale
    key = torch.randn((batch_size, kv_len, k_len), device=device) * scale
    value = torch.randn((batch_size, kv_len, k_len), device=device) * scale

    # in this case, most of the blocks in a row get masked
    attn_bias = torch.full((3, 32), float("-inf"), device=device)
    attn_bias[:2, :4] = 0
    attn_bias = attn_bias.flatten()[None, None, :].expand(1, q_len, -1)

    out = xformers.ops.memory_efficient_attention(query, key, value, attn_bias)
    ref = ref_attention(query, key, value, attn_bias)

    assert_allclose(out, ref, atol=2e-4)

    query.requires_grad_(True)
    key.requires_grad_(True)
    value.requires_grad_(True)

    grad_out = torch.ones_like(query)

    out = xformers.ops.memory_efficient_attention(query, key, value, attn_bias)
    out.backward(grad_out)

    grad_q = query.grad
    grad_k = key.grad
    grad_v = value.grad

    query.grad = None
    key.grad = None
    value.grad = None

    ref = ref_attention(query, key, value, attn_bias)
    ref.backward(grad_out)

    # there is some extra precision loss in the CPU implementation due to an
    # extra accumulation step in grad_q, which is not present in the CUDA
    # implementation
    atol = 5e-4 if device == "cuda" else 6e-4
    assert_allclose(grad_q, query.grad, "grad_q", atol=atol)
    assert_allclose(grad_k, key.grad, "grad_k", atol=atol)
    assert_allclose(grad_v, value.grad, "grad_v", atol=atol)


@pytest.mark.parametrize(
    "op_device_dtype_B_Mq_Mkv_H_K_Kv",
    _opFW_device_dtype_B_Mq_Mkv_H_K_Kv__xs,
    ids=_opFW_device_dtype_B_Mq_Mkv_H_K_Kv__xs_ids,
)
def test_cuda_streams(
    op_device_dtype_B_Mq_Mkv_H_K_Kv,
):
    (
        op,
        device,
        dtype,
        batch_size,
        q_len,
        kv_len,
        h,
        k,
        kv,
    ) = op_device_dtype_B_Mq_Mkv_H_K_Kv
    if device != "cuda":
        pytest.skip("Not CUDA")
    # Needs to be big enough so kernels take some time
    # as we are trying to do a race-condition here
    q_len = 1024
    kv_len = 1024
    op_device_dtype_B_Mq_Mkv_H_K_Kv = [
        op,
        device,
        dtype,
        batch_size,
        q_len,
        kv_len,
        h,
        k,
        kv,
    ]
    s_hipri = torch.cuda.Stream(priority=-1)
    s_lopri = torch.cuda.Stream(priority=0)
    with torch.cuda.stream(s_lopri):
        query, key, value, attn_bias = create_tensors(
            *op_device_dtype_B_Mq_Mkv_H_K_Kv, attn_bias_type=None, fmt="BMHK"
        )
        # Queue a lot of kernels
        for i in range(20):
            query = query.relu()
        query = query * 2
    s_hipri.wait_stream(s_lopri)
    with torch.cuda.stream(s_hipri):
        out = xformers.ops.memory_efficient_attention(query, key, value, op=(op, None))
        # This will run in hi-pri AFTER the kernel if it
        # runs on the correct stream
        out = out / 2
    torch.cuda.synchronize()
    ref = ref_attention(query, key, value) / 2
    assert out.shape == ref.shape, out.shape

    assert_allclose(
        out.float(),
        ref.float(),
        atol=op.ERROR_ATOL[dtype],
        rtol=op.ERROR_RTOL.get(dtype, 1e-5),
    )


@pytest.mark.parametrize(
    "op_device_dtype_B_Mq_Mkv_H_K_Kv",
    _opBW_device_dtype_B_Mq_Mkv_H_K_Kv__xs,
    ids=_opBW_device_dtype_B_Mq_Mkv_H_K_Kv__xs_ids,
)
def test_custom_scale(op_device_dtype_B_Mq_Mkv_H_K_Kv):
    torch.manual_seed(42)
    p = 0.0
    scale = 1

    (
        op_bw,
        device,
        dtype,
        _,
        q_len,
        kv_len,
        _,
        k,
        _,
    ) = op_device_dtype_B_Mq_Mkv_H_K_Kv
    if device != "cuda":
        pytest.skip("Not CUDA")

    query, key, value, attn_bias = create_tensors(
        *op_device_dtype_B_Mq_Mkv_H_K_Kv, attn_bias_type=None, fmt="BMK"
    )
    inputs = fmha.Inputs(
        query=query, key=key, value=value, attn_bias=attn_bias, scale=scale
    )
    op_fw = sample_random_supported_fw(inputs, seed=q_len * k + kv_len * k)
    grad_out = torch.ones_like(query)
    query.requires_grad_(True)
    key.requires_grad_(True)
    value.requires_grad_(True)

    if not op_fw.supports(inputs):
        pytest.skip(f"{op_fw.NAME}: unsupported ({inputs})")
    if not op_bw.supports(inputs):
        pytest.skip(f"{op_bw.NAME}: unsupported ({inputs})")

    out = xformers.ops.memory_efficient_attention(
        query, key, value, attn_bias, p, scale, op=(op_fw, op_bw)
    )
    out.backward(grad_out)
    grad_q, grad_k, grad_v = query.grad, key.grad, value.grad
    query.grad = key.grad = value.grad = None

    ref = ref_attention(query, key, value, attn_bias, None, p, scale)
    ref.backward(grad_out)
    ref_grad_q, ref_grad_k, ref_grad_v = query.grad, key.grad, value.grad
    query.grad = key.grad = value.grad = None

    atol = op_fw.ERROR_ATOL[dtype]
    assert_allclose(out.float(), ref.float(), atol=atol)
    atol = op_bw.ERROR_ATOL[dtype]
    rtol = op_bw.ERROR_RTOL[dtype]
    assert_allclose(grad_q, ref_grad_q, atol=atol, rtol=rtol)
    assert_allclose(grad_k, ref_grad_k, atol=atol, rtol=rtol)
    assert_allclose(grad_v, ref_grad_v, atol=atol, rtol=rtol)
