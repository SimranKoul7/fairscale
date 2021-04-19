import operator
from typing import Dict, List, Optional, cast

from torch.distributed.nn import RemoteModule
import torch.fx
import torch.nn as nn

from . import PipelineModulesGraph


class RemoteModuleTracer(torch.fx.Tracer):
    def is_leaf_module(self, m: torch.nn.Module, module_qualified_name: str) -> bool:
        if isinstance(m, RemoteModule):
            return True
        return False


class GraphCreator:
    def __init__(self, tracer: RemoteModuleTracer) -> None:
        self.tracer = tracer

    def get_module(self, node: torch.fx.Node) -> Optional[nn.Module]:
        """
        Given a call_module node, returns the module corresponding to this module call
        """
        if node.op != "call_module":
            return None
        module = self.tracer.root
        for t in cast(str, node.target).split("."):
            module = getattr(module, t)
        return module

    def create_graph(self, arg_names: List[str]) -> PipelineModulesGraph:
        node_to_data: Dict[torch.fx.Node, PipelineModulesGraph.DataSourceSpec] = {}
        node_to_module = {}

        for node in self.tracer.graph.nodes:
            if node.op == "call_module":
                module = self.get_module(node)
                assert isinstance(module, RemoteModule)
                node_to_data[node] = module
                node_to_module[node] = module
            elif node.target == operator.__getitem__ and node.op == "call_function":
                assert node.args[0] in node_to_data
                d = node_to_data[node.args[0]]
                assert isinstance(d, RemoteModule)
                node_to_data[node] = (d, node.args[1])
            elif node.op == "placeholder":
                node_to_data[node] = arg_names.index(node.target)
            elif node.op == "output":
                pass
            else:
                assert False

        module_to_num_outputs: Dict[nn.Module, Optional[int]] = {}
        for node in node_to_module.keys():
            for arg in node.args:
                data = node_to_data[arg]
                if isinstance(data, int):
                    continue
                if isinstance(data, RemoteModule):
                    assert module_to_num_outputs.get(data, None) is None
                    module_to_num_outputs[data] = None
                else:
                    module, output_num = data
                    if module in module_to_num_outputs:
                        prev_value = module_to_num_outputs[module]
                        assert prev_value is not None
                        module_to_num_outputs[module] = max(prev_value, output_num + 1)
                    else:
                        module_to_num_outputs[module] = output_num + 1

        graph = PipelineModulesGraph()
        for node, module in node_to_module.items():
            inputs = [node_to_data[arg] for arg in node.args]
            graph.add_layer(module, inputs, module_to_num_outputs.get(module))

        for node in graph.nodes:
            print(node.get_debug_str())

        return graph


def _call_trace(tracer: RemoteModuleTracer, module: nn.Module) -> torch.fx.Graph:
    try:
        org_named_modules = RemoteModule.named_modules
        org_named_children = RemoteModule.named_children
        RemoteModule.named_modules = nn.Module.named_modules  # type: ignore
        RemoteModule.named_children = nn.Module.named_children  # type: ignore
        return tracer.trace(module)
    finally:
        RemoteModule.named_modules = org_named_modules  # type: ignore
        RemoteModule.named_children = org_named_children  # type: ignore


def make_graph(module: nn.Module, arg_names: List[str]) -> PipelineModulesGraph:
    tracer = RemoteModuleTracer()
    r = _call_trace(tracer, module)
    g = torch.fx.GraphModule(module, r)
    print(g.code)
    return GraphCreator(tracer).create_graph(arg_names)