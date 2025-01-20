import itertools
import math
from operator import mul
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn.functional as F
import torch.utils._pytree as pytree
from torch._subclasses.fake_tensor import FakeTensor
from torch._subclasses.functional_tensor import FunctionalTensor
from torch.utils._python_dispatch import return_and_correct_aliasing

from utils import *


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


class RegularOp:
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        raise NotImplementedError


class OnesLikeOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        return [input_shape]


class ViewOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs) -> List[torch.Size]:
        def find_mapping(input_shape: torch.Size, output_shape: List[int]):
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
                output_dim = 1
                for index in current_mapping:
                    output_dim *= input_shape[index]

                output_shape.append(output_dim)

            return output_shape

        input_shape = input_shapes[0]
        padded_input_shape = args[0].shape
        output_shape = list(args[1])

        # If the shapes are compatible, we can just return the orig output shape.
        if math.prod(input_shape) == math.prod(output_shape):
            return [torch.Size(output_shape)]

        # Does the output shape contain -1? If so, we need to infer the value of -1
        if -1 in output_shape:
            input_shape_prod = math.prod(padded_input_shape)
            output_shape_prod = math.prod(output_shape) * -1

            for idx, output_dim in enumerate(output_shape):
                if output_dim == -1:
                    output_shape[idx] = input_shape_prod // output_shape_prod
                    break
            return [torch.Size(output_shape)]

        # Then apply this mapping to the orig input shape, to find the orig output shape.
        # E.g. input_shape = [32, 32, 32], output_shape = [1024, 32]
        # The mapping is: [[0, 1], [2]]
        mapping = find_mapping(padded_input_shape, output_shape)
        orig_output_shape = apply_mapping(input_shape, mapping)

        return [torch.Size(orig_output_shape)]


class ViewAsRealOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        return [input_shape + (2,)]


class UnsqueezeOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        dim = args[1]

        if dim < 0:
            dim += len(input_shape) + 1

        return [input_shape[:dim] + (1,) + input_shape[dim:]]


class PolarOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        return [input_shape]


class TransposeOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
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


class ExpandOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        shape = args[1]

        return [torch.Size(shape)]


class ElementwiseUnaryOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        return [input_shape]


class ElementwiseBinaryOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        # Broadcasting
        lhs_shape = args[0].orig_shape if type(args[0]) is PaddedTensor else [1]
        rhs_shape = args[1].orig_shape if type(args[1]) is PaddedTensor else [1]

        new_shape = []
        for idx in range(max(len(lhs_shape), len(rhs_shape))):
            lhs_dim = lhs_shape[-idx - 1] if idx < len(lhs_shape) else 1
            rhs_dim = rhs_shape[-idx - 1] if idx < len(rhs_shape) else 1
            new_shape.append(max(lhs_dim, rhs_dim))

        return [torch.Size(reversed(new_shape))]


class MatmulOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        return [torch.Size([args[0].orig_shape[0], args[1].orig_shape[1]])]


class BmmOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        b1, n1, m1 = args[0].orig_shape
        b2, m2, p2 = args[1].orig_shape

        assert b1 == b2
        assert m1 == m2

        return [torch.Size([b1, n1, p2])]


class ScaledDotProductAttentionOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]

        attn_shape = input_shape[:-1]
        return [input_shape, attn_shape]


class IndexOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
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


class SelectOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        input_shape = args[0].orig_shape
        dim = args[1]
        index = args[2]

        if dim < 0:
            dim += len(input_shape)
        if index < 0:
            index += input_shape[dim]

        return [input_shape[:dim] + input_shape[dim + 1 :]]


class IndexPutOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        return [torch.Size(input_shape)]


class SplitWithSizesOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
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


class StackOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        input = args[0]
        dim = args[1]

        if dim < 0:
            dim += len(input[0].orig_shape) + 1

        return [input[0].orig_shape[:dim] + (len(input),) + input[0].orig_shape[dim:]]


class DetachOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        return [input_shape]


class EmbeddingOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        # Embedding is a special case, where we don't do any padding
        input_shape = input_shapes[0]
        indices = args[1]

        out_shape = list(indices.orig_shape) + list(input_shape)[1:]

        return [torch.Size(out_shape)]


class NoOp(RegularOp):
    def __init__(self) -> None:
        super().__init__()

    def infer_shape(self, input_shapes, args, kwargs):
        input_shape = input_shapes[0]
        return [input_shape]


class OpDatabase:
    def __init__(self):
        self.ops = {
            # Tensor creation and manipulation
            "ones_like": OnesLikeOp(),
            "view": ViewOp(),
            "_unsafe_view": ViewOp(),
            "view_as_real": ViewOp(),
            "unsqueeze": UnsqueezeOp(),
            "polar": PolarOp(),
            "transpose": TransposeOp(),
            "clone": ElementwiseUnaryOp(),
            "expand": ExpandOp(),
            # Elementwise operations
            "where": ElementwiseUnaryOp(),
            "tril": ElementwiseUnaryOp(),
            "sin": ElementwiseUnaryOp(),
            "rsqrt": ElementwiseUnaryOp(),
            "silu": ElementwiseUnaryOp(),
            # Elementwise binary operations
            "add": ElementwiseBinaryOp(),
            "sub": ElementwiseBinaryOp(),
            "mul": ElementwiseBinaryOp(),
            "div": ElementwiseBinaryOp(),
            # Contraction / Reduction operations
            "mm": MatmulOp(),
            "bmm": BmmOp(),
            "_scaled_dot_product_flash_attention": ScaledDotProductAttentionOp(),
            "_scaled_dot_product_efficient_attention": ScaledDotProductAttentionOp(),
            # Indexing operations
            "index": IndexOp(),
            "select": SelectOp(),
            "index_put_": IndexPutOp(),
            # Splitting / Stacking
            "split_with_sizes": SplitWithSizesOp(),
            "stack": StackOp(),
            # Other
            "detach": DetachOp(),
            "embedding": EmbeddingOp(),
            "slice": NoOp(),
            "unbind": NoOp(),
            "_to_copy": NoOp(),
            "copy_": NoOp(),
            "mean": NoOp(),
            "t": NoOp(),
            "sum": NoOp(),
            "pow": NoOp(),
        }

    def get_op(self, opname):
        if opname in self.ops:
            return self.ops[opname]
        else:
            raise NotImplementedError(f"Op '{opname}' not supported")


OP_DATABASE = OpDatabase()


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
        self.orig_shape = tensor.shape if orig_shape is None else orig_shape
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
    def __torch_dispatch__(cls, func, types, args, kwargs):
        log()
        log("Dispatching", func._opname)
        log("-" * 40)

        op = OP_DATABASE.get_op(func._opname)

        # Convert arg tensors to padded tensors
        args = convert_tensor_args(args)

        # Infer original shape
        orig_in_shapes = pytree.tree_map_only(
            PaddedTensor, lambda x: x.orig_shape, args
        )
        orig_out_shapes = op.infer_shape(orig_in_shapes, args, kwargs)

        tensor_args, tensor_kwargs = get_tensors_from_padded(args, kwargs)

        # Run function
        out = func(*tensor_args, **tensor_kwargs)

        log_function_with_shapes(func, args, tensor_args, out, orig_out_shapes)

        # Convert results tensors to padded tensors
        out = convert_tensor_results(out, orig_out_shapes)

        return return_and_correct_aliasing(func, args, kwargs, out)
