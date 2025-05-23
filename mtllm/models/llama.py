import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import Embedding, Linear

from fairseq.data.data_utils import lengths_to_padding_mask, lengths_to_mask
from fairseq.models import (
    FairseqEncoder,
    FairseqDecoder,
    FairseqEncoderDecoderModel,
    register_model,
    register_model_architecture,
)

from torch.distributed.fsdp.wrap import _or_policy, transformer_auto_wrap_policy
import torch.utils.checkpoint as cp
from torch.distributed.fsdp.fully_sharded_data_parallel import CPUOffload
from torch.distributed.fsdp import (
    FullyShardedDataParallel as FSDP,
    MixedPrecision
)

from typing import Optional, Tuple, List, Dict
from dataclasses import dataclass
from sentencepiece import SentencePieceProcessor
from torch import Tensor

import math
import json
import os
import numpy as np
import functools
import time

try:
    from xformers.ops import memory_efficient_attention, LowerTriangularMask, MemoryEfficientAttentionCutlassOp
except ModuleNotFoundError:
    print ("xformers.ops ModuleNotFoundError")
    memory_efficient_attention, LowerTriangularMask, MemoryEfficientAttentionCutlassOp = None, None, None

from functools import partial

from torch.distributed.algorithms._checkpoint.checkpoint_wrapper import (
    checkpoint_wrapper,
    CheckpointImpl,
    apply_activation_checkpointing,
)

non_reentrant_wrapper = partial(
    checkpoint_wrapper,
    checkpoint_impl=CheckpointImpl.NO_REENTRANT,
)

check_fn = lambda submodule: isinstance(submodule, TransformerBlock)


def apply_fsdp_checkpointing(model):
    """apply activation checkpointing to model
    returns None as model is updated directly
    """
    print(f"--> applying fsdp activation checkpointing...")

    apply_activation_checkpointing(
        model, checkpoint_wrapper_fn=non_reentrant_wrapper, check_fn=check_fn
    )

def get_llama_wrapper():
    """we register our main layer class and use the fsdp transformer wrapping policy
    ensures embedding layers are in the root fsdp unit for shared access and that fsdp units map to transformer layers
    """

    transformer_wrap_policy = functools.partial(
        transformer_auto_wrap_policy,
        transformer_layer_cls={
            FeedForward,
        },
    )
    auto_wrap_policy = transformer_wrap_policy
    # auto_wrap_policy = functools.partial(_or_policy, policies=[lambda_policy, transformer_wrap_policy])
    return auto_wrap_policy

class LLaMADecoder(FairseqDecoder):

    # TODO: change it to incremental decoder!!

    def __init__( self, dictionary, use_lora, lora_r, lora_alpha, enable_fsdp, use_xformers):
        super().__init__(dictionary)
        model_args = ModelArgs(use_lora=use_lora, lora_r=lora_r, lora_alpha=lora_alpha,
                               use_xformers=use_xformers, enable_fsdp=enable_fsdp)
        self.model_llama = LLAMA(model_args)
        # self.model_llama.load_state_dict(checkpoint, strict=False)
        apply_fsdp_checkpointing(self.model_llama)

    def forward(self, prev_output_tokens, audio_out, left_prompts=None):
        return self.model_llama(prev_output_tokens, audio_out, left_prompts), None

    def forward_generate(self, prev_output_tokens, start_pos, audio_out, left_prompts=None, incremental_state=None):
        return self.model_llama.forward_generate(prev_output_tokens, start_pos, audio_out, left_prompts=left_prompts, incremental_state=incremental_state), None

    def reorder_incremental_state_scripting(
        self,
        incremental_state: Dict[str, Dict[str, Optional[Tensor]]],
        new_order: Tensor,
    ):
        return self.model_llama.reorder_incremental_state_scripting(incremental_state, new_order)


# LLAMA model
@dataclass
class ModelArgs:
    dim: int = 4096
    n_layers: int = 32
    n_heads: int = 32
    vocab_size: int = 32000  # defined later by tokenizer
    multiple_of: int = 256  # make SwiGLU hidden layer size multiple of large power of 2
    norm_eps: float = 1e-6
    # max_batch_size: int = 32
    max_seq_len: int = 1024

    gradient_checkpointing: bool = False

    use_lora: bool = True
    lora_r: int = 8
    lora_alpha: int = 32
    lora_dropout: float = 0.1

    flash_attention: bool = False
    use_xformers: bool = False

    enable_fsdp: bool = False

