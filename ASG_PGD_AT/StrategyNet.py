import torch.nn as nn
from ffc import *

import torch
import torch.nn as nn_1
from timm.models.layers import DropPath, trunc_normal_
from timm.models.layers.helpers import to_2tuple
import einops

import math

CoreMLConversion = False

class Mlp(nn_1.Module):

    def __init__(self, in_features, hidden_features=None,
                 out_features=None, act_layer=nn_1.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.norm1 = nn_1.BatchNorm2d(in_features)
        self.fc1 = nn_1.Conv2d(in_features, hidden_features, 1)
        self.act = act_layer()
        self.fc2 = nn_1.Conv2d(hidden_features, out_features, 1)
        self.drop = nn_1.Dropout(drop)
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn_1.Conv2d):
            trunc_normal_(m.weight, std=.02)
            if m.bias is not None:
                nn_1.init.constant_(m.bias, 0)

    def forward(self, x):
        x = self.norm1(x)
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x

class EfficientAdditiveAttnetion(nn_1.Module):

    def __init__(self, in_dims=512, token_dim=256, num_heads=2):
        super().__init__()

        self.to_query = nn_1.Linear(in_dims, token_dim * num_heads)
        self.to_key = nn_1.Linear(in_dims, token_dim * num_heads)

        self.w_g = nn_1.Parameter(torch.randn(token_dim * num_heads, 1))
        self.scale_factor = token_dim ** -0.5
        self.Proj = nn_1.Linear(token_dim * num_heads, token_dim * num_heads)
        self.final = nn_1.Linear(token_dim * num_heads, token_dim)

    def forward(self, x):
        query = self.to_query(x)
        key = self.to_key(x)

        if not CoreMLConversion:
            # torch.nn.functional.normalize is not supported by the ANE of iPhone devices.
            # Using this layer improves the accuracy by ~0.1-0.2%
            query = torch.nn.functional.normalize(query, dim=-1)
            key = torch.nn.functional.normalize(key, dim=-1)

        query_weight = query @ self.w_g
        A = query_weight * self.scale_factor

        A = A.softmax(dim=-1)

        G = torch.sum(A * query, dim=1)

        G = einops.repeat(
            G, "b d -> b repeat d", repeat=key.shape[1]
        )

        out = self.Proj(G * key) + query

        out = self.final(out)

        return out


class SwiftFormerLocalRepresentation(nn_1.Module):

    def __init__(self, dim, kernel_size=3, drop_path=0., use_layer_scale=True):
        super().__init__()
        self.dwconv = nn_1.Conv2d(dim, dim, kernel_size=kernel_size, padding=kernel_size // 2, groups=dim)
        self.norm = nn_1.BatchNorm2d(dim)
        self.pwconv1 = nn_1.Conv2d(dim, dim, kernel_size=1)
        self.act = nn_1.GELU()
        self.pwconv2 = nn_1.Conv2d(dim, dim, kernel_size=1)
        self.drop_path = DropPath(drop_path) if drop_path > 0. \
            else nn_1.Identity()
        self.use_layer_scale = use_layer_scale
        if use_layer_scale:
            self.layer_scale = nn_1.Parameter(torch.ones(dim).unsqueeze(-1).unsqueeze(-1), requires_grad=True)
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn_1.Conv2d):
            trunc_normal_(m.weight, std=.02)
            if m.bias is not None:
                nn_1.init.constant_(m.bias, 0)

    def forward(self, x):
        input = x
        x = self.dwconv(x)
        x = self.norm(x)
        x = self.pwconv1(x)
        x = self.act(x)
        x = self.pwconv2(x)
        if self.use_layer_scale:
            x = input + self.drop_path(self.layer_scale * x)
        else:
            x = input + self.drop_path(x)
        return x

