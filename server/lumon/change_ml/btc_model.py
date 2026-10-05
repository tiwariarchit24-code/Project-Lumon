"""
The BTC-B change-detection network, rebuilt for inference only.

BTC ("Be The Change", Rolih et al., IEEE TGRS 2025) compares two co-registered
RGB images of the same place:

    before ─┐                       ┌─ stage1..4 features (before)
            ├─ Swin-B encoder ──────┤                                 ┌──────────────┐
    after ──┘   (shared weights)    └─ stage1..4 features (after) ──► │ before − after│
                                                                     └──────┬───────┘
                                                    UperNet decoder ◄───────┘
                                                          │
                                                  1-channel logit map  → sigmoid → change score

Source: github.com/blaz-r/BTC-change-detection at commit db41090 (MIT
licence), configuration configs/exp/BTC-B.yaml and the published checkpoint
blaz-r/BTC-B_oscd96. Only the parts needed for inference are rebuilt here:

  - encoder: the `transformers` Swin backbone, built from the architecture in
    config/ai/btc_b_backbone.json (no pretrained download; the weights come
    from the BTC checkpoint)
  - difference: plain subtraction of the before/after features (BTC's
    "SubDiff" with norm=False; NOT the absolute difference, so the image
    order matters: before first)
  - decoder: the UperNet head below, copied from BTC's models/modules/upernet.py,
    which is itself adapted from Hugging Face transformers
    (Copyright 2022 The HuggingFace Inc. team, Apache License 2.0)

Loading is STRICT: every tensor in the checkpoint must match a parameter
here, and every parameter must be filled. A mismatch raises instead of
running a partly initialised network.
"""

import json
from pathlib import Path

import torch
from torch import nn

IMAGE_SIZE = 256  # BTC-B's input and output size (OSCD 96 px tiles are resized to this)
HIDDEN_SIZE = 512
POOL_SCALES = (1, 2, 3, 6)


# ---------------------------------------------------------------------------
# UperNet decoder (Apache-2.0, adapted from Hugging Face transformers via BTC).
# Parameter names are kept identical to BTC's so the checkpoint loads as is.
# ---------------------------------------------------------------------------

class UperNetConvModule(nn.Module):
    """conv -> batch norm -> ReLU."""

    def __init__(self, in_channels: int, out_channels: int, kernel_size: int, padding: int = 0):
        super().__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size, padding=padding, bias=False)
        self.batch_norm = nn.BatchNorm2d(out_channels)
        self.activation = nn.ReLU()

    def forward(self, x):
        return self.activation(self.batch_norm(self.conv(x)))


class UperNetPyramidPoolingBlock(nn.Module):
    """Average-pool to a fixed grid, then a 1x1 conv block (registered as "0" and "1" like the original)."""

    def __init__(self, pool_scale: int, in_channels: int, channels: int):
        super().__init__()
        self.layers = [nn.AdaptiveAvgPool2d(pool_scale), UperNetConvModule(in_channels, channels, kernel_size=1)]
        for index, layer in enumerate(self.layers):
            self.add_module(str(index), layer)

    def forward(self, x):
        for layer in self.layers:
            x = layer(x)
        return x


class UperNetPyramidPoolingModule(nn.Module):
    """Pyramid pooling (PSPNet): pool at several scales and upsample back."""

    def __init__(self, pool_scales, in_channels: int, channels: int):
        super().__init__()
        self.blocks = []
        for index, scale in enumerate(pool_scales):
            block = UperNetPyramidPoolingBlock(scale, in_channels, channels)
            self.blocks.append(block)
            self.add_module(str(index), block)

    def forward(self, x):
        return [nn.functional.interpolate(block(x), size=x.shape[2:], mode="bilinear", align_corners=False)
                for block in self.blocks]


class UperNetHead(nn.Module):
    """Feature-pyramid decoder that turns the 4 difference maps into one logit map of IMAGE_SIZE²."""

    def __init__(self, in_channels: list[int], channels: int = HIDDEN_SIZE, out_channels: int = 1, out_size: int = IMAGE_SIZE):
        super().__init__()
        self.out_size = out_size
        self.classifier = nn.Conv2d(channels, out_channels, kernel_size=1)
        self.psp_modules = UperNetPyramidPoolingModule(POOL_SCALES, in_channels[-1], channels)
        self.bottleneck = UperNetConvModule(in_channels[-1] + len(POOL_SCALES) * channels, channels, kernel_size=3, padding=1)
        self.lateral_convs = nn.ModuleList()
        self.fpn_convs = nn.ModuleList()
        for size in in_channels[:-1]:
            self.lateral_convs.append(UperNetConvModule(size, channels, kernel_size=1))
            self.fpn_convs.append(UperNetConvModule(channels, channels, kernel_size=3, padding=1))
        self.fpn_bottleneck = UperNetConvModule(len(in_channels) * channels, channels, kernel_size=3, padding=1)

    def forward(self, features: list[torch.Tensor]) -> torch.Tensor:
        laterals = [conv(features[i]) for i, conv in enumerate(self.lateral_convs)]
        top = features[-1]
        laterals.append(self.bottleneck(torch.cat([top, *self.psp_modules(top)], dim=1)))
        # top-down: add each coarser level, upsampled, to the next finer one
        for i in range(len(laterals) - 1, 0, -1):
            laterals[i - 1] = laterals[i - 1] + nn.functional.interpolate(
                laterals[i], size=laterals[i - 1].shape[2:], mode="bilinear", align_corners=False)
        outputs = [self.fpn_convs[i](laterals[i]) for i in range(len(laterals) - 1)] + [laterals[-1]]
        for i in range(len(outputs) - 1, 0, -1):
            outputs[i] = nn.functional.interpolate(outputs[i], size=outputs[0].shape[2:], mode="bilinear", align_corners=False)
        logits = self.classifier(self.fpn_bottleneck(torch.cat(outputs, dim=1)))
        return nn.functional.interpolate(logits, self.out_size, mode="bilinear", align_corners=False)


