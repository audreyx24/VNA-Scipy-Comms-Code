"""Instrument connection / station setup.

One entry point: ``connect_scpi()``. It opens the Keysight PNA N5222B over raw
SCPI and nothing else:

    our code  ->  pyvisa  ->  VNA

There is deliberately **no PycQED and no QCoDeS anywhere in this package**. The
instrument is controlled and read directly; the data comes straight back to the
lab PC with no intermediate framework.

pyvisa itself is imported only inside ``connect_scpi()`` (via
``SCPIVNA.open()``), so this package still imports cleanly on a machine with no
instrument libraries installed.

Original source: setup cells of QLab_tra_HH.ipynb (cells 2-8) and
HH_qubit.ipynb (cells 2-7), reduced to what the direct path actually needs.
"""

import contextlib

from . import config

# Printed whenever the USB device cannot be claimed. These are the steps that
# actually work, in the order that costs least -- and none of them can be done
# from Python, which is the whole reason they are written down here.
_STUCK_LINK_HELP = """\
The VNA is still claimed by something else, and no Python call can break that
claim from inside a new process. In order:

  1. Task Manager -> end every leftover python.exe. Jupyter's "shut down
     kernel" cannot kill a kernel blocked inside a native VISA read, so a
     zombie survives and keeps holding the USB device.
  2. services.msc -> restart "Keysight IO Libraries Service". This releases an
     orphaned USB claim without rebooting anything, and it is the step that
     survives shutting down all kernels.
  3. PNA front panel -> Preset. Clears channel state properly.
  4. System -> Preferences -> Power On State: set it to Preset, not Last
     State. The default restores the pre-shutdown state, which is why power
     cycling keeps handing back the same broken setup."""


class LabStation:
    """Handles for one measurement session: the VNA and where data goes."""

    def __init__(self):
        self.vna = None
        self.datadir = None


# Every VNA handle this module has opened and not yet closed.
#
# This exists because of how the notebook is actually used: when something goes
# wrong you re-run the setup cell. Each run built a brand-new session and simply
# rebound `st`, dropping the old one on the floor -- Python does not promptly
# close a pyvisa resource just because the last reference went away, so the old
# session kept its claim on a device that permits exactly one. Re-run the cell
# five times while debugging and you have five sessions interleaving reads on
# one USB endpoint, which looks exactly like "it worked, then it stopped".
_OPEN_VNAS = []


def _close_open_vnas():
    """Close every session this module still has open. Returns how many."""
    closed = 0
    while _OPEN_VNAS:
        vna = _OPEN_VNAS.pop()
        try:
            vna.close()
            closed += 1
        except Exception:
            pass
    return closed


def connect_scpi(datadir=config.DATADIR_LAB, address=config.VNA_ADDRESS,
                 timeout_s=None, open_timeout_s=config.DEFAULT_OPEN_TIMEOUT_S,
                 unfreeze=True):
    """Open the real N5222B over raw SCPI. Lab PC only.

    The front door for everything in this library — one call and you are
    talking straight to the instrument. Data lands in the dated tree (see
    ``config.make_run_dir``).

        st = qlab.connect_scpi()
        qlab.resonator_scan(st, center=8.0122e9, span=20e6, power=-30)

    **Safe to re-run.** It closes any session this module still has open before
    building a new one, so re-running the setup cell replaces the connection
    instead of stacking another one onto a single-session USB device.

    Opening Device Clears the link and checks *IDN? before anything
    instrument-specific, so a session left dirty by a killed kernel is cleaned
    up here rather than quietly corrupting the first read.

    ``unfreeze=True`` hands the screen back to the front panel on connect. A
    previous measurement leaves the channel in HOLD, and the PNA restores that
    on power-up, so without this the display sits frozen on an old scan — which
    reads as "the VNA is stuck" when nothing is actually wrong with the link.
    """
    from .scpi_vna import SCPIVNA

    reclaimed = _close_open_vnas()
    if reclaimed:
        print(f'[qlab] closed {reclaimed} earlier session(s) before reconnecting')

    st = LabStation()
    try:
        st.vna = SCPIVNA.open(address,
                              open_timeout_ms=int(open_timeout_s * 1000))
    except Exception as exc:
        raise RuntimeError(
            f'could not open the VNA at {address}: {exc}\n\n'
            + _STUCK_LINK_HELP) from exc
    _OPEN_VNAS.append(st.vna)

    if timeout_s is not None:
        st.vna.timeout(timeout_s)
    if unfreeze:
        st.vna.free_run()
    st.datadir = datadir
    print(f"[qlab] direct-SCPI station: {st.vna._measurement!r} on the N5222B "
          f"at {address}.  Data -> {datadir}")
    return st


