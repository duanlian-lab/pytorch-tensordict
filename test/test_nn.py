# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import warnings

import pytest
import torch

from tensordict import tensorclass, TensorDict
from tensordict.nn import (
    dispatch,
    probabilistic as nn_probabilistic,
    ProbabilisticTensorDictModule,
    ProbabilisticTensorDictSequential,
    TensorDictModuleBase,
    TensorDictSequential,
)
from tensordict.nn.common import TensorDictModule, TensorDictModuleWrapper
from tensordict.nn.distributions import NormalParamExtractor, NormalParamWrapper
from tensordict.nn.functional_modules import is_functional, make_functional
from tensordict.nn.probabilistic import InteractionType, set_interaction_type
from tensordict.nn.utils import set_skip_existing, skip_existing
from torch import nn
from torch.distributions import Normal

try:
    import functorch  # noqa
    from functorch import make_functional_with_buffers as make_functional_functorch

    try:
        from torch import vmap
    except ImportError:
        from functorch import vmap

    _has_functorch = True
    FUNCTORCH_ERR = ""
except ImportError as err:
    _has_functorch = False
    FUNCTORCH_ERR = str(err)


class TestInteractionType:
    @pytest.mark.parametrize(
        "str_and_expected_type",
        [
            ("mode", InteractionType.MODE),
            ("MEDIAN", InteractionType.MEDIAN),
            ("Mean", InteractionType.MEAN),
            ("RanDom", InteractionType.RANDOM),
        ],
    )
    def test_from_str_correct_conversion(self, str_and_expected_type):
        type_str, expected_type = str_and_expected_type
        assert InteractionType.from_str(type_str) == expected_type

    @pytest.mark.parametrize("unsupported_type_str", ["foo"])
    def test_from_str_correct_raise(self, unsupported_type_str):
        with pytest.raises(ValueError) as err:
            InteractionType.from_str(unsupported_type_str)
        assert unsupported_type_str in str(err) and "is unsupported" in str(err)


