"""Direct SCPI driver for the Keysight PNA N5222B — no PycQED, no KST_VNA.

This is the "cut out the middleman" layer. Instead of
    our code -> PycQED detector -> KST_VNA driver -> QCoDeS VisaInstrument -> VNA
we go
    our code -> pyvisa -> VNA

It exposes the SAME method names as FakeVNA and the parts of KST_VNA that
measurements.py touches, so it is a drop-in replacement for either.

Two speed decisions, both measured against the 2026-07-20 format capture
(see VNA_GETTING_STARTED.md):

1. BINARY TRANSFER. The instrument was found sending 'ASC,0' — ASCII text,
   ~15-20 bytes per number, which then has to be parsed from strings. We set
   REAL,64 instead: 8 raw bytes per number, no parsing. For 2001 points that
   is ~80 kB of text down to 32 kB of bytes.

2. ONE ROUND TRIP PER SWEEP. The frequency axis is computed locally with
   linspace instead of asking the instrument (CALC:X?), because it is fully
   determined by start/stop/points, which we set ourselves.

TRANSPORT SPLIT
---------------
Nothing here imports pyvisa at module level. The class takes a `transport`
object with .write() / .query() / .query_raw(), so the exact same SCPI and
parsing code can run against a fake instrument on a laptop. Use
``SCPIVNA.open(address)`` on the lab PC to build a real pyvisa transport.
"""

import time

import numpy as np

from . import config

# Map SCPI format name -> numpy dtype. The byte-order prefix is filled in from
# FORM:BORD (NORM = big-endian '>', SWAP = little-endian '<').
_FORMATS = {
    ('REAL', 64): 'f8',
    ('REAL', 32): 'f4',
}


def parse_binary_block(raw):
    """Parse an IEEE 488.2 definite-length block into its payload bytes.

    Layout:  #  <n>  <n digits of byte count>  <payload>  [trailing newline]

    e.g. b'#432000<32000 bytes>' -> n=4, count=2000, payload is 32000 bytes.

    This is the parsing step that silently produces wrong numbers if you get
    it wrong, so it is a standalone function with its own tests rather than
    being buried inline in a read method.
    """
    if not raw:
        raise ValueError('empty response from instrument (no data at all)')
    if raw[0:1] != b'#':
        raise ValueError(
            f'expected IEEE 488.2 block starting with #, got {raw[:20]!r}. '
            'The instrument is probably still in ASCII mode (FORM:DATA ASC).')

    ndigits = int(raw[1:2])
    if ndigits == 0:
        # '#0' means indefinite length: payload runs to the final newline.
        return raw[2:].rstrip(b'\n')

    header_len = 2 + ndigits
    nbytes = int(raw[2:header_len])
    payload = raw[header_len:header_len + nbytes]
    if len(payload) != nbytes:
        raise ValueError(
            f'block header promised {nbytes} bytes but only {len(payload)} '
            'arrived — the read was truncated (raise the VISA timeout).')
    return payload


def _assert_live_trace(s, errors=None):
    """Refuse to return a trace that no sweep ever wrote into.

    A sweep that never fires (see the trigger note in acquire_trace) leaves the
    just-cleared average buffer in place, and CALC:DATA? hands it back without
    complaint: a mathematically constant trace, which plots as a flat line and
    saves as a perfectly good-looking CSV. Real data is never exactly constant
    — even an open port wobbles by several dB of noise floor — so zero
    variance across the whole sweep means "no measurement", not "flat device".

    Cheap to check, and it turns a silent afternoon of empty files into one
    loud error on the first scan.
    """
    if len(s) < 2:
        return
    mag = np.abs(s)
    if np.ptp(mag) != 0 or np.ptp(np.angle(s)) != 0:
        return

    level = 20 * np.log10(mag[0]) if mag[0] > 0 else float('-inf')
    msg = (
        f'the VNA returned a dead trace: all {len(s)} points identical at '
        f'{level:.1f} dB. No sweep actually ran, so this is the cleared '
        'average buffer, not a measurement.\n'
        'Usual cause: the channel is armed but never triggered — check that '
        'TRIG:SEQ:SOUR is IMM (not MAN) while INIT:CONT is OFF.')
    if errors is not None:
        leftover = [e for e in errors() if not e.startswith('+0')]
        if leftover:
            msg += '\nInstrument error queue: ' + '; '.join(leftover)
    raise RuntimeError(msg)


