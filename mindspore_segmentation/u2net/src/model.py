'''model'''
from typing import Union, List
import mindspore as ms
from mindspore import nn, Tensor, ops


class ConvBNReLU(nn.Cell):
    '''ConvBNRELU'''

    def __init__(self, in_ch: int, out_ch: int, kernel_size: int = 3, dilation: int = 1):
        super().__init__()

        padding = kernel_size // 2 if dilation == 1 else dilation
        self.conv = nn.Conv2d(in_ch, out_ch, kernel_size,
                              padding=padding, dilation=dilation, has_bias=False)
        self.bn = nn.BatchNorm2d(out_ch)
        self.relu = nn.ReLU()

    def construct(self, x: Tensor) -> Tensor:
        return self.relu(self.bn(self.conv(x)))


class DownConvBNReLU(ConvBNReLU):
    '''DownConvBNReLU'''

    def __init__(self, in_ch: int, out_ch: int, kernel_size: int = 3, dilation: int = 1, flag: bool = True):
        super().__init__(in_ch, out_ch, kernel_size, dilation)
        self.down_flag = flag

    def construct(self, x: Tensor) -> Tensor:
        if self.down_flag:
            x = ops.max_pool2d(x, kernel_size=2, stride=2, ceil_mode=True)

        return self.relu(self.bn(self.conv(x)))


class UpConvBNReLU(ConvBNReLU):
    '''UpConvBNReLU'''

    def __init__(self, in_ch: int, out_ch: int, kernel_size: int = 3, dilation: int = 1, flag: bool = True):
        super().__init__(in_ch, out_ch, kernel_size, dilation)
        self.up_flag = flag

    def construct(self, x1: Tensor, x2: Tensor) -> Tensor:
        if self.up_flag:
            x1 = ops.interpolate(
                x1, size=x2.shape[2:], mode='bilinear', align_corners=False)
        return self.relu(self.bn(self.conv(ops.cat([x1, x2], axis=1))))


class RSU(nn.Cell):
    '''RSU'''

    def __init__(self, height: int, in_ch: int, mid_ch: int, out_ch: int):
        super().__init__()

        assert height >= 2
        self.conv_in = ConvBNReLU(in_ch, out_ch)

        encode_list = [DownConvBNReLU(out_ch, mid_ch, flag=False)]
        decode_list = [UpConvBNReLU(mid_ch * 2, mid_ch, flag=False)]
        for i in range(height - 2):
            encode_list.append(DownConvBNReLU(mid_ch, mid_ch))
            decode_list.append(UpConvBNReLU(
                mid_ch * 2, mid_ch if i < height - 3 else out_ch))

        encode_list.append(ConvBNReLU(mid_ch, mid_ch, dilation=2))
        self.encode_modules = nn.CellList(encode_list)
        self.decode_modules = nn.CellList(decode_list)

    def construct(self, x: Tensor) -> Tensor:
        x_in = self.conv_in(x)

        x = x_in
        encode_outputs = []
        for m in self.encode_modules:
            x = m(x)
            encode_outputs.append(x)

        x = encode_outputs.pop()
        for m in self.decode_modules:
            x2 = encode_outputs.pop()
            x = m(x, x2)

        return x + x_in


class RSU4F(nn.Cell):
    '''RSU4F'''

    def __init__(self, in_ch: int, mid_ch: int, out_ch: int):
        super().__init__()
        self.conv_in = ConvBNReLU(in_ch, out_ch)
        self.encode_modules = nn.CellList([ConvBNReLU(out_ch, mid_ch),
                                           ConvBNReLU(
            mid_ch, mid_ch, dilation=2),
            ConvBNReLU(
            mid_ch, mid_ch, dilation=4),
            ConvBNReLU(mid_ch, mid_ch, dilation=8)])

        self.decode_modules = nn.CellList([ConvBNReLU(mid_ch * 2, mid_ch, dilation=4),
                                           ConvBNReLU(
            mid_ch * 2, mid_ch, dilation=2),
            ConvBNReLU(mid_ch * 2, out_ch)])

    def construct(self, x: Tensor) -> Tensor:
        x_in = self.conv_in(x)

        x = x_in
        encode_outputs = []
        for m in self.encode_modules:
            x = m(x)
            encode_outputs.append(x)

        x = encode_outputs.pop()
        for m in self.decode_modules:
            x2 = encode_outputs.pop()
            x = m(ops.cat([x, x2], axis=1))

        return x + x_in