class RMSNorm(torch.nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        output = self._norm(x.float()).type_as(x)
        return output * self.weight


def precompute_freqs_cis(dim: int, end: int, theta: float = 10000.0):
    freqs = 1.0 / (theta ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim))
    t = torch.arange(end, device=freqs.device)  # type: ignore
    freqs = torch.outer(t, freqs).float()  # type: ignore
    freqs_cis = torch.polar(torch.ones_like(freqs), freqs)  # complex64
    return freqs_cis


def reshape_for_broadcast(freqs_cis: torch.Tensor, x: torch.Tensor):
    ndim = x.ndim
    assert 0 <= 1 < ndim
    assert freqs_cis.shape == (x.shape[1], x.shape[-1])
    shape = [d if i == 1 or i == ndim - 1 else 1 for i, d in enumerate(x.shape)]
    return freqs_cis.view(*shape)


def apply_rotary_emb(
    xq: torch.Tensor,
    xk: torch.Tensor,
    freqs_cis: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    xq_ = torch.view_as_complex(xq.float().reshape(*xq.shape[:-1], -1, 2))
    xk_ = torch.view_as_complex(xk.float().reshape(*xk.shape[:-1], -1, 2))
    freqs_cis = reshape_for_broadcast(freqs_cis, xq_)
    xq_out = torch.view_as_real(xq_ * freqs_cis).flatten(3)
    xk_out = torch.view_as_real(xk_ * freqs_cis).flatten(3)
    return xq_out.type_as(xq), xk_out.type_as(xk)


class Attention_LoRA(nn.Module):
    def __init__(self, args):
        super().__init__()

        self.n_local_heads = args.n_heads
        self.head_dim = args.dim // args.n_heads

        self.wq = Linear(
            args.dim,
            args.n_heads * self.head_dim,
            bias=False
        )
        self.wk = Linear(
            args.dim,
            args.n_heads * self.head_dim,
            bias=False
        )
        self.wv = Linear(
            args.dim,
            args.n_heads * self.head_dim,
            bias=False
        )
        self.wo = Linear(
            args.n_heads * self.head_dim,
            args.dim,
            bias=False
        )

        self.wq_lora_A = nn.Parameter(self.wq.weight.new_zeros((args.dim, args.lora_r)))
        self.wq_lora_B = nn.Parameter(self.wq.weight.new_zeros((args.lora_r, args.dim)))
        self.wk_lora_A = nn.Parameter(self.wk.weight.new_zeros((args.dim, args.lora_r)))
        self.wk_lora_B = nn.Parameter(self.wk.weight.new_zeros((args.lora_r, args.dim)))
        self.wv_lora_A = nn.Parameter(self.wv.weight.new_zeros((args.dim, args.lora_r)))
        self.wv_lora_B = nn.Parameter(self.wv.weight.new_zeros((args.lora_r, args.dim)))
        self.wo_lora_A = nn.Parameter(self.wo.weight.new_zeros((args.dim, args.lora_r)))
        self.wo_lora_B = nn.Parameter(self.wo.weight.new_zeros((args.lora_r, args.dim)))
        self.scaling = args.lora_alpha / args.lora_r

        self.reset_parameters()

        if args.lora_dropout > 0.:
            self.lora_dropout = nn.Dropout(p=args.lora_dropout)
        else:
            self.lora_dropout = lambda x: x

        # TODO use incremental states
        # self.cache_k = torch.zeros(
        #     (args.max_batch_size, args.max_seq_len, self.n_local_heads, self.head_dim)
        # ).cuda()
        # self.cache_v = torch.zeros(
        #     (args.max_batch_size, args.max_seq_len, self.n_local_heads, self.head_dim)
        # ).cuda()

    def reset_parameters(self):
        nn.init.kaiming_uniform_(self.wq_lora_A, a=math.sqrt(5))
        nn.init.kaiming_uniform_(self.wk_lora_A, a=math.sqrt(5))
        nn.init.kaiming_uniform_(self.wv_lora_A, a=math.sqrt(5))
        nn.init.kaiming_uniform_(self.wo_lora_A, a=math.sqrt(5))
        nn.init.zeros_(self.wq_lora_B)
        nn.init.zeros_(self.wk_lora_B)
        nn.init.zeros_(self.wv_lora_B)
        nn.init.zeros_(self.wo_lora_B)

    def _checkpointed_forward(self, x):
        xq = self.wq(x) + (self.lora_dropout(x) @ self.wq_lora_A @ self.wq_lora_B) * self.scaling
        xk = self.wk(x) + (self.lora_dropout(x) @ self.wk_lora_A @ self.wk_lora_B) * self.scaling
        xv = self.wv(x) + (self.lora_dropout(x) @ self.wv_lora_A @ self.wv_lora_B) * self.scaling
        return xq, xk, xv
    
    def forward(self, x: torch.Tensor, start_pos: int, freqs_cis: torch.Tensor, mask: Optional[torch.Tensor], 
                incremental_state=None, gradient_checkpointing=False, layer_id=None):

        bsz, seqlen, _ = x.shape

        xq = self.wq(x) + (self.lora_dropout(x) @ self.wq_lora_A @ self.wq_lora_B) * self.scaling
        xk = self.wk(x) + (self.lora_dropout(x) @ self.wk_lora_A @ self.wk_lora_B) * self.scaling
        xv = self.wv(x) + (self.lora_dropout(x) @ self.wv_lora_A @ self.wv_lora_B) * self.scaling

        xq = xq.view(bsz, seqlen, self.n_local_heads, self.head_dim)
        xk = xk.view(bsz, seqlen, self.n_local_heads, self.head_dim)
        xv = xv.view(bsz, seqlen, self.n_local_heads, self.head_dim)

        xq, xk = apply_rotary_emb(xq, xk, freqs_cis=freqs_cis)

        if incremental_state is not None:
            if "prev_key" in incremental_state:
                prev_key = incremental_state["prev_key"].view(
                    bsz, -1, self.n_local_heads, self.head_dim
                )
                prev_value = incremental_state["prev_value"].view(
                    bsz, -1, self.n_local_heads, self.head_dim
                )

                xk = torch.cat([prev_key, xk], dim=1)
                xv = torch.cat([prev_value, xv], dim=1)
                #print ("test1")

            incremental_state["prev_key"] = xk.view(
                bsz, -1, self.n_local_heads, self.head_dim
            )
            incremental_state["prev_value"] = xv.view(
                bsz, -1, self.n_local_heads, self.head_dim
            )
            #src_len = k.size(1)
 
               
        keys = xk
        values = xv

        xq = xq.transpose(1, 2)
        keys = keys.transpose(1, 2)
        values = values.transpose(1, 2)

        scores = torch.matmul(xq, keys.transpose(2, 3)) / math.sqrt(self.head_dim)
        # print("scores: ", scores.shape)
        if mask is not None:
            scores = scores + mask  # (bs, n_local_heads, slen, cache_len + slen)

        scores = F.softmax(scores.float(), dim=-1).type_as(xq)
        output = torch.matmul(scores, values)  # (bs, n_local_heads, slen, head_dim)
        output = output.transpose(
            1, 2
        ).contiguous().view(bsz, seqlen, -1)

        return self.wo(output) + (self.lora_dropout(output) @ self.wo_lora_A @ self.wo_lora_B) * self.scaling


class Attention(nn.Module):
    def __init__(self, args: ModelArgs):
        super().__init__()

        self.n_local_heads = args.n_heads
        self.head_dim = args.dim // args.n_heads

        self.wq = Linear(
            args.dim,
            args.n_heads * self.head_dim,
            bias=False
        )
        self.wk = Linear(
            args.dim,
            args.n_heads * self.head_dim,
            bias=False
        )
        self.wv = Linear(
            args.dim,
            args.n_heads * self.head_dim,
            bias=False
        )
        self.wo = Linear(
            args.n_heads * self.head_dim,
            args.dim,
            bias=False
        )

        # TODO use incremental states
        # self.cache_k = torch.zeros(
        #     (args.max_batch_size, args.max_seq_len, self.n_local_heads, self.head_dim)
        # ).cuda()
        # self.cache_v = torch.zeros(
        #     (args.max_batch_size, args.max_seq_len, self.n_local_heads, self.head_dim)
        # ).cuda()

        self.flash_attention = args.flash_attention

    def _checkpointed_forward(self, x):
        return self.wq(x), self.wk(x), self.wv(x)

    def forward(self, x: torch.Tensor, start_pos: int, freqs_cis: torch.Tensor, 
                mask: Optional[torch.Tensor], incremental_state=None, gradient_checkpointing=False):

        bsz, seqlen, _ = x.shape
        if gradient_checkpointing and self.training:
            xq, xk, xv = cp.checkpoint(self._checkpointed_forward, x)
        else:
            xq, xk, xv = self.wq(x), self.wk(x), self.wv(x)

        xq = xq.view(bsz, seqlen, self.n_local_heads, self.head_dim)
        xk = xk.view(bsz, seqlen, self.n_local_heads, self.head_dim)
        xv = xv.view(bsz, seqlen, self.n_local_heads, self.head_dim)

        xq, xk = apply_rotary_emb(xq, xk, freqs_cis=freqs_cis)

        if incremental_state is not None:
            if "prev_key" in incremental_state:
                prev_key = incremental_state["prev_key"].view(
                    bsz, -1, self.n_local_heads, self.head_dim
                )
                prev_value = incremental_state["prev_value"].view(
                    bsz, -1, self.n_local_heads, self.head_dim
                )
                xk = torch.cat([prev_key, xk], dim=1)
                xv = torch.cat([prev_value, xv], dim=1)
                #print ("test1")
            incremental_state["prev_key"] = xk.view(
                bsz, -1, self.n_local_heads, self.head_dim
            )
            incremental_state["prev_value"] = xv.view(
                bsz, -1, self.n_local_heads, self.head_dim
            )
            #src_len = k.size(1)
 
        if self.flash_attention:
            # attn_bias = LowerTriangularMask()
            attn_bias = mask
            attn = memory_efficient_attention(xq, xk, xv, attn_bias, op=MemoryEfficientAttentionCutlassOp)  # B M H K
            attn = attn.contiguous().view(bsz, seqlen, -1)
            return self.wo(attn)
        else: 
            keys = xk
            values = xv

            xq = xq.transpose(1, 2)
            keys = keys.transpose(1, 2)
            values = values.transpose(1, 2)
            #print ("xq: ", xq.shape)
            #print ("keys: ", keys.shape)
            scores = torch.matmul(xq, keys.transpose(2, 3)) / math.sqrt(self.head_dim)

            if mask is not None:
                scores = scores + mask  # (bs, n_local_heads, slen, cache_len + slen)

            scores = F.softmax(scores.float(), dim=-1).type_as(xq)
            output = torch.matmul(scores, values)  # (bs, n_local_heads, slen, head_dim)
            output = output.transpose(
                1, 2
            ).contiguous().view(bsz, seqlen, -1)

            return self.wo(output)


class FeedForward(nn.Module):
    def __init__(
        self,
        dim: int,
        hidden_dim: int,
        multiple_of: int,
    ):
        super().__init__()
        hidden_dim = int(2 * hidden_dim / 3)
        hidden_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)

        self.w1 = Linear(
            dim, hidden_dim, bias=False
        )
        self.w2 = Linear(
            hidden_dim, dim, bias=False
        )
        self.w3 = Linear(
            dim, hidden_dim, bias=False
        )

    def forward(self, x, gradient_checkpointing):
        if gradient_checkpointing and self.training:
            output = cp.checkpoint(self._checkpointed_forward, x)
        else:
            output = self.w2(F.silu(self.w1(x)) * self.w3(x))
        return output

    def _checkpointed_forward(self, x):
        return self.w2(F.silu(self.w1(x)) * self.w3(x))