class TestTDModule:
    @pytest.mark.parametrize("lazy", [True, False])
    def test_stateful(self, lazy):
        torch.manual_seed(0)
        param_multiplier = 1
        if lazy:
            net = nn.LazyLinear(4 * param_multiplier)
        else:
            net = nn.Linear(3, 4 * param_multiplier)

        tensordict_module = TensorDictModule(
            module=net, in_keys=["in"], out_keys=["out"]
        )

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        tensordict_module(td)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

    @pytest.mark.parametrize("lazy", [True, False])
    def test_stateful_tensorclass(self, lazy):
        @tensorclass
        class Data:
            inputs: torch.Tensor
            outputs: torch.Tensor = None

        torch.manual_seed(0)
        param_multiplier = 1
        if lazy:
            net = nn.LazyLinear(4 * param_multiplier)
        else:
            net = nn.Linear(3, 4 * param_multiplier)

        tensordict_module = TensorDictModule(
            module=net, in_keys=["inputs"], out_keys=["outputs"]
        )

        tc = Data(inputs=torch.randn(3, 3), batch_size=[3])
        tensordict_module(tc)
        assert tc.shape == torch.Size([3])
        assert tc.get("outputs").shape == torch.Size([3, 4])

    @pytest.mark.parametrize("out_keys", [["loc", "scale"], ["loc_1", "scale_1"]])
    @pytest.mark.parametrize("lazy", [True, False])
    @pytest.mark.parametrize(
        "interaction_type", [InteractionType.MODE, InteractionType.RANDOM, None]
    )
    def test_stateful_probabilistic_deprec(self, lazy, interaction_type, out_keys):
        torch.manual_seed(0)
        param_multiplier = 2
        if lazy:
            net = nn.LazyLinear(4 * param_multiplier)
        else:
            net = nn.Linear(3, 4 * param_multiplier)

        in_keys = ["in"]
        net = TensorDictModule(
            module=NormalParamWrapper(net), in_keys=in_keys, out_keys=out_keys
        )

        kwargs = {"distribution_class": Normal}
        if out_keys == ["loc", "scale"]:
            dist_in_keys = ["loc", "scale"]
        elif out_keys == ["loc_1", "scale_1"]:
            dist_in_keys = {"loc": "loc_1", "scale": "scale_1"}
        else:
            raise NotImplementedError

        prob_module = ProbabilisticTensorDictModule(
            in_keys=dist_in_keys, out_keys=["out"], **kwargs
        )

        tensordict_module = ProbabilisticTensorDictSequential(net, prob_module)

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        with set_interaction_type(interaction_type):
            tensordict_module(td)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

    @pytest.mark.parametrize("out_keys", [["low"], ["low1"], [("stuff", "low1")]])
    @pytest.mark.parametrize("lazy", [True, False])
    @pytest.mark.parametrize("max_dist", [1.0, 2.0])
    @pytest.mark.parametrize(
        "interaction_type", [InteractionType.MODE, InteractionType.RANDOM, None]
    )
    def test_stateful_probabilistic_kwargs(
        self, lazy, interaction_type, out_keys, max_dist
    ):
        torch.manual_seed(0)
        if lazy:
            net = nn.LazyLinear(4)
        else:
            net = nn.Linear(3, 4)

        in_keys = ["in"]
        net = TensorDictModule(module=net, in_keys=in_keys, out_keys=out_keys)

        kwargs = {
            "distribution_class": torch.distributions.Uniform,
            "distribution_kwargs": {"high": max_dist},
        }
        if out_keys == ["low"]:
            dist_in_keys = ["low"]
        else:
            dist_in_keys = {"low": out_keys[0]}

        prob_module = ProbabilisticTensorDictModule(
            in_keys=dist_in_keys, out_keys=["out"], **kwargs
        )

        tensordict_module = ProbabilisticTensorDictSequential(net, prob_module)

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        with set_interaction_type(interaction_type):
            tensordict_module(td)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

    @pytest.mark.parametrize(
        "out_keys",
        [
            ["loc", "scale"],
            ["loc_1", "scale_1"],
            [("params_td", "loc_1"), ("scale_1",)],
        ],
    )
    @pytest.mark.parametrize("lazy", [True, False])
    @pytest.mark.parametrize(
        "interaction_type", [InteractionType.MODE, InteractionType.RANDOM, None]
    )
    def test_stateful_probabilistic(self, lazy, interaction_type, out_keys):
        torch.manual_seed(0)
        param_multiplier = 2
        if lazy:
            net = nn.LazyLinear(4 * param_multiplier)
        else:
            net = nn.Linear(3, 4 * param_multiplier)

        in_keys = ["in"]
        net = TensorDictModule(module=net, in_keys=in_keys, out_keys=["params"])
        normal_params = TensorDictModule(
            NormalParamExtractor(), in_keys=["params"], out_keys=out_keys
        )

        kwargs = {"distribution_class": Normal}
        if out_keys == ["loc", "scale"]:
            dist_in_keys = ["loc", "scale"]
        else:
            dist_in_keys = {"loc": out_keys[0], "scale": out_keys[1]}

        prob_module = ProbabilisticTensorDictModule(
            in_keys=dist_in_keys, out_keys=["out"], **kwargs
        )

        tensordict_module = ProbabilisticTensorDictSequential(
            net, normal_params, prob_module
        )

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        with set_interaction_type(interaction_type):
            tensordict_module(td)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_before(self):
        torch.manual_seed(0)
        param_multiplier = 1

        net = nn.Linear(3, 4 * param_multiplier)

        params = make_functional(net)

        tensordict_module = TensorDictModule(
            module=net, in_keys=["in"], out_keys=["out"]
        )

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        tensordict_module(td, params=TensorDict({"module": params}, []))
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional(self):
        torch.manual_seed(0)
        param_multiplier = 1

        net = nn.Linear(3, 4 * param_multiplier)

        tensordict_module = TensorDictModule(
            module=net, in_keys=["in"], out_keys=["out"]
        )

        params = make_functional(tensordict_module)

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        tensordict_module(td, params=params)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_tensorclass(self):
        @tensorclass
        class Data:
            inputs: torch.Tensor
            outputs: torch.Tensor = None

        torch.manual_seed(0)
        param_multiplier = 1

        net = nn.Linear(3, 4 * param_multiplier)

        tensordict_module = TensorDictModule(
            module=net, in_keys=["inputs"], out_keys=["outputs"]
        )

        params = make_functional(tensordict_module)

        tc = Data(inputs=torch.randn(3, 3), batch_size=[3])
        tensordict_module(tc, params=params)
        assert tc.shape == torch.Size([3])
        assert tc.get("outputs").shape == torch.Size([3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_functorch(self):
        torch.manual_seed(0)
        param_multiplier = 1

        net = nn.Linear(3, 4 * param_multiplier)

        tensordict_module = TensorDictModule(
            module=net, in_keys=["in"], out_keys=["out"]
        )

        tensordict_module, params, buffers = make_functional_functorch(
            tensordict_module
        )

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        tensordict_module(params, buffers, td)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_probabilistic_deprec(self):
        torch.manual_seed(0)
        param_multiplier = 2

        tdnet = TensorDictModule(
            module=NormalParamWrapper(nn.Linear(3, 4 * param_multiplier)),
            in_keys=["in"],
            out_keys=["loc", "scale"],
        )

        kwargs = {"distribution_class": Normal}
        prob_module = ProbabilisticTensorDictModule(
            in_keys=["loc", "scale"], out_keys=["out"], **kwargs
        )

        tensordict_module = ProbabilisticTensorDictSequential(tdnet, prob_module)
        params = make_functional(tensordict_module)

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        tensordict_module(td, params=params)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_probabilistic(self):
        torch.manual_seed(0)
        param_multiplier = 2

        tdnet = TensorDictModule(
            module=nn.Linear(3, 4 * param_multiplier),
            in_keys=["in"],
            out_keys=["params"],
        )
        normal_params = TensorDictModule(
            NormalParamExtractor(), in_keys=["params"], out_keys=["loc", "scale"]
        )

        kwargs = {"distribution_class": Normal}
        prob_module = ProbabilisticTensorDictModule(
            in_keys=["loc", "scale"], out_keys=["out"], **kwargs
        )

        tensordict_module = ProbabilisticTensorDictSequential(
            tdnet, normal_params, prob_module
        )
        params = make_functional(tensordict_module)

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        tensordict_module(td, params=params)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_with_buffer(self):
        torch.manual_seed(0)
        param_multiplier = 1

        net = nn.BatchNorm1d(32 * param_multiplier)
        params = make_functional(net)

        tdmodule = TensorDictModule(module=net, in_keys=["in"], out_keys=["out"])

        td = TensorDict({"in": torch.randn(3, 32 * param_multiplier)}, [3])
        tdmodule(td, params=TensorDict({"module": params}, []))
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 32])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_with_buffer_probabilistic_deprec(self):
        torch.manual_seed(0)
        param_multiplier = 2

        tdnet = TensorDictModule(
            module=NormalParamWrapper(nn.BatchNorm1d(32 * param_multiplier)),
            in_keys=["in"],
            out_keys=["loc", "scale"],
        )

        kwargs = {"distribution_class": Normal}
        prob_module = ProbabilisticTensorDictModule(
            in_keys=["loc", "scale"], out_keys=["out"], **kwargs
        )

        tdmodule = ProbabilisticTensorDictSequential(tdnet, prob_module)
        params = make_functional(tdmodule)

        td = TensorDict({"in": torch.randn(3, 32 * param_multiplier)}, [3])
        tdmodule(td, params=params)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 32])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_with_buffer_probabilistic(self):
        torch.manual_seed(0)
        param_multiplier = 2

        tdnet = TensorDictModule(
            module=nn.BatchNorm1d(32 * param_multiplier),
            in_keys=["in"],
            out_keys=["params"],
        )
        normal_params = TensorDictModule(
            NormalParamExtractor(), in_keys=["params"], out_keys=["loc", "scale"]
        )

        kwargs = {"distribution_class": Normal}
        prob_module = ProbabilisticTensorDictModule(
            in_keys=["loc", "scale"], out_keys=["out"], **kwargs
        )

        tdmodule = ProbabilisticTensorDictSequential(tdnet, normal_params, prob_module)
        params = make_functional(tdmodule)

        td = TensorDict({"in": torch.randn(3, 32 * param_multiplier)}, [3])
        tdmodule(td, params=params)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 32])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_vmap(self):
        torch.manual_seed(0)
        param_multiplier = 1

        net = nn.Linear(3, 4 * param_multiplier)
        tdmodule = TensorDictModule(module=net, in_keys=["in"], out_keys=["out"])

        params = make_functional(tdmodule)

        # vmap = True
        params = params.expand(10)
        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        td_out = vmap(tdmodule, (None, 0))(td, params)
        assert td_out is not td
        assert td_out.shape == torch.Size([10, 3])
        assert td_out.get("out").shape == torch.Size([10, 3, 4])

        # vmap = (0, 0)
        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        td_repeat = td.expand(10, *td.batch_size)
        td_out = vmap(tdmodule, (0, 0))(td_repeat, params)
        assert td_out is not td_repeat
        assert td_out.shape == torch.Size([10, 3])
        assert td_out.get("out").shape == torch.Size([10, 3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_vmap_probabilistic_deprec(self):
        torch.manual_seed(0)
        param_multiplier = 2

        net = NormalParamWrapper(nn.Linear(3, 4 * param_multiplier))

        tdnet = TensorDictModule(module=net, in_keys=["in"], out_keys=["loc", "scale"])

        kwargs = {"distribution_class": Normal}
        prob_module = ProbabilisticTensorDictModule(
            in_keys=["loc", "scale"], out_keys=["out"], **kwargs
        )

        tdmodule = ProbabilisticTensorDictSequential(tdnet, prob_module)
        params = make_functional(tdmodule)

        # vmap = True
        params = params.expand(10)
        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        td_out = vmap(tdmodule, (None, 0))(td, params)
        assert td_out is not td
        assert td_out.shape == torch.Size([10, 3])
        assert td_out.get("out").shape == torch.Size([10, 3, 4])

        # vmap = (0, 0)
        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        td_repeat = td.expand(10, *td.batch_size)
        td_out = vmap(tdmodule, (0, 0))(td_repeat, params)
        assert td_out is not td_repeat
        assert td_out.shape == torch.Size([10, 3])
        assert td_out.get("out").shape == torch.Size([10, 3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_vmap_probabilistic(self):
        torch.manual_seed(0)
        param_multiplier = 2

        net = nn.Linear(3, 4 * param_multiplier)

        tdnet = TensorDictModule(module=net, in_keys=["in"], out_keys=["params"])
        normal_params = TensorDictModule(
            NormalParamExtractor(), in_keys=["params"], out_keys=["loc", "scale"]
        )

        kwargs = {"distribution_class": Normal}
        prob_module = ProbabilisticTensorDictModule(
            in_keys=["loc", "scale"], out_keys=["out"], **kwargs
        )

        tdmodule = ProbabilisticTensorDictSequential(tdnet, normal_params, prob_module)
        params = make_functional(tdmodule)

        # vmap = True
        params = params.expand(10)
        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        td_out = vmap(tdmodule, (None, 0))(td, params)
        assert td_out is not td
        assert td_out.shape == torch.Size([10, 3])
        assert td_out.get("out").shape == torch.Size([10, 3, 4])

        # vmap = (0, 0)
        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        td_repeat = td.expand(10, *td.batch_size)
        td_out = vmap(tdmodule, (0, 0))(td_repeat, params)
        assert td_out is not td_repeat
        assert td_out.shape == torch.Size([10, 3])
        assert td_out.get("out").shape == torch.Size([10, 3, 4])

    def test_dispatch(self):
        tdm = TensorDictModule(nn.Linear(1, 1), ["a"], ["b"])
        td = TensorDict({"a": torch.zeros(1, 1)}, 1)
        tdm(td)
        out = tdm(a=torch.zeros(1, 1))
        assert (out == td["b"]).all()

    def test_dispatch_changing_size(self):
        # regression test on non max batch-size for dispatch
        tdm = TensorDictModule(nn.Linear(1, 2), ["a"], ["b"])
        td = TensorDict({"a": torch.zeros(1, 1)}, 1)
        tdm(td)
        out = tdm(a=torch.zeros(1, 1))
        assert (out == td["b"]).all()

    def test_dispatch_nested(self):
        tdm = TensorDictModule(nn.Linear(1, 1), [("a", "c")], [("b", "d")])
        td = TensorDict({("a", "c"): torch.zeros(1, 1)}, [1])
        tdm(td)
        out = tdm(a_c=torch.zeros(1, 1))
        assert (out == td["b", "d"]).all()

    def test_dispatch_nested_confusing(self):
        tdm = TensorDictModule(nn.Linear(1, 1), [("a_1", "c")], [("b_2", "d")])
        td = TensorDict({("a_1", "c"): torch.zeros(1, 1)}, [1])
        tdm(td)
        out = tdm(a_1_c=torch.zeros(1, 1))
        assert (out == td["b_2", "d"]).all()

    def test_dispatch_nested_args(self):
        class MyModuleNest(nn.Module):
            in_keys = [("a", "c"), "d"]
            out_keys = ["b"]

            @dispatch(separator="_")
            def forward(self, tensordict):
                tensordict["b"] = tensordict["a", "c"] + tensordict["d"]
                return tensordict

        module = MyModuleNest()
        (b,) = module(torch.zeros(1, 2), d=torch.ones(1, 2))
        assert (b == 1).all()
        with pytest.raises(RuntimeError, match="Duplicated argument"):
            module(torch.zeros(1, 2), a_c=torch.ones(1, 2))

    def test_dispatch_nested_extra_args(self):
        class MyModuleNest(nn.Module):
            in_keys = [("a", "c"), "d"]
            out_keys = ["b"]

            @dispatch(separator="_")
            def forward(self, tensordict, other):
                tensordict["b"] = tensordict["a", "c"] + tensordict["d"] + other
                return tensordict

        module = MyModuleNest()
        other = 1
        (b,) = module(torch.zeros(1, 2), torch.ones(1, 2), other)
        assert (b == 2).all()

    def test_dispatch_nested_sep(self):
        class MyModuleNest(nn.Module):
            in_keys = [("a", "c")]
            out_keys = ["b"]

            @dispatch(separator="sep")
            def forward(self, tensordict):
                tensordict["b"] = tensordict["a", "c"] + 1
                return tensordict

        module = MyModuleNest()
        (b,) = module(asepc=torch.zeros(1, 2))
        assert (b == 1).all()

    @pytest.mark.parametrize("source", ["keys_in", [("a", "c")]])
    def test_dispatch_nested_source(self, source):
        class MyModuleNest(nn.Module):
            keys_in = [("a", "c")]
            out_keys = ["b"]

            @dispatch(separator="sep", source=source)
            def forward(self, tensordict):
                tensordict["b"] = tensordict["a", "c"] + 1
                return tensordict

        module = MyModuleNest()
        (b,) = module(asepc=torch.zeros(1, 2))
        assert (b == 1).all()

    @pytest.mark.parametrize("dest", ["other", ["b"]])
    def test_dispatch_nested_dest(self, dest):
        class MyModuleNest(nn.Module):
            in_keys = [("a", "c")]
            other = ["b"]

            @dispatch(separator="sep", dest=dest)
            def forward(self, tensordict):
                tensordict["b"] = tensordict["a", "c"] + 1
                return tensordict

        module = MyModuleNest()
        (b,) = module(asepc=torch.zeros(1, 2))
        assert (b == 1).all()

    def test_dispatch_multi(self):
        tdm = TensorDictSequential(
            TensorDictModule(nn.Linear(1, 1), [("a", "c")], [("b", "d")]),
            TensorDictModule(nn.Linear(1, 1), [("a", "c")], ["e"]),
        )
        td = TensorDict({("a", "c"): torch.zeros(1, 1)}, [1])
        tdm(td)
        out1, out2 = tdm(a_c=torch.zeros(1, 1))
        assert (out1 == td["b", "d"]).all()
        assert (out2 == td["e"]).all()

    def test_dispatch_module_with_additional_parameters(self):
        class MyModule(nn.Identity):
            def forward(self, input, c):
                return input

        m = MyModule()
        tdm = TensorDictModule(m, ["a"], ["b"])
        tdm(a=torch.zeros(1, 1), c=1)

    @pytest.mark.parametrize("output_type", [dict, TensorDict])
    def test_tdmodule_dict_output(self, output_type):
        class MyModule(nn.Identity):
            def forward(self, input):
                if output_type is dict:
                    return {"b": input}
                else:
                    return TensorDict({"b": input}, [])

        module = TensorDictModule(MyModule(), in_keys=["a"], out_keys=["b"])
        out = module(TensorDict({"a": torch.randn(3)}, []))
        assert (out["b"] == out["a"]).all()


class TestTDSequence:
    def test_key_exclusion(self):
        module1 = TensorDictModule(
            nn.Linear(3, 4), in_keys=["key1", "key2"], out_keys=["foo1"]
        )
        module2 = TensorDictModule(
            nn.Linear(3, 4), in_keys=["key1", "key3"], out_keys=["key1"]
        )
        module3 = TensorDictModule(
            nn.Linear(3, 4), in_keys=["foo1", "key3"], out_keys=["key2"]
        )
        seq = TensorDictSequential(module1, module2, module3)
        assert set(seq.in_keys) == {"key1", "key2", "key3"}
        assert set(seq.out_keys) == {"foo1", "key1", "key2"}

    @pytest.mark.parametrize("lazy", [True, False])
    def test_stateful(self, lazy):
        torch.manual_seed(0)
        param_multiplier = 1
        if lazy:
            net1 = nn.LazyLinear(4)
            dummy_net = nn.LazyLinear(4)
            net2 = nn.LazyLinear(4 * param_multiplier)
        else:
            net1 = nn.Linear(3, 4)
            dummy_net = nn.Linear(4, 4)
            net2 = nn.Linear(4, 4 * param_multiplier)

        kwargs = {}
        tdmodule1 = TensorDictModule(net1, in_keys=["in"], out_keys=["hidden"])
        dummy_tdmodule = TensorDictModule(
            dummy_net, in_keys=["hidden"], out_keys=["hidden"]
        )
        tdmodule2 = TensorDictModule(
            module=net2,
            in_keys=["hidden"],
            out_keys=["out"],
            **kwargs,
        )
        tdmodule = TensorDictSequential(tdmodule1, dummy_tdmodule, tdmodule2)

        assert hasattr(tdmodule, "__setitem__")
        assert len(tdmodule) == 3
        tdmodule[1] = tdmodule2
        assert len(tdmodule) == 3

        assert hasattr(tdmodule, "__delitem__")
        assert len(tdmodule) == 3
        del tdmodule[2]
        assert len(tdmodule) == 2

        assert hasattr(tdmodule, "__getitem__")
        assert tdmodule[0] is tdmodule1
        assert tdmodule[1] is tdmodule2

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        tdmodule(td)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

    @pytest.mark.parametrize("lazy", [True, False])
    def test_stateful_probabilistic_deprec(self, lazy):
        torch.manual_seed(0)
        param_multiplier = 2
        if lazy:
            net1 = nn.LazyLinear(4)
            dummy_net = nn.LazyLinear(4)
            net2 = nn.LazyLinear(4 * param_multiplier)
        else:
            net1 = nn.Linear(3, 4)
            dummy_net = nn.Linear(4, 4)
            net2 = nn.Linear(4, 4 * param_multiplier)
        net2 = NormalParamWrapper(net2)

        kwargs = {"distribution_class": Normal}
        tdmodule1 = TensorDictModule(net1, in_keys=["in"], out_keys=["hidden"])
        dummy_tdmodule = TensorDictModule(
            dummy_net, in_keys=["hidden"], out_keys=["hidden"]
        )
        tdmodule2 = TensorDictModule(
            net2, in_keys=["hidden"], out_keys=["loc", "scale"]
        )

        prob_module = ProbabilisticTensorDictModule(
            in_keys=["loc", "scale"], out_keys=["out"], **kwargs
        )
        tdmodule = ProbabilisticTensorDictSequential(
            tdmodule1, dummy_tdmodule, tdmodule2, prob_module
        )

        assert hasattr(tdmodule, "__setitem__")
        assert len(tdmodule) == 4
        tdmodule[1] = tdmodule2
        tdmodule[2] = prob_module
        assert len(tdmodule) == 4

        assert hasattr(tdmodule, "__delitem__")
        assert len(tdmodule) == 4
        del tdmodule[3]
        assert len(tdmodule) == 3

        assert hasattr(tdmodule, "__getitem__")
        assert tdmodule[0] is tdmodule1
        assert tdmodule[1] is tdmodule2
        assert tdmodule[2] is prob_module

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        tdmodule(td)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

        dist = tdmodule.get_dist(td)
        assert dist.rsample().shape[: td.ndimension()] == td.shape

    @pytest.mark.parametrize("lazy", [True, False])
    def test_stateful_probabilistic(self, lazy):
        torch.manual_seed(0)
        param_multiplier = 2
        if lazy:
            net1 = nn.LazyLinear(4)
            dummy_net = nn.LazyLinear(4)
            net2 = nn.LazyLinear(4 * param_multiplier)
        else:
            net1 = nn.Linear(3, 4)
            dummy_net = nn.Linear(4, 4)
            net2 = nn.Linear(4, 4 * param_multiplier)

        kwargs = {"distribution_class": Normal}
        tdmodule1 = TensorDictModule(net1, in_keys=["in"], out_keys=["hidden"])
        dummy_tdmodule = TensorDictModule(
            dummy_net, in_keys=["hidden"], out_keys=["hidden"]
        )
        tdmodule2 = TensorDictModule(net2, in_keys=["hidden"], out_keys=["params"])

        normal_params = TensorDictModule(
            NormalParamExtractor(), in_keys=["params"], out_keys=["loc", "scale"]
        )
        prob_module = ProbabilisticTensorDictModule(
            in_keys=["loc", "scale"], out_keys=["out"], **kwargs
        )
        tdmodule = ProbabilisticTensorDictSequential(
            tdmodule1, dummy_tdmodule, tdmodule2, normal_params, prob_module
        )

        assert hasattr(tdmodule, "__setitem__")
        assert len(tdmodule) == 5
        tdmodule[1] = tdmodule2
        tdmodule[2] = normal_params
        tdmodule[3] = prob_module
        assert len(tdmodule) == 5

        assert hasattr(tdmodule, "__delitem__")
        assert len(tdmodule) == 5
        del tdmodule[4]
        assert len(tdmodule) == 4

        assert hasattr(tdmodule, "__getitem__")
        assert tdmodule[0] is tdmodule1
        assert tdmodule[1] is tdmodule2
        assert tdmodule[2] is normal_params
        assert tdmodule[3] is prob_module

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        tdmodule(td)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

        dist = tdmodule.get_dist(td)
        assert dist.rsample().shape[: td.ndimension()] == td.shape

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional(self):
        torch.manual_seed(0)
        param_multiplier = 1

        net1 = nn.Linear(3, 4)
        dummy_net = nn.Linear(4, 4)
        net2 = nn.Linear(4, 4 * param_multiplier)

        tdmodule1 = TensorDictModule(net1, in_keys=["in"], out_keys=["hidden"])
        dummy_tdmodule = TensorDictModule(
            dummy_net, in_keys=["hidden"], out_keys=["hidden"]
        )
        tdmodule2 = TensorDictModule(net2, in_keys=["hidden"], out_keys=["out"])
        tdmodule = TensorDictSequential(tdmodule1, dummy_tdmodule, tdmodule2)

        params = make_functional(tdmodule)

        assert hasattr(tdmodule, "__setitem__")
        assert len(tdmodule) == 3
        tdmodule[1] = tdmodule2
        params["module", "1"] = params["module", "2"]
        assert len(tdmodule) == 3

        assert hasattr(tdmodule, "__delitem__")
        assert len(tdmodule) == 3
        del tdmodule[2]
        del params["module", "2"]
        assert len(tdmodule) == 2

        assert hasattr(tdmodule, "__getitem__")
        assert tdmodule[0] is tdmodule1
        assert tdmodule[1] is tdmodule2

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        tdmodule(td, params)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_functorch(self):
        torch.manual_seed(0)
        param_multiplier = 1

        net1 = nn.Linear(3, 4)
        dummy_net = nn.Linear(4, 4)
        net2 = nn.Linear(4, 4 * param_multiplier)

        tdmodule1 = TensorDictModule(net1, in_keys=["in"], out_keys=["hidden"])
        dummy_tdmodule = TensorDictModule(
            dummy_net, in_keys=["hidden"], out_keys=["hidden"]
        )
        tdmodule2 = TensorDictModule(net2, in_keys=["hidden"], out_keys=["out"])
        tdmodule = TensorDictSequential(tdmodule1, dummy_tdmodule, tdmodule2)

        ftdmodule, params, buffers = make_functional_functorch(tdmodule)

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        ftdmodule(params, buffers, td)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_probabilistic_deprec(self):
        torch.manual_seed(0)
        param_multiplier = 2

        net1 = nn.Linear(3, 4)
        dummy_net = nn.Linear(4, 4)
        net2 = nn.Linear(4, 4 * param_multiplier)
        net2 = NormalParamWrapper(net2)

        tdmodule1 = TensorDictModule(net1, in_keys=["in"], out_keys=["hidden"])
        dummy_tdmodule = TensorDictModule(
            dummy_net, in_keys=["hidden"], out_keys=["hidden"]
        )
        tdmodule2 = TensorDictModule(
            net2, in_keys=["hidden"], out_keys=["loc", "scale"]
        )

        kwargs = {"distribution_class": Normal}
        prob_module = ProbabilisticTensorDictModule(
            out_keys=["out"],
            in_keys=["loc", "scale"],
            **kwargs,
        )
        tdmodule = ProbabilisticTensorDictSequential(
            tdmodule1, dummy_tdmodule, tdmodule2, prob_module
        )

        params = make_functional(tdmodule, funs_to_decorate=["forward", "get_dist"])

        assert hasattr(tdmodule, "__setitem__")
        assert len(tdmodule) == 4
        tdmodule[1] = tdmodule2
        tdmodule[2] = prob_module
        params["module", "1"] = params["module", "2"]
        params["module", "2"] = params["module", "3"]
        assert len(tdmodule) == 4

        assert hasattr(tdmodule, "__delitem__")
        assert len(tdmodule) == 4
        del tdmodule[3]
        del params["module", "3"]
        assert len(tdmodule) == 3

        assert hasattr(tdmodule.module, "__getitem__")
        assert tdmodule[0] is tdmodule1
        assert tdmodule[1] is tdmodule2
        assert tdmodule[2] is prob_module

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        tdmodule(td, params=params)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

        dist = tdmodule.get_dist(td, params=params)
        assert dist.rsample().shape[: td.ndimension()] == td.shape

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_probabilistic(self):
        torch.manual_seed(0)
        param_multiplier = 2

        net1 = nn.Linear(3, 4)
        dummy_net = nn.Linear(4, 4)
        net2 = nn.Linear(4, 4 * param_multiplier)

        tdmodule1 = TensorDictModule(net1, in_keys=["in"], out_keys=["hidden"])
        dummy_tdmodule = TensorDictModule(
            dummy_net, in_keys=["hidden"], out_keys=["hidden"]
        )
        tdmodule2 = TensorDictModule(net2, in_keys=["hidden"], out_keys=["params"])

        normal_params = TensorDictModule(
            NormalParamExtractor(), in_keys=["params"], out_keys=["loc", "scale"]
        )
        kwargs = {"distribution_class": Normal}
        prob_module = ProbabilisticTensorDictModule(
            out_keys=["out"],
            in_keys=["loc", "scale"],
            **kwargs,
        )
        tdmodule = ProbabilisticTensorDictSequential(
            tdmodule1, dummy_tdmodule, tdmodule2, normal_params, prob_module
        )

        params = make_functional(tdmodule, funs_to_decorate=["forward", "get_dist"])

        assert hasattr(tdmodule, "__setitem__")
        assert len(tdmodule) == 5
        tdmodule[1] = tdmodule2
        tdmodule[2] = normal_params
        tdmodule[3] = prob_module
        params["module", "1"] = params["module", "2"]
        params["module", "2"] = params["module", "3"]
        params["module", "3"] = params["module", "4"]
        assert len(tdmodule) == 5

        assert hasattr(tdmodule, "__delitem__")
        assert len(tdmodule) == 5
        del tdmodule[4]
        del params["module", "4"]
        assert len(tdmodule) == 4

        assert hasattr(tdmodule.module, "__getitem__")
        assert tdmodule[0] is tdmodule1
        assert tdmodule[1] is tdmodule2
        assert tdmodule[2] is normal_params
        assert tdmodule[3] is prob_module

        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        tdmodule(td, params=params)
        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 4])

        dist = tdmodule.get_dist(td, params=params)
        assert dist.rsample().shape[: td.ndimension()] == td.shape

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_with_buffer(self):
        torch.manual_seed(0)
        param_multiplier = 1

        net1 = nn.Sequential(nn.Linear(7, 7), nn.BatchNorm1d(7))
        dummy_net = nn.Sequential(nn.Linear(7, 7), nn.BatchNorm1d(7))
        net2 = nn.Sequential(
            nn.Linear(7, 7 * param_multiplier), nn.BatchNorm1d(7 * param_multiplier)
        )

        tdmodule1 = TensorDictModule(net1, in_keys=["in"], out_keys=["hidden"])
        dummy_tdmodule = TensorDictModule(
            dummy_net, in_keys=["hidden"], out_keys=["hidden"]
        )
        tdmodule2 = TensorDictModule(net2, in_keys=["hidden"], out_keys=["out"])
        tdmodule = TensorDictSequential(tdmodule1, dummy_tdmodule, tdmodule2)

        params = make_functional(tdmodule)

        assert hasattr(tdmodule, "__setitem__")
        assert len(tdmodule) == 3
        tdmodule[1] = tdmodule2
        params["module", "1"] = params["module", "2"]
        assert len(tdmodule) == 3

        assert hasattr(tdmodule, "__delitem__")
        assert len(tdmodule) == 3
        del tdmodule[2]
        del params["module", "2"]
        assert len(tdmodule) == 2

        assert hasattr(tdmodule, "__getitem__")
        assert tdmodule[0] is tdmodule1
        assert tdmodule[1] is tdmodule2

        td = TensorDict({"in": torch.randn(3, 7)}, [3])
        tdmodule(td, params=params)

        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 7])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_with_buffer_probabilistic_deprec(self):
        torch.manual_seed(0)
        param_multiplier = 2

        net1 = nn.Sequential(nn.Linear(7, 7), nn.BatchNorm1d(7))
        dummy_net = nn.Sequential(nn.Linear(7, 7), nn.BatchNorm1d(7))
        net2 = nn.Sequential(
            nn.Linear(7, 7 * param_multiplier), nn.BatchNorm1d(7 * param_multiplier)
        )
        net2 = NormalParamWrapper(net2)

        tdmodule1 = TensorDictModule(net1, in_keys=["in"], out_keys=["hidden"])
        dummy_tdmodule = TensorDictModule(
            dummy_net, in_keys=["hidden"], out_keys=["hidden"]
        )
        tdmodule2 = TensorDictModule(
            net2, in_keys=["hidden"], out_keys=["loc", "scale"]
        )

        kwargs = {"distribution_class": Normal}
        prob_module = ProbabilisticTensorDictModule(
            in_keys=["loc", "scale"],
            out_keys=["out"],
            **kwargs,
        )

        tdmodule = ProbabilisticTensorDictSequential(
            tdmodule1, dummy_tdmodule, tdmodule2, prob_module
        )

        params = make_functional(tdmodule, ["forward", "get_dist"])

        assert hasattr(tdmodule.module, "__setitem__")
        assert len(tdmodule.module) == 4
        tdmodule[1] = tdmodule2
        tdmodule[2] = prob_module
        params["module", "1"] = params["module", "2"]
        params["module", "2"] = params["module", "3"]
        assert len(tdmodule) == 4

        assert hasattr(tdmodule.module, "__delitem__")
        assert len(tdmodule.module) == 4
        del tdmodule.module[3]
        del params["module", "3"]
        assert len(tdmodule.module) == 3

        assert hasattr(tdmodule.module, "__getitem__")
        assert tdmodule[0] is tdmodule1
        assert tdmodule[1] is tdmodule2
        assert tdmodule[2] is prob_module

        td = TensorDict({"in": torch.randn(3, 7)}, [3])
        tdmodule(td, params=params)

        dist = tdmodule.get_dist(td, params=params)
        assert dist.rsample().shape[: td.ndimension()] == td.shape

        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 7])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_functional_with_buffer_probabilistic(self):
        torch.manual_seed(0)
        param_multiplier = 2

        net1 = nn.Sequential(nn.Linear(7, 7), nn.BatchNorm1d(7))
        dummy_net = nn.Sequential(nn.Linear(7, 7), nn.BatchNorm1d(7))
        net2 = nn.Sequential(
            nn.Linear(7, 7 * param_multiplier), nn.BatchNorm1d(7 * param_multiplier)
        )

        tdmodule1 = TensorDictModule(net1, in_keys=["in"], out_keys=["hidden"])
        dummy_tdmodule = TensorDictModule(
            dummy_net, in_keys=["hidden"], out_keys=["hidden"]
        )
        tdmodule2 = TensorDictModule(net2, in_keys=["hidden"], out_keys=["params"])

        normal_params = TensorDictModule(
            NormalParamExtractor(), in_keys=["params"], out_keys=["loc", "scale"]
        )
        kwargs = {"distribution_class": Normal}
        prob_module = ProbabilisticTensorDictModule(
            in_keys=["loc", "scale"],
            out_keys=["out"],
            **kwargs,
        )

        tdmodule = ProbabilisticTensorDictSequential(
            tdmodule1, dummy_tdmodule, tdmodule2, normal_params, prob_module
        )

        params = make_functional(tdmodule, ["forward", "get_dist"])

        assert hasattr(tdmodule.module, "__setitem__")
        assert len(tdmodule.module) == 5
        tdmodule[1] = tdmodule2
        tdmodule[2] = normal_params
        tdmodule[3] = prob_module
        params["module", "1"] = params["module", "2"]
        params["module", "2"] = params["module", "3"]
        params["module", "3"] = params["module", "4"]
        assert len(tdmodule) == 5

        assert hasattr(tdmodule.module, "__delitem__")
        assert len(tdmodule.module) == 5
        del tdmodule.module[4]
        del params["module", "4"]
        assert len(tdmodule.module) == 4

        assert hasattr(tdmodule.module, "__getitem__")
        assert tdmodule[0] is tdmodule1
        assert tdmodule[1] is tdmodule2
        assert tdmodule[2] is normal_params
        assert tdmodule[3] is prob_module

        td = TensorDict({"in": torch.randn(3, 7)}, [3])
        tdmodule(td, params=params)

        dist = tdmodule.get_dist(td, params=params)
        assert dist.rsample().shape[: td.ndimension()] == td.shape

        assert td.shape == torch.Size([3])
        assert td.get("out").shape == torch.Size([3, 7])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_vmap(self):
        torch.manual_seed(0)
        param_multiplier = 1

        net1 = nn.Linear(3, 4)
        dummy_net = nn.Linear(4, 4)
        net2 = nn.Linear(4, 4 * param_multiplier)

        tdmodule1 = TensorDictModule(net1, in_keys=["in"], out_keys=["hidden"])
        dummy_tdmodule = TensorDictModule(
            dummy_net, in_keys=["hidden"], out_keys=["hidden"]
        )
        tdmodule2 = TensorDictModule(net2, in_keys=["hidden"], out_keys=["out"])
        tdmodule = TensorDictSequential(tdmodule1, dummy_tdmodule, tdmodule2)

        params = make_functional(tdmodule)

        assert hasattr(tdmodule, "__setitem__")
        assert len(tdmodule) == 3
        tdmodule[1] = tdmodule2
        params["module", "1"] = params["module", "2"]
        assert len(tdmodule) == 3

        assert hasattr(tdmodule, "__delitem__")
        assert len(tdmodule) == 3
        del tdmodule[2]
        del params["module", "2"]
        assert len(tdmodule) == 2

        assert hasattr(tdmodule, "__getitem__")
        assert tdmodule[0] is tdmodule1
        assert tdmodule[1] is tdmodule2

        # vmap = True
        params = params.expand(10)
        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        td_out = vmap(tdmodule, (None, 0))(td, params)
        assert td_out is not td
        assert td_out.shape == torch.Size([10, 3])
        assert td_out.get("out").shape == torch.Size([10, 3, 4])

        # vmap = (0, 0)
        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        td_repeat = td.expand(10, *td.batch_size)
        td_out = vmap(tdmodule, (0, 0))(td_repeat, params)
        assert td_out is not td_repeat
        assert td_out.shape == torch.Size([10, 3])
        assert td_out.get("out").shape == torch.Size([10, 3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_vmap_probabilistic_deprec(self):
        torch.manual_seed(0)
        param_multiplier = 2

        net1 = nn.Linear(3, 4)

        net2 = nn.Linear(4, 4 * param_multiplier)
        net2 = NormalParamWrapper(net2)

        kwargs = {"distribution_class": Normal}
        tdmodule1 = TensorDictModule(net1, in_keys=["in"], out_keys=["hidden"])
        tdmodule2 = TensorDictModule(
            net2, in_keys=["hidden"], out_keys=["loc", "scale"]
        )
        tdmodule = ProbabilisticTensorDictSequential(
            tdmodule1,
            tdmodule2,
            ProbabilisticTensorDictModule(
                out_keys=["out"],
                in_keys=["loc", "scale"],
                **kwargs,
            ),
        )

        params = make_functional(tdmodule)

        # vmap = True
        params = params.expand(10)
        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        td_out = vmap(tdmodule, (None, 0))(td, params)
        assert td_out is not td
        assert td_out.shape == torch.Size([10, 3])
        assert td_out.get("out").shape == torch.Size([10, 3, 4])

        # vmap = (0, 0)
        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        td_repeat = td.expand(10, *td.batch_size)
        td_out = vmap(tdmodule, (0, 0))(td_repeat, params)
        assert td_out is not td_repeat
        assert td_out.shape == torch.Size([10, 3])
        assert td_out.get("out").shape == torch.Size([10, 3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    def test_vmap_probabilistic(self):
        torch.manual_seed(0)
        param_multiplier = 2

        net1 = nn.Linear(3, 4)
        net2 = nn.Linear(4, 4 * param_multiplier)

        kwargs = {"distribution_class": Normal}
        tdmodule1 = TensorDictModule(net1, in_keys=["in"], out_keys=["hidden"])
        tdmodule2 = TensorDictModule(net2, in_keys=["hidden"], out_keys=["params"])
        normal_params = TensorDictModule(
            NormalParamExtractor(), in_keys=["params"], out_keys=["loc", "scale"]
        )
        tdmodule = ProbabilisticTensorDictSequential(
            tdmodule1,
            tdmodule2,
            normal_params,
            ProbabilisticTensorDictModule(
                out_keys=["out"], in_keys=["loc", "scale"], **kwargs
            ),
        )

        params = make_functional(tdmodule)

        # vmap = True
        params = params.expand(10)
        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        td_out = vmap(tdmodule, (None, 0))(td, params)
        assert td_out is not td
        assert td_out.shape == torch.Size([10, 3])
        assert td_out.get("out").shape == torch.Size([10, 3, 4])

        # vmap = (0, 0)
        td = TensorDict({"in": torch.randn(3, 3)}, [3])
        td_repeat = td.expand(10, *td.batch_size)
        td_out = vmap(tdmodule, (0, 0))(td_repeat, params)
        assert td_out is not td_repeat
        assert td_out.shape == torch.Size([10, 3])
        assert td_out.get("out").shape == torch.Size([10, 3, 4])

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    @pytest.mark.parametrize("functional", [True, False])
    def test_submodule_sequence(self, functional):
        td_module_1 = TensorDictModule(
            nn.Linear(3, 2), in_keys=["in"], out_keys=["hidden"]
        )
        td_module_2 = TensorDictModule(
            nn.Linear(2, 4), in_keys=["hidden"], out_keys=["out"]
        )
        td_module = TensorDictSequential(td_module_1, td_module_2)

        if functional:
            td_1 = TensorDict({"in": torch.randn(5, 3)}, [5])
            sub_seq_1 = td_module.select_subsequence(out_keys=["hidden"])
            params = make_functional(sub_seq_1)
            sub_seq_1(td_1, params=params)
            assert "hidden" in td_1.keys()
            assert "out" not in td_1.keys()
            td_2 = TensorDict({"hidden": torch.randn(5, 2)}, [5])
            sub_seq_2 = td_module.select_subsequence(in_keys=["hidden"])
            params = make_functional(sub_seq_2)
            sub_seq_2(td_2, params=params)
            assert "out" in td_2.keys()
            assert td_2.get("out").shape == torch.Size([5, 4])
        else:
            td_1 = TensorDict({"in": torch.randn(5, 3)}, [5])
            sub_seq_1 = td_module.select_subsequence(out_keys=["hidden"])
            sub_seq_1(td_1)
            assert "hidden" in td_1.keys()
            assert "out" not in td_1.keys()
            td_2 = TensorDict({"hidden": torch.randn(5, 2)}, [5])
            sub_seq_2 = td_module.select_subsequence(in_keys=["hidden"])
            sub_seq_2(td_2)
            assert "out" in td_2.keys()
            assert td_2.get("out").shape == torch.Size([5, 4])

    @pytest.mark.parametrize(
        "in_keys", [None, ("a",), ("b",), ("d",), ("b", "d"), ("a", "d")]
    )
    @pytest.mark.parametrize(
        "out_keys",
        [None, ("b",), ("c",), ("d",), ("e",), ("b", "c"), ("b", "d"), ("b", "e")],
    )
    def test_submodule_sequence_nested(self, in_keys, out_keys):
        Seq = TensorDictSequential
        Mod = TensorDictModule
        idn = lambda x: x + 1
        module_1 = Seq(
            Mod(idn, in_keys=["a"], out_keys=["b"]),
            Mod(idn, in_keys=["b"], out_keys=["c"]),
            Mod(idn, in_keys=["b"], out_keys=["d"]),
            Mod(idn, in_keys=["d"], out_keys=["e"]),
        )
        module_2 = Seq(
            Seq(
                Mod(idn, in_keys=["a"], out_keys=["b"]),
                Mod(idn, in_keys=["b"], out_keys=["c"]),
            ),
            Seq(
                Mod(idn, in_keys=["b"], out_keys=["d"]),
                Mod(idn, in_keys=["d"], out_keys=["e"]),
            ),
        )
        try:
            sel_module_1 = module_1.select_subsequence(
                in_keys=in_keys, out_keys=out_keys
            )
        except ValueError:
            # incongruent keys
            return
        sel_module_2 = module_2.select_subsequence(in_keys=in_keys, out_keys=out_keys)
        td = TensorDict(
            {
                "a": torch.zeros(()),
                "b": torch.zeros(()),
                "c": torch.zeros(()),
                "d": torch.zeros(()),
                "e": torch.zeros(()),
            },
            [],
        )
        assert (sel_module_1(td.clone()) == sel_module_2(td.clone())).all()

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    @pytest.mark.parametrize("stack", [True, False])
    @pytest.mark.parametrize("functional", [True, False])
    def test_sequential_partial_deprec(self, stack, functional):
        torch.manual_seed(0)
        param_multiplier = 2

        net1 = nn.Linear(3, 4)

        net2 = nn.Linear(4, 4 * param_multiplier)
        net2 = NormalParamWrapper(net2)
        net2 = TensorDictModule(net2, in_keys=["b"], out_keys=["loc", "scale"])

        net3 = nn.Linear(4, 4 * param_multiplier)
        net3 = NormalParamWrapper(net3)
        net3 = TensorDictModule(net3, in_keys=["c"], out_keys=["loc", "scale"])

        kwargs = {"distribution_class": Normal}

        tdmodule1 = TensorDictModule(net1, in_keys=["a"], out_keys=["hidden"])
        tdmodule2 = ProbabilisticTensorDictSequential(
            net2,
            ProbabilisticTensorDictModule(
                out_keys=["out"], in_keys=["loc", "scale"], **kwargs
            ),
        )
        tdmodule3 = ProbabilisticTensorDictSequential(
            net3,
            ProbabilisticTensorDictModule(
                out_keys=["out"], in_keys=["loc", "scale"], **kwargs
            ),
        )
        tdmodule = TensorDictSequential(
            tdmodule1, tdmodule2, tdmodule3, partial_tolerant=True
        )

        if functional:
            params = make_functional(tdmodule)
        else:
            params = None

        if stack:
            td = torch.stack(
                [
                    TensorDict({"a": torch.randn(3), "b": torch.randn(4)}, []),
                    TensorDict({"a": torch.randn(3), "c": torch.randn(4)}, []),
                ],
                0,
            )
            if functional:
                tdmodule(td, params=params)
            else:
                tdmodule(td)
            assert "loc" in td.keys()
            assert "scale" in td.keys()
            assert "out" in td.keys()
            assert td["out"].shape[0] == 2
            assert td["loc"].shape[0] == 2
            assert td["scale"].shape[0] == 2
            assert "b" not in td.keys()
            assert "b" in td[0].keys()
        else:
            td = TensorDict({"a": torch.randn(3), "b": torch.randn(4)}, [])
            if functional:
                tdmodule(td, params=params)
            else:
                tdmodule(td)
            assert "loc" in td.keys()
            assert "scale" in td.keys()
            assert "out" in td.keys()
            assert "b" in td.keys()

    @pytest.mark.skipif(
        not _has_functorch, reason=f"functorch not found: err={FUNCTORCH_ERR}"
    )
    @pytest.mark.parametrize("stack", [True, False])
    @pytest.mark.parametrize("functional", [True, False])
    def test_sequential_partial(self, stack, functional):
        torch.manual_seed(0)
        param_multiplier = 2

        net1 = nn.Linear(3, 4)

        net2 = nn.Linear(4, 4 * param_multiplier)
        net2 = TensorDictModule(net2, in_keys=["b"], out_keys=["params2"])

        net3 = nn.Linear(4, 4 * param_multiplier)
        net3 = TensorDictModule(net3, in_keys=["c"], out_keys=["params3"])

        kwargs = {"distribution_class": Normal}

        tdmodule1 = TensorDictModule(net1, in_keys=["a"], out_keys=["hidden"])
        tdmodule2 = ProbabilisticTensorDictSequential(
            net2,
            TensorDictModule(
                NormalParamExtractor(), in_keys=["params2"], out_keys=["loc", "scale"]
            ),
            ProbabilisticTensorDictModule(
                out_keys=["out"], in_keys=["loc", "scale"], **kwargs
            ),
        )
        tdmodule3 = ProbabilisticTensorDictSequential(
            net3,
            TensorDictModule(
                NormalParamExtractor(), in_keys=["params3"], out_keys=["loc", "scale"]
            ),
            ProbabilisticTensorDictModule(
                out_keys=["out"], in_keys=["loc", "scale"], **kwargs
            ),
        )
        tdmodule = TensorDictSequential(
            tdmodule1, tdmodule2, tdmodule3, partial_tolerant=True
        )

        if functional:
            params = make_functional(tdmodule)
        else:
            params = None

        if stack:
            td = torch.stack(
                [
                    TensorDict({"a": torch.randn(3), "b": torch.randn(4)}, []),
                    TensorDict({"a": torch.randn(3), "c": torch.randn(4)}, []),
                ],
                0,
            )
            if functional:
                tdmodule(td, params=params)
            else:
                tdmodule(td)
            assert "loc" in td.keys()
            assert "scale" in td.keys()
            assert "out" in td.keys()
            assert td["out"].shape[0] == 2
            assert td["loc"].shape[0] == 2
            assert td["scale"].shape[0] == 2
            assert "b" not in td.keys()
            assert "b" in td[0].keys()
        else:
            td = TensorDict({"a": torch.randn(3), "b": torch.randn(4)}, [])
            if functional:
                tdmodule(td, params=params)
            else:
                tdmodule(td)
            assert "loc" in td.keys()
            assert "scale" in td.keys()
            assert "out" in td.keys()
            assert "b" in td.keys()

    def test_subsequence_weight_update(self):
        td_module_1 = TensorDictModule(
            nn.Linear(3, 2), in_keys=["in"], out_keys=["hidden"]
        )
        td_module_2 = TensorDictModule(
            nn.Linear(2, 4), in_keys=["hidden"], out_keys=["out"]
        )
        td_module = TensorDictSequential(td_module_1, td_module_2)

        td_1 = TensorDict({"in": torch.randn(5, 3)}, [5])
        sub_seq_1 = td_module.select_subsequence(out_keys=["hidden"])
        copy = sub_seq_1[0].module.weight.clone()

        opt = torch.optim.SGD(td_module.parameters(), lr=0.1)
        opt.zero_grad()
        td_1 = td_module(td_1)
        td_1["out"].mean().backward()
        opt.step()

        assert not torch.allclose(copy, sub_seq_1[0].module.weight)
        assert torch.allclose(td_module[0].module.weight, sub_seq_1[0].module.weight)


@pytest.mark.parametrize(
    "interaction_type", [InteractionType.RANDOM, InteractionType.MODE]
)
class TestSIM:
    def test_cm(self, interaction_type):
        with set_interaction_type(interaction_type):
            assert nn_probabilistic._INTERACTION_TYPE == interaction_type

    def test_dec(self, interaction_type):
        @set_interaction_type(interaction_type)
        def dummy():
            assert nn_probabilistic._INTERACTION_TYPE == interaction_type

        dummy()


def test_probabilistic_sequential_type_checks():
    td_module_1 = TensorDictModule(nn.Linear(3, 2), in_keys=["in"], out_keys=["hidden"])
    td_module_2 = TensorDictModule(
        nn.Linear(2, 4), in_keys=["hidden"], out_keys=["out"]
    )
    with pytest.raises(
        TypeError,
        match="The final module passed to ProbabilisticTensorDictSequential",
    ):
        ProbabilisticTensorDictSequential(td_module_1, td_module_2)


def test_keyerr_msg():
    module = TensorDictModule(nn.Linear(2, 3), in_keys=["a"], out_keys=["b"])
    with pytest.raises(
        RuntimeError, match="TensorDictModule failed with operation"
    ) as err:
        module(TensorDict({"c": torch.randn(())}, []))
    assert "Some tensors that are necessary for the module call" in str(
        err.value.__cause__
    )


def test_input():
    class MyModule(nn.Module):
        pass

    def mycallable():
        pass

    # this should work
    for module in [MyModule(), mycallable]:
        TensorDictModule(module, in_keys=["i"], out_keys=["o"])
        TensorDictModule(module, in_keys=["i", "i2"], out_keys=["o"])
        TensorDictModule(module, in_keys=["i"], out_keys=["o", "o2"])
        TensorDictModule(module, in_keys=["i", "i2"], out_keys=["o", "o2"])
        TensorDictModule(module, in_keys=[tuple("i")], out_keys=[tuple("o")])
        TensorDictModule(module, in_keys=[("i", "i2")], out_keys=[tuple("o")])
        TensorDictModule(module, in_keys=[tuple("i")], out_keys=[("o", "o2")])
        TensorDictModule(module, in_keys=[("i", "i2")], out_keys=[("o", "o2")])

        # corner cases that should work
        TensorDictModule(module, in_keys=[("_", "")], out_keys=[("_", "")])
        TensorDictModule(module, in_keys=[("_", "")], out_keys=[("a", "a")])
        TensorDictModule(module, in_keys=[""], out_keys=["_"])
        TensorDictModule(module, in_keys=["_"], out_keys=[""])

    # this should raise
    for wrong_model in (MyModule, int, [123], 1, torch.randn(2)):
        with pytest.raises(ValueError, match=r"Module .* is not callable"):
            TensorDictModule(wrong_model, in_keys=["in"], out_keys=["out"])

    # missing or wrong keys
    for wrong_keys in (None, 123, [123], [(("too", "much", "nesting"),)]):
        with pytest.raises(ValueError, match="seq should be a Sequence"):
            TensorDictModule(MyModule(), in_keys=["in"], out_keys=wrong_keys)

        with pytest.raises(ValueError, match="seq should be a Sequence"):
            TensorDictModule(MyModule(), in_keys=wrong_keys, out_keys=["out"])


def test_method_forward():
    # ensure calls to custom methods are correctly forwarded to wrapped module
    from unittest.mock import MagicMock

    class MyModule(nn.Module):
        def mycustommethod(self):
            pass

        def overwrittenmethod(self):
            pass

    MyModule.mycustommethod = MagicMock()
    MyModule.overwrittenmethod = MagicMock()

    module = TensorDictModule(MyModule(), in_keys=["in"], out_keys=["out"])
    module.mycustommethod()
    assert MyModule.mycustommethod.called

    module.mycustommethod()
    assert not MyModule.overwrittenmethod.called


class TestMakeFunctional:
    @pytest.mark.parametrize("keep_params", [True, False])
    @pytest.mark.parametrize("return_params", [True, False])
    def test_is_functional(self, return_params, keep_params):
        module = nn.Sequential(
            nn.ModuleList(
                [
                    nn.Linear(3, 3),
                    nn.Dropout(0.1),
                ]
            ),
            nn.Transformer(16),
        )
        for m in module.modules():
            assert not is_functional(m)
        make_functional(module, keep_params=keep_params, return_params=return_params)
        for m in module.modules():
            assert is_functional(m)

    @pytest.mark.parametrize("keep_params", [True, False])
    @pytest.mark.parametrize("return_params", [True, False])
    @torch.no_grad()
    def test_make_functional(self, return_params, keep_params):
        module = nn.Sequential(
            nn.Linear(3, 3),
            nn.Linear(3, 3),
        )
        td = TensorDict(
            {
                "0": {"weight": torch.zeros(3, 3), "bias": torch.zeros(3)},
                "1": {"weight": torch.zeros(3, 3), "bias": torch.zeros(3)},
            },
            [],
        )
        params = make_functional(
            module, keep_params=keep_params, return_params=return_params
        )
        if return_params:
            assert (params.zero_() == td).all()
        else:
            assert params is None
        if keep_params:
            for m in module.modules():
                assert not m._is_stateless, m
            assert module(torch.randn(3)).shape == torch.Size([3])
            for m in module.modules():
                assert not m._is_stateless, m
        else:
            for m in module.modules():
                assert m._is_stateless, m
            assert module(torch.randn(3), params=td).shape == torch.Size([3])
            for m in module.modules():
                assert m._is_stateless, m

        assert module(torch.randn(3), params=td).shape == torch.Size([3])

    def test_make_functional_twice(self):
        model = nn.Linear(3, 4)
        make_functional(model)
        assert model._is_stateless
        make_functional(model, keep_params=True, return_params=False)
        assert model._is_stateless
        with pytest.raises(
            RuntimeError, match="Calling make_functional with return_params=True"
        ):
            make_functional(model, keep_params=True, return_params=True)
        assert model._is_stateless

        model = nn.Linear(3, 4)
        make_functional(model, keep_params=True, return_params=False)
        assert not model._is_stateless
        make_functional(model, keep_params=True, return_params=True)
        assert not model._is_stateless
        make_functional(model)
        assert model._is_stateless


class TestMakeFunctionalVmap:
    def TDMBase(self, extra_kwargs):
        if not extra_kwargs:

            def _forward(self, tensordict):
                tensordict[self.out_keys[0]] = self.linear(tensordict[self.in_keys[0]])
                return tensordict

        else:

            def _forward(self, tensordict, extra=None):
                tensordict[self.out_keys[0]] = self.linear(tensordict[self.in_keys[0]])
                return tensordict

        class MyModule(TensorDictModuleBase):
            in_keys = ["a"]
            out_keys = ["b"]

            def __init__(self):
                super().__init__()
                self.linear = nn.Linear(4, 4)

            forward = _forward

        return MyModule()

    def nnModule(self, extra_kwargs):
        if not extra_kwargs:

            def _forward(self, tensordict):
                tensordict[self.out_keys[0]] = self.linear(tensordict[self.in_keys[0]])
                return tensordict

        else:

            def _forward(self, tensordict, extra=None):
                tensordict[self.out_keys[0]] = self.linear(tensordict[self.in_keys[0]])
                return tensordict

        class MyModule(nn.Module):
            in_keys = ["a"]
            out_keys = ["b"]

            def __init__(self):
                super().__init__()
                self.linear = nn.Linear(4, 4)

            forward = _forward

        return MyModule()

    def TDM(self, extra_kwargs):
        if extra_kwargs:

            class MyLinear(nn.Linear):
                def forward(self, x, extra=None):
                    return super().forward(x)

        else:
            MyLinear = nn.Linear

        class MyModule(TensorDictModule):
            def __init__(self):
                in_keys = ["a"]
                out_keys = ["b"]
                linear = MyLinear(4, 4)
                super().__init__(linear, in_keys, out_keys)

        return MyModule()

    @property
    def td(self):
        return TensorDict({"a": torch.ones(3, 4)}, [3])

    @property
    def td_zero(self):
        return TensorDict({"a": torch.ones(3, 4), "b": torch.zeros(3, 4)}, [3])

    @pytest.mark.parametrize("module_type", ["TDMBase", "nnModule", "TDM"])
    @pytest.mark.parametrize("stateless", [True, False])
    @pytest.mark.parametrize("keyword", [True, False])
    @pytest.mark.parametrize("extra_kwargs", [True, False])
    def test_make_func(self, module_type, stateless, keyword, extra_kwargs):
        module = getattr(self, module_type)(extra_kwargs)
        params = make_functional(module, keep_params=not stateless)
        if not stateless:
            td = self.td
            if module_type != "nnModule":
                with warnings.catch_warnings():
                    module(td)
            else:
                # users are told to use TensorDictModuleBase
                with pytest.warns(UserWarning):
                    module(td)
            assert td.shape == torch.Size([3])
        params = params.clone()
        params.zero_()
        td = self.td
        if not keyword:
            if not stateless and module_type == "nnModule":
                with pytest.raises(TypeError, match="It seems you tried to provide"):
                    if extra_kwargs:
                        _ = module(td, params, extra=None)
                    else:
                        _ = module(td, params)
                return
            tdout = module(td, params)
            assert (tdout == self.td_zero).all()
        else:
            tdout = module(td, params=params)
            assert (tdout == self.td_zero).all(), tdout

    @pytest.mark.parametrize("module_type", ["TDMBase", "nnModule", "TDM"])
    @pytest.mark.parametrize("stateless", [True, False])
    @pytest.mark.parametrize("keyword", [True, False])
    @pytest.mark.parametrize("extra_kwargs", [True, False])
    def test_make_func_vmap(self, module_type, stateless, keyword, extra_kwargs):
        module = getattr(self, module_type)(extra_kwargs)
        params = make_functional(module, keep_params=not stateless)
        params = params.expand(5).to_tensordict()
        params.zero_()
        td = self.td.expand(5, 3).to_tensordict()
        if not keyword:
            if not stateless and module_type == "nnModule":
                with pytest.raises(TypeError, match="It seems you tried to provide"):
                    if extra_kwargs:
                        _ = vmap(module)(td, params, extra=None)
                    else:
                        _ = vmap(module)(td, params)
                return
            tdout = vmap(module)(td, params)
            assert (tdout == self.td_zero).all()
        else:
            # this isn't supposed to work: keyword arguments are not expanded with vmap
            with pytest.raises(Exception):
                tdout = vmap(module)(td, params=params)
                assert (tdout == self.td_zero).all(), tdout


class TestSkipExisting:
    @pytest.mark.parametrize("mode", [True, False, None])
    def test_global(self, mode):
        assert skip_existing() is False
        if mode is None:
            with pytest.raises(RuntimeError, match="It seems"):
                with set_skip_existing(mode):
                    pass
            assert skip_existing() is False
            return

        with set_skip_existing(mode):
            assert skip_existing() is mode
        assert skip_existing() is False

    def test_global_with_module(self):
        class MyModule(TensorDictModuleBase):
            in_keys = []
            out_keys = ["out"]

            @set_skip_existing(None)
            def forward(self, tensordict):
                tensordict.set("out", torch.ones(()))
                return tensordict

        module = MyModule()
        td = module(TensorDict({"out": torch.zeros(())}, []))
        assert (td["out"] == 1).all()
        with set_skip_existing(True):
            td = module(TensorDict({"out": torch.zeros(())}, []))  # no print
        assert (td["out"] == 0).all()
        td = module(TensorDict({"out": torch.zeros(())}, []))
        assert (td["out"] == 1).all()

    def test_module(self):
        class MyModule(TensorDictModuleBase):
            in_keys = []
            out_keys = ["out"]

            @set_skip_existing()
            def forward(self, tensordict):
                tensordict.set("out", torch.ones(()))
                return tensordict

        module = MyModule()
        td = module(TensorDict({"out": torch.zeros(())}, []))
        assert (td["out"] == 0).all()
        td = module(TensorDict({}, []))  # prints hello
        assert (td["out"] == 1).all()

    def test_tdmodule(self):
        module = TensorDictModule(lambda x: x + 1, in_keys=["in"], out_keys=["out"])
        td = TensorDict({"in": torch.zeros(())}, [])
        module(td)
        assert (td["out"] == 1).all()

        td = TensorDict({"in": torch.zeros(()), "out": torch.zeros(())}, [])
        module(td)
        assert (td["out"] == 1).all()

        td = TensorDict({"in": torch.zeros(()), "out": torch.zeros(())}, [])
        with set_skip_existing(True):
            module(td)
        assert (td["out"] == 0).all()

        td = TensorDict({"in": torch.zeros(()), "out": torch.zeros(())}, [])
        with set_skip_existing(False):
            module(td)
        assert (td["out"] == 1).all()

    def test_tdseq(self):
        class MyModule(TensorDictModuleBase):
            in_keys = ["in"]
            out_keys = ["out"]

            def forward(self, tensordict):
                tensordict["out"] = tensordict["in"] + 1
                return tensordict

        module = TensorDictSequential(MyModule())

        td = TensorDict({"in": torch.zeros(())}, [])
        module(td)
        assert (td["out"] == 1).all()

        td = TensorDict({"in": torch.zeros(()), "out": torch.zeros(())}, [])
        module(td)
        assert (td["out"] == 1).all()

        td = TensorDict({"in": torch.zeros(()), "out": torch.zeros(())}, [])
        with set_skip_existing(True):
            module(td)
        assert (td["out"] == 0).all()

        td = TensorDict({"in": torch.zeros(()), "out": torch.zeros(())}, [])
        with set_skip_existing(False):
            module(td)
        assert (td["out"] == 1).all()


@pytest.mark.parametrize("out_d_key", [("d", "e"), ["d"], ["d", "e"]])
@pytest.mark.parametrize("unpack", [True, False])
class TestSelectOutKeys:
    def test_tdmodule(self, out_d_key, unpack):
        mod = TensorDictModule(
            lambda x, y: (x + 2, y + 2, x), in_keys=["a", "b"], out_keys=["c", "d", "e"]
        )
        assert mod.out_keys == ["c", "d", "e"]
        td = mod(TensorDict({"a": torch.zeros(()), "b": torch.ones(())}, []))
        assert all(key in td.keys() for key in ["a", "b", "c", "d", "e"])
        if unpack:
            mod2 = mod.select_out_keys(*out_d_key)
            assert mod2 is mod
            assert mod.out_keys == list(out_d_key)
            td = mod(TensorDict({"a": torch.zeros(()), "b": torch.ones(())}, []))
            assert "c" not in td.keys()
            assert all(key in td.keys() for key in ["a", "b", "d"])
            mod2 = mod.reset_out_keys()
            assert mod2 is mod
            td = mod(TensorDict({"a": torch.zeros(()), "b": torch.ones(())}, []))
            assert all(key in td.keys() for key in ["a", "b", "c", "d", "e"])
        else:
            with pytest.raises(ValueError, match="Can't select non "):
                mod2 = mod.select_out_keys(out_d_key)

    def test_tdmodule_dispatch(self, out_d_key, unpack):
        mod = TensorDictModule(
            lambda x, y: (x + 2, y + 2, x), in_keys=["a", "b"], out_keys=["c", "d", "e"]
        )
        exp_res = {"c": 2, "d": 3, "e": 0}
        res = mod(torch.zeros(()), torch.ones(()))
        assert len(res) == 3
        for i, v in enumerate(["c", "d", "e"]):
            assert (res[i] == exp_res[v]).all()
        if unpack:
            mod2 = mod.select_out_keys(*out_d_key)
            assert mod2 is mod
            assert mod.out_keys == list(out_d_key)
            res = mod(torch.zeros(()), torch.ones(()))
            if len(list(out_d_key)) == 1:
                res = [res]
            for i, v in enumerate(list(out_d_key)):
                assert (res[i] == exp_res[v]).all()
            mod2 = mod.reset_out_keys()
            assert mod2 is mod
            res = mod(torch.zeros(()), torch.ones(()))
            assert len(res) == 3
            for i, v in enumerate(["c", "d", "e"]):
                assert (res[i] == exp_res[v]).all()
        else:
            with pytest.raises(ValueError, match="Can't select non "):
                mod2 = mod.select_out_keys(out_d_key)

    def test_tdmodule_dispatch_firstcall(self, out_d_key, unpack):
        # calling the dispatch first or not may mess up the init
        mod = TensorDictModule(
            lambda x, y: (x + 2, y + 2, x), in_keys=["a", "b"], out_keys=["c", "d", "e"]
        )
        exp_res = {"c": 2, "d": 3, "e": 0}
        res = mod(torch.zeros(()), torch.ones(()))
        assert len(res) == 3
        for i, v in enumerate(["c", "d", "e"]):
            assert (res[i] == exp_res[v]).all()
        if unpack:
            mod2 = mod.select_out_keys(*out_d_key)
            assert mod2 is mod
            assert mod.out_keys == list(out_d_key)
            # ignore result but make sure we call _init
            mod(TensorDict({"a": torch.zeros(()), "b": torch.ones(())}, []))
            res = mod(torch.zeros(()), torch.ones(()))
            if len(list(out_d_key)) == 1:
                res = [res]
            for i, v in enumerate(list(out_d_key)):
                assert (res[i] == exp_res[v]).all()
            mod2 = mod.reset_out_keys()
            assert mod2 is mod
            res = mod(torch.zeros(()), torch.ones(()))
            assert len(res) == 3
            for i, v in enumerate(["c", "d", "e"]):
                assert (res[i] == exp_res[v]).all()
        else:
            with pytest.raises(ValueError, match="Can't select non "):
                mod2 = mod.select_out_keys(out_d_key)

    def test_tdseq(self, out_d_key, unpack):
        mod = TensorDictSequential(
            TensorDictModule(lambda x: x + 2, in_keys=["a"], out_keys=["c"]),
            TensorDictModule(lambda x: (x + 2, x), in_keys=["b"], out_keys=["d", "e"]),
        )
        td = mod(TensorDict({"a": torch.zeros(()), "b": torch.ones(())}, []))
        assert all(key in td.keys() for key in ["a", "b", "c", "d"])
        if unpack:
            mod2 = mod.select_out_keys(*out_d_key)
            assert mod2 is mod
            assert mod.out_keys == list(out_d_key)
            td = mod(TensorDict({"a": torch.zeros(()), "b": torch.ones(())}, []))
            assert "c" not in td.keys()
            assert all(key in td.keys() for key in ["a", "b", "d"])
            mod2 = mod.reset_out_keys()
            assert mod2 is mod
            td = mod(TensorDict({"a": torch.zeros(()), "b": torch.ones(())}, []))
            assert all(key in td.keys() for key in ["a", "b", "c", "d"])
        else:
            with pytest.raises(ValueError, match="Can't select non "):
                mod2 = mod.select_out_keys(out_d_key)

    def test_tdseq_dispatch(self, out_d_key, unpack):
        mod = TensorDictSequential(
            TensorDictModule(lambda x: x + 2, in_keys=["a"], out_keys=["c"]),
            TensorDictModule(lambda x: (x + 2, x), in_keys=["b"], out_keys=["d", "e"]),
        )

        exp_res = {"c": 2, "d": 3, "e": 1}
        res = mod(torch.zeros(()), torch.ones(()))
        assert len(res) == 3
        for i, v in enumerate(["c", "d", "e"]):
            assert (res[i] == exp_res[v]).all()
        if unpack:
            mod2 = mod.select_out_keys(*out_d_key)
            assert mod2 is mod
            assert mod.out_keys == list(out_d_key)
            res = mod(torch.zeros(()), torch.ones(()))
            if len(list(out_d_key)) == 1:
                res = [res]
            for i, v in enumerate(list(out_d_key)):
                assert (res[i] == exp_res[v]).all()
            mod2 = mod.reset_out_keys()
            assert mod2 is mod
            res = mod(torch.zeros(()), torch.ones(()))
            assert len(res) == 3
            for i, v in enumerate(["c", "d", "e"]):
                assert (res[i] == exp_res[v]).all()
        else:
            with pytest.raises(ValueError, match="Can't select non "):
                mod2 = mod.select_out_keys(out_d_key)

    @pytest.mark.parametrize("inplace", [True, False])
    def test_tdbase(self, inplace, out_d_key, unpack):
        class MyModule(TensorDictModuleBase):
            in_keys = ["a", "b"]
            out_keys = ["c", "d", "e"]

            def forward(self, tensordict):
                c = tensordict["a"] + 2
                d = tensordict["b"] + 2
                if not inplace:
                    tensordict = tensordict.select()
                tensordict["c"] = c
                tensordict["d"] = d
                tensordict["e"] = d + 2
                return tensordict

        # since we play with the __new__ and class attributes, let's check that everything's ok for a second instance
        for i in range(2):
            mod = MyModule()
            # some magic happened and in_keys and out_keys are not class attributes anymore!
            assert mod.out_keys is mod._out_keys, i
            td = mod(TensorDict({"a": torch.zeros(()), "b": torch.ones(())}, []))
            if inplace:
                assert set(td.keys()) == {"a", "b", "c", "d", "e"}
            else:
                assert set(td.keys()) == {"c", "d", "e"}
            if unpack:
                mod2 = mod.select_out_keys(*out_d_key)
                assert mod2 is mod
                assert mod.out_keys == list(out_d_key)
                td = mod(TensorDict({"a": torch.zeros(()), "b": torch.ones(())}, []))
                assert "c" not in td.keys()
                if inplace:
                    assert set(td.keys()) == {"a", "b", *out_d_key}
                else:
                    assert set(td.keys()) == {*out_d_key}
                mod2 = mod.reset_out_keys()
                assert mod2 is mod
                td = mod(TensorDict({"a": torch.zeros(()), "b": torch.ones(())}, []))
                if inplace:
                    assert set(td.keys()) == {"a", "b", "c", "d", "e"}
                else:
                    assert set(td.keys()) == {"c", "d", "e"}
            else:
                with pytest.raises(ValueError, match="Can't select non "):
                    mod2 = mod.select_out_keys(out_d_key)

    def test_tdmodule_wrap(self, out_d_key, unpack):
        mod = TensorDictModuleWrapper(
            TensorDictModule(
                lambda x, y: (x + 2, y + 2, x),
                in_keys=["a", "b"],
                out_keys=["c", "d", "e"],
            )
        )
        td = mod(TensorDict({"a": torch.zeros(()), "b": torch.ones(())}, []))
        assert all(key in td.keys() for key in ["a", "b", "c", "d", "e"])
        if unpack:
            mod2 = mod.select_out_keys(*out_d_key)
            assert mod2 is mod
            assert mod.out_keys == list(out_d_key)
            td = mod(TensorDict({"a": torch.zeros(()), "b": torch.ones(())}, []))
            assert all(key not in td.keys() for key in {"c", "d", "e"} - {*out_d_key})
            assert all(key in td.keys() for key in ["a", "b", *out_d_key])
            mod2 = mod.reset_out_keys()
            assert mod2 is mod
            td = mod(TensorDict({"a": torch.zeros(()), "b": torch.ones(())}, []))
            assert all(key in td.keys() for key in ["a", "b", "c", "d", "e"])
        else:
            with pytest.raises(ValueError, match="Can't select non "):
                mod2 = mod.select_out_keys(out_d_key)

    def test_tdmodule_wrap_dispatch(self, out_d_key, unpack):
        mod = TensorDictModuleWrapper(
            TensorDictModule(
                lambda x, y: (x + 2, y + 2, x),
                in_keys=["a", "b"],
                out_keys=["c", "d", "e"],
            )
        )
        exp_res = {"c": 2, "d": 3, "e": 0}
        res = mod(torch.zeros(()), torch.ones(()))
        assert len(res) == 3
        for i, v in enumerate(["c", "d", "e"]):
            assert (res[i] == exp_res[v]).all()
        if unpack:
            mod2 = mod.select_out_keys(*out_d_key)
            assert mod2 is mod
            assert mod.out_keys == list(out_d_key)
            res = mod(torch.zeros(()), torch.ones(()))
            if len(list(out_d_key)) == 1:
                res = [res]
            for i, v in enumerate(list(out_d_key)):
                assert (res[i] == exp_res[v]).all()
            mod2 = mod.reset_out_keys()
            assert mod2 is mod
            res = mod(torch.zeros(()), torch.ones(()))
            assert len(res) == 3
            for i, v in enumerate(["c", "d", "e"]):
                assert (res[i] == exp_res[v]).all()
        else:
            with pytest.raises(ValueError, match="Can't select non "):
                mod2 = mod.select_out_keys(out_d_key)


if __name__ == "__main__":
    args, unknown = argparse.ArgumentParser().parse_known_args()
    pytest.main([__file__, "--capture", "no", "--exitfirst"] + unknown)
