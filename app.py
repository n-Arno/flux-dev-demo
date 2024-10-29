# import os
# import spaces

import time
import gradio as gr
import torch
from PIL import Image
from torchvision import transforms
from dataclasses import dataclass
import math
from typing import Callable

from tqdm import tqdm
import bitsandbytes as bnb
from bitsandbytes.nn.modules import Params4bit, QuantState

import torch
import random
from einops import rearrange, repeat
from diffusers import AutoencoderKL
from torch import Tensor, nn
from transformers import CLIPTextModel, CLIPTokenizer
from transformers import T5EncoderModel, T5Tokenizer
# from optimum.quanto import freeze, qfloat8, quantize


# ---------------- Encoders ----------------


class HFEmbedder(nn.Module):
    def __init__(self, version: str, max_length: int, **hf_kwargs):
        super().__init__()
        self.is_clip = version.startswith("openai")
        self.max_length = max_length
        self.output_key = "pooler_output" if self.is_clip else "last_hidden_state"

        if self.is_clip:
            self.tokenizer: CLIPTokenizer = CLIPTokenizer.from_pretrained(version, max_length=max_length)
            self.hf_module: CLIPTextModel = CLIPTextModel.from_pretrained(version, **hf_kwargs)
        else:
            self.tokenizer: T5Tokenizer = T5Tokenizer.from_pretrained(version, max_length=max_length)
            self.hf_module: T5EncoderModel = T5EncoderModel.from_pretrained(version, **hf_kwargs)

        self.hf_module = self.hf_module.eval().requires_grad_(False)

    def forward(self, text: list[str]) -> Tensor:
        batch_encoding = self.tokenizer(
            text,
            truncation=True,
            max_length=self.max_length,
            return_length=False,
            return_overflowing_tokens=False,
            padding="max_length",
            return_tensors="pt",
        )

        outputs = self.hf_module(
            input_ids=batch_encoding["input_ids"].to(self.hf_module.device),
            attention_mask=None,
            output_hidden_states=False,
        )
        return outputs[self.output_key]
    

device = "cuda"
t5 = HFEmbedder("DeepFloyd/t5-v1_1-xxl", max_length=512, torch_dtype=torch.bfloat16).to(device)
clip = HFEmbedder("openai/clip-vit-large-patch14", max_length=77, torch_dtype=torch.bfloat16).to(device)
ae = AutoencoderKL.from_pretrained("black-forest-labs/FLUX.1-dev", subfolder="vae", torch_dtype=torch.bfloat16).to(device)
# quantize(t5, weights=qfloat8)
# freeze(t5)


# ---------------- NF4 ----------------


def functional_linear_4bits(x, weight, bias):
    out = bnb.matmul_4bit(x, weight.t(), bias=bias, quant_state=weight.quant_state)
    out = out.to(x)
    return out


def copy_quant_state(state: QuantState, device: torch.device = None) -> QuantState:
    if state is None:
        return None

    device = device or state.absmax.device

    state2 = (
        QuantState(
            absmax=state.state2.absmax.to(device),
            shape=state.state2.shape,
            code=state.state2.code.to(device),
            blocksize=state.state2.blocksize,
            quant_type=state.state2.quant_type,
            dtype=state.state2.dtype,
        )
        if state.nested
        else None
    )

    return QuantState(
        absmax=state.absmax.to(device),
        shape=state.shape,
        code=state.code.to(device),
        blocksize=state.blocksize,
        quant_type=state.quant_type,
        dtype=state.dtype,
        offset=state.offset.to(device) if state.nested else None,
        state2=state2,
    )


class ForgeParams4bit(Params4bit):
    def to(self, *args, **kwargs):
        device, dtype, non_blocking, convert_to_format = torch._C._nn._parse_to(*args, **kwargs)
        if device is not None and device.type == "cuda" and not self.bnb_quantized:
            return self._quantize(device)
        else:
            n = ForgeParams4bit(
                torch.nn.Parameter.to(self, device=device, dtype=dtype, non_blocking=non_blocking),
                requires_grad=self.requires_grad,
                quant_state=copy_quant_state(self.quant_state, device),
                # blocksize=self.blocksize,
                # compress_statistics=self.compress_statistics,
                compress_statistics=False,
                blocksize=64,
                quant_type=self.quant_type,
                quant_storage=self.quant_storage,
                bnb_quantized=self.bnb_quantized,
                module=self.module
            )
            self.module.quant_state = n.quant_state
            self.data = n.data
            self.quant_state = n.quant_state
            return n


