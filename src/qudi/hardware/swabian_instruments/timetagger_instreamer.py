# -*- coding: utf-8 -*-

"""
Qudi hardware module that exposes a Swabian Instruments TimeTagger as a continuous data input
stream (`DataInStreamInterface`). It wraps the `TimeTagger.Counter` measurement, which bins photon
clicks into fixed-width time bins continuously, and presents the binned count rate of one or more
APD channels as a live stream. This is intended to drive the qudi time series (`TimeSeriesGui` ->
`TimeSeriesReaderLogic`) for real-time fluorescence monitoring.

Copyright (c) 2024, the qudi developers. See the AUTHORS.md file at the top-level directory of
this distribution and on <https://github.com/Ulm-IQO/qudi-iqo-modules/>.

This file is part of qudi.

Qudi is free software: you can redistribute it and/or modify it under the terms of
the GNU Lesser General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.

Qudi is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the GNU Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public License along with qudi.
If not, see <https://www.gnu.org/licenses/>.
"""

import time
import numpy as np
import TimeTagger as tt
from typing import List, Optional, Sequence, Tuple, Union

from qudi.core.configoption import ConfigOption
from qudi.util.mutex import RecursiveMutex
from qudi.util.constraints import ScalarConstraint
from qudi.interface.data_instream_interface import (
    DataInStreamInterface,
    DataInStreamConstraints,
    StreamingMode,
    SampleTiming,
)