def handshake_link(t, verbose=True):
    """Put a link into a known-clean state and confirm the instrument answers.

    Three steps, in this order, because each one depends on the last:

    1. Device Clear  -- aborts any transfer left in flight and flushes the
       instrument's buffers. Without it, a session that died partway through a
       32 kB CALC:DATA? block leaves the rest of that block queued, and the
       next query silently reads trace bytes as its answer.
    2. ``*CLS``      -- clears the status byte and the error queue, so errors
       reported later belong to this session and not the previous one.
    3. ``*IDN?``     -- the cheapest possible round trip. If this returns a
       Keysight identity string the link is genuinely healthy; anything else
       means the pipe is still dirty, and we say so loudly rather than letting
       a wrong catalog read poison the whole session.

    Returns the identity string.
    """
    for step in ('clear', 'drain'):
        fn = getattr(t, step, None)      # fake transports have neither
        if fn is None:
            continue
        result = fn()
        if step == 'drain' and result and verbose:
            print(f'[qlab] discarded {result} stale bytes left by a previous '
                  'session (this is the junk that was corrupting reads)')

    t.write('*CLS')
    idn = t.query('*IDN?').strip()
    if 'Keysight' not in idn and 'Agilent' not in idn:
        raise RuntimeError(
            f'*IDN? returned {idn!r}, which is not a Keysight identity string.\n'
            'The link is still carrying junk from a previous session. Run '
            'qlab.reset_link(), and if that does not clear it see its '
            'docstring for the manual recovery steps.')
    return idn