class ForgeLoader4Bit(torch.nn.Module):
    def __init__(self, *, device, dtype, quant_type, **kwargs):
        super().__init__()
        self.dummy = torch.nn.Parameter(torch.empty(1, device=device, dtype=dtype))
        self.weight = None
        self.quant_state = None
        self.bias = None
        self.quant_type = quant_type

    def _save_to_state_dict(self, destination, prefix, keep_vars):
        super()._save_to_state_dict(destination, prefix, keep_vars)
        quant_state = getattr(self.weight, "quant_state", None)
        if quant_state is not None:
            for k, v in quant_state.as_dict(packed=True).items():
                destination[prefix + "weight." + k] = v if keep_vars else v.detach()
        return

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs):
        quant_state_keys = {k[len(prefix + "weight."):] for k in state_dict.keys() if k.startswith(prefix + "weight.")}

        if any('bitsandbytes' in k for k in quant_state_keys):
            quant_state_dict = {k: state_dict[prefix + "weight." + k] for k in quant_state_keys}

            self.weight = ForgeParams4bit.from_prequantized(
                data=state_dict[prefix + 'weight'],
                quantized_stats=quant_state_dict,
                requires_grad=False,
                # device=self.dummy.device,
                device=torch.device('cuda'),
                module=self
            )
            self.quant_state = self.weight.quant_state

            if prefix + 'bias' in state_dict:
                self.bias = torch.nn.Parameter(state_dict[prefix + 'bias'].to(self.dummy))

            del self.dummy
        elif hasattr(self, 'dummy'):
            if prefix + 'weight' in state_dict:
                self.weight = ForgeParams4bit(
                    state_dict[prefix + 'weight'].to(self.dummy),
                    requires_grad=False,
                    compress_statistics=True,
                    quant_type=self.quant_type,
                    quant_storage=torch.uint8,
                    module=self,
                )
                self.quant_state = self.weight.quant_state

            if prefix + 'bias' in state_dict:
                self.bias = torch.nn.Parameter(state_dict[prefix + 'bias'].to(self.dummy))

            del self.dummy
        else:
            super()._load_from_state_dict(state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs)


class Linear(ForgeLoader4Bit):
    def __init__(self, *args, device=None, dtype=None, **kwargs):
        super().__init__(device=device, dtype=dtype, quant_type='nf4')

    def forward(self, x):
        self.weight.quant_state = self.quant_state

        if self.bias is not None and self.bias.dtype != x.dtype:
            # Maybe this can also be set to all non-bnb ops since the cost is very low.
            # And it only invokes one time, and most linear does not have bias
            self.bias.data = self.bias.data.to(x.dtype)

        return functional_linear_4bits(x, self.weight, self.bias)
    

nn.Linear = Linear


# ---------------- Model ----------------


def attention(q: Tensor, k: Tensor, v: Tensor, pe: Tensor) -> Tensor:
    q, k = apply_rope(q, k, pe)

    x = torch.nn.functional.scaled_dot_product_attention(q, k, v)
    # x = rearrange(x, "B H L D -> B L (H D)")
    x = x.permute(0, 2, 1, 3).reshape(x.size(0), x.size(2), -1)

    return x


def rope(pos, dim, theta):
    scale = torch.arange(0, dim, 2, dtype=torch.float64, device=pos.device) / dim
    omega = 1.0 / (theta ** scale)

    # out = torch.einsum("...n,d->...nd", pos, omega)
    out = pos.unsqueeze(-1) * omega.unsqueeze(0)

    cos_out = torch.cos(out)
    sin_out = torch.sin(out)
    out = torch.stack([cos_out, -sin_out, sin_out, cos_out], dim=-1)

    # out = rearrange(out, "b n d (i j) -> b n d i j", i=2, j=2)
    b, n, d, _ = out.shape
    out = out.view(b, n, d, 2, 2)

    return out.float()


def apply_rope(xq: Tensor, xk: Tensor, freqs_cis: Tensor) -> tuple[Tensor, Tensor]:
    xq_ = xq.float().reshape(*xq.shape[:-1], -1, 1, 2)
    xk_ = xk.float().reshape(*xk.shape[:-1], -1, 1, 2)
    xq_out = freqs_cis[..., 0] * xq_[..., 0] + freqs_cis[..., 1] * xq_[..., 1]
    xk_out = freqs_cis[..., 0] * xk_[..., 0] + freqs_cis[..., 1] * xk_[..., 1]
    return xq_out.reshape(*xq.shape).type_as(xq), xk_out.reshape(*xk.shape).type_as(xk)


