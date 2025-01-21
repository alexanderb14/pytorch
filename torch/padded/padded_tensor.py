import functools
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import torch
import torch.nn.functional as F
import torch.utils._pytree as pytree
from torch._subclasses.fake_tensor import FakeTensor
from torch._subclasses.functional_tensor import FunctionalTensor
from torch.utils._python_dispatch import return_and_correct_aliasing

from utils import *


INVALID_ID = -1337
PADDED_OP_TABLE: Dict[Any, Any] = {}


def register_func(table: Dict[Any, Any], aten_ops: List[str]):
    assert isinstance(aten_ops, list)
    assert all(isinstance(op, str) for op in aten_ops)

    def wrapper(cls):
        for aten_op in aten_ops:
            table[aten_op] = cls()

    return wrapper


register_padded_op = functools.partial(register_func, PADDED_OP_TABLE)


class Dimension(int):
    def __new__(cls, value, is_padded=None, *args, **kwargs):
        ret = super(cls, cls).__new__(cls, value)
        ret.is_padded = is_padded
        return ret

    def __add__(self, other):
        res = super(Dimension, self).__add__(other)
        return self.__class__(res, self.is_padded or other.is_padded)

    def __sub__(self, other):
        res = super(Dimension, self).__sub__(other)
        return self.__class__(res, self.is_padded or other.is_padded)

    def __mul__(self, other):
        res = super(Dimension, self).__mul__(other)
        return self.__class__(res, self.is_padded or other.is_padded)

    def __repr__(self) -> str:
        if self.is_padded:
            return super().__repr__() + "(P)"
        else:
            return super().__repr__()


def strip_common_suffix(
    list1: List[int], list2: List[int]
) -> Tuple[List[int], List[int]]:
    list1, list2 = list(list1), list(list2)

    if len(list1) == 0 or len(list2) == 0:
        return list1, list2

    idx = 0
    while list1[len(list1) - idx - 1] == list2[len(list2) - idx - 1]:
        idx += 1

    return list1[: len(list1) - idx], list2[: len(list2) - idx]


def strip_common_prefix(
    list1: List[int], list2: List[int]
) -> Tuple[List[int], List[int]]:
    if len(list1) == 0 or len(list2) == 0:
        return list1, list2

    idx = 0
    while list1[idx] == list2[idx]:
        idx += 1

    return list1[idx:], list2[idx:]


def slice_nd(
    input: torch.Tensor, start_idxs: List[int], end_idxs: List[int]
) -> torch.Tensor:
    log("Slicing tensor with shape %s to %s" % (input.shape, end_idxs))

    # Slice a tensor along multiple dimensions
    # This is a generalization of torch.slice, which only supports slicing along one dimension
    assert len(start_idxs) == len(end_idxs)

    # Check if input.shape and end_idx are identical. Skip slicing if so.
    if all(
        input.shape[dim_idx] == end_idx
        for dim_idx, end_idx in enumerate(end_idxs)
        if end_idx is not None
    ):
        return input

    # Slice the tensor
    for dim_idx, (start_idx, end_idx) in enumerate(zip(start_idxs, end_idxs)):
        if start_idx is not None and end_idx is not None:
            if end_idx != input.shape[dim_idx]:
                assert start_idx >= 0
                assert end_idx <= input.shape[dim_idx]

                if not start_idx < end_idx:
                    raise ValueError(
                        f"Invalid slice indices: {start_idx}:{end_idx} for dimension {dim_idx}"
                    )

                input = torch.ops.aten.slice(input, dim_idx, start_idx, end_idx)

    return input


class PaddedOp:
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        raise NotImplementedError


@register_padded_op(["ones_like"])
class OnesLikeOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        return [input_shape]


