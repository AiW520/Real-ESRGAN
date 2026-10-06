"""RRDBNet matching the official BasicSR / Real-ESRGAN state dict layout.

Architecture: https://github.com/XPixelGroup/BasicSR/blob/master/basicsr/archs/rrdbnet_arch.py
Only the x4 RGB networks needed by the official release weights are included.
Copyright 2018-2022 BasicSR Authors; Apache-2.0 (see THIRD_PARTY_NOTICES.md).
"""
import torch
from torch import nn
from torch.nn import functional as F


class ResidualDenseBlock(nn.Module):
    def __init__(self, features=64, growth=32):
        super().__init__()
        for index in range(1, 6):
            incoming = features + (index - 1) * growth
            outgoing = features if index == 5 else growth
            setattr(self, f'conv{index}', nn.Conv2d(incoming, outgoing, 3, 1, 1))
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        values = [x]
        for index in range(1, 5):
            values.append(self.lrelu(getattr(self, f'conv{index}')(torch.cat(values, 1))))
        return x + self.conv5(torch.cat(values, 1)) * 0.2


class RRDB(nn.Module):
    def __init__(self):
        super().__init__()
        self.rdb1 = ResidualDenseBlock()
        self.rdb2 = ResidualDenseBlock()
        self.rdb3 = ResidualDenseBlock()

    def forward(self, x):
        return x + self.rdb3(self.rdb2(self.rdb1(x))) * 0.2


class RRDBNet(nn.Module):
    def __init__(self, blocks):
        super().__init__()
        self.conv_first = nn.Conv2d(3, 64, 3, 1, 1)
        self.body = nn.Sequential(*(RRDB() for _ in range(blocks)))
        self.conv_body = nn.Conv2d(64, 64, 3, 1, 1)
        self.conv_up1 = nn.Conv2d(64, 64, 3, 1, 1)
        self.conv_up2 = nn.Conv2d(64, 64, 3, 1, 1)
        self.conv_hr = nn.Conv2d(64, 64, 3, 1, 1)
        self.conv_last = nn.Conv2d(64, 3, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        initial = self.conv_first(x)
        feature = initial + self.conv_body(self.body(initial))
        feature = self.lrelu(self.conv_up1(F.interpolate(feature, scale_factor=2, mode='nearest')))
        feature = self.lrelu(self.conv_up2(F.interpolate(feature, scale_factor=2, mode='nearest')))
        return self.conv_last(self.lrelu(self.conv_hr(feature)))