class EmbedND(nn.Module):
    def __init__(self, dim: int, theta: int, axes_dim: list[int]):
        super().__init__()
        self.dim = dim
        self.theta = theta
        self.axes_dim = axes_dim

    def forward(self, ids: Tensor) -> Tensor:
        n_axes = ids.shape[-1]
        emb = torch.cat(
            [rope(ids[..., i], self.axes_dim[i], self.theta) for i in range(n_axes)],
            dim=-3,
        )

        return emb.unsqueeze(1)


def timestep_embedding(t: Tensor, dim, max_period=10000, time_factor: float = 1000.0):
    """
    Create sinusoidal timestep embeddings.
    :param t: a 1-D Tensor of N indices, one per batch element.
                      These may be fractional.
    :param dim: the dimension of the output.
    :param max_period: controls the minimum frequency of the embeddings.
    :return: an (N, D) Tensor of positional embeddings.
    """
    t = time_factor * t
    half = dim // 2
    
    # Do not block CUDA steam, but having about 1e-4 differences with Flux official codes:
    # freqs = torch.exp(-math.log(max_period) * torch.arange(start=0, end=half, dtype=torch.float32, device=t.device) / half)

    # Block CUDA steam, but consistent with official codes:
    freqs = torch.exp(-math.log(max_period) * torch.arange(start=0, end=half, dtype=torch.float32) / half).to(t.device)

    args = t[:, None].float() * freqs[None]
    embedding = torch.cat([torch.cos(args), torch.sin(args)], dim=-1)
    if dim % 2:
        embedding = torch.cat([embedding, torch.zeros_like(embedding[:, :1])], dim=-1)
    if torch.is_floating_point(t):
        embedding = embedding.to(t)
    return embedding


class MLPEmbedder(nn.Module):
    def __init__(self, in_dim: int, hidden_dim: int):
        super().__init__()
        self.in_layer = nn.Linear(in_dim, hidden_dim, bias=True)
        self.silu = nn.SiLU()
        self.out_layer = nn.Linear(hidden_dim, hidden_dim, bias=True)

    def forward(self, x: Tensor) -> Tensor:
        return self.out_layer(self.silu(self.in_layer(x)))


class RMSNorm(torch.nn.Module):
    def __init__(self, dim: int):
        super().__init__()
        self.scale = nn.Parameter(torch.ones(dim))

    def forward(self, x: Tensor):
        x_dtype = x.dtype
        x = x.float()
        rrms = torch.rsqrt(torch.mean(x**2, dim=-1, keepdim=True) + 1e-6)
        return (x * rrms).to(dtype=x_dtype) * self.scale


class QKNorm(torch.nn.Module):
    def __init__(self, dim: int):
        super().__init__()
        self.query_norm = RMSNorm(dim)
        self.key_norm = RMSNorm(dim)

    def forward(self, q: Tensor, k: Tensor, v: Tensor) -> tuple[Tensor, Tensor]:
        q = self.query_norm(q)
        k = self.key_norm(k)
        return q.to(v), k.to(v)


class SelfAttention(nn.Module):
    def __init__(self, dim: int, num_heads: int = 8, qkv_bias: bool = False):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.norm = QKNorm(head_dim)
        self.proj = nn.Linear(dim, dim)

    def forward(self, x: Tensor, pe: Tensor) -> Tensor:
        qkv = self.qkv(x)
        # q, k, v = rearrange(qkv, "B L (K H D) -> K B H L D", K=3, H=self.num_heads)
        B, L, _ = qkv.shape
        qkv = qkv.view(B, L, 3, self.num_heads, -1)
        q, k, v = qkv.permute(2, 0, 3, 1, 4)
        q, k = self.norm(q, k, v)
        x = attention(q, k, v, pe=pe)
        x = self.proj(x)
        return x


@dataclass
class ModulationOut:
    shift: Tensor
    scale: Tensor
    gate: Tensor


class Modulation(nn.Module):
    def __init__(self, dim: int, double: bool):
        super().__init__()
        self.is_double = double
        self.multiplier = 6 if double else 3
        self.lin = nn.Linear(dim, self.multiplier * dim, bias=True)

    def forward(self, vec: Tensor) -> tuple[ModulationOut, ModulationOut | None]:
        out = self.lin(nn.functional.silu(vec))[:, None, :].chunk(self.multiplier, dim=-1)

        return (
            ModulationOut(*out[:3]),
            ModulationOut(*out[3:]) if self.is_double else None,
        )


