"""qlab: SNAIL / resonator measurement library for Xu Han's group setup.

Talks to the Keysight PNA N5222B over raw SCPI:

    our code  ->  pyvisa  ->  VNA

No PycQED, no QCoDeS anywhere in this package. Usage on the lab PC:

    import qlab
    with qlab.session() as st:                       # always closes the link
        qlab.read_trace(st)                                          # snapshot
        qlab.resonator_scan(st, center=8.0122e9, span=20e6, power=-30)

If a cell hangs on connect, or a read comes back as nonsense, the link is
carrying leftover bytes from a session that died mid-transfer:

    qlab.reset_link()          # Device Clear + drain; run it, then reconnect
"""

from .instruments import (
    connect_scpi, disconnect, session, reset_link, diagnose,
    all_off, free_run, LabStation,
)
from .measurements import (
    read_trace,
    compare_to_baseline,
    resonator_scan,
    resonator_power_sweep,
    stability_monitor,
)
from . import config

__all__ = [
    'connect_scpi', 'disconnect', 'session', 'reset_link', 'diagnose',
    'all_off', 'free_run', 'LabStation', 'config',
    'read_trace', 'compare_to_baseline', 'resonator_scan',
    'resonator_power_sweep', 'stability_monitor',
]
