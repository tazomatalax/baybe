"""Random forest surrogates."""

from typing import Any, ClassVar, Dict, Optional, Tuple

import numpy as np
import torch
from attr import define, field
from sklearn.ensemble import RandomForestRegressor
from torch import Tensor

from baybe.searchspace import SearchSpace
from baybe.surrogates.base import Surrogate
from baybe.surrogates.utils import (
    _get_model_params_validator,
    batchify,
    catch_constant_targets,
    scale_model,
)


@catch_constant_targets
@scale_model
@define
class RandomForestSurrogate(Surrogate):
    """A random forest surrogate model.

    Args:
        model_params: Optional model parameters.
    """

    # Class variables
    joint_posterior: ClassVar[bool] = False
    supports_transfer_learning: ClassVar[bool] = False

    # Object variables
    model_params: Dict[str, Any] = field(
        factory=dict,
        converter=dict,
        validator=_get_model_params_validator(RandomForestRegressor.__init__),
    )
    _model: Optional[RandomForestRegressor] = field(init=False, default=None)

    @batchify
    def _posterior(self, candidates: Tensor) -> Tuple[Tensor, Tensor]:
        # See base class.

        # Evaluate all trees
        # NOTE: explicit conversion to ndarray is needed due to a pytorch issue:
        # https://github.com/pytorch/pytorch/pull/51731
        # https://github.com/pytorch/pytorch/issues/13918
        predictions = torch.from_numpy(
            np.asarray(
                [
                    self._model.estimators_[tree].predict(candidates)
                    for tree in range(self._model.n_estimators)
                ]
            )
        )

        # Compute posterior mean and variance
        mean = predictions.mean(dim=0)
        var = predictions.var(dim=0)

        return mean, var

    def _fit(self, searchspace: SearchSpace, train_x: Tensor, train_y: Tensor) -> None:
        # See base class.
        self._model = RandomForestRegressor(**(self.model_params))
        self._model.fit(train_x, train_y.ravel())