@register_padded_op(["view", "_unsafe_view", "view_as_real"])
class ViewOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs) -> List[torch.Size]:
        def find_mapping(input_shape: List[int], output_shape: List[int]):
            mapping = []
            input_index = 0

            for output_dim in output_shape:
                current_mapping = []

                while True:
                    if (
                        input_index >= len(input_shape)
                        or output_dim < input_shape[input_index]
                    ):
                        break

                    current_mapping.append(input_index)
                    output_dim //= input_shape[input_index]
                    input_index += 1
                mapping.append(current_mapping)

            return mapping

        def apply_mapping(
            input_shape: torch.Size, mapping: List[List[int]]
        ) -> List[int]:
            output_shape = []

            for current_mapping in mapping:
                output_dim = Dimension(1, False)
                for index in current_mapping:
                    output_dim = input_shape[index] * output_dim

                output_shape.append(output_dim)

            return output_shape

        def maybe_infer_minus_1_dims(input_shape: List[int], output_shape: List[int]):
            input_shape_prod = math.prod(input_shape)
            output_shape_prod = math.prod(output_shape) * -1

            for idx, output_dim in enumerate(output_shape):
                if output_dim == -1:
                    output_shape[idx] = input_shape_prod // output_shape_prod
                    break
            return output_shape

        def maybe_insert_1_dims(input_shape: List[int], output_shape: List[int]):
            # Check if it applies. Bail out if not
            if 1 not in output_shape:
                return output_shape

            orig_output_shape_new = []
            idx = 0
            for dim in output_shape:
                if dim == 1:
                    orig_output_shape_new.append(1)
                else:
                    orig_output_shape_new.append(input_shape[idx])
                    idx += 1
            return orig_output_shape_new

        orig_input_shape = input_shapes[0]
        input_shape = list(args[0].shape)
        output_shape = list(args[1])

        is_equal = len(input_shape) == len(output_shape)
        is_expanding = len(input_shape) < len(output_shape)
        is_collapsing = len(input_shape) > len(output_shape)

        if is_equal:
            return [torch.Size(output_shape)]

        if is_collapsing:
            # Find the mapping from input_shape to output_shape, then apply this mapping to the orig input shape, to find the orig output shape.
            # E.g. if the input_shape is [32, 32, 32], the output_shape [1024, 32]
            # The mapping is: [[0, 1], [2]]
            # For an orig input shape [16, 16, 16], the mapped orig output shape is [256, 16]
            mapping = find_mapping(input_shape, output_shape)
            orig_output_shape = apply_mapping(orig_input_shape, mapping)

            return [torch.Size(orig_output_shape)]

        if is_expanding:
            is_prefix_equal = input_shape[0] == output_shape[0]
            is_suffix_equal = input_shape[-1] == output_shape[-1]
            is_no_equal = not is_prefix_equal and not is_suffix_equal

            if is_no_equal:
                raise NotImplementedError(
                    "ViewOp with collapsing and no equal dimensions is not supported"
                )

            orig_output_shape = None
            if is_prefix_equal:
                # We strip the common prefix. Then attach the suffix of the output shape to the orig input shape
                suffix_in, suffix_out = strip_common_prefix(input_shape, output_shape)

                offset = len(orig_input_shape) - len(suffix_in)
                orig_output_shape = list(orig_input_shape[:offset]) + suffix_out

            if is_suffix_equal:
                # We strip the common suffix. Then attach the prefix of the output shape to the orig input shape
                prefix_in, prefix_out = strip_common_suffix(input_shape, output_shape)

                offset = len(prefix_in)
                orig_output_shape = prefix_out + list(orig_input_shape[offset:])

            # Infer -1 dimensions if any
            orig_output_shape = maybe_infer_minus_1_dims(
                orig_input_shape, orig_output_shape
            )

            # In case original input and output shapes don't multiply to the same value, we try some heuristics
            if not (
                math.prod(orig_input_shape) == math.prod(orig_output_shape)
                or -1 in orig_output_shape
            ):
                # Check if there are any added 1 dimensions. If so, we can apply them to the original input shape
                orig_output_shape = maybe_insert_1_dims(orig_input_shape, output_shape)

            # If we still can't find a valid output shape, we return a dummy one
            if not (
                math.prod(orig_input_shape) == math.prod(orig_output_shape)
                or -1 in orig_output_shape
            ):
                return [torch.Size([INVALID_ID] * len(output_shape))]

            return [torch.Size(orig_output_shape)]


@register_padded_op(["unsqueeze"])
class UnsqueezeOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        dim = args[1]

        if dim < 0:
            dim += len(input_shape) + 1

        return [input_shape[:dim] + (1,) + input_shape[dim:]]


@register_padded_op(["polar"])
class PolarOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        return [input_shape]


@register_padded_op(["transpose"])
class TransposeOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        dim0 = args[1]
        dim1 = args[2]

        if dim0 < 0:
            dim0 += len(input_shape)
        if dim1 < 0:
            dim1 += len(input_shape)

        # Exchange dim0 and dim1
        input_shape = list(input_shape)
        input_shape[dim0], input_shape[dim1] = input_shape[dim1], input_shape[dim0]

        return [torch.Size(input_shape)]