class TimeTaggerInstreamer(DataInStreamInterface):
    """ Continuous count-rate data stream from a Swabian Instruments TimeTagger.

    Implements `DataInStreamInterface` on top of `TimeTagger.Counter`. Each configured TimeTagger
    click channel becomes a logical data channel whose value is the count rate (counts/s) measured
    over each time bin. The bin width is derived from the requested sample rate
    (`bin_width = 1 / sample_rate`).

    Example config for copy-paste:

    timetagger_instreamer:
        module.Class: 'swabian_instruments.timetagger_instreamer.TimeTaggerInstreamer'
        options:
            channels:
                APD1: 1          # logical channel name -> TimeTagger click channel number
                # APD2: 2
            sample_rate_limits: [0.1, 1e7]   # Hz (optional)
            channel_buffer_size: 1048576     # samples per channel (optional)
            # timetagger_serial: ''          # optional, connect to a specific device
            # reset: False                   # optional, reset the TimeTagger on activation
    """

    _channels = ConfigOption(name='channels', missing='error')
    _sample_rate_limits = ConfigOption(name='sample_rate_limits', default=(0.1, 1e7))
    _max_channel_buffer_size = ConfigOption(name='channel_buffer_size',
                                            default=1024 ** 2,
                                            constructor=lambda x: max(int(round(x)), 2))
    _timetagger_serial = ConfigOption(name='timetagger_serial', default='', missing='nothing')
    _reset = ConfigOption(name='reset', default=False, missing='nothing')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._tagger = None
        self._counter = None
        self._constraints = None

        # mapping logical channel name -> TimeTagger channel number
        self._channel_numbers = dict()

        self.__sample_rate = -1.0
        self.__buffer_size = -1
        self.__streaming_mode = StreamingMode.CONTINUOUS
        self.__active_channels = tuple()

        self._bin_width_ps = 0
        # global number of completed bins already handed out to the caller
        self._consumed = 0

        self._thread_lock = RecursiveMutex()

    def on_activate(self):
        if self._timetagger_serial:
            self._tagger = tt.createTimeTagger(self._timetagger_serial)
        else:
            self._tagger = tt.createTimeTagger()
        if self._reset:
            self._tagger.reset()

        # Normalise the channel mapping to {str: int} preserving config order
        self._channel_numbers = {str(name): int(ch) for name, ch in dict(self._channels).items()}
        if not self._channel_numbers:
            raise ValueError('At least one channel must be configured for TimeTaggerInstreamer.')

        channel_units = {name: 'counts/s' for name in self._channel_numbers}
        sample_rate_min, sample_rate_max = (float(x) for x in self._sample_rate_limits)
        self._constraints = DataInStreamConstraints(
            channel_units=channel_units,
            sample_timing=SampleTiming.CONSTANT,
            streaming_modes=[StreamingMode.CONTINUOUS],
            data_type=np.float64,
            channel_buffer_size=ScalarConstraint(default=min(1024 ** 2, self._max_channel_buffer_size),
                                                 bounds=(2, self._max_channel_buffer_size),
                                                 increment=1,
                                                 enforce_int=True),
            sample_rate=ScalarConstraint(default=min(50.0, sample_rate_max),
                                         bounds=(sample_rate_min, sample_rate_max),
                                         increment=1,
                                         enforce_int=False),
        )

        self.__active_channels = tuple(channel_units)
        self.configure(active_channels=self.__active_channels,
                       streaming_mode=StreamingMode.CONTINUOUS,
                       channel_buffer_size=self._constraints.channel_buffer_size.default,
                       sample_rate=self._constraints.sample_rate.default)

        self.log.info(
            'TimeTaggerInstreamer ready: channels={0}.'.format(self._channel_numbers)
        )

    def on_deactivate(self):
        self._stop_counter()
        free_fn = getattr(tt, 'freeTimeTagger', None)
        if free_fn is not None and self._tagger is not None:
            try:
                free_fn(self._tagger)
            except Exception:
                self.log.exception('Failed to free TimeTagger instance.')
        self._tagger = None

    @property
    def constraints(self) -> DataInStreamConstraints:
        return self._constraints

    @property
    def sample_rate(self) -> float:
        return self.__sample_rate

    @property
    def channel_buffer_size(self) -> int:
        return self.__buffer_size

    @property
    def streaming_mode(self) -> StreamingMode:
        return self.__streaming_mode

    @property
    def active_channels(self) -> List[str]:
        return list(self.__active_channels)

    def configure(self,
                  active_channels: Sequence[str],
                  streaming_mode: Union[StreamingMode, int],
                  channel_buffer_size: int,
                  sample_rate: float) -> None:
        with self._thread_lock:
            if self.module_state() == 'locked':
                raise RuntimeError('Unable to configure data stream while it is already running.')

            streaming_mode = StreamingMode(streaming_mode)
            channel_buffer_size = int(round(channel_buffer_size))

            if any(ch not in self._constraints.channel_units for ch in active_channels):
                raise ValueError(
                    f'Invalid channel to stream from encountered {tuple(active_channels)}. \n'
                    f'Valid channels are: {tuple(self._constraints.channel_units)}'
                )
            if streaming_mode not in self._constraints.streaming_modes:
                raise ValueError(f'Invalid streaming mode "{streaming_mode}" encountered.\n'
                                 f'Valid modes are: {self._constraints.streaming_modes}.')
            self._constraints.channel_buffer_size.check(channel_buffer_size)
            self._constraints.sample_rate.check(sample_rate)

            self.__active_channels = tuple(active_channels)
            self.__streaming_mode = streaming_mode
            self.__buffer_size = channel_buffer_size
            self.__sample_rate = float(sample_rate)
            self._bin_width_ps = max(1, int(round(1e12 / self.__sample_rate)))

    @property
    def available_samples(self) -> int:
        with self._thread_lock:
            if self.module_state() != 'locked' or self._counter is None:
                return 0
            total = self._total_completed_bins()
            return max(0, min(total - self._consumed, self.__buffer_size))

    def start_stream(self) -> None:
        with self._thread_lock:
            if self.module_state() == 'locked':
                self.log.warning('Unable to start input stream. It is already running.')
                return
            self.module_state.lock()
            try:
                channel_list = [self._channel_numbers[name] for name in self.__active_channels]
                self._counter = tt.Counter(
                    tagger=self._tagger,
                    channels=channel_list,
                    binwidth=int(self._bin_width_ps),
                    n_values=int(self.__buffer_size),
                )
                self._consumed = 0
                self._counter.clear()
                self._counter.start()
            except Exception:
                self.module_state.unlock()
                self._counter = None
                raise

    def stop_stream(self) -> None:
        with self._thread_lock:
            self._stop_counter()
            if self.module_state() == 'locked':
                self.module_state.unlock()

    def _stop_counter(self):
        if self._counter is not None:
            try:
                self._counter.stop()
            except Exception:
                self.log.exception('Failed to stop TimeTagger Counter measurement.')
            self._counter = None

    def _total_completed_bins(self) -> int:
        """ Number of fully integrated bins since the measurement started. """
        if self._counter is None:
            return 0
        try:
            duration_ps = float(self._counter.getCaptureDuration())
        except Exception:
            self.log.exception('Failed to query TimeTagger capture duration.')
            return self._consumed
        return int(duration_ps // self._bin_width_ps)

    def read_data_into_buffer(self,
                              data_buffer: np.ndarray,
                              samples_per_channel: Optional[int] = None,
                              timestamp_buffer: Optional[np.ndarray] = None) -> None:
        with self._thread_lock:
            if self.module_state() != 'locked' or self._counter is None:
                raise RuntimeError('Unable to read data. Device is not running.')
            if not isinstance(data_buffer, np.ndarray) or \
                    data_buffer.dtype != self._constraints.data_type:
                raise TypeError(
                    f'data_buffer must be numpy.ndarray with dtype {self._constraints.data_type}'
                )

            channel_count = len(self.__active_channels)
            if samples_per_channel is None:
                samples_per_channel = data_buffer.size // channel_count
            samples_per_channel = int(samples_per_channel)
            if samples_per_channel <= 0:
                return

            # Block until the requested number of bins has been integrated (or timeout).
            timeout_s = 5.0 + 2.0 * samples_per_channel / max(self.__sample_rate, 1e-9)
            t_start = time.time()
            while (self._total_completed_bins() - self._consumed) < samples_per_channel:
                if (time.time() - t_start) > timeout_s:
                    raise TimeoutError(
                        f'Timed out waiting for {samples_per_channel} TimeTagger samples '
                        f'after {timeout_s:.2f} s.'
                    )
                time.sleep(min(0.01, 0.5 / max(self.__sample_rate, 1e-9)))

            raw = np.asarray(self._counter.getData(), dtype=np.float64)
            n_values = raw.shape[1]
            total = self._total_completed_bins()

            # Column of the newest completed bin is n_values - 1, which maps to global bin
            # index (total - 1). Hence global bin g lives in column (g - total + n_values).
            start_col = self._consumed - total + n_values
            if start_col < 0:
                # The caller fell behind by more than the rolling buffer: oldest unread bins were
                # overwritten. Drop the lost samples and resync to the start of the window.
                lost = -start_col
                self.log.warning(
                    f'TimeTagger stream buffer overflow: {lost} samples were overwritten before '
                    f'being read. Consider increasing channel_buffer_size or the readout rate.'
                )
                self._consumed += lost
                start_col = 0

            stop_col = start_col + samples_per_channel
            block = raw[:, start_col:stop_col]  # shape (channel_count, samples_per_channel)

            # Convert raw counts per bin into counts/second and interleave sample-major:
            # data_buffer layout is [s0c0, s0c1, ..., s1c0, s1c1, ...].
            block = block * self.__sample_rate
            total_samples = channel_count * samples_per_channel
            for ch_idx in range(channel_count):
                data_buffer[ch_idx:total_samples:channel_count] = block[ch_idx, :]

            if timestamp_buffer is not None:
                start_t = self._consumed / self.__sample_rate
                timestamp_buffer[:samples_per_channel] = start_t + np.arange(
                    samples_per_channel, dtype=np.float64
                ) / self.__sample_rate

            self._consumed += samples_per_channel

    def read_available_data_into_buffer(self,
                                        data_buffer: np.ndarray,
                                        timestamp_buffer: Optional[np.ndarray] = None) -> int:
        with self._thread_lock:
            channel_count = len(self.__active_channels)
            samples_per_channel = min(self.available_samples, data_buffer.size // channel_count)
            if samples_per_channel > 0:
                self.read_data_into_buffer(data_buffer=data_buffer,
                                           samples_per_channel=samples_per_channel,
                                           timestamp_buffer=timestamp_buffer)
            return samples_per_channel

    def read_data(self,
                  samples_per_channel: Optional[int] = None
                  ) -> Tuple[np.ndarray, Union[np.ndarray, None]]:
        with self._thread_lock:
            if samples_per_channel is None:
                samples_per_channel = self.available_samples
            samples_per_channel = int(samples_per_channel)
            channel_count = len(self.__active_channels)
            data_buffer = np.empty(samples_per_channel * channel_count,
                                   dtype=self._constraints.data_type)
            if samples_per_channel > 0:
                self.read_data_into_buffer(data_buffer=data_buffer,
                                           samples_per_channel=samples_per_channel)
            return data_buffer, None

    def read_single_point(self) -> Tuple[np.ndarray, Union[None, np.float64]]:
        with self._thread_lock:
            if self.module_state() != 'locked' or self._counter is None:
                raise RuntimeError('Unable to read data. Device is not running.')
            channel_count = len(self.__active_channels)
            data_buffer = np.empty(channel_count, dtype=self._constraints.data_type)
            raw = np.asarray(self._counter.getData(), dtype=np.float64)
            data_buffer[:] = raw[:, -1] * self.__sample_rate
            return data_buffer, None
