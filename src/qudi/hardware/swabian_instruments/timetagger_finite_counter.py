# -*- coding: utf-8 -*-

"""
Qudi hardware module that wraps the Swabian Instruments TimeTagger `CountBetweenMarkers`
measurement into the `FiniteSamplingInputInterface`. It is intended to be used together with
the NI X-series finite sampling IO module configured to export its sample clock to a PFI line.
The exported clock is wired into a TimeTagger digital channel and used as `begin_channel` for
`CountBetweenMarkers`, so that each pixel of a confocal scan corresponds to one bin counted by
the TimeTagger between two consecutive clock edges.

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

from qudi.core.configoption import ConfigOption
from qudi.util.mutex import RecursiveMutex
from qudi.interface.finite_sampling_input_interface import (
    FiniteSamplingInputInterface,
    FiniteSamplingInputConstraints,
)


class TimeTaggerFiniteCounter(FiniteSamplingInputInterface):
    """ Implements `FiniteSamplingInputInterface` on top of `TimeTagger.CountBetweenMarkers`.

    The measurement is externally clocked: an upstream device (e.g. the NI sample clock exported
    via `sample_clock_output`) must drive the `timetagger_channel_clock` input with one rising
    edge per pixel boundary. For ``N`` pixels the upstream device must emit ``N + 1`` edges
    (this matches the behaviour of `NIXSeriesFiniteSamplingIO`, which programs
    ``samps_per_chan = frame_size + 1`` on its clock counter).

    The `sample_rate` configured here is only used to convert raw photon counts per pixel into
    counts per second, replicating the ``counts * sample_rate`` conversion that the NI module
    applies for its PFI counters.

    Example config for copy-paste:

    timetagger_finite_counter:
        module.Class: 'swabian_instruments.timetagger_finite_counter.TimeTaggerFiniteCounter'
        options:
            timetagger_channel_apd: 1        # APD click channel on the TimeTagger
            timetagger_channel_clock: 8      # TimeTagger channel wired to /Dev1/PFI11
            channel_name: 'APD1'             # logical channel name exposed to qudi
            channel_unit: 'c/s'              # logical channel unit
            # trigger_level: 0.5             # optional, trigger level on the clock channel (V)
            sample_rate_limits: [1, 1e6]
            frame_size_limits: [1, 1e8]
    """

    _channel_apd = ConfigOption(name='timetagger_channel_apd', missing='error')
    _channel_clock = ConfigOption(name='timetagger_channel_clock', missing='error')
    _channel_name = ConfigOption(name='channel_name', default='APD1', missing='info')
    _channel_unit = ConfigOption(name='channel_unit', default='c/s', missing='info')
    _trigger_level = ConfigOption(name='trigger_level', default=None, missing='nothing')

    _sample_rate_limits = ConfigOption(name='sample_rate_limits', default=(1, 1e6))
    _frame_size_limits = ConfigOption(name='frame_size_limits', default=(1, 1e8))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._tagger = None
        self._cbm = None

        self._constraints = None
        self._active_channels = frozenset()
        self._sample_rate = -1.0
        self._frame_size = 0
        self._samples_consumed = 0

        self._thread_lock = RecursiveMutex()

    def on_activate(self):
        self._tagger = tt.createTimeTagger()
        self._tagger.reset()

        if self._trigger_level is not None:
            try:
                self._tagger.setTriggerLevel(int(self._channel_clock), float(self._trigger_level))
            except Exception:
                self.log.exception(
                    'Could not set trigger level on clock channel '
                    f'{self._channel_clock}.'
                )

        channel_units = {str(self._channel_name): str(self._channel_unit)}
        self._constraints = FiniteSamplingInputConstraints(
            channel_units=channel_units,
            frame_size_limits=tuple(self._frame_size_limits),
            sample_rate_limits=tuple(self._sample_rate_limits),
        )
        self._sample_rate_limits = self._constraints.sample_rate_limits
        self._frame_size_limits = self._constraints.frame_size_limits

        self._sample_rate = self._constraints.max_sample_rate
        self._frame_size = 0
        self._active_channels = frozenset(channel_units.keys())
        self._samples_consumed = 0

        self.log.info(
            'TimeTaggerFiniteCounter ready: click_channel={0}, begin_channel={1}, '
            'logical_channel="{2}".'.format(
                self._channel_apd, self._channel_clock, self._channel_name
            )
        )

    def on_deactivate(self):
        self._stop_cbm()
        free_fn = getattr(tt, 'freeTimeTagger', None)
        if free_fn is not None:
            try:
                free_fn(self._tagger)
            except Exception:
                self.log.exception('Failed to free TimeTagger instance.')
        self._tagger = None

    @property
    def constraints(self):
        return self._constraints

    @property
    def active_channels(self):
        return self._active_channels

    @property
    def sample_rate(self):
        return self._sample_rate

    @property
    def frame_size(self):
        return self._frame_size

    @property
    def samples_in_buffer(self):
        """ Number of pixel bins that have already been fully captured.

        Returns 0 when the measurement is idle. When running we count how many bins of the
        `CountBetweenMarkers` measurement report a non-zero bin width: a bin only acquires a
        non-zero width once both its begin and end markers have arrived, so this is a precise
        completion indicator (no time-based estimation).
        """
        with self._thread_lock:
            if self.module_state() != 'locked' or self._cbm is None:
                return 0
            try:
                if self._cbm.ready():
                    available = self._frame_size
                else:
                    widths = np.asarray(self._cbm.getBinWidths())
                    available = int(np.count_nonzero(widths))
            except Exception:
                self.log.exception('Failed to query TimeTagger capture progress.')
                return 0
            available = max(0, min(self._frame_size, available))
            return max(0, available - self._samples_consumed)

    def set_sample_rate(self, rate):
        sample_rate = float(rate)
        assert self._constraints.sample_rate_in_range(sample_rate)[0], (
            f'Sample rate "{sample_rate} Hz" out of bounds '
            f'{self._constraints.sample_rate_limits}.'
        )
        with self._thread_lock:
            assert self.module_state() != 'locked', (
                'Unable to set sample rate. Data acquisition in progress.'
            )
            self._sample_rate = sample_rate

    def set_active_channels(self, channels):
        assert hasattr(channels, '__iter__') and not isinstance(channels, str), (
            f'Given channels {channels} are not iterable.'
        )
        channels = tuple(channels)
        assert set(channels).issubset(set(self._constraints.channel_names)), (
            f'Trying to activate unknown channels: '
            f'{set(channels).difference(set(self._constraints.channel_names))}.'
        )
        with self._thread_lock:
            assert self.module_state() != 'locked', (
                'Unable to change active channels while acquisition is running.'
            )
            self._active_channels = frozenset(channels)

    def set_frame_size(self, size):
        samples = int(round(size))
        assert self._constraints.frame_size_in_range(samples)[0], (
            f'Frame size "{samples}" out of bounds {self._constraints.frame_size_limits}.'
        )
        with self._thread_lock:
            assert self.module_state() != 'locked', (
                'Unable to set frame size. Data acquisition in progress.'
            )
            self._frame_size = samples

    def start_buffered_acquisition(self):
        with self._thread_lock:
            assert self.module_state() != 'locked', (
                'Unable to start acquisition. Acquisition already running.'
            )
            assert self._frame_size > 0, (
                'Frame size has not been configured. Call set_frame_size() first.'
            )
            assert self._channel_name in self._active_channels, (
                f'Active channels do not contain the configured channel '
                f'"{self._channel_name}".'
            )

            self.module_state.lock()
            self._samples_consumed = 0
            try:
                # Use the rising edge of the clock channel to open each bin and the falling edge
                # of the same channel to close it. Each NI sample-clock pulse therefore produces
                # exactly one self-contained bin whose duration equals the clock HIGH-time.
                # Negative channel numbers select falling-edge triggers in the TimeTagger API.
                clock_ch = int(self._channel_clock)
                self._cbm = tt.CountBetweenMarkers(
                    tagger=self._tagger,
                    click_channel=int(self._channel_apd),
                    begin_channel=clock_ch,
                    end_channel=-clock_ch,
                    n_values=int(self._frame_size),
                )
            except Exception:
                self.module_state.unlock()
                self._cbm = None
                raise

    def stop_buffered_acquisition(self):
        with self._thread_lock:
            self._stop_cbm()
            if self.module_state() == 'locked':
                self.module_state.unlock()

    def _stop_cbm(self):
        if self._cbm is not None:
            try:
                self._cbm.stop()
            except Exception:
                self.log.exception('Failed to stop CountBetweenMarkers measurement.')
            self._cbm = None

    def get_buffered_samples(self, number_of_samples=None):
        with self._thread_lock:
            if self.module_state() != 'locked' and self.samples_in_buffer < 1:
                self.log.error(
                    'Unable to read data. Acquisition is not running and buffer is empty.'
                )
                return dict()

            remaining = self._frame_size - self._samples_consumed
            if number_of_samples is None:
                requested = self.samples_in_buffer
            else:
                requested = int(number_of_samples)

            if requested > remaining:
                raise ValueError(
                    f'Number of requested samples ({requested}) exceeds samples pending in '
                    f'this frame ({remaining}).'
                )

            if requested <= 0:
                return {self._channel_name: np.empty(0, dtype=np.float64)}

            cbm = self._cbm
            if cbm is None:
                self.log.error('CountBetweenMarkers instance is missing.')
                return dict()

            timeout_s = max(1.0, 1.5 * self._frame_size / max(self._sample_rate, 1.0))
            t_start = time.time()
            while self.samples_in_buffer < requested:
                if cbm.ready():
                    break
                if (time.time() - t_start) > timeout_s:
                    raise TimeoutError(
                        f'Timed out waiting for {requested} TimeTagger samples '
                        f'after {timeout_s:.2f} s.'
                    )
                time.sleep(0.01)

            try:
                raw_counts = np.asarray(cbm.getData(), dtype=np.float64)
                raw_widths_ps = np.asarray(cbm.getBinWidths(), dtype=np.float64)
            except Exception:
                self.log.exception('Failed to read data from CountBetweenMarkers.')
                return dict()

            start = self._samples_consumed
            stop = start + requested
            counts_chunk = raw_counts[start:stop]
            widths_chunk_s = raw_widths_ps[start:stop] * 1e-12

            # Convert raw counts to counts/second using the actual bin durations reported by the
            # TimeTagger. Bins with zero width are not yet complete; we leave them at 0 here, but
            # in practice `samples_in_buffer` ensures we never include incomplete bins in the
            # chunk in the first place.
            chunk = np.zeros_like(counts_chunk)
            valid = widths_chunk_s > 0
            chunk[valid] = counts_chunk[valid] / widths_chunk_s[valid]

            self._samples_consumed = stop
            return {self._channel_name: chunk}

    def acquire_frame(self, frame_size=None):
        with self._thread_lock:
            previous_frame_size = None
            if frame_size is not None:
                previous_frame_size = self._frame_size
                self.set_frame_size(frame_size)

            self.start_buffered_acquisition()
            try:
                data = self.get_buffered_samples(self._frame_size)
            finally:
                self.stop_buffered_acquisition()

            if previous_frame_size is not None:
                self._frame_size = previous_frame_size

            return data
