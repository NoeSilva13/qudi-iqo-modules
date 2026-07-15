# -*- coding: utf-8 -*-

"""
Interfuse that wraps a Swabian Instruments Pulse Streamer (trigger generation) and a TimeTagger
`CountBetweenMarkers` counter (photon counting) into a single `FiniteSamplingInputInterface` for
ODMR measurements.

Wiring: one Pulse Streamer digital output channel is split (e.g. with a BNC T-piece or a fast
fan-out buffer) into two destinations:
  1. A TimeTagger digital input channel, used as `begin_channel` of the `CountBetweenMarkers`
     measurement performed by the connected `counter` module (typically an instance of
     `swabian_instruments.timetagger_finite_counter.TimeTaggerFiniteCounter`).
  2. The external trigger input ([TRIGGER IN]) of the microwave source (e.g. a Rigol DSG836
     running `hardware.microwave.mw_source_rigol_dsg8xx.MicrowaveRigolDsg8xx`), which advances
     its frequency sweep by one point on every edge.

For a scan of N frequency points, N + 1 trigger edges are required: the first edge opens
TimeTagger bin 0 (frequency point 0) and arms the very first microwave dwell, and each
subsequent edge simultaneously closes/opens a TimeTagger bin and steps the microwave to the
next point. This module only owns the Pulse Streamer connection; the actual counting is fully
delegated to the connected `counter` module so both hardware objects keep a single well-defined
API surface (this also lets the same TimeTagger physical device be shared with a confocal scan
via `TimeTaggerDevice`, using a separate `TimeTaggerFiniteCounter` instance dedicated to ODMR).

Note: this module opens its own direct network connection to the Pulse Streamer. Do not also
configure a `PulserInterface` module (e.g. `swabian_instruments.pulse_streamer.PulseStreamer`)
pointed at the same physical device while this interfuse is active, since the two connections
would fight over control of the same output channel.

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
import pulsestreamer as ps

from qudi.core.configoption import ConfigOption
from qudi.core.connector import Connector
from qudi.util.mutex import RecursiveMutex
from qudi.interface.finite_sampling_input_interface import FiniteSamplingInputInterface


class PulseStreamerTimeTaggerOdmrInterfuse(FiniteSamplingInputInterface):
    """ Combines a Pulse Streamer (trigger/clock generation) with a TimeTagger-based counter
    (connected via `FiniteSamplingInputInterface`) for externally-triggered ODMR measurements.

    Example config for copy-paste:

    ps_tt_odmr_counter:
        module.Class: 'interfuse.pulsestreamer_timetagger_odmr_interfuse.PulseStreamerTimeTaggerOdmrInterfuse'
        connect:
            counter: 'tt_counter_odmr'   # a dedicated TimeTaggerFiniteCounter instance
        options:
            pulsestreamer_ip: '169.254.8.2'
            trigger_channel: 0            # PS digital output channel driving the clock/trigger
            trigger_pulse_width: 200e-9   # seconds, HIGH time of each trigger pulse
            arm_delay: 0.05               # seconds, settle time between arming the counter
                                          # and starting the Pulse Streamer trigger train
    """

    _counter = Connector(name='counter', interface=FiniteSamplingInputInterface)

    _pulsestreamer_ip = ConfigOption(name='pulsestreamer_ip', default='169.254.8.2', missing='warn')
    _trigger_channel = ConfigOption(name='trigger_channel', default=0, missing='info')
    _trigger_pulse_width = ConfigOption(name='trigger_pulse_width', default=200e-9, missing='nothing')
    _arm_delay = ConfigOption(name='arm_delay', default=0.05, missing='nothing')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._thread_lock = RecursiveMutex()
        self._ps = None

    def on_activate(self):
        self._ps = ps.PulseStreamer(self._pulsestreamer_ip)
        # Make sure the trigger channel starts low and nothing is streaming from a previous run.
        self._ps.forceFinal()
        try:
            serial = self._ps.getSerial()
        except Exception:
            serial = 'unknown'
        self.log.info(
            f'Connected to Pulse Streamer at "{self._pulsestreamer_ip}" (serial {serial}). '
            f'ODMR trigger train will be emitted on digital channel {self._trigger_channel}.'
        )

    def on_deactivate(self):
        if self._ps is not None:
            try:
                self._ps.forceFinal()
            except Exception:
                self.log.exception('Failed to force Pulse Streamer output to idle state.')
        self._ps = None

    @property
    def constraints(self):
        return self._counter().constraints

    @property
    def active_channels(self):
        return self._counter().active_channels

    @property
    def sample_rate(self):
        return self._counter().sample_rate

    @property
    def frame_size(self):
        return self._counter().frame_size

    @property
    def samples_in_buffer(self):
        return self._counter().samples_in_buffer

    def set_sample_rate(self, rate):
        self._counter().set_sample_rate(rate)

    def set_active_channels(self, channels):
        self._counter().set_active_channels(channels)

    def set_frame_size(self, size):
        self._counter().set_frame_size(size)

    def start_buffered_acquisition(self):
        with self._thread_lock:
            counter = self._counter()
            # Arm the TimeTagger CountBetweenMarkers measurement first, so the very first trigger
            # edge we emit below is guaranteed to open bin 0 instead of being missed.
            counter.start_buffered_acquisition()
            try:
                time.sleep(self._arm_delay)
                self._fire_trigger_train(counter.frame_size + 1, counter.sample_rate)
            except Exception:
                counter.stop_buffered_acquisition()
                raise

    def stop_buffered_acquisition(self):
        with self._thread_lock:
            try:
                if self._ps is not None:
                    self._ps.forceFinal()
            except Exception:
                self.log.exception('Failed to stop Pulse Streamer trigger output.')
            self._counter().stop_buffered_acquisition()

    def get_buffered_samples(self, number_of_samples=None):
        return self._counter().get_buffered_samples(number_of_samples)

    def acquire_frame(self, frame_size=None):
        with self._thread_lock:
            previous_frame_size = None
            if frame_size is not None:
                previous_frame_size = self.frame_size
                self.set_frame_size(frame_size)

            self.start_buffered_acquisition()
            try:
                data = self.get_buffered_samples(self.frame_size)
            finally:
                self.stop_buffered_acquisition()

            if previous_frame_size is not None:
                self.set_frame_size(previous_frame_size)

            return data

    def _fire_trigger_train(self, n_edges, sample_rate):
        """ Stream `n_edges` trigger pulses on `_trigger_channel`, one rising edge per period
        `1 / sample_rate`. The Pulse Streamer has 8 ns timing resolution, so both the HIGH and
        LOW segment durations are rounded to the nearest multiple of 8 ns.
        """
        n_edges = int(n_edges)
        assert n_edges > 0, 'Need at least one trigger edge to fire.'
        assert sample_rate > 0, 'Sample rate must be > 0 to compute the trigger period.'

        period_ns = 1e9 / float(sample_rate)
        high_ns = min(self._trigger_pulse_width * 1e9, period_ns / 2.0)
        high_ns = max(8.0, 8.0 * round(high_ns / 8.0))
        low_ns = max(8.0, 8.0 * round((period_ns - high_ns) / 8.0))
        if high_ns + low_ns < 16.0:
            raise ValueError(
                f'Requested ODMR sample rate ({sample_rate:.4g} Hz) is too high for the Pulse '
                'Streamer to generate distinguishable trigger pulses (8 ns resolution).'
            )

        pattern = [(int(high_ns), 1), (int(low_ns), 0)]
        sequence = self._ps.createSequence()
        sequence.setDigital(int(self._trigger_channel), pattern)
        self._ps.setTrigger(ps.TriggerStart.SOFTWARE)
        self._ps.stream(sequence, n_runs=n_edges)
        self._ps.startNow()