class DoubleStreamBlock(nn.Module):
    def __init__(self, hidden_size: int, num_heads: int, mlp_ratio: float, qkv_bias: bool = False):
        super().__init__()

        mlp_hidden_dim = int(hidden_size * mlp_ratio)
        self.num_heads = num_heads
        self.hidden_size = hidden_size
        self.img_mod = Modulation(hidden_size, double=True)
        self.img_norm1 = nn.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6)
        self.img_attn = SelfAttention(dim=hidden_size, num_heads=num_heads, qkv_bias=qkv_bias)

        self.img_norm2 = nn.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6)
        self.img_mlp = nn.Sequential(
            nn.Linear(hidden_size, mlp_hidden_dim, bias=True),
            nn.GELU(approximate="tanh"),
            nn.Linear(mlp_hidden_dim, hidden_size, bias=True),
        )

        self.txt_mod = Modulation(hidden_size, double=True)
        self.txt_norm1 = nn.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6)
        self.txt_attn = SelfAttention(dim=hidden_size, num_heads=num_heads, qkv_bias=qkv_bias)

        self.txt_norm2 = nn.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6)
        self.txt_mlp = nn.Sequential(
            nn.Linear(hidden_size, mlp_hidden_dim, bias=True),
            nn.GELU(approximate="tanh"),
            nn.Linear(mlp_hidden_dim, hidden_size, bias=True),
        )

    def forward(self, img: Tensor, txt: Tensor, vec: Tensor, pe: Tensor) -> tuple[Tensor, Tensor]:
        img_mod1, img_mod2 = self.img_mod(vec)
        txt_mod1, txt_mod2 = self.txt_mod(vec)

        # prepare image for attention
        img_modulated = self.img_norm1(img)
        img_modulated = (1 + img_mod1.scale) * img_modulated + img_mod1.shift
        img_qkv = self.img_attn.qkv(img_modulated)
         # img_q, img_k, img_v = rearrange(img_qkv, "B L (K H D) -> K B H L D", K=3, H=self.num_heads)
        B, L, _ = img_qkv.shape
        H = self.num_heads
        D = img_qkv.shape[-1] // (3 * H)
        img_q, img_k, img_v = img_qkv.view(B, L, 3, H, D).permute(2, 0, 3, 1, 4)
        img_q, img_k = self.img_attn.norm(img_q, img_k, img_v)

        # prepare txt for attention
        txt_modulated = self.txt_norm1(txt)
        txt_modulated = (1 + txt_mod1.scale) * txt_modulated + txt_mod1.shift
        txt_qkv = self.txt_attn.qkv(txt_modulated)
        # txt_q, txt_k, txt_v = rearrange(txt_qkv, "B L (K H D) -> K B H L D", K=3, H=self.num_heads)
        B, L, _ = txt_qkv.shape
        txt_q, txt_k, txt_v = txt_qkv.view(B, L, 3, H, D).permute(2, 0, 3, 1, 4)
        txt_q, txt_k = self.txt_attn.norm(txt_q, txt_k, txt_v)

        # run actual attention
        q = torch.cat((txt_q, img_q), dim=2)
        k = torch.cat((txt_k, img_k), dim=2)
        v = torch.cat((txt_v, img_v), dim=2)

        attn = attention(q, k, v, pe=pe)
        txt_attn, img_attn = attn[:, : txt.shape[1]], attn[:, txt.shape[1] :]

        # calculate the img bloks
        img = img + img_mod1.gate * self.img_attn.proj(img_attn)
        img = img + img_mod2.gate * self.img_mlp((1 + img_mod2.scale) * self.img_norm2(img) + img_mod2.shift)

        # calculate the txt bloks
        txt = txt + txt_mod1.gate * self.txt_attn.proj(txt_attn)
        txt = txt + txt_mod2.gate * self.txt_mlp((1 + txt_mod2.scale) * self.txt_norm2(txt) + txt_mod2.shift)
        return img, txt


