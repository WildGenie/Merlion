#
# Copyright (c) 2022 salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
#
"""
Spectral Residual algorithm for anomaly detection
"""
import logging

import numpy as np
import pandas as pd

from merlion.models.anomaly.base import DetectorConfig, DetectorBase
from merlion.transform.resample import TemporalResample
from merlion.utils import TimeSeries, UnivariateTimeSeries

logger = logging.getLogger(__name__)


class SpectralResidualConfig(DetectorConfig):
    """
    Config class for `SpectralResidual` anomaly detector.
    """

    _default_transform = TemporalResample(granularity=None)

    def __init__(self, local_wind_sz=21, q=3, estimated_points=5, predicting_points=5, target_seq_index=None, **kwargs):
        r"""
        :param local_wind_sz: Number of previous saliency points to consider when computing the anomaly score
        :param q: Window size of local frequency average computations
        :param estimated_points: Number of padding points to add to the timeseries for saliency map calculations.
        :param predicting_points: Number of points to consider when computing gradient for padding points
        :param target_seq_index: Index of the univariate whose anomalies we want to detect.

        The Saliency Map is computed as follows:

        .. math::
            R(f) &= \log(A(\mathscr{F}(\textbf{x}))) - \left(\frac{1}{q}\right)_{1 \times q}
            * (A(\mathscr{F}(\textbf{x})) \\
            S_m &= \mathscr{F}^{-1} (R(f))


        where :math:`*` is the convolution operator, and :math:`\mathscr{F}` is the Fourier Transform.
        The anomaly scores then are computed as:

        .. math::
            S(x) = \frac{S(x) - \overline{S(\textbf{x})}}{\overline{S(\textbf{x})}}


        where :math:`\textbf{x}` are the last ``local_wind_sz`` points in the timeseries.

        The ``estimated_points`` and ``predicting_points`` parameters are used to pad the end of the timeseries with reasonable
        values. This is done so that the later points in the timeseries are in the middle of averaging windows rather
        than in the end.
        """
        self.estimated_points = estimated_points
        self.q = q
        self.predicting_points = predicting_points
        self.local_wind_sz = local_wind_sz
        self.target_seq_index = target_seq_index
        super().__init__(**kwargs)


class SpectralResidual(DetectorBase):
    """
    Spectral Residual Algorithm for Anomaly Detection.

    Spectral Residual Anomaly Detection algorithm based on the algorithm described by
    `Ren et al. (2019) <https://arxiv.org/abs/1906.03821>`__. After taking the frequency spectrum, compute the
    log deviation from the mean. Use inverse fourier transform to obtain the saliency map. Anomaly scores
    for a point in the time series are obtained by comparing the saliency score of the point to the
    average of the previous points.
    """

    config_class = SpectralResidualConfig

    def __init__(self, config: SpectralResidualConfig = None):
        super().__init__(SpectralResidualConfig() if config is None else config)
        self.q_conv_map = np.ones(self.config.q) / self.config.q
        self.local_wind_sz = self.config.local_wind_sz
        self.local_conv_map = np.ones(self.local_wind_sz)
        self.train_data = None

    @property
    def require_even_sampling(self) -> bool:
        return True

    @property
    def require_univariate(self) -> bool:
        return False

    @property
    def target_seq_index(self) -> int:
        return self.config.target_seq_index

    def _get_saliency_map(self, values: np.array) -> np.array:
        transform = np.fft.fft(values)
        log_amps = np.log(np.abs(transform))
        phases = np.angle(transform)
        avg_log_amps = np.convolve(log_amps, self.q_conv_map, mode="same")  # approximation
        residuals = log_amps - avg_log_amps

        return np.abs(np.fft.ifft(np.exp(residuals + 1j * phases)))

    def _compute_grad(self, values: np.array) -> int:
        m = min(self.config.predicting_points, values.shape[0] - 1)
        x_n = values[-1]
        a = x_n - np.copy(values[-m - 1 : -1])
        b = np.flip(np.arange(1, m + 1))
        averages = a / b
        return np.average(averages)

    def _pad(self, values: np.array) -> np.array:
        grad = self._compute_grad(values)
        m = min(self.config.predicting_points, values.shape[0] - 1)
        item = values[-m] + grad * m
        return np.pad(values, ((0, self.config.estimated_points),), constant_values=item)

    def _get_anomaly_score(self, time_series: pd.DataFrame, time_series_prev: pd.DataFrame = None) -> pd.DataFrame:
        i = self.target_seq_index
        if time_series_prev is None:
            values = time_series.values[:, i]
        else:
            values = np.concatenate((time_series_prev.values[:, i], time_series.values[:, i]))

        padded_values = self._pad(values) if self.config.estimated_points > 0 else values
        saliency_map = self._get_saliency_map(padded_values)
        if self.config.estimated_points > 0:
            saliency_map = saliency_map[: -self.config.estimated_points]

        average_values = np.convolve(saliency_map, self.local_conv_map, mode="full")[: values.shape[0]]
        a = np.arange(1, average_values.shape[0] + 1)
        a = np.where(a > self.local_wind_sz, self.local_wind_sz, a)
        average_values = (average_values / a)[:-1]
        output_values = np.append(np.asarray([0.0]), (saliency_map[1:] - average_values) / (average_values + 1e-8))

        return pd.DataFrame(output_values[-len(time_series) :], index=time_series.index)

    def _train(self, train_data: pd.DataFrame, train_config=None) -> pd.DataFrame:
        dim = train_data.shape[1]
        if dim == 1:
            self.config.target_seq_index = 0
        elif self.target_seq_index is None:
            raise RuntimeError(
                f"Attempting to use the SR algorithm on a {dim}-variable "
                f"time series, but didn't specify a `target_seq_index` "
                f"indicating which univariate is the target."
            )
        assert 0 <= self.target_seq_index < dim, (
            f"Expected `target_seq_index` to be between 0 and {dim} "
            f"(the dimension of the transformed data), but got {self.target_seq_index}"
        )

        return self._get_anomaly_score(train_data)