class SwiftFormerEncoder(nn_1.Module):

    def __init__(self, dim, mlp_ratio=4.,
                 act_layer=nn_1.GELU,
                 drop=0., drop_path=0.,
                 use_layer_scale=True, layer_scale_init_value=1e-5):

        super().__init__()

        self.local_representation = SwiftFormerLocalRepresentation(dim=dim, kernel_size=3, drop_path=0.,
                                                                   use_layer_scale=True)
        self.attn = EfficientAdditiveAttnetion(in_dims=dim, token_dim=dim, num_heads=1)
        self.linear = Mlp(in_features=dim, hidden_features=int(dim * mlp_ratio), act_layer=act_layer, drop=drop)
        self.drop_path = DropPath(drop_path) if drop_path > 0. \
            else nn.Identity()
        self.use_layer_scale = use_layer_scale
        if use_layer_scale:
            self.layer_scale_1 = nn_1.Parameter(
                layer_scale_init_value * torch.ones(dim).unsqueeze(-1).unsqueeze(-1), requires_grad=True)
            self.layer_scale_2 = nn_1.Parameter(
                layer_scale_init_value * torch.ones(dim).unsqueeze(-1).unsqueeze(-1), requires_grad=True)

    def forward(self, x):
        x = self.local_representation(x)
        B, C, H, W = x.shape
        if self.use_layer_scale:
            x = x + self.drop_path(
                self.layer_scale_1 * self.attn(x.permute(0, 2, 3, 1).reshape(B, H * W, C)).reshape(B, H, W, C).permute(
                    0, 3, 1, 2))
            x = x + self.drop_path(self.layer_scale_2 * self.linear(x))

        else:
            x = x + self.drop_path(
                self.attn(x.permute(0, 2, 3, 1).reshape(B, H * W, C)).reshape(B, H, W, C).permute(0, 3, 1, 2))
            x = x + self.drop_path(self.linear(x))
        return x

class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None, groups=1,
                 base_width=64, dilation=1, ratio_gin=0.5, ratio_gout=0.5, lfu=True, use_se=False, norm_layer=None):
        super(BasicBlock, self).__init__()
        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        if groups != 1 or base_width != 64:
            raise ValueError(
                "BasicBlock only supports groups=1 and base_width=64")
        if dilation > 1:
            raise NotImplementedError(
                "Dilation > 1 not supported in BasicBlock")
        width = int(planes * (base_width / 64.)) * groups
        # Both self.conv2 and self.downsample layers downsample the input when
        # stride != 1
        self.conv1 = FFC_BN_ACT(inplanes, width, kernel_size=3, padding=1, stride=stride,
                                ratio_gin=ratio_gin, ratio_gout=ratio_gout, norm_layer=norm_layer, activation_layer=nn.ReLU, enable_lfu=lfu)
        self.conv2 = FFC_BN_ACT(width, planes * self.expansion, kernel_size=3, padding=1,
                                ratio_gin=ratio_gout, ratio_gout=ratio_gout, norm_layer=norm_layer, enable_lfu=lfu)
        self.se_block = FFCSE_block(
            planes * self.expansion, ratio_gout) if use_se else nn.Identity()
        self.relu_l = nn.Identity() if ratio_gout == 1 else nn.ReLU(inplace=True)
        self.relu_g = nn.Identity() if ratio_gout == 0 else nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        x = x if type(x) is tuple else (x, 0)
        id_l, id_g = x if self.downsample is None else self.downsample(x)

        x = self.conv1(x)
        x = self.conv2(x)
        x_l, x_g = self.se_block(x)

        x_l = self.relu_l(x_l + id_l)
        x_g = self.relu_g(x_g + id_g)

        return x_l, x_g


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None, groups=1,
                 base_width=64, dilation=1, ratio_gin=0.5, ratio_gout=0.5, lfu=True, use_se=False):
        super(Bottleneck, self).__init__()
        width = int(planes * (base_width / 64.)) * groups
        # Both self.conv2 and self.downsample layers downsample the input when
        # stride != 1
        self.conv1 = FFC_BN_ACT(inplanes, width, kernel_size=1,
                                ratio_gin=ratio_gin, ratio_gout=ratio_gout,
                                activation_layer=nn.ReLU, enable_lfu=lfu)
        self.conv2 = FFC_BN_ACT(width, width, kernel_size=3,
                                ratio_gin=ratio_gout, ratio_gout=ratio_gout,
                                stride=stride, padding=1, groups=groups,
                                activation_layer=nn.ReLU, enable_lfu=lfu)
        self.conv3 = FFC_BN_ACT(width, planes * self.expansion, kernel_size=1,
                                ratio_gin=ratio_gout, ratio_gout=ratio_gout, enable_lfu=lfu)
        self.se_block = FFCSE_block(
            planes * self.expansion, ratio_gout) if use_se else nn.Identity()
        self.relu_l = nn.Identity() if ratio_gout == 1 else nn.ReLU(inplace=True)
        self.relu_g = nn.Identity() if ratio_gout == 0 else nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        x = x if type(x) is tuple else (x, 0)
        id_l, id_g = x if self.downsample is None else self.downsample(x)

        x = self.conv1(x)
        x = self.conv2(x)
        x = self.conv3(x)
        x_l, x_g = self.se_block(x)

        x_l = self.relu_l(x_l + id_l)
        x_g = self.relu_g(x_g + id_g)

        return x_l, x_g