class TransformerBlock(nn.Module):
    def __init__(self, layer_id: int, args: ModelArgs):
        super().__init__()
        self.n_heads = args.n_heads
        self.dim = args.dim
        self.head_dim = args.dim // args.n_heads
        self.ffn_gradient_checkpointing = args.gradient_checkpointing
        if args.use_lora:
            self.attention = Attention_LoRA(args)
        else:
            self.attention = Attention(args)
        
        self.feed_forward = FeedForward(
            dim=args.dim, hidden_dim=4 * args.dim, multiple_of=args.multiple_of
        )
        self.layer_id = layer_id
        self.attention_norm = RMSNorm(args.dim, eps=args.norm_eps)
        self.ffn_norm = RMSNorm(args.dim, eps=args.norm_eps)

        wrapping_policy = get_llama_wrapper()
        fpSixteen = MixedPrecision(
            param_dtype=torch.float16,
            reduce_dtype=torch.float16,
            buffer_dtype=torch.float16
        )
        if args.enable_fsdp:
            self.feed_forward = FSDP(
                self.feed_forward,
                auto_wrap_policy = wrapping_policy,
                device_id=torch.cuda.current_device(),
                limit_all_gathers=True,
                mixed_precision=fpSixteen,
                # cpu_offload=CPUOffload(offload_params=True),
            )

    def forward(self, x: torch.Tensor, start_pos: int, freqs_cis: torch.Tensor, mask: Optional[torch.Tensor], incremental_state=None,):
        h = x + self.attention.forward(self.attention_norm(x), start_pos, freqs_cis, mask, incremental_state, layer_id=self.layer_id)
        ffn_output = self.feed_forward.forward(self.ffn_norm(h), self.ffn_gradient_checkpointing)
        out = h + ffn_output
        return out


