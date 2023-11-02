"""Functionality for building custom surrogates."""

from typing import Callable, ClassVar, Tuple

import torch
from attrs import define, field, validators
from torch import Tensor

from baybe.exceptions import ModelParamsNotSupportedError
from baybe.parameters import (
    CategoricalParameter,
    CustomDiscreteParameter,
    NumericalContinuousParameter,
    NumericalDiscreteParameter,
    TaskParameter,
)
from baybe.searchspace import SearchSpace
from baybe.surrogates.base import Surrogate
from baybe.surrogates.utils import batchify, catch_constant_targets
from baybe.utils import DTypeFloatONNX, DTypeFloatTorch

try:
    import onnxruntime as ort

    _ONNX_INSTALLED = True
except ImportError:
    _ONNX_INSTALLED = False


def _validate_custom_arch_cls(model_cls: type) -> None:
    """Validates a custom architecture to have the correct attributes.

    Args:
        model_cls: The user defined model class.

    Raises:
        ValueError: When model_cls does not have _fit or _posterior.
        ValueError: When _fit or _posterior is not a callable method.
        ValueError: When _fit does not have the required signature.
        ValueError: When _posterior does not have the required signature.
    """
    # Methods must exist
    if not (hasattr(model_cls, "_fit") and hasattr(model_cls, "_posterior")):
        raise ValueError(
            "`_fit` and a `_posterior` must exist for custom architectures"
        )

    fit = model_cls._fit  # pylint: disable=protected-access
    posterior = model_cls._posterior  # pylint: disable=protected-access

    # They must be methods
    if not (callable(fit) and callable(posterior)):
        raise ValueError(
            "`_fit` and a `_posterior` must be methods for custom architectures"
        )

    # Methods must have the correct arguments
    params = fit.__code__.co_varnames[: fit.__code__.co_argcount]

    if (
        params
        != Surrogate._fit.__code__.co_varnames  # pylint: disable=protected-access
    ):
        raise ValueError(
            "Invalid args in `_fit` method definition for custom architecture. "
            "Please refer to Surrogate._fit for the required function signature."
        )

    params = posterior.__code__.co_varnames[: posterior.__code__.co_argcount]

    if (
        params
        != Surrogate._posterior.__code__.co_varnames  # pylint: disable=protected-access
    ):
        raise ValueError(
            "Invalid args in `_posterior` method definition for custom architecture. "
            "Please refer to Surrogate._posterior for the required function signature."
        )


def register_custom_architecture(
    joint_posterior_attr: bool = False,
    constant_target_catching: bool = True,
    batchify_posterior: bool = True,
) -> Callable:
    """Wraps a given custom model architecture class into a ```Surrogate```.

    Args:
        joint_posterior_attr: Boolean indicating if the model returns a posterior
            distribution jointly across candidates or on individual points.
        constant_target_catching: Boolean indicating if the model cannot handle
            constant target values and needs the @catch_constant_targets decorator.
        batchify_posterior: Boolean indicating if the model is incompatible
            with t- and q-batching and needs the @batchify decorator for its posterior.

    Returns:
        A function that wraps around a model class based on the specifications.
    """

    def construct_custom_architecture(model_cls):
        """Constructs a surrogate class wrapped around the custom class."""
        _validate_custom_arch_cls(model_cls)

        class CustomArchitectureSurrogate(Surrogate):
            """Wraps around a custom architecture class."""

            joint_posterior: ClassVar[bool] = joint_posterior_attr
            supports_transfer_learning: ClassVar[bool] = False

            def __init__(self, *args, **kwargs):
                self.model = model_cls(*args, **kwargs)

            def _fit(
                self, searchspace: SearchSpace, train_x: Tensor, train_y: Tensor
            ) -> None:
                return self.model._fit(  # pylint: disable=protected-access
                    searchspace, train_x, train_y
                )

            def _posterior(self, candidates: Tensor) -> Tuple[Tensor, Tensor]:
                return self.model._posterior(  # pylint: disable=protected-access
                    candidates
                )

            def __get_attribute__(self, attr):
                """Accesses the attributes of the class instance if available.

                If the attributes are not available,
                it uses the attributes of the internal model instance.
                """
                # Try to retrieve the attribute in the class
                try:
                    val = super().__getattribute__(attr)
                except AttributeError:
                    pass
                else:
                    return val

                # If the attribute is not overwritten, use that of the internal model
                return self.model.__getattribute__(attr)

        # Catch constant targets if needed
        cls = (
            catch_constant_targets(CustomArchitectureSurrogate)
            if constant_target_catching
            else CustomArchitectureSurrogate
        )

        # batchify posterior if needed
        if batchify_posterior:
            cls._posterior = batchify(  # pylint: disable=protected-access
                cls._posterior  # pylint: disable=protected-access
            )

        return cls

    return construct_custom_architecture