def disconnect(st):
    """Close the VISA session and release the USB claim. Safe to call twice.

    Worth calling every single time. The claim lives until the owning *process*
    exits, so an abandoned session blocks the next connect even after you shut
    every kernel down.
    """
    if st is not None and st.vna is not None:
        if st.vna in _OPEN_VNAS:
            _OPEN_VNAS.remove(st.vna)
        st.vna.close()
        st.vna = None


@contextlib.contextmanager
def session(datadir=config.DATADIR_LAB, address=config.VNA_ADDRESS, **kw):
    """``connect_scpi()`` that always closes — on Ctrl-C, on exception, always.

        with qlab.session() as st:
            qlab.resonator_scan(st, center=8.01225e9, span=20e6, power=-30)

    Meant for scripts and one-shot runs, where nothing else owns the link. Do
    NOT open one while a ``connect_scpi()`` station is already live: USBTMC is
    single-session, so a second connection either fails outright or interleaves
    reads with the first — which is the corruption this module exists to
    prevent. In the notebook, keep the one ``st`` from the setup cell.
    """
    st = connect_scpi(datadir=datadir, address=address, **kw)
    try:
        yield st
    finally:
        try:
            all_off(st)
        except Exception as exc:
            # Never let tidy-up stop the close: releasing the USB claim is the
            # part that matters, and a wedged link fails here first.
            print(f'[qlab] all_off() failed during shutdown ({exc}); '
                  'closing the link anyway')
        disconnect(st)


def reset_link(address=config.VNA_ADDRESS, preset=False):
    """Recover a wedged link. Run this when ``connect_scpi()`` hangs or fails.

    Opens the raw VISA session, sends a USBTMC Device Clear, drains whatever
    bytes were left queued, clears the error queue, confirms *IDN?, and closes
    again. Deliberately does *not* go through ``SCPIVNA``: that class opens
    with a catalog query, and a confused instrument is exactly when that query
    reads back somebody else's leftover data.

        qlab.reset_link()                 # flush the pipe
        qlab.reset_link(preset=True)      # also SYST:PRES the instrument

    This fixes a *dirty* link. It cannot fix a *claimed* one — if another
    process still owns the device, opening fails and you get the manual
    recovery steps printed instead.
    """
    from .visa_transport import VisaTransport
    from .scpi_vna import handshake_link

    # Drop our own sessions first, or we are the thing blocking the open.
    reclaimed = _close_open_vnas()
    if reclaimed:
        print(f'[qlab] closed {reclaimed} session(s) held by this kernel')

    try:
        t = VisaTransport(address, timeout_ms=5000,
                          open_timeout_ms=int(config.DEFAULT_OPEN_TIMEOUT_S * 1000))
    except Exception as exc:
        raise RuntimeError(
            f'could not open the VNA at {address}: {exc}\n\n'
            + _STUCK_LINK_HELP) from exc

    try:
        idn = handshake_link(t)
        if preset:
            t.write('SYST:PRES')
            t.query('*OPC?')
    finally:
        t.close()

    print(f'[qlab] link reset OK — {idn}'
          + ('  (instrument preset)' if preset else ''))
    return idn