class LLAMA(nn.Module):
    def __init__(self, params: ModelArgs):
        super().__init__()
        self.params = params
        self.vocab_size = params.vocab_size
        self.n_layers = params.n_layers
    
        self.tok_embeddings = Embedding(
            params.vocab_size, params.dim
        )

        self.layers = torch.nn.ModuleList()
        for layer_id in range(params.n_layers):
            self.layers.append(TransformerBlock(layer_id, params))

        self.norm = RMSNorm(params.dim, eps=params.norm_eps)
        self.output = Linear(
            params.dim, params.vocab_size, bias=False
        )

        self.freqs_cis = precompute_freqs_cis(
            self.params.dim // self.params.n_heads, self.params.max_seq_len * 2
        )

    def freeze_module(self, module):
        for param in module.parameters():
            param.requires_grad = False

    def get_text_embedding(self, text):
        return self.tok_embeddings(text)

    def forward_generate(self, prev_output_tokens, start_pos, audio_out, left_prompts=None, incremental_state=None):
        with torch.no_grad():
            if audio_out is not None:

                if start_pos == 0:
                    if left_prompts is not None:
                        left_h = self.tok_embeddings(left_prompts)
                    h = self.tok_embeddings(prev_output_tokens)
                    if left_prompts is not None:
                        h = torch.cat((left_h, audio_out["encoder_out"], h), dim=1)
                    else:
                        h = torch.cat((audio_out["encoder_out"], h), dim=1)
                else:
                    prev_output_tokens = prev_output_tokens[:, -1:]
                    if left_prompts is not None:
                        start_pos = start_pos + left_prompts.shape[1] + audio_out["encoder_out"].shape[1]
                    else:
                        start_pos = start_pos + audio_out["encoder_out"].shape[1]
                    h = self.tok_embeddings(prev_output_tokens)
            else:
                if start_pos == 0:
                    h = self.tok_embeddings(prev_output_tokens)
                else:
                    prev_output_tokens = prev_output_tokens[:, -1:]
                    h = self.tok_embeddings(prev_output_tokens)

            _bsz, seqlen, _ = h.shape
            freqs_cis = self.freqs_cis.to(h.device)
            freqs_cis = freqs_cis[start_pos : start_pos + seqlen]
            mask = None
            if seqlen > 1:
                mask = torch.full((1, 1, seqlen, seqlen), float("-inf"), device=h.device)
                mask = torch.triu(mask, diagonal=start_pos + 1).type_as(h)

            #start_pos = 0
            for i in range(self.n_layers):
                if i not in incremental_state:
                    incremental_state[i] = {}
                h = self.layers[i](h, start_pos, freqs_cis, mask, incremental_state[i])

        h = self.norm(h)
        
        out = self.output(h)
        return out

    def reorder_incremental_state_scripting(
        self,
        incremental_state: Dict[str, Dict[str, Optional[Tensor]]],
        new_order: Tensor,
    ):
        for key in incremental_state:
            for param_name in incremental_state[key]:
                if incremental_state[key][param_name] is not None:
                    incremental_state[key][param_name] = incremental_state[key][param_name].index_select(0, new_order)


class Tokenizer:
    def __init__(self, model_path: str):
        # reload tokenizer
        assert os.path.isfile(model_path), model_path
        self.sp_model = SentencePieceProcessor(model_file=model_path)

        # BOS / EOS token IDs
        self.n_words: int = self.sp_model.vocab_size()
        self.bos_id: int = self.sp_model.bos_id()
        self.eos_id: int = self.sp_model.eos_id()
        self.pad_id: int = self.sp_model.pad_id()

        print(self.n_words)
        print(self.bos_id)
        print(self.eos_id)
        print(self.pad_id)
        print(self.sp_model.unk_id())

        assert self.sp_model.vocab_size() == self.sp_model.get_piece_size()

    def encode(self, s: str, bos: bool, eos: bool) -> List[int]:
        assert type(s) is str
        t = self.sp_model.encode(s)
        if bos:
            t = [self.bos_id] + t
        if eos:
            t = t + [self.eos_id]
        return t

    def decode(self, t: List[int]) -> str:
        return self.sp_model.decode(t)