if _ONNX_INSTALLED:

    @define(kw_only=True)
    class CustomONNXSurrogate(Surrogate):
        """A wrapper class for custom pretrained surrogate models.

        Args:
            onnx_input_name: The input name used for constructing the ONNX str.
            onnx_str: The ONNX byte str representing the model.
        """

        # Class variables
        joint_posterior: ClassVar[bool] = False
        supports_transfer_learning: ClassVar[bool] = False

        # Object variables
        onnx_input_name: str = field(validator=validators.instance_of(str))
        onnx_str: bytes = field(validator=validators.instance_of(bytes))
        _model: ort.InferenceSession = field(init=False, eq=False)

        @_model.default
        def default_model(self) -> ort.InferenceSession:
            """Instantiate the ONNX inference session."""
            try:
                return ort.InferenceSession(self.onnx_str)
            except Exception as exc:
                raise ValueError("Invalid ONNX string") from exc

        def __attrs_post_init__(self) -> None:
            # TODO: This is a temporary workaround to avoid silent errors when users
            #   provide model parameters to this class.
            if self.model_params or not isinstance(self.model_params, dict):
                raise ModelParamsNotSupportedError()

        @batchify
        def _posterior(self, candidates: Tensor) -> Tuple[Tensor, Tensor]:
            model_inputs = {
                self.onnx_input_name: candidates.numpy().astype(DTypeFloatONNX)
            }
            results = self._model.run(None, model_inputs)

            # IMPROVE: At the moment, we assume that the second model output contains
            #   standard deviations. Currently, most available ONNX converters care
            #   about the mean only and it's not clear how this will be handled in the
            #   future. Once there are more choices available, this should be revisited.
            return (
                torch.from_numpy(results[0]).to(DTypeFloatTorch),
                torch.from_numpy(results[1]).pow(2).to(DTypeFloatTorch),
            )

        def _fit(
            self, searchspace: SearchSpace, train_x: Tensor, train_y: Tensor
        ) -> None:
            # TODO: This method actually needs to raise a NotImplementedError because
            #   ONNX surrogate models cannot be retrained. However, this would currently
            #   break the code since `BayesianRecommender` assumes that surrogates
            #   can be trained and attempts to do so for each new DOE iteration.
            #   Therefore, a refactoring is required in order to properly incorporate
            #   "static" surrogates and account for them in the exposed APIs.
            pass

        @classmethod
        def validate_compatibility(cls, searchspace: SearchSpace) -> None:
            """Validate if the class is compatible with a given search space.

            Args:
                searchspace: The search space to be tested for compatibility.

            Raises:
                TypeError: If the search space is incompatible with the class.
            """
            if not all(
                isinstance(
                    p,
                    (
                        NumericalContinuousParameter,
                        NumericalDiscreteParameter,
                        TaskParameter,
                    ),
                )
                or (isinstance(p, CustomDiscreteParameter) and not p.decorrelate)
                or (isinstance(p, CategoricalParameter) and p.encoding == "INT")
                for p in searchspace.parameters
            ):
                raise TypeError(
                    f"To prevent potential hard-to-detect bugs that stem from wrong "
                    f"wiring of model inputs, {cls.__name__} "
                    f"is currently restricted for use with parameters that have "
                    f"a one-dimensional computational representation or "
                    f"{CustomDiscreteParameter.__name__}."
                )
