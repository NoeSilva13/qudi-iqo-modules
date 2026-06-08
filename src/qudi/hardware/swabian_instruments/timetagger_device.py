# -*- coding: utf-8 -*-

"""
Shared Swabian Instruments TimeTagger device manager for qudi.

This module owns the single physical connection to a TimeTagger and (optionally) starts a
TimeTagger server, so that several qudi hardware modules (e.g. a confocal scan counter using
`CountBetweenMarkers` and a live time-series streamer using `Counter`) can run their own
measurements on the *same* device at the same time. Other qudi modules connect to this module
via a `Connector` and obtain the shared tagger instance through `get_tagger()`.

Because the TimeTagger is designed to run an arbitrary number of measurement objects in parallel
on a single tagger instance, sharing one instance is what enables simultaneous operation. The
embedded server additionally exposes the same device to external processes (e.g. a separate
acquisition script) which can connect via `TimeTagger.createTimeTaggerNetwork`.

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

import TimeTagger as tt

from qudi.core.module import Base
from qudi.core.configoption import ConfigOption


class TimeTaggerDevice(Base):
    """ Owns the physical TimeTagger connection and shares it with other qudi modules.

    Example config for copy-paste:

    tt_device:
        module.Class: 'swabian_instruments.timetagger_device.TimeTaggerDevice'
        options:
            # timetagger_serial: ''        # optional, connect to a specific device serial
            reset: True                    # reset the device once on activation
            start_server: True             # start a TimeTagger server for external clients
            server_port: 41101             # TCP port of the server
            access_mode: 'Control'         # Control, SynchronousControl or Listen
    """

    _serial = ConfigOption(name='timetagger_serial', default='', missing='nothing')
    _reset = ConfigOption(name='reset', default=True, missing='nothing')
    _start_server = ConfigOption(name='start_server', default=True, missing='nothing')
    _server_port = ConfigOption(name='server_port', default=41101, missing='nothing')
    _access_mode = ConfigOption(name='access_mode', default='Control', missing='nothing')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tagger = None
        self._server_running = False

    def on_activate(self):
        if self._serial:
            self._tagger = tt.createTimeTagger(self._serial)
        else:
            self._tagger = tt.createTimeTagger()

        if self._reset:
            self._tagger.reset()

        if self._start_server:
            try:
                access_mode = getattr(tt.AccessMode, str(self._access_mode))
            except AttributeError:
                self.log.warning(
                    f'Unknown TimeTagger access_mode "{self._access_mode}". '
                    f'Falling back to "Control".'
                )
                access_mode = tt.AccessMode.Control
            try:
                self._tagger.startServer(access_mode, port=int(self._server_port))
                self._server_running = True
                self.log.info(
                    f'TimeTagger server started on port {int(self._server_port)} '
                    f'(access_mode={self._access_mode}).'
                )
            except Exception:
                self.log.exception('Failed to start TimeTagger server.')

        self.log.info('TimeTaggerDevice ready; shared tagger instance available.')

    def on_deactivate(self):
        if self._tagger is not None and self._server_running:
            try:
                self._tagger.stopServer()
            except Exception:
                self.log.exception('Failed to stop TimeTagger server.')
            self._server_running = False

        free_fn = getattr(tt, 'freeTimeTagger', None)
        if free_fn is not None and self._tagger is not None:
            try:
                free_fn(self._tagger)
            except Exception:
                self.log.exception('Failed to free TimeTagger instance.')
        self._tagger = None

    def get_tagger(self):
        """ Return the shared in-process TimeTagger instance.

        Connecting qudi modules should use this object to create their own measurement objects
        (Counter, CountBetweenMarkers, ...). Multiple measurements run in parallel on it.
        """
        if self._tagger is None:
            raise RuntimeError('TimeTaggerDevice is not active; no tagger instance available.')
        return self._tagger

    @property
    def server_port(self) -> int:
        """ TCP port of the running TimeTagger server (or the configured one if not started). """
        return int(self._server_port)

    def create_network_tagger(self):
        """ Convenience factory returning a network proxy to the local TimeTagger server.

        Intended for clients that prefer a network connection over the shared in-process object.
        Requires `start_server: True`.
        """
        if not self._server_running:
            raise RuntimeError('TimeTagger server is not running; cannot create a network tagger.')
        return tt.createTimeTaggerNetwork(f'localhost:{int(self._server_port)}')