@register_padded_op(["expand"])
class ExpandOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        shape = args[1]

        return [torch.Size(shape)]


@register_padded_op(["clone", "where", "tril", "sin", "rsqrt", "silu"])
class ElementwiseUnaryOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        return [input_shape]


@register_padded_op(["add", "sub", "mul", "div"])
class ElementwiseBinaryOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        # Broadcasting
        lhs_shape = args[0].orig_shape if type(args[0]) is PaddedTensor else [1]
        rhs_shape = args[1].orig_shape if type(args[1]) is PaddedTensor else [1]

        new_shape = []
        for idx in range(max(len(lhs_shape), len(rhs_shape))):
            lhs_dim = lhs_shape[-idx - 1] if idx < len(lhs_shape) else 1
            rhs_dim = rhs_shape[-idx - 1] if idx < len(rhs_shape) else 1
            new_shape.append(max(lhs_dim, rhs_dim))

        return [torch.Size(reversed(new_shape))]


@register_padded_op(["addmm"])
class AddMmOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        return [torch.Size([args[0].orig_shape[0], args[1].orig_shape[1]])]


@register_padded_op(["mm"])
class MatmulOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        return [torch.Size([args[0].orig_shape[0], args[1].orig_shape[1]])]


@register_padded_op(["bmm"])
class BmmOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        b1, n1, m1 = args[0].orig_shape
        b2, m2, p2 = args[1].orig_shape

        assert b1 == b2
        assert m1 == m2

        return [torch.Size([b1, n1, p2])]


@register_padded_op(
    ["_scaled_dot_product_flash_attention", "_scaled_dot_product_efficient_attention"]
)
class ScaledDotProductAttentionOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]

        attn_shape = input_shape[:-1]
        return [input_shape, attn_shape]


@register_padded_op(["index"])
class IndexOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        input_shape_mod = list(input_shape)
        dims = args[1]

        for dim_idx, dim in enumerate(dims):
            if dim is None:
                continue
            elif (
                type(dim) in [torch.Tensor, FakeTensor, FunctionalTensor]
                or type(dim) is PaddedTensor
            ):
                input_shape_mod[dim_idx] = dim.orig_shape[0]
            else:
                raise NotImplementedError(f"Encountered unsupported type: {type(dim)}")

        return [torch.Size(input_shape_mod)]


@register_padded_op(["select"])
class SelectOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input_shape = args[0].orig_shape
        dim = args[1]
        index = args[2]

        if dim < 0:
            dim += len(input_shape)
        if index < 0:
            index += input_shape[dim]

        return [input_shape[:dim] + input_shape[dim + 1 :]]


@register_padded_op(["index_put_"])
class IndexPutOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        return [torch.Size(input_shape)]


@register_padded_op(["split_with_sizes"])
class SplitWithSizesOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        indices_or_sections = args[1]
        dim = args[2]

        if dim < 0:
            dim += len(input_shape)

        return [
            list(input_shape[:dim])
            + [indices_or_sections[i]]
            + list(input_shape[dim + 1 :])
            for i in range(len(indices_or_sections))
        ]


@register_padded_op(["stack"])
class StackOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input = args[0]
        dim = args[1]

        if dim < 0:
            dim += len(input[0].orig_shape) + 1

        return [input[0].orig_shape[:dim] + (len(input),) + input[0].orig_shape[dim:]]


@register_padded_op(["detach"])
class DetachOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        return [input_shape]


@register_padded_op(["embedding"])
class EmbeddingOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        # Embedding is a special case, where we don't do any padding
        input_shape = input_shapes[0]
        indices = args[1]

        out_shape = list(indices.orig_shape) + list(input_shape)[1:]

        return [torch.Size(out_shape)]