class FFCResNet(nn.Module):

    def __init__(self, block, layers, num_classes=1000, zero_init_residual=False,
                 groups=1, width_per_group=64, norm_layer=None, ratio=0.5, lfu=True, use_se=False):
        super(FFCResNet, self).__init__()

        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        self._norm_layer = norm_layer

        inplanes = 64
        # TODO add ratio-inplanes-groups assertion

        self.inplanes = inplanes
        self.dilation = 1
        self.groups = groups
        self.base_width = width_per_group
        self.lfu = lfu
        self.use_se = use_se
        self.conv1 = nn.Conv2d(3, inplanes, kernel_size=7,
                               stride=2, padding=3, bias=False)
        self.bn1 = norm_layer(inplanes)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(
            block, inplanes * 1, layers[0], stride=1, ratio_gin=0, ratio_gout=ratio)
        self.layer2 = self._make_layer(
            block, inplanes * 2, layers[1], stride=2, ratio_gin=ratio, ratio_gout=ratio)
        self.layer3 = self._make_layer(
            block, inplanes * 4, layers[2], stride=2, ratio_gin=ratio, ratio_gout=ratio)
        self.layer4 = self._make_layer(
            block, inplanes * 8, layers[3], stride=2, ratio_gin=ratio, ratio_gout=0)
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        #self.fc = nn.Linear(inplanes * 8 * block.expansion, num_classes)
        self.trans = SwiftFormerEncoder(dim=64)

        self.Attack_method = nn.Linear(512*block.expansion, 1)  # 所用攻击方式的个数 1
        self.Attack_epsilon = nn.Linear(512*block.expansion, 11)  # 攻击强度 11
        self.Attack_iters = nn.Linear(512*block.expansion, 11)  # 迭代次数 11
        self.Attack_step_size = nn.Linear(512*block.expansion, 1)  # 步长

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(
                    m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

        # Zero-initialize the last BN in each residual branch,
        # so that the residual branch starts with zeros, and each residual block behaves like an identity.
        # This improves the model by 0.2~0.3% according to
        # https://arxiv.org/abs/1706.02677
        if zero_init_residual:
            for m in self.modules():
                if isinstance(m, Bottleneck):
                    nn.init.constant_(m.bn3.weight, 0)
                elif isinstance(m, BasicBlock):
                    nn.init.constant_(m.bn2.weight, 0)

    def _make_layer(self, block, planes, blocks, stride=1, ratio_gin=0.5, ratio_gout=0.5):
        norm_layer = self._norm_layer
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion or ratio_gin == 0:
            downsample = FFC_BN_ACT(self.inplanes, planes * block.expansion, kernel_size=1, stride=stride,
                                    ratio_gin=ratio_gin, ratio_gout=ratio_gout, enable_lfu=self.lfu)

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample, self.groups, self.base_width,
                            self.dilation, ratio_gin, ratio_gout, lfu=self.lfu, use_se=self.use_se))
        self.inplanes = planes * block.expansion
        for _ in range(1, blocks):
            layers.append(block(self.inplanes, planes, groups=self.groups, base_width=self.base_width, dilation=self.dilation,
                                ratio_gin=ratio_gout, ratio_gout=ratio_gout, lfu=self.lfu, use_se=self.use_se))

        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.trans(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        x = self.avgpool(x[0])
        out = x.view(x.size(0), -1)
        #x = self.fc(x)

        Attack_method = self.Attack_method(out)
        Attack_epsilon = self.Attack_epsilon(out)
        Attack_iters = self.Attack_iters(out)
        Attack_step_size = self.Attack_step_size(out)

        return Attack_method, Attack_epsilon, Attack_iters, Attack_step_size


def ResNet18_Strategy(pretrained=True, **kwargs):
    
    model = FFCResNet(Bottleneck, [2, 2, 2, 2], **kwargs)
    return model
