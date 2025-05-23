# Copyright 2024-2025 Arm Limited and/or its affiliates.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
#

# pyre-unsafe
from typing import Any, cast, Dict

import numpy as np
import torch
import torch.fx
from executorch.backends.arm.operators.node_visitor import NodeVisitor
from executorch.backends.arm.tosa_mapping import TosaArg
from executorch.backends.arm.tosa_specification import (
    Tosa_0_80,
    Tosa_1_00,
    TosaSpecification,
)
from executorch.backends.arm.tosa_utils import getNodeArgs, tosa_shape
from torch._export.utils import (
    get_buffer,
    get_lifted_tensor_constant,
    get_param,
    is_buffer,
    is_lifted_tensor_constant,
    is_param,
)
from torch.export.exported_program import ExportedProgram


def process_call_function(
    node: torch.fx.Node,
    tosa_graph: Any,
    node_visitors: Dict[str, NodeVisitor],
    tosa_spec: TosaSpecification,
):
    # Unpack arguments and convert
    inputs = getNodeArgs(node, tosa_spec)

    # Convert output (this node itself)
    try:
        output = TosaArg(node, tosa_spec)
    except ValueError as e:
        raise ValueError(
            f"Failed processing call_function: {node.name}. "
            "Is the original torch function supported?"
        ) from e
    tosa_graph.currRegion.currBasicBlock.addTensor(
        output.name, tosa_shape(output.shape, output.dim_order), output.dtype
    )

    # Visiting each Node
    # pyre-ignore[16]: Undefined attribute.
    if node.target.__name__ in node_visitors:  # type: ignore[union-attr]
        # pyre-ignore[16]: Undefined attribute.
        node_visitors[node.target.__name__].define_node(  # type: ignore[union-attr]
            node,
            tosa_graph,
            inputs,
            output,
        )
    else:
        raise RuntimeError(f"Unknown operator {node.target} for TOSA : {tosa_spec}")


def process_inputs(
    node: torch.fx.Node,
    tosa_graph: Any,
    tosa_spec: TosaSpecification,
):
    """Serialize an input node"""
    # inputs need to be in default dim_order (contiguous memory format)
    meta = node.meta["val"]
    if meta.dim_order() != tuple(range(meta.dim())):
        raise RuntimeError(
            f"Arm backend only supports contiguous memory format for inputs. "
            f"Expected dim_order: {tuple(range(meta.dim()))}, but got: {meta.dim_order()} for node {node.name}"
        )
    try:
        tosa_arg = TosaArg(node, tosa_spec)
    except ValueError as e:
        raise ValueError(
            f"Failed processing input placeholder: {node.name}. "
            "Is the original torch function supported?"
        ) from e

    if isinstance(tosa_spec, Tosa_0_80):
        import tosa_tools.v0_80.serializer.tosa_serializer as ts  # type: ignore
    elif isinstance(tosa_spec, Tosa_1_00):
        import serializer.tosa_serializer as ts
    else:
        raise ValueError(f"Unsupported TOSA spec: {tosa_spec}")

    input_shape = tosa_arg.shape
    input_dim_order = tosa_arg.dim_order
    tensor = ts.TosaSerializerTensor(
        tosa_arg.name,
        tosa_shape(input_shape, input_dim_order),
        tosa_arg.dtype,
        data=None,
        placeholderFilename=tosa_arg.name + ".npy",
    )
    tosa_graph.addInputTensor(tensor)


def process_inputs_to_parameters(
    node: torch.fx.Node,
    tosa_graph: Any,
    edge_program: ExportedProgram,
    tosa_spec: TosaSpecification,
):
    """Serialize bias and non-quantized weights"""
    try:
        tosa_arg = TosaArg(node, tosa_spec)
    except ValueError as e:
        raise ValueError(
            f"Failed processing parameter placeholder: {node.name}. "
            "Is the original torch function supported?"
        ) from e
    parameter_data = get_param(edge_program, node)

    assert isinstance(parameter_data, torch.Tensor), "Expect Attr to be tensor"
    parameter_values = parameter_data.detach().numpy()

    if tosa_arg.dtype == torch.float32:
        assert tosa_spec.support_float(), f"{tosa_spec} doesn't support float"

    parameter_values = np.transpose(parameter_values, tosa_arg.dim_order)

    tosa_graph.addConst(
        parameter_values.shape, tosa_arg.dtype, parameter_values, name=tosa_arg.name
    )


def process_inputs_to_buffers(
    node: torch.fx.Node,
    tosa_graph: Any,
    edge_program: ExportedProgram,
    tosa_spec: TosaSpecification,
):
    """Serialize quantized weights"""
    try:
        tosa_arg = TosaArg(node, tosa_spec)
    except ValueError as e:
        raise ValueError(
            f"Failed processing buffer placeholder: {node.name}. "
            "Is the original torch function supported?"
        ) from e
    buffer_data = get_buffer(edge_program, node)

    assert isinstance(buffer_data, torch.Tensor), "Expect Attr to be tensor"
    buffer_values = buffer_data.detach().numpy()

    # TODO: fragile code for temporary fix
    # the mean and var tensors are also stored here but they have shape (1, )
    # we only transpose weights here
    buffer_values = np.transpose(buffer_values, tosa_arg.dim_order)

    tosa_graph.addConst(
        buffer_values.shape, tosa_arg.dtype, buffer_values, name=node.name
    )


def process_inputs_to_lifted_tensor_constants(
    node: torch.fx.Node,
    tosa_graph: Any,
    edge_program: ExportedProgram,
    tosa_spec: TosaSpecification,
):
    try:
        tosa_arg = TosaArg(node, tosa_spec)
    except ValueError as e:
        raise ValueError(
            f"Failed processing lifted tensor constant placeholder: {node.name}. "
            "Is the original torch function supported?"
        ) from e
    tensor = get_lifted_tensor_constant(edge_program, node)
    tensor_data = tensor.detach().numpy()  # type: ignore[union-attr]

    tosa_graph.addConst(
        tensor_data.shape, tosa_arg.dtype, tensor_data, name=tosa_arg.name
    )


def process_placeholder(
    node: torch.fx.Node,
    tosa_graph: Any,
    edge_program: ExportedProgram,
    tosa_spec: TosaSpecification,
):
    """Wrapper for processing and serializing all types of placeholders"""
    assert node.name == node.target, "Expect placeholder name and target to match"
    assert 0 == len(node.args), "Can't handle default input values"

    if node.name in edge_program.graph_signature.user_inputs:
        process_inputs(node, tosa_graph, tosa_spec)
    elif is_param(edge_program, node):
        process_inputs_to_parameters(node, tosa_graph, edge_program, tosa_spec)
    elif is_buffer(edge_program, node):
        process_inputs_to_buffers(node, tosa_graph, edge_program, tosa_spec)
    elif is_lifted_tensor_constant(edge_program, node):
        process_inputs_to_lifted_tensor_constants(
            node, tosa_graph, edge_program, tosa_spec
        )
    elif node.name in edge_program.graph_signature.inputs_to_lifted_custom_objs:
        raise NotImplementedError(
            "Placeholder is of type 'lifted custom object' which is not supported."
        )
    else:
        raise RuntimeError(f"Placeholder '{node.name}' is of unknown type.")


def process_output(
    node: torch.fx.Node,
    tosa_graph: Any,
):
    for output in cast(tuple[torch.fx.Node, ...], node.args[0]):
        tosa_graph.addOutputTensor(
            tosa_graph.currRegion.currBasicBlock.tensors[output.name]
        )