@register_padded_op(["slice", "unbind", "_to_copy", "copy_", "mean", "t", "sum", "pow"])
class NoOp(PaddedOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shapes(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        return [input_shape]


def log_function_with_shapes(func, args, tensor_args, out=None, orig_shape_out=None):
    def to_shape_str(arg):
        if (
            isinstance(arg, torch.Tensor)
            or isinstance(arg, FakeTensor)
            or isinstance(arg, FunctionalTensor)
        ):
            return [i for i in arg.shape]
        else:
            return arg

    func_name_str = str(func)

    arg_shapes = []
    for arg in args:
        arg_shapes.append(str(pytree.tree_map(to_shape_str, arg)))

    arg_shapes_str = "[" + ", ".join(arg_shapes) + "]"

    out_shape_str = str(pytree.tree_map(to_shape_str, out)) if out is not None else ""

    out_str = "{0:40} P: {1:60} {2:20}".format(
        func_name_str, arg_shapes_str, out_shape_str
    )
    log(out_str)

    def to_orig_shape_str(arg):
        if isinstance(arg, PaddedTensor):
            return [i for i in arg.orig_shape]
        elif (
            isinstance(arg, torch.Tensor)
            or isinstance(arg, FakeTensor)
            or isinstance(arg, FunctionalTensor)
        ):
            return "Tensor"
        else:
            return arg

    arg_shapes = []
    for arg in args:
        arg_shapes.append(str(pytree.tree_map(to_orig_shape_str, arg)))

    arg_shapes_str = "[" + ", ".join(arg_shapes) + "]"

    out_shape_str = (
        str(pytree.tree_map(to_shape_str, orig_shape_out))
        if orig_shape_out is not None
        else ""
    )

    out_str = "{0:40} U: {1:60} {2:20}".format("", arg_shapes_str, out_shape_str)
    log(out_str)


def get_strides(shape: torch.Size) -> List[int]:
    if len(shape) == 0:
        return []

    strides = [1]
    for i in range(len(shape) - 1, 0, -1):
        strides.append(strides[-1] * shape[i])
    return strides[::-1]


def get_padded_shape(shape: torch.Size, multipliers: List[int]) -> torch.Size:
    padded_shape = list(shape)
    for dim, multiplier in enumerate(multipliers):
        if dim >= len(padded_shape):
            continue
        padded_shape[dim] = (
            (padded_shape[dim] + multiplier - 1) // multiplier * multiplier
        )
    return torch.Size(padded_shape)


def get_pad(shape: torch.Size, multipliers: List[int]) -> Tuple[int, ...]:
    pad = [0] * (len(shape) * 2)
    for dim, multiplier in enumerate(multipliers):
        if dim >= len(shape):
            continue
        pad[2 * dim] = (shape[dim] + multiplier - 1) // multiplier * multiplier - shape[
            dim
        ]
        pad[2 * dim + 1] = 0
    return tuple(pad[::-1])


def convert_tensor_args(args: List[object]) -> Tuple[object]:
    args_padded = []
    for arg in args:
        if (
            type(arg) is torch.Tensor
            or type(arg) is torch.nn.Parameter
            or type(arg) is FakeTensor
            or type(arg) is FunctionalTensor
        ):
            multipliers = [1] * len(arg.shape)
            args_padded.append(PaddedTensor(arg, multipliers))
            log(
                "Encountered tensor with shape",
                arg.shape,
                "and converted to padded tensor",
            )
        else:
            args_padded.append(arg)
    return tuple(args_padded)


def convert_tensor_results(out, orig_out_shapes):
    out_flat, spec = pytree.tree_flatten(out)
    out_flat_padded = []
    for idx, out_tensor in enumerate(out_flat):
        if type(out_tensor) in [
            torch.Tensor,
            FakeTensor,
            FunctionalTensor,
        ] and idx < len(orig_out_shapes):
            s = orig_out_shapes[idx]
            multipliers = [1] * len(out_tensor.shape)
            out_flat_padded.append(PaddedTensor(out_tensor, multipliers, s))
        else:
            out_flat_padded.append(out_tensor)
    out = pytree.tree_unflatten(out_flat_padded, spec)
    return out


def get_tensors_from_padded(
    args: Tuple, kwargs: Dict
) -> Tuple[List[torch.Tensor], Dict]:
    if kwargs is None:
        kwargs = {}
    tensor_args, tensor_kwargs = pytree.tree_map_only(
        PaddedTensor, lambda x: x.tensor, (args, kwargs)
    )
    tensor_args = list(tensor_args)

    return tensor_args, tensor_kwargs


def create_padded_dims(tensor: torch.Tensor, multipliers: List[int]) -> List[Dimension]:
    shape_new = []
    for dim_idx, dim in enumerate(tensor.shape):
        is_padded = multipliers[dim_idx] != 1
        shape_new.append(Dimension(dim, is_padded))
    return shape_new


class PaddedTensor(torch.Tensor):
    @staticmethod
    def __new__(
        cls,
        tensor: torch.Tensor,
        multipliers: Optional[List[int]],
        orig_shape: Optional[torch.Size] = None,
        neutral_element=0,
    ):
        assert type(multipliers) is list

        # TODO: change ori_shape as torch.Tensor
        if multipliers is None:
            multipliers = []

        padded_shape = get_padded_shape(tensor.shape, multipliers)
        kwargs = {}
        # TODO: Improve kwargs. Support different strides, storage_offset, etc.
        kwargs["strides"] = get_strides(padded_shape)
        kwargs["storage_offset"] = tensor.storage_offset()
        kwargs["device"] = tensor.device
        kwargs["layout"] = tensor.layout
        kwargs["requires_grad"] = tensor.requires_grad
        kwargs["dtype"] = tensor.dtype
        out = torch.Tensor._make_wrapper_subclass(cls, padded_shape, **kwargs)

        log(
            "Creating padded tensor with shape",
            list(out.shape),
            "orig_shape",
            list(orig_shape) if orig_shape is not None else list(tensor.shape),
            "multipliers",
            multipliers,
        )

        return out

    def __init__(
        self,
        tensor: torch.Tensor,
        multipliers: Optional[List[int]],
        orig_shape: Optional[torch.Size] = None,
        neutral_element=0,
    ):
        if multipliers is None:
            multipliers = []
        self.multipliers = multipliers

        if orig_shape is None:
            self.orig_shape = torch.Size(create_padded_dims(tensor, multipliers))
        else:
            self.orig_shape = orig_shape

        self.neutral_element = neutral_element
        if tensor.shape != self.shape:
            pad = get_pad(tensor.shape, multipliers)
            self.tensor = F.pad(
                input=tensor, pad=pad, mode="constant", value=neutral_element
            )
        else:
            self.tensor = tensor

    def __repr__(self):
        return f"PaddedTensor(shape:{self.tensor.shape}, orig_shape:{self.orig_shape})"

    def __tensor_flatten__(self):
        return ["tensor"], {
            "multipliers": self.multipliers,
            "orig_shape": self.orig_shape,
            "neutral_element": self.neutral_element,
        }

    @staticmethod
    def __tensor_unflatten__(inner_tensors, meta, outer_size, outer_stride):
        return PaddedTensor(
            inner_tensors["tensor"],
            meta["multipliers"],
            meta["orig_shape"],
            meta["neutral_element"],
        )

    @classmethod
    def __torch_function__(cls, func, types, args=(), kwargs=None):
        if kwargs is None:
            kwargs = {}

        with torch._C.DisableTorchFunctionSubclass():
            out = func(*args, **kwargs)

        if func.__name__ == "linear":
            in_shape_1 = args[0].orig_shape
            in_shape_2 = args[1].shape

            prefix1, prefix2 = strip_common_suffix(in_shape_1, in_shape_2)
            out_shape = prefix1 + prefix2

            out.orig_shape = torch.Size(out_shape)

        return out

    @classmethod
    def __torch_dispatch__(cls, func, types, args, kwargs):
        log("Dispatching %s" % func._overloadpacket.__name__)
        log("-" * 40)

        if func._opname not in PADDED_OP_TABLE:
            raise NotImplementedError(
                f"Function '{func._opname}' is not implemented for PaddedTensor"
            )
        op = PADDED_OP_TABLE[func._opname]

        # Convert arg tensors to padded tensors
        args = convert_tensor_args(args)

        # Infer original shape
        orig_in_shapes = pytree.tree_map_only(
            PaddedTensor, lambda x: x.orig_shape, args
        )
        orig_out_shapes = op.infer_shapes(orig_in_shapes, args, kwargs)

        tensor_args, tensor_kwargs = get_tensors_from_padded(args, kwargs)

        # Run function
        out = func(*tensor_args, **tensor_kwargs)

        log_function_with_shapes(func, args, tensor_args, out, orig_out_shapes)

        # Convert results tensors to padded tensors
        out = convert_tensor_results(out, orig_out_shapes)

        return return_and_correct_aliasing(func, args, kwargs, out)

    def unpad(self) -> torch.Tensor:
        if INVALID_ID in self.orig_shape:
            raise Exception(
                "PaddedTensor couldn't figure out a shape, likely due to an expansion."
            )

        start_idxs = [0] * len(self.orig_shape)
        end_idxs = list(self.orig_shape)
        return slice_nd(self.tensor, start_idxs, end_idxs)