class SingleStreamBlock(nn.Module):
    """
    A DiT block with parallel linear layers as described in
    https://arxiv.org/abs/2302.05442 and adapted modulation interface.
    """

    def __init__(
        self,
        hidden_size: int,
        num_heads: int,
        mlp_ratio: float = 4.0,
        qk_scale: float | None = None,
    ):
        super().__init__()
        self.hidden_dim = hidden_size
        self.num_heads = num_heads
        head_dim = hidden_size // num_heads
        self.scale = qk_scale or head_dim**-0.5

        self.mlp_hidden_dim = int(hidden_size * mlp_ratio)
        # qkv and mlp_in
        self.linear1 = nn.Linear(hidden_size, hidden_size * 3 + self.mlp_hidden_dim)
        # proj and mlp_out
        self.linear2 = nn.Linear(hidden_size + self.mlp_hidden_dim, hidden_size)

        self.norm = QKNorm(head_dim)

        self.hidden_size = hidden_size
        self.pre_norm = nn.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6)

        self.mlp_act = nn.GELU(approximate="tanh")
        self.modulation = Modulation(hidden_size, double=False)

    def forward(self, x: Tensor, vec: Tensor, pe: Tensor) -> Tensor:
        mod, _ = self.modulation(vec)
        x_mod = (1 + mod.scale) * self.pre_norm(x) + mod.shift
        qkv, mlp = torch.split(self.linear1(x_mod), [3 * self.hidden_size, self.mlp_hidden_dim], dim=-1)

        # q, k, v = rearrange(qkv, "B L (K H D) -> K B H L D", K=3, H=self.num_heads)
        qkv = qkv.view(qkv.size(0), qkv.size(1), 3, self.num_heads, self.hidden_size // self.num_heads)
        q, k, v = qkv.permute(2, 0, 3, 1, 4)
        q, k = self.norm(q, k, v)

        # compute attention
        attn = attention(q, k, v, pe=pe)
        # compute activation in mlp stream, cat again and run second linear layer
        output = self.linear2(torch.cat((attn, self.mlp_act(mlp)), 2))
        return x + mod.gate * output
    

class LastLayer(nn.Module):
    def __init__(self, hidden_size: int, patch_size: int, out_channels: int):
        super().__init__()
        self.norm_final = nn.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6)
        self.linear = nn.Linear(hidden_size, patch_size * patch_size * out_channels, bias=True)
        self.adaLN_modulation = nn.Sequential(nn.SiLU(), nn.Linear(hidden_size, 2 * hidden_size, bias=True))

    def forward(self, x: Tensor, vec: Tensor) -> Tensor:
        shift, scale = self.adaLN_modulation(vec).chunk(2, dim=1)
        x = (1 + scale[:, None, :]) * self.norm_final(x) + shift[:, None, :]
        x = self.linear(x)
        return x
   
   
class FluxParams:
    in_channels: int = 64
    vec_in_dim: int = 768
    context_in_dim: int = 4096
    hidden_size: int = 3072
    mlp_ratio: float = 4.0
    num_heads: int = 24
    depth: int = 19
    depth_single_blocks: int = 38
    axes_dim: list = [16, 56, 56]
    theta: int = 10_000
    qkv_bias: bool = True
    guidance_embed: bool = True


class Flux(nn.Module):
    """
    Transformer model for flow matching on sequences.
    """

    def __init__(self, params = FluxParams()):
        super().__init__()

        self.params = params
        self.in_channels = params.in_channels
        self.out_channels = self.in_channels
        if params.hidden_size % params.num_heads != 0:
            raise ValueError(
                f"Hidden size {params.hidden_size} must be divisible by num_heads {params.num_heads}"
            )
        pe_dim = params.hidden_size // params.num_heads
        if sum(params.axes_dim) != pe_dim:
            raise ValueError(f"Got {params.axes_dim} but expected positional dim {pe_dim}")
        self.hidden_size = params.hidden_size
        self.num_heads = params.num_heads
        self.pe_embedder = EmbedND(dim=pe_dim, theta=params.theta, axes_dim=params.axes_dim)
        self.img_in = nn.Linear(self.in_channels, self.hidden_size, bias=True)
        self.time_in = MLPEmbedder(in_dim=256, hidden_dim=self.hidden_size)
        self.vector_in = MLPEmbedder(params.vec_in_dim, self.hidden_size)
        self.guidance_in = (
            MLPEmbedder(in_dim=256, hidden_dim=self.hidden_size) if params.guidance_embed else nn.Identity()
        )
        self.txt_in = nn.Linear(params.context_in_dim, self.hidden_size)

        self.double_blocks = nn.ModuleList(
            [
                DoubleStreamBlock(
                    self.hidden_size,
                    self.num_heads,
                    mlp_ratio=params.mlp_ratio,
                    qkv_bias=params.qkv_bias,
                )
                for _ in range(params.depth)
            ]
        )

        self.single_blocks = nn.ModuleList(
            [
                SingleStreamBlock(self.hidden_size, self.num_heads, mlp_ratio=params.mlp_ratio)
                for _ in range(params.depth_single_blocks)
            ]
        )

        self.final_layer = LastLayer(self.hidden_size, 1, self.out_channels)

    def forward(
        self,
        img: Tensor,
        img_ids: Tensor,
        txt: Tensor,
        txt_ids: Tensor,
        timesteps: Tensor,
        y: Tensor,
        guidance: Tensor | None = None,
    ) -> Tensor:
        if img.ndim != 3 or txt.ndim != 3:
            raise ValueError("Input img and txt tensors must have 3 dimensions.")

        # running on sequences img
        img = self.img_in(img)
        vec = self.time_in(timestep_embedding(timesteps, 256))
        if self.params.guidance_embed:
            if guidance is None:
                raise ValueError("Didn't get guidance strength for guidance distilled model.")
            vec = vec + self.guidance_in(timestep_embedding(guidance, 256))
        vec = vec + self.vector_in(y)
        txt = self.txt_in(txt)

        ids = torch.cat((txt_ids, img_ids), dim=1)
        pe = self.pe_embedder(ids)

        for block in self.double_blocks:
            img, txt = block(img=img, txt=txt, vec=vec, pe=pe)

        img = torch.cat((txt, img), 1)
        for block in self.single_blocks:
            img = block(img, vec=vec, pe=pe)
        img = img[:, txt.shape[1] :, ...]

        img = self.final_layer(img, vec)  # (N, T, patch_size ** 2 * out_channels)
        return img