class U2Net(nn.Cell):
    '''U2Net'''

    def __init__(self, cfg: dict, out_ch: int = 1):
        super().__init__()
        assert "encode" in cfg
        assert "decode" in cfg
        self.encode_num = len(cfg["encode"])

        encode_list = []
        side_list = []
        for c in cfg["encode"]:
            # c: [height, in_ch, mid_ch, out_ch, RSU4F, side]
            assert len(c) == 6
            encode_list.append(RSU(*c[:4]) if c[4]
                               is False else RSU4F(*c[1:4]))

            if c[5] is True:
                side_list.append(
                    nn.Conv2d(c[3], out_ch, kernel_size=3, padding=1))
        self.encode_modules = nn.CellList(encode_list)

        decode_list = []
        for c in cfg["decode"]:
            # c: [height, in_ch, mid_ch, out_ch, RSU4F, side]
            assert len(c) == 6
            decode_list.append(RSU(*c[:4]) if c[4]
                               is False else RSU4F(*c[1:4]))

            if c[5] is True:
                side_list.append(
                    nn.Conv2d(c[3], out_ch, kernel_size=3, padding=1))
        self.decode_modules = nn.CellList(decode_list)
        self.side_modules = nn.CellList(side_list)
        self.out_conv = nn.Conv2d(
            self.encode_num * out_ch, out_ch, kernel_size=1)

    def construct(self, x: Tensor) -> Union[Tensor, List[Tensor]]:
        _, _, h, w = x.shape

        # collect encode outputs
        encode_outputs = []
        for i, m in enumerate(self.encode_modules):
            x = m(x)
            encode_outputs.append(x)
            if i != self.encode_num - 1:
                x = ops.max_pool2d(x, kernel_size=2, stride=2, ceil_mode=True)

        # collect decode outputs
        x = encode_outputs.pop()
        decode_outputs = [x]
        for m in self.decode_modules:
            x2 = encode_outputs.pop()
            x = ops.interpolate(
                x, size=x2.shape[2:], mode='bilinear', align_corners=False)
            x = m(ops.concat([x, x2], axis=1))
            decode_outputs.insert(0, x)

        # collect side outputs
        side_outputs = []
        for m in self.side_modules:
            x = decode_outputs.pop()
            x = ops.interpolate(m(x), size=[h, w],
                                mode='bilinear', align_corners=False)
            side_outputs.insert(0, x)

        x = self.out_conv(ops.concat(side_outputs, axis=1))

        if self.training:
            # do not use ops.sigmoid for amp safe
            out = [x] + side_outputs
        else:
            out = ops.sigmoid(x)
        return out


def u2net_full(out_ch: int = 1):
    '''u2net_full'''
    cfg = {
        # height, in_ch, mid_ch, out_ch, RSU4F, side
        "encode": [[7, 3, 32, 64, False, False],      # En1
                   [6, 64, 32, 128, False, False],    # En2
                   [5, 128, 64, 256, False, False],   # En3
                   [4, 256, 128, 512, False, False],  # En4
                   [4, 512, 256, 512, True, False],   # En5
                   [4, 512, 256, 512, True, True]],   # En6
        # height, in_ch, mid_ch, out_ch, RSU4F, side
        "decode": [[4, 1024, 256, 512, True, True],   # De5
                   [4, 1024, 128, 256, False, True],  # De4
                   [5, 512, 64, 128, False, True],    # De3
                   [6, 256, 32, 64, False, True],     # De2
                   [7, 128, 16, 64, False, True]]     # De1
    }

    return U2Net(cfg, out_ch)


def u2net_lite(out_ch: int = 1):
    '''u2net_lite'''
    cfg = {
        # height, in_ch, mid_ch, out_ch, RSU4F, side
        "encode": [[7, 3, 16, 64, False, False],  # En1
                   [6, 64, 16, 64, False, False],  # En2
                   [5, 64, 16, 64, False, False],  # En3
                   [4, 64, 16, 64, False, False],  # En4
                   [4, 64, 16, 64, True, False],  # En5
                   [4, 64, 16, 64, True, True]],  # En6
        # height, in_ch, mid_ch, out_ch, RSU4F, side
        "decode": [[4, 128, 16, 64, True, True],  # De5
                   [4, 128, 16, 64, False, True],  # De4
                   [5, 128, 16, 64, False, True],  # De3
                   [6, 128, 16, 64, False, True],  # De2
                   [7, 128, 16, 64, False, True]]  # De1
    }

    return U2Net(cfg, out_ch)


def convert_onnx(m, name):
    '''convert_onnx'''
    m.ser_train(False)
    x = ops.rand(1, 3, 288, 288)

    # export the model
    ms.export(m,  # model being run
              x,  # model input (or a tuple for multiple inputs)
              # where to save the model (can be a file or file-like object)
              file_name=name,
              file_format='ONNX')


if __name__ == '__main__':
    # n_m = RSU(height=7, in_ch=3, mid_ch=12, out_ch=3)
    # convert_onnx(n_m, "RSU7.onnx")
    #
    # n_m = RSU4F(in_ch=3, mid_ch=12, out_ch=3)
    # convert_onnx(n_m, "RSU4F.onnx")

    u2net = u2net_full()
    convert_onnx(u2net, "u2net_fulls")