class SCPIVNA:
    """Keysight PNA N5222B over raw SCPI."""

    def __init__(self, transport, measurement=None, binary=True,
                 float_bits=64, byte_order='SWAP'):
        self.t = transport
        self.binary = binary
        self.float_bits = float_bits
        self.byte_order = byte_order
        self._dtype = None
        self._measurement = measurement
        self.idn = None             # filled in by open() after the handshake

        # Local mirror of the sweep settings, so freq_axis() needs no I/O.
        self._start = None
        self._stop = None
        self._npts = None

        self._configure_format()
        if measurement is None:
            self._measurement = self.first_measurement()
        self.select_measurement(self._measurement)

    # ---------------- construction ---------------- #
    @classmethod
    def open(cls, address=config.VNA_ADDRESS, timeout_ms=30000,
             open_timeout_ms=None, handshake=True, **kw):
        """Build a real pyvisa transport and connect. Lab PC only.

        The handshake runs *before* ``__init__``, which is deliberate:
        ``__init__`` opens with a CALC:PAR:CAT:EXT? query, and a link carrying
        leftover bytes answers that query with somebody else's data. Clean the
        pipe first, then ask it questions.
        """
        from .visa_transport import VisaTransport
        if open_timeout_ms is None:
            open_timeout_ms = int(config.DEFAULT_OPEN_TIMEOUT_S * 1000)
        transport = VisaTransport(address, timeout_ms=timeout_ms,
                                  open_timeout_ms=open_timeout_ms)
        # Everything from here on can raise, and an exception on the way up
        # would strand an open session holding the USB claim -- so the next
        # attempt fails too, and the one after that, until the kernel is
        # killed. Close on the way out of any failure.
        try:
            idn = handshake_link(transport) if handshake else None
            vna = cls(transport, **kw)
        except BaseException:
            transport.close()
            raise
        vna.idn = idn
        return vna

    # ---------------- link hygiene ---------------- #
    def clear(self):
        """Device Clear + drain this session mid-flight, e.g. after a Ctrl-C."""
        return handshake_link(self.t)

    def close(self):
        """Close the VISA session and release the USB claim."""
        closer = getattr(self.t, 'close', None)
        if closer is not None:
            closer()

    # ---------------- format / selection ---------------- #
    def _configure_format(self):
        """Put the instrument into binary mode and remember how to decode it."""
        if not self.binary:
            self._dtype = None
            self.t.write('FORM:DATA ASC,0')
            return
        self.t.write(f'FORM:DATA REAL,{self.float_bits}')
        self.t.write(f'FORM:BORD {self.byte_order}')
        prefix = '<' if self.byte_order.upper().startswith('SWAP') else '>'
        self._dtype = np.dtype(prefix + _FORMATS[('REAL', self.float_bits)])

    def catalog(self):
        """List (name, s_parameter) for every measurement defined on the VNA."""
        raw = self.t.query('CALC:PAR:CAT:EXT?').strip().strip('"')
        if not raw or raw.upper() == 'NO CATALOG':
            return []
        fields = raw.split(',')
        return list(zip(fields[0::2], fields[1::2]))

    def first_measurement(self):
        cat = self.catalog()
        if not cat:
            raise RuntimeError(
                'No measurements defined on the VNA. Set up a trace on the '
                'front panel first, or call create_measurement().')
        return cat[0][0]

    def select_measurement(self, name):
        """Point this VISA session's CALC: commands at measurement `name`.

        Required before any CALC:DATA? read. Without it the instrument answers
        nothing and the read times out with '+103,"CALC measurement selection
        set to none"' in the error queue — exactly what the 2026-07-20 capture
        hit. Per-session, so it does not disturb anyone else's connection.
        """
        self.t.write(f"CALC:PAR:SEL '{name}'")
        self._measurement = name

    def set_s_parameter(self, sparam):
        """Set which S-parameter the selected trace measures ('S21' or 'S11').

        Mirrors the notebook's ``CALC1:PAR:MOD:EXT 'S21'`` (CALL_CHAIN §2): it
        does not create a trace, it re-points the existing one at a different
        receiver pair. Same source, different listener — the whole difference
        between a transmission and a reflection scan.
        """
        self.t.write(f"CALC:PAR:MOD:EXT '{sparam}'")

    # ---------------- sweep configuration ---------------- #
    def set_sweep(self, start, stop, npts, power=None, delay_t=None,
                  if_bandwidth=None):
        """Configure the frequency axis. Same signature as FakeVNA.set_sweep."""
        self.t.write(f'SENS:FREQ:STAR {float(start)}')
        self.t.write(f'SENS:FREQ:STOP {float(stop)}')
        self.t.write(f'SENS:SWE:POIN {int(npts)}')
        if power is not None:
            self.t.write(f'SOUR:POW {float(power)}')
        if delay_t is not None:
            self.t.write(f'CALC:CORR:EDEL:TIME {float(delay_t)}')
        if if_bandwidth is not None:
            self.t.write(f'SENS:BWID {float(if_bandwidth)}')
        self._start, self._stop, self._npts = float(start), float(stop), int(npts)

    def freq_axis(self):
        """The frequency axis, computed locally (no instrument round trip).

        Equivalent to CALC:X? for a LIN sweep, which is what we always use.
        verify_against_instrument() checks this claim on real hardware.
        """
        if self._npts is None:
            self._sync_sweep_from_instrument()
        return np.linspace(self._start, self._stop, self._npts)

    def _sync_sweep_from_instrument(self):
        """Read start/stop/points back, for when we did not set them ourselves."""
        self._start = float(self.t.query('SENS:FREQ:STAR?'))
        self._stop = float(self.t.query('SENS:FREQ:STOP?'))
        self._npts = int(float(self.t.query('SENS:SWE:POIN?')))

    def continuous(self, on):
        """INIT:CONT — free-run (True) vs triggered-only (False).

        Off is what lets software own the timing: the instrument sits idle and
        sweeps only when we trigger, so a CALC:DATA? read cannot return a
        stale free-running sweep.
        """
        self.t.write(f"INIT:CONT {'ON' if on else 'OFF'}")

    def trigger_source(self, src='IMM'):
        """TRIG:SEQ:SOUR — 'IMM' (internal) or 'MAN' (front panel / INIT:IMM).

        Use IMM. MAN means the sweep waits for a human to press Trigger, which
        makes SENS:SWE:MODE SING silently produce nothing — see acquire_trace.
        """
        self.t.write(f'TRIG:SEQ:SOUR {src}')

    def free_run(self):
        """Hand the sweep back to the instrument (INIT:CONT ON, TRIG:SOUR IMM).

        A measurement leaves the channel in HOLD, and a held PNA keeps drawing
        the trace it last acquired — so the front panel stays frozen on our
        last scan even after the settings underneath have moved on. Call this
        when you are done and want the screen live again.
        """
        self.trigger_source('IMM')
        self.continuous(True)

    def sweep_type(self, kind='LIN'):
        """SENS:SWE:TYPE — 'LIN' evenly spaced in Hz (always used here)."""
        self.t.write(f'SENS:SWE:TYPE {kind}')

    def min_sweep_time(self, on=True):
        """SENS:SWE:TIME:AUTO — 'sweep as fast as IF bandwidth/points allow'."""
        self.t.write(f"SENS:SWE:TIME:AUTO {'ON' if on else 'OFF'}")

    def sweep_dwell_sdel(self, seconds):
        """SENS:SWE:DWEL:SDEL — one-time settle delay at the start of a sweep."""
        self.t.write(f'SENS:SWE:DWEL:SDEL {float(seconds)}')

    # ---------------- KST_VNA-compatible surface ---------------- #
    def timeout(self, t=None):
        if t is None:
            return self.t.timeout_ms / 1000
        self.t.timeout_ms = float(t) * 1000

    def bandwidth(self, bw=None):
        if bw is None:
            return float(self.t.query('SENS:BWID?'))
        self.t.write(f'SENS:BWID {float(bw)}')

    def avg(self, n=None):
        if n is None:
            return int(float(self.t.query('SENS:AVER:COUN?')))
        self.t.write(f'SENS:AVER:COUN {int(n)}')

    def average_state(self, s=None):
        if s is None:
            return 'on' if self.t.query('SENS:AVER:STAT?').strip() in ('1', 'ON') else 'off'
        self.t.write(f"SENS:AVER:STAT {'ON' if str(s).lower() in ('on', '1', 'true') else 'OFF'}")

    def average_mode(self, m=None):
        if m is None:
            return self.t.query('SENS:AVER:MODE?').strip()
        self.t.write(f'SENS:AVER:MODE {m}')

    def average_clear(self):
        self.t.write('SENS:AVER:CLE')

    def rf_on(self):
        self.t.write('OUTP ON')

    def rf_off(self):
        self.t.write('OUTP OFF')

    def autoscale_trace(self):
        self.t.write('DISP:WIND:TRAC:Y:AUTO')

    def reset(self):
        self.t.write('SYST:PRES')

    # ---------------- acquisition ---------------- #
    def start_sweep_all(self):
        """Trigger one fresh, fully-averaged sweep and block until it finishes.

        Why not just read the trace: with INIT:CONT ON the instrument is free
        running, and CALC:DATA? then returns whatever sweep happened to be in
        the buffer — usually a stale one taken under the *previous* settings.
        Every wrong-looking pump sweep starts here.

        Every path in this library uses POINT averaging (SENS:AVER:MODE POIN),
        where a *single* sweep already measures each frequency point `averages`
        times — so one SENS:SWE:MODE SING is the complete, fully-averaged
        result, matching the notebook's single INIT:IMM. (An earlier version
        armed a GROUP of N sweeps *on top of* POINT averaging = N*N
        measurements; that is dropped — see SCPI_REFERENCE, "Averaging".)
        average_clear() first so a fresh average starts each time this is
        called in a loop (e.g. per pump point).
        """
        if self.average_state() == 'on':
            self.average_clear()
        self.t.write('SENS:SWE:MODE SING')
        self.t.query('*OPC?')

    def acquire_trace(self, start, stop, npts, power, if_bandwidth, delay_t,
                      averages, measure='S21', verbose=True):
        """Configure and take one averaged scan, returning (freq_Hz, complex_S).

        This is the direct-SCPI collapse of the notebook's
        ``QLab_Neon.measure_resonator_spectroscopy_vna`` — the
        ``KST_VNA_sweep.prepare()`` sequence (CALL_CHAIN §3) plus the averaging
        triplet, then a single triggered sweep and one SDATA read
        (CALL_CHAIN §4). Every command it sends is one the notebook path sent;
        the five wrapper layers are gone.

        Note it always triggers a fresh sweep before reading — the fix for the
        stale-buffer read the old fake-mode helper had.

        The channel is handed back to free-run once the data is safely read, so
        the front panel is live again between scans instead of frozen on the
        trace we just took. See the comment at the free_run() call below.
        """
        self.set_s_parameter(measure)
        self.continuous(False)      # we own the trigger; no free-running buffer
        self.min_sweep_time(True)
        # IMM, not MAN. Under TRIG:SEQ:SOUR MAN the PNA waits for a trigger from
        # the front panel (or an explicit INIT:IMM), so the SENS:SWE:MODE SING
        # in start_sweep_all() arms a sweep that never fires — *OPC? returns,
        # and CALC:DATA? hands back the average buffer we just cleared. That is
        # a trace of exactly 1e-10 at 45 deg: -200.000 dB flat, identical at
        # every power. INIT:CONT is still OFF, so this does not free-run; IMM
        # only means SENS:SWE:MODE SING is allowed to trigger itself.
        self.trigger_source('IMM')
        self.avg(averages)
        self.average_state('on')
        self.average_mode('POIN')   # each point averaged N times within one sweep
        self.rf_on()
        self.sweep_type('LIN')
        self.set_sweep(start, stop, npts, power=power, delay_t=delay_t,
                       if_bandwidth=if_bandwidth)
        self.sweep_dwell_sdel(1e-4)

        # Say up front how long the instrument thinks this will take. A sweep
        # with POIN averaging dwells `averages` times on every point, so a
        # 2001-point/200-average scan is minutes long and the screen barely
        # visibly advances -- which looks identical to a hang. Printing the
        # number turns "it's frozen" into "it has 4 minutes to go", and the
        # same estimate is reused below to catch a sweep that didn't happen.
        try:
            expected_s = self.sweep_time()
        except Exception:
            expected_s = None
        if verbose and expected_s is not None:
            print(f'  sweeping {npts} pts x {averages} avg @ '
                  f'{if_bandwidth} Hz IFBW -> ~{expected_s:.1f} s')

        t0 = time.perf_counter()
        self.start_sweep_all()      # one fully-averaged sweep, blocks until done
        elapsed = time.perf_counter() - t0
        re, im = self.get_real_imaginary_data()

        # A real sweep cannot finish much faster than the instrument's own
        # estimate -- POIN averaging dwells `averages` times on every point,
        # so there is no shortcut. Returning early means SENS:SWE:MODE SING
        # never actually re-armed (see the TRIG:SEQ:SOUR note above) and
        # CALC:DATA? just handed back the previous sweep's buffer again. That
        # trace is *not* flat (it's someone else's real data), so
        # _assert_live_trace lets it through -- this is the check that catches
        # it instead. Threshold is generous (half the estimate) because
        # min_sweep_time(True) means the real sweep can finish a bit under the
        # nominal estimate.
        if expected_s is not None and expected_s > 1.0 and elapsed < 0.5 * expected_s:
            raise RuntimeError(
                f'stale sweep: returned in {elapsed:.1f} s but the instrument '
                f'estimated ~{expected_s:.1f} s for {npts} pts x {averages} '
                'avg. SENS:SWE:MODE SING almost certainly did not trigger a '
                'new sweep, so this trace is a stale buffer from the previous '
                'scan, not new data. Run qlab.diagnose() / qlab.reset_link() '
                'before trusting the link again.')

        # Data is in hand, so give the screen back. Taking the sweep required
        # INIT:CONT OFF (above) and that leaves the channel in HOLD; a held PNA
        # keeps redrawing the trace it last acquired, so without this the front
        # panel sits frozen on this scan's span until something else triggers
        # it -- for a punch-out loop, for hours. Restoring here rather than in
        # the caller keeps it symmetric with the continuous(False) that caused
        # it, and it is placed before _assert_live_trace so a failed scan
        # unfreezes too. The next acquire_trace re-takes the trigger anyway.
        try:
            self.free_run()
        except Exception:
            pass                    # never let tidy-up lose a good trace

        s = re + 1j * im
        _assert_live_trace(s, self.errors)
        return self.freq_axis(), s

    def wait_to_continue(self):
        self.t.query('*OPC?')

    def get_real_imaginary_data(self):
        """Return (real, imag) arrays of the corrected complex trace.

        CALC:DATA? SDATA gives real,imag,real,imag,... interleaved, one pair
        per frequency point.
        """
        values = self._read_data('CALC:DATA? SDATA')
        npts = self._npts if self._npts is not None else len(values) // 2
        if len(values) != 2 * npts:
            raise ValueError(
                f'expected {2 * npts} numbers for {npts} points, got '
                f'{len(values)}. Format or point count is out of sync.')
        return values[0::2], values[1::2]

    def get_complex_data(self):
        re, im = self.get_real_imaginary_data()
        return re + 1j * im

    def get_formatted_data(self):
        """CALC:DATA? FDATA — what the screen shows, one value per point.

        Used by verify_against_instrument() as an independent check on our
        SDATA parse.
        """
        return self._read_data('CALC:DATA? FDATA')

    def get_x_axis(self):
        """CALC:X? — the instrument's own frequency axis. Slow; for checking."""
        return self._read_data('CALC:X?')

    def _read_data(self, command):
        if not self.binary:
            return np.array(
                [float(x) for x in self.t.query(command).strip().split(',')])
        payload = parse_binary_block(self.t.query_raw(command))
        return np.frombuffer(payload, dtype=self._dtype)

    # ---------------- diagnostics ---------------- #
    def errors(self):
        """Drain and return the SCPI error queue."""
        out = []
        for _ in range(50):
            e = self.t.query('SYST:ERR?').strip()
            out.append(e)
            if e.startswith('+0') or e.startswith('0,'):
                break
        return out

    def sweep_time(self):
        return float(self.t.query('SENS:SWE:TIME?'))

    def verify_against_instrument(self, rtol=1e-3):
        """Prove the binary parse is correct, using the VNA as its own oracle.

        Three independent checks, all run against a single fresh sweep:

        1. length   — SDATA must contain exactly 2 numbers per point.
        2. magnitude— our |S| from SDATA must match the instrument's own FDATA
                      (when the trace format is log-mag). This catches wrong
                      float width, wrong byte order, and swapped real/imag all
                      at once, because any of them changes the magnitude.
        3. axis     — our local linspace must match CALC:X?.

        Run this once on real hardware before trusting the driver with data.
        Returns a dict of results; raises nothing, so you can inspect it.
        """
        # This is usually called right after .open(), before any set_sweep(),
        # so the local sweep mirror is still empty. Read the instrument's
        # current start/stop/points first, otherwise length_ok compares against
        # a None self._npts and always reports False on a valid trace.
        if self._npts is None:
            self._sync_sweep_from_instrument()

        # This triggers a sweep on whatever the channel is currently set to,
        # including averaging left on by an earlier scan -- which can make one
        # sweep minutes long. Size the read timeout from the instrument's own
        # estimate rather than letting the session default decide, or a healthy
        # instrument gets reported as a timeout.
        was_timeout = self.timeout()
        try:
            self.timeout(max(was_timeout, 5 * self.sweep_time() + 10))
        except Exception:
            pass
        try:
            self.start_sweep_all()
            re, im = self.get_real_imaginary_data()
        finally:
            self.timeout(was_timeout)
        result = {'npts': len(re), 'length_ok': len(re) == self._npts}

        ours = 20 * np.log10(np.abs(re + 1j * im))
        theirs = self.get_formatted_data()
        if len(theirs) == len(ours):
            result['max_mag_diff_dB'] = float(np.max(np.abs(ours - theirs)))
            result['magnitude_ok'] = bool(
                np.allclose(ours, theirs, rtol=rtol, atol=1e-3))
        else:
            result['magnitude_ok'] = None  # trace not in log-mag format
            result['note'] = (f'FDATA has {len(theirs)} values vs {len(ours)} '
                              'points; set the trace to log-mag to compare.')

        theirs_x = self.get_x_axis()
        ours_x = self.freq_axis()
        if len(theirs_x) == len(ours_x):
            result['max_freq_diff_Hz'] = float(np.max(np.abs(ours_x - theirs_x)))
            result['axis_ok'] = bool(np.allclose(ours_x, theirs_x, rtol=0, atol=1.0))
        else:
            result['axis_ok'] = False

        result['errors'] = self.errors()
        return result