# ---------------------------------------------------------------------------
# The full network
# ---------------------------------------------------------------------------

class BTCChangeModel(nn.Module):
    """Swin-B encoder (shared) -> before − after -> UperNet -> change logits (N, 1, 256, 256)."""

    def __init__(self, backbone_config: dict):
        super().__init__()
        from transformers import SwinBackbone, SwinConfig

        self.enc = SwinBackbone(SwinConfig(**backbone_config))
        self.dec = UperNetHead(list(self.enc.num_features[1:]))

    def forward(self, before: torch.Tensor, after: torch.Tensor) -> torch.Tensor:
        features = self.enc(torch.cat([before, after], dim=0)).feature_maps
        differences = []
        for feature in features:
            first, second = feature.chunk(2, dim=0)
            differences.append(first - second)
        return self.dec(differences)


# The BTC checkpoint was saved with transformers 4.x, whose Swin used other
# parameter names. These are the renaming rules transformers 5 itself applies
# to legacy Swin checkpoints (transformers/conversion_mapping.py, entries
# "SwinBackbone" and "swin"), written out here so the mapping is explicit.
LEGACY_SWIN_RENAMES = [
    ("attention.self.relative_position_bias_table", "attention.relative_position_bias.relative_position_bias_table"),
    ("attention.self.query", "attention.q_proj"),
    ("attention.self.key", "attention.k_proj"),
    ("attention.self.value", "attention.v_proj"),
    ("attention.output.dense", "attention.o_proj"),
    ("intermediate.dense", "mlp.fc1"),
    ("output.dense", "mlp.fc2"),
]
# Present in the model but deliberately not in the checkpoint: the final
# LayerNorm of SwinModel, whose output a backbone never uses (transformers
# lists it in SwinBackbone._keys_to_ignore_on_load_missing).
ALLOWED_MISSING_PREFIX = "enc.swin.layernorm."


def _rename_legacy(name: str) -> str | None:
    """Checkpoint name -> name in BTCChangeModel; None for tensors that are recomputed, not loaded."""
    if not name.startswith("enc.backbone."):
        return name  # decoder: identical names
    name = name[len("enc.backbone."):]
    if name.endswith("relative_position_index"):
        return None  # fixed index, recomputed from window_size (checked in load_btc)
    if name.startswith(("encoder.", "embeddings.")):
        name = f"swin.{name}"
    for old, new in LEGACY_SWIN_RENAMES:
        name = name.replace(old, new)
    return f"enc.{name}"


def load_btc(weights_path: Path, backbone_config_path: Path) -> BTCChangeModel:
    """
    Build the network and load the BTC checkpoint; returns it in eval mode.

    Raises RuntimeError unless every checkpoint tensor is used and every
    model parameter is filled (the one documented exception above), and
    unless the checkpoint's stored relative-position indices equal the ones
    this architecture recomputes (proof that the window layout matches).
    """
    from safetensors.torch import load_file

    backbone_config = json.loads(Path(backbone_config_path).read_text())["backbone_config"]
    model = BTCChangeModel(backbone_config)
    state = load_file(str(weights_path))

    renamed, stored_indices = {}, {}
    for name, tensor in state.items():
        target = _rename_legacy(name)
        if target is None:
            stored_indices[name] = tensor
        else:
            renamed[target] = tensor

    missing, unexpected = model.load_state_dict(renamed, strict=False)
    missing = [m for m in missing if not m.startswith(ALLOWED_MISSING_PREFIX)]
    if missing or unexpected:
        raise RuntimeError(f"BTC checkpoint does not match the network: missing {missing[:5]}, unexpected {unexpected[:5]}")

    buffers = dict(model.named_buffers())
    for name, tensor in stored_indices.items():
        layer = _rename_legacy(name.replace("relative_position_index", "relative_position_bias_table"))
        ours = buffers[layer.replace("relative_position_bias_table", "relative_position_index")]
        if not torch.equal(ours.view(-1).long(), tensor.view(-1).long()):
            raise RuntimeError(f"relative position index differs for {name}: window layout mismatch")
    return model.eval()