def diagnose(address=config.VNA_ADDRESS):
    """Print what the instrument and the VISA layer actually report right now.

    Read-only and non-raising: every step is reported as pass/fail rather than
    thrown, so one call gets you the whole picture even when half of it is
    broken. Run it when behaviour does not match the story, and read the output
    top to bottom — the first FAIL is the one to chase.

    Answers, in order: does VISA see the device, how long does the claim take,
    does it answer *IDN?, what is the channel actually configured to do, and is
    it sitting in HOLD (frozen screen) or sweeping.
    """
    import time
    from .visa_transport import VisaTransport

    def show(label, value):
        print(f'  {label:<22} {value}')

    print('=== qlab.diagnose ===')
    show('sessions open here', len(_OPEN_VNAS))

    try:
        import pyvisa
        names = pyvisa.ResourceManager().list_resources()
        show('VISA sees', f'{len(names)} resource(s)')
        for n in names:
            show('', ('-> ' if n == address else '   ') + n)
        if address not in names:
            show('WARNING', f'{address} is not in the list')
    except Exception as exc:
        show('FAIL list_resources', exc)
        print('  -> VISA itself is unhealthy. Restart the Keysight IO service.')
        return

    t0 = time.time()
    try:
        t = VisaTransport(address, timeout_ms=5000,
                          open_timeout_ms=int(config.DEFAULT_OPEN_TIMEOUT_S * 1000))
    except Exception as exc:
        show('FAIL open', f'{exc}   (after {time.time() - t0:.1f} s)')
        print('\n' + _STUCK_LINK_HELP)
        return
    show('open took', f'{time.time() - t0:.2f} s')

    try:
        show('stale bytes queued', t.drain())
        t.write('*CLS')
        show('*IDN?', t.query('*IDN?').strip())

        # Channel state. INIT:CONT 0 with a stale span is the frozen screen.
        for label, cmd in (
                ('traces defined', 'CALC:PAR:CAT:EXT?'),
                ('start freq [Hz]', 'SENS:FREQ:STAR?'),
                ('stop freq [Hz]', 'SENS:FREQ:STOP?'),
                ('points', 'SENS:SWE:POIN?'),
                ('IF bandwidth [Hz]', 'SENS:BWID?'),
                ('sweep time [s]', 'SENS:SWE:TIME?'),
                ('averaging on', 'SENS:AVER:STAT?'),
                ('average count', 'SENS:AVER:COUN?'),
                ('continuous sweep', 'INIT:CONT?'),
                ('trigger source', 'TRIG:SEQ:SOUR?'),
                ('RF output on', 'OUTP?'),
                ('data format', 'FORM:DATA?')):
            try:
                show(label, t.query(cmd).strip())
            except Exception as exc:
                show('FAIL ' + label, exc)

        errors = []
        for _ in range(20):
            e = t.query('SYST:ERR?').strip()
            if e.startswith('+0') or e.startswith('0,'):
                break
            errors.append(e)
        show('error queue', errors or 'empty')
    finally:
        t.close()

    print('\nReading it: "continuous sweep 0" means the channel is in HOLD, so\n'
          'the screen is frozen on an old scan — cosmetic, fixed by free_run().\n'
          'A long "sweep time" explains a call that looks hung but is working.')


def free_run(st):
    """Give the VNA's screen back to the front panel (sweeping again).

    Handy mid-session: after a scan the channel is held, so the display keeps
    showing that scan until something triggers it again.
    """
    if st.vna is not None:
        st.vna.free_run()


def all_off(st):
    """VNA RF off, and hand the sweep back to the front panel.

    Measurements park the channel in HOLD so nothing free-running can slip a
    stale sweep into our data. Without the free_run() the instrument stays
    held after the session and the screen keeps showing our last scan, which
    reads as "the VNA is stuck".

    Note this is tidy-up, not recovery: it sends commands, so it needs a link
    that already works. If the session is wedged, ``all_off`` is the thing that
    fails first — use ``reset_link()`` there. It also leaves the session open;
    ``disconnect()`` is what releases the USB claim.
    """
    if st.vna is not None:
        st.vna.rf_off()
        if hasattr(st.vna, 'free_run'):
            st.vna.free_run()
