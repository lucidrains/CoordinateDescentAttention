import torch
import torch.nn.functional as F
from torch import nn, einsum

from einops import rearrange, repeat

# helpers

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

# tensor helpers

def log(t, eps = 1e-20):
    return torch.log(t.clamp(min = eps))

# coordinate descent helpers

def coor_descent(
    s,
    *,
    n_iters,
    k,
    eps = 1e-1,
    clamp_fn = F.relu,
    mask = None,
):
    mask_value = -torch.finfo(s.dtype).max
    constant = eps * log(k)

    b = -clamp_fn(s)

    for _ in range(n_iters):
        if exists(mask):
            s = s.masked_fill(~mask, mask_value)

        a = constant - eps * ((s + b) / eps).logsumexp(dim = -1, keepdim = True)
        b = -clamp_fn(s + a)

    if exists(mask):
        s = s.masked_fill(~mask, mask_value)

    scores = ((s + a + b) / eps).exp()
    return scores

# classes

def FeedForward(dim, mult = 4):
    dim_hidden = int(dim * mult)

    return nn.Sequential(
        nn.LayerNorm(dim),
        nn.Linear(dim, dim_hidden),
        nn.GELU(),
        nn.Linear(dim_hidden, dim)
    )

class Attention(nn.Module):
    def __init__(
        self,
        dim,
        dim_head = 64,
        heads = 8,
        use_coor_descent = False,
        coor_descent_iters = 50,
        coor_descent_sparsity_k = 1,
        coor_descent_eps = 1e-1,
        attn_null_kv = 0
    ):
        super().__init__()
        self.scale = dim_head ** -0.5
        self.heads = heads
        dim_inner = dim_head * heads

        self.use_coor_descent = use_coor_descent
        self.coor_descent_iters = coor_descent_iters
        self.coor_descent_sparsity_k = coor_descent_sparsity_k
        self.coor_descent_eps = coor_descent_eps

        self.norm = nn.LayerNorm(dim)

        self.null_kv = nn.Parameter(torch.randn(2, heads, attn_null_kv, dim_head))

        self.to_qkv = nn.Linear(dim, dim_inner * 3, bias = False)
        self.to_out = nn.Linear(dim_inner, dim, bias = False)

    def forward(self, x):
        b, n, h, device, dtype = *x.shape[:2], self.heads, x.device, x.dtype
        x = self.norm(x)

        # get queries, keys, values, and split heads

        q, k, v = self.to_qkv(x).chunk(3, dim = -1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = h), (q, k, v))

        # add null key value if needed

        if self.null_kv.numel() > 0:
            nk, nv = map(lambda t: repeat(t, 'h n d -> b h n d', b = b), self.null_kv)
            k = torch.cat((nk, k), dim = -2)
            v = torch.cat((nv, v), dim = -2)

        # measure similarity

        q = q * self.scale
        sim = einsum('b h i d, b h j d -> b h i j', q, k)

        i, j = sim.shape[-2:]
        causal_mask = torch.ones((i, j), device = device, dtype = torch.bool).triu(j - i + 1)

        # whether to use coordinate descent or not

        if self.use_coor_descent:
            sparsity_k = torch.ones(i, device = device, dtype = dtype) * self.coor_descent_sparsity_k
            sparsity_k = rearrange(sparsity_k, 'i -> i 1')

            attn = coor_descent(
                sim,
                n_iters = self.coor_descent_iters,
                k = sparsity_k,
                eps = self.coor_descent_eps,
                mask = ~causal_mask
            )
        else:
            sim = sim.masked_fill(causal_mask, -torch.finfo(sim.dtype).max)
            attn = sim.softmax(dim = -1)

        # aggregate

        out = einsum('b h i j, b h j d -> b h i d', attn, v)

        # combine heads

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

# transformer

class Transformer(nn.Module):
    def __init__(
        self,
        *,
        num_tokens,
        dim,
        seq_len,
        depth,
        dim_head = 64,
        heads = 8,
        ff_mult = 4,
        use_coor_descent = False,
        coor_descent_iters = 50,
        coor_descent_sparsity_k = 1,
        coor_descent_eps = 1e-1,
        attn_null_kv = 0
    ):
        super().__init__()
        self.seq_len = seq_len

        self.token_emb = nn.Embedding(num_tokens, dim)
        self.pos_emb = nn.Embedding(seq_len, dim)

        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                Attention(
                    dim,
                    dim_head = dim_head,
                    heads = heads,
                    use_coor_descent = use_coor_descent,
                    coor_descent_iters = coor_descent_iters,
                    coor_descent_sparsity_k = coor_descent_sparsity_k,
                    coor_descent_eps = coor_descent_eps,
                    attn_null_kv = attn_null_kv
                ),
                FeedForward(dim, ff_mult)
            ]))

        self.to_logits = nn.Sequential(
            nn.LayerNorm(dim),
            nn.Linear(dim, num_tokens)
        )

    def forward(self, x):
        n, device = x.shape[-1], x.device
        assert n <= self.seq_len

        x = self.token_emb(x)
        x = x + self.pos_emb(torch.arange(n, device = device))

        for attn, ff in self.layers:
            x = attn(x) + x
            x = ff(x) + x

        return self.to_logits(x)