def prepare(t5: HFEmbedder, clip: HFEmbedder, img: Tensor, prompt: str | list[str]) -> dict[str, Tensor]:
    bs, c, h, w = img.shape
    if bs == 1 and not isinstance(prompt, str):
        bs = len(prompt)

    img = rearrange(img, "b c (h ph) (w pw) -> b (h w) (c ph pw)", ph=2, pw=2)
    if img.shape[0] == 1 and bs > 1:
        img = repeat(img, "1 ... -> bs ...", bs=bs)

    img_ids = torch.zeros(h // 2, w // 2, 3)
    img_ids[..., 1] = img_ids[..., 1] + torch.arange(h // 2)[:, None]
    img_ids[..., 2] = img_ids[..., 2] + torch.arange(w // 2)[None, :]
    img_ids = repeat(img_ids, "h w c -> b (h w) c", b=bs)

    if isinstance(prompt, str):
        prompt = [prompt]
    txt = t5(prompt)
    if txt.shape[0] == 1 and bs > 1:
        txt = repeat(txt, "1 ... -> bs ...", bs=bs)
    txt_ids = torch.zeros(bs, txt.shape[1], 3)

    vec = clip(prompt)
    if vec.shape[0] == 1 and bs > 1:
        vec = repeat(vec, "1 ... -> bs ...", bs=bs)

    return {
        "img": img,
        "img_ids": img_ids.to(img.device),
        "txt": txt.to(img.device),
        "txt_ids": txt_ids.to(img.device),
        "vec": vec.to(img.device),
    }


def time_shift(mu: float, sigma: float, t: Tensor):
    return math.exp(mu) / (math.exp(mu) + (1 / t - 1) ** sigma)


def get_lin_function(
    x1: float = 256, y1: float = 0.5, x2: float = 4096, y2: float = 1.15
) -> Callable[[float], float]:
    m = (y2 - y1) / (x2 - x1)
    b = y1 - m * x1
    return lambda x: m * x + b


def get_schedule(
    num_steps: int,
    image_seq_len: int,
    base_shift: float = 0.5,
    max_shift: float = 1.15,
    shift: bool = True,
) -> list[float]:
    # extra step for zero
    timesteps = torch.linspace(1, 0, num_steps + 1)

    # shifting the schedule to favor high timesteps for higher signal images
    if shift:
        # eastimate mu based on linear estimation between two points
        mu = get_lin_function(y1=base_shift, y2=max_shift)(image_seq_len)
        timesteps = time_shift(mu, 1.0, timesteps)

    return timesteps.tolist()


def denoise(
    model: Flux,
    # model input
    img: Tensor,
    img_ids: Tensor,
    txt: Tensor,
    txt_ids: Tensor,
    vec: Tensor,
    # sampling parameters
    timesteps: list[float],
    guidance: float = 4.0,
):
    # this is ignored for schnell
    guidance_vec = torch.full((img.shape[0],), guidance, device=img.device, dtype=img.dtype)
    for t_curr, t_prev in tqdm(zip(timesteps[:-1], timesteps[1:]), total=len(timesteps) - 1):
        t_vec = torch.full((img.shape[0],), t_curr, dtype=img.dtype, device=img.device)
        pred = model(
            img=img,
            img_ids=img_ids,
            txt=txt,
            txt_ids=txt_ids,
            y=vec,
            timesteps=t_vec,
            guidance=guidance_vec,
        )
        img = img + (t_prev - t_curr) * pred
    return img


def unpack(x: Tensor, height: int, width: int) -> Tensor:
    return rearrange(
        x,
        "b (h w) (c ph pw) -> b c (h ph) (w pw)",
        h=math.ceil(height / 16),
        w=math.ceil(width / 16),
        ph=2,
        pw=2,
    )

@dataclass
class SamplingOptions:
    prompt: str
    width: int
    height: int
    guidance: float
    seed: int | None
    

def get_image(image) -> torch.Tensor | None:
    if image is None:
        return None
    image = Image.fromarray(image).convert("RGB")

    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Lambda(lambda x: 2.0 * x - 1.0),
    ])
    img: torch.Tensor = transform(image)
    return img[None, ...]


# ---------------- Demo ----------------


from huggingface_hub import hf_hub_download
from safetensors.torch import load_file

sd = load_file(hf_hub_download(repo_id="lllyasviel/flux1-dev-bnb-nf4", filename="flux1-dev-bnb-nf4-v2.safetensors"))
sd = {k.replace("model.diffusion_model.", ""): v for k, v in sd.items() if "model.diffusion_model" in k}
model = Flux().to(dtype=torch.bfloat16, device="cuda")
result = model.load_state_dict(sd)
model_zero_init = False

# model = Flux().to(dtype=torch.bfloat16, device="cuda")
# result = model.load_state_dict(load_file("/storage/dev/nyanko/flux-dev/flux1-dev.sft"))

# @spaces.GPU
@torch.no_grad()
def generate_image(
    prompt, width, height, guidance, inference_steps, seed,
    do_img2img, init_image, image2image_strength, resize_img,
    progress=gr.Progress(track_tqdm=True),
):
    if seed == 0:
        seed = int(random.random() * 1000000)
        
    device = "cuda" if torch.cuda.is_available() else "cpu"
    torch_device = torch.device(device)
    
    global model, model_zero_init
    if not model_zero_init:
        model = model.to(torch_device)
        model_zero_init = True
    
    if do_img2img and init_image is not None:
        init_image = get_image(init_image)
        if resize_img:
            init_image = torch.nn.functional.interpolate(init_image, (height, width))
        else:
            h, w = init_image.shape[-2:]
            init_image = init_image[..., : 16 * (h // 16), : 16 * (w // 16)]
            height = init_image.shape[-2]
            width = init_image.shape[-1]
        init_image = ae.encode(init_image.to(torch_device).to(torch.bfloat16)).latent_dist.sample()
        init_image =  (init_image - ae.config.shift_factor) * ae.config.scaling_factor

    generator = torch.Generator(device=device).manual_seed(seed)
    x = torch.randn(1, 16, 2 * math.ceil(height / 16), 2 * math.ceil(width / 16), device=device, dtype=torch.bfloat16, generator=generator) 
    
    num_steps = inference_steps
    timesteps = get_schedule(num_steps, (x.shape[-1] * x.shape[-2]) // 4, shift=True)

    if do_img2img and init_image is not None:
        t_idx = int((1 - image2image_strength) * num_steps)
        t = timesteps[t_idx]
        timesteps = timesteps[t_idx:]
        x = t * x + (1.0 - t) * init_image.to(x.dtype)

    inp = prepare(t5=t5, clip=clip, img=x, prompt=prompt)
    x = denoise(model, **inp, timesteps=timesteps, guidance=guidance)
    
    # with profile(activities=[ProfilerActivity.CPU],record_shapes=True,profile_memory=True) as prof:
    # print(prof.key_averages().table(sort_by="cpu_time_total", row_limit=20))

    x = unpack(x.float(), height, width)
    with torch.autocast(device_type=torch_device.type, dtype=torch.bfloat16):
        x = x = (x / ae.config.scaling_factor) + ae.config.shift_factor 
        x = ae.decode(x).sample

    x = x.clamp(-1, 1)
    x = rearrange(x[0], "c h w -> h w c")
    img = Image.fromarray((127.5 * (x + 1.0)).cpu().byte().numpy())
    
    return img, seed

def create_demo():
    with gr.Blocks(theme="bethecloud/storj_theme") as demo:
        gr.HTML(
            """
                <div style="text-align: center; margin: 0 auto;">
                <div
                    style="
                    display: inline-flex;
                    align-items: center;
                    gap: 0.8rem;
                    font-size: 1.75rem;
                    "
                >
                    <svg
                    width="0.65em"
                    height="0.65em"
                    viewBox="0 0 115 115"
                    fill="none"
                    xmlns="http://www.w3.org/2000/svg"
                    >
                    <rect width="23" height="23" fill="white"></rect>
                    <rect y="69" width="23" height="23" fill="white"></rect>
                    <rect x="23" width="23" height="23" fill="#AEAEAE"></rect>
                    <rect x="23" y="69" width="23" height="23" fill="#AEAEAE"></rect>
                    <rect x="46" width="23" height="23" fill="white"></rect>
                    <rect x="46" y="69" width="23" height="23" fill="white"></rect>
                    <rect x="69" width="23" height="23" fill="black"></rect>
                    <rect x="69" y="69" width="23" height="23" fill="black"></rect>
                    <rect x="92" width="23" height="23" fill="#D9D9D9"></rect>
                    <rect x="92" y="69" width="23" height="23" fill="#AEAEAE"></rect>
                    <rect x="115" y="46" width="23" height="23" fill="white"></rect>
                    <rect x="115" y="115" width="23" height="23" fill="white"></rect>
                    <rect x="115" y="69" width="23" height="23" fill="#D9D9D9"></rect>
                    <rect x="92" y="46" width="23" height="23" fill="#AEAEAE"></rect>
                    <rect x="92" y="115" width="23" height="23" fill="#AEAEAE"></rect>
                    <rect x="92" y="69" width="23" height="23" fill="white"></rect>
                    <rect x="69" y="46" width="23" height="23" fill="white"></rect>
                    <rect x="69" y="115" width="23" height="23" fill="white"></rect>
                    <rect x="69" y="69" width="23" height="23" fill="#D9D9D9"></rect>
                    <rect x="46" y="46" width="23" height="23" fill="black"></rect>
                    <rect x="46" y="115" width="23" height="23" fill="black"></rect>
                    <rect x="46" y="69" width="23" height="23" fill="black"></rect>
                    <rect x="23" y="46" width="23" height="23" fill="#D9D9D9"></rect>
                    <rect x="23" y="115" width="23" height="23" fill="#AEAEAE"></rect>
                    <rect x="23" y="69" width="23" height="23" fill="black"></rect>
                    </svg>
                    <h1 style="font-weight: 900; margin-bottom: 7px;margin-top:5px">
                    FLUX.1 dev NF4 Quantized Demo
                    </h1>
                </div>
                <p style="margin-bottom: 20px; font-size: 94%; line-height: 23px;">
                    12B param rectified flow transformer guidance-distilled from <a href="https://blackforestlabs.ai/">FLUX.1 [pro]</a>
                    <a href="https://huggingface.co/black-forest-labs/FLUX.1-dev/blob/main/LICENSE.md">[non-commercial license]</a> <a href="https://blackforestlabs.ai/announcing-black-forest-labs/">[blog]</a> <a href="https://huggingface.co/black-forest-labs/FLUX.1-dev">[model]</a>
                </p>
                </div>
            """
        )
        with gr.Row():
            with gr.Column():
                prompt = gr.Textbox(label="Prompt", value="a picture of a datacenter room, multiple servers with glowing leds are visible. An engineer is working on one of the server. The word 'SCALEWAY' is painted over it in big, dark purple paintbrush strokes with visible texture")
                
                width = gr.Slider(minimum=128, maximum=2048, step=64, label="Width", value=1360)
                height = gr.Slider(minimum=128, maximum=2048, step=64, label="Height", value=768)
                guidance = gr.Slider(minimum=1.0, maximum=5.0, step=0.1, label="Guidance", value=3.5)
                inference_steps = gr.Slider(
                    label="Inference steps",
                    minimum=1,
                    maximum=30,
                    step=1,
                    value=16,
                )
                seed = gr.Number(label="Seed", precision=-1)
                do_img2img = gr.Checkbox(label="Image to Image", value=False)
                init_image = gr.Image(label="Input Image", visible=False)
                image2image_strength = gr.Slider(minimum=0.0, maximum=1.0, step=0.01, label="Noising strength", value=0.8, visible=False)
                resize_img = gr.Checkbox(label="Resize image", value=True, visible=False)
                generate_button = gr.Button("Generate")
            
            with gr.Column():
                output_image = gr.Image(label="Generated Image")
                output_seed = gr.Text(label="Used Seed")

        do_img2img.change(
            fn=lambda x: [gr.update(visible=x), gr.update(visible=x), gr.update(visible=x)],
            inputs=[do_img2img],
            outputs=[init_image, image2image_strength, resize_img]
        )

        generate_button.click(
            fn=generate_image,
            inputs=[prompt, width, height, guidance, inference_steps, seed, do_img2img, init_image, image2image_strength, resize_img],
            outputs=[output_image, output_seed]
        )
        
        examples = [
            "a tiny astronaut hatching from an egg on the moon",
            "a cat holding a sign that says hello world",
            "an anime illustration of an unicorn",
        ]

    return demo

if __name__ == "__main__":
    demo = create_demo()
    demo.launch(server_name="0.0.0.0", server_port=8000, share=False)
