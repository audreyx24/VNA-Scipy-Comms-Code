"""Measurement routines, moved out of the two notebooks and onto direct SCPI.

Every function takes the ``LabStation`` from ``connect_scpi()`` as its first
argument. Every one of them talks straight to the VNA — no PycQED, no QCoDeS,
nothing between this code and the instrument.

Cell mapping (see README.md for the full table):

    read_trace              (new) snapshot whatever the VNA is showing now
    compare_to_baseline     (new) check a trace against the reference run
    resonator_scan          QLab_tra cells 10/15/17, HH_qubit cell 11
    resonator_power_sweep   QLab_tra cell 12  (punch-out)
    stability_monitor       QLab_tra cell 14

NOT HERE YET — the pump-based measurements (two-tone, pump sweeps, Kerr,
observe/ramp helpers) drove the Anritsu MG3692C through PycQED/QCoDeS. That
whole path was removed rather than left as a hidden dependency. It comes back
once the Anritsu is connected and we have its programming manual, as a direct
SCPI driver alongside scpi_vna.py. The original code is in git history and in
the two frozen notebooks; see the README roadmap.
"""

import os
import time

import numpy as np

from . import config


# ====================================================================== #
#  Read the current trace (no reconfiguration)                           #
# ====================================================================== #

def read_trace(st, name='vna_snapshot', plot=True, save=True, close_fig=False):
    """Snapshot the trace the VNA is showing RIGHT NOW. Read-only.

    Reads whatever measurement is currently selected, exactly as the
    instrument is configured: it does NOT touch frequency, power, averaging or
    triggering, so it is safe to run against a live free-running trace you set
    up on the front panel.

    Two uses:
      1. Capture a front-panel setup to CSV/PNG without rebuilding it in code.
      2. Prove the data path works — the CSV carries a ``screen_FDATA`` column
         (the instrument's own formatted values) next to our decode of SDATA,
         so you can compare them directly, or against a marker on the screen.

    Returns the trace as a dict; saves CSV + PNG into the dated tree.
    """
    import pandas as pd

    if not hasattr(st.vna, 'get_formatted_data'):
        raise TypeError(
            "read_trace needs a direct-SCPI station: st = qlab.connect_scpi().")

    freqs = st.vna.freq_axis()      # auto-reads start/stop/points off the instrument
    real_data, imag_data = st.vna.get_real_imaginary_data()
    complex_data = real_data + 1j * imag_data
    ampl_dB = 20 * np.log10(np.abs(complex_data))
    phase_deg = np.unwrap(np.angle(complex_data)) * 180 / np.pi

    # The instrument's own formatted trace, for cross-checking our decode.
    # Only one value per point in log-mag/phase/linear formats; Smith and Polar
    # return two, so drop it rather than misalign the CSV.
    try:
        screen = st.vna.get_formatted_data()
        if len(screen) != len(freqs):
            screen = None
    except Exception:
        screen = None

    print(f'trace         : {st.vna._measurement!r}   ({len(freqs)} points)')
    print(f'freq range    : {freqs[0]/1e9:.6f} -> {freqs[-1]/1e9:.6f} GHz')
    print(f'|S| via SDATA : min {ampl_dB.min():.2f}  max {ampl_dB.max():.2f}  '
          f'p2p {np.ptp(ampl_dB):.2f} dB')
    if screen is not None:
        print(f'screen FDATA  : min {screen.min():.2f}  max {screen.max():.2f}  '
              f'p2p {np.ptp(screen):.2f} dB')
        if np.allclose(ampl_dB, screen, rtol=1e-3, atol=1e-2):
            print('              -> matches our SDATA decode: the data path is '
                  'confirmed end to end.')
        else:
            print('              -> differs from our dB, which is expected '
                  'unless the trace format is log-mag.')
    print(f'min |S| at    : {freqs[np.argmin(ampl_dB)]/1e9:.6f} GHz')
    if np.ptp(ampl_dB) == 0:
        print('WARNING: perfectly flat, zero ripple — that is a constant, not a '
              'measurement. Check RF is on and that the selected trace is the '
              'one being displayed.')

    cols = {'freq_Hz': freqs, 'real': real_data, 'imag': imag_data,
            'amplitude_dB': ampl_dB, 'phase_deg': phase_deg}
    if screen is not None:
        cols['screen_FDATA'] = screen

    csv_path = None
    if save:
        # Tolerate a hand-built LabStation (st.vna set directly, datadir never
        # filled in) — that is a natural way to reuse an already-open session.
        run_dir = config.make_run_dir(st.datadir or config.DATADIR_LAB, name)
        csv_path = os.path.join(run_dir, f'{name}.csv')
        pd.DataFrame(cols).to_csv(csv_path, index=False)
        print(f'saved         : {csv_path}')

    if plot:
        import matplotlib.pyplot as plt
        fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(8, 5))
        ax1.plot(freqs / 1e9, ampl_dB, label='|S| from SDATA')
        if screen is not None:
            ax1.plot(freqs / 1e9, screen, '--', lw=1, label='screen (FDATA)')
            ax1.legend(fontsize=8)
        ax1.set_ylabel('[dB]')
        ax1.set_title(f'{name} — {st.vna._measurement}')
        ax2.plot(freqs / 1e9, phase_deg)
        ax2.set_ylabel('phase [deg]')
        ax2.set_xlabel('frequency [GHz]')
        fig.tight_layout()
        if csv_path:
            fig.savefig(csv_path.replace('.csv', '.png'), dpi=110)
        if close_fig:
            plt.close(fig)

    return {'freq_Hz': freqs, 's': complex_data,
            'amplitude_dB': ampl_dB, 'phase_deg': phase_deg,
            'screen_FDATA': screen, 'csv_path': csv_path}


def compare_to_baseline(snap=None, baseline=None, tol_dB=0.5, tol_Hz=5e6,
                        plot=True, close_fig=False):
    """Check a trace against the stored reference run (config.BASELINE_TRACE).

    Two modes, and the difference matters:

    REPLAY (``snap=None``) — no hardware needed, runs on any machine. Takes the
    baseline's own ``real``/``imag`` columns and pushes them back through the
    same dB/phase math ``read_trace`` uses, then checks that it reproduces the
    ``amplitude_dB`` and ``phase_deg`` columns the baseline recorded. This is a
    unit test of the decode arithmetic: it must agree to floating-point noise,
    so any tolerance failure here is a real code change, never instrument drift.

    LIVE (``snap=`` a ``read_trace`` result) — compares a fresh measurement
    against the reference. This one is physics, not arithmetic: the reference
    is an *uncalibrated* trace of a standing-wave pattern in the cabling, so it
    only reproduces if the VNA is in the same front-panel state and nothing was
    recabled. Deviations here mean "the setup moved", which may be fine.

    Neither mode proves ``read_trace`` is *correct* — the baseline was produced
    by this same code. What argues for correctness is the ``screen_FDATA``
    column, the instrument's own formatted numbers, which this function also
    re-checks against our decode. There the VNA is the oracle, not us.

    Returns a dict of the measured deviations plus ``ok``.
    """
    import pandas as pd

    path = baseline or config.BASELINE_TRACE
    ref = pd.read_csv(path)
    ref_freq = ref['freq_Hz'].to_numpy()
    ref_dB = ref['amplitude_dB'].to_numpy()

    if snap is None:
        mode = 'replay (baseline real/imag through our math — no hardware)'
        s = ref['real'].to_numpy() + 1j * ref['imag'].to_numpy()
        freqs = ref_freq
        ampl_dB = 20 * np.log10(np.abs(s))
        phase_deg = np.unwrap(np.angle(s)) * 180 / np.pi
    else:
        mode = 'live (this run vs the reference measurement)'
        freqs = np.asarray(snap['freq_Hz'])
        ampl_dB = np.asarray(snap['amplitude_dB'])
        phase_deg = np.asarray(snap['phase_deg'])

    print(f'baseline      : {os.path.basename(path)}  '
          f'({len(ref_freq)} points, '
          f'{ref_freq[0]/1e9:.6f} -> {ref_freq[-1]/1e9:.6f} GHz)')
    print(f'mode          : {mode}')

    checks = []       # (label, detail, passed_or_None)

    same_n = len(freqs) == len(ref_freq)
    checks.append(('point count', f'{len(ref_freq)}  vs  {len(freqs)}', same_n))

    if not same_n:
        # Point-by-point comparison is meaningless; say so and stop rather than
        # print a misleading number from interpolated or truncated data.
        print(f'{checks[0][0]:<15}: {checks[0][1]:<47}FAIL')
        print('The traces have different lengths, so nothing further can be '
              'compared point by point.\nRe-take the snapshot with the same '
              'number of sweep points as the baseline.')
        return {'ok': False, 'n_points': len(freqs)}

    freq_dev = float(np.max(np.abs(freqs - ref_freq)))
    checks.append(('freq axis', f'max offset {freq_dev:.1f} Hz', freq_dev <= 1.0))

    dev = ampl_dB - ref_dB
    max_dev, rms_dev = float(np.max(np.abs(dev))), float(np.sqrt(np.mean(dev**2)))
    checks.append(('|S| deviation',
                   f'max {max_dev:.4f}  rms {rms_dev:.4f} dB  (tol {tol_dB:.2f})',
                   max_dev <= tol_dB))

    f_min, f_min_ref = freqs[np.argmin(ampl_dB)], ref_freq[np.argmin(ref_dB)]
    checks.append(('min |S| freq',
                   f'{f_min_ref/1e9:.6f}  vs  {f_min/1e9:.6f} GHz  '
                   f'(tol {tol_Hz/1e6:.3f} MHz)',
                   abs(f_min - f_min_ref) <= tol_Hz))

    checks.append(('p2p', f'{np.ptp(ref_dB):.3f}  vs  {np.ptp(ampl_dB):.3f} dB',
                   abs(np.ptp(ampl_dB) - np.ptp(ref_dB)) <= tol_dB))

    # Phase carries the cabling's electrical delay, which winds through tens of
    # thousands of degrees across the span. In replay it must land exactly; in
    # live mode a different delay setting shifts it wholesale, so it is
    # reported but not scored.
    ph_dev = float(np.max(np.abs(phase_deg - ref['phase_deg'].to_numpy())))
    checks.append(('phase dev', f'max {ph_dev:.4f} deg',
                   (ph_dev <= 1e-6) if snap is None else None))

    # The independent check: the instrument's own formatted trace vs our decode.
    if 'screen_FDATA' in ref:
        scr_dev = float(np.max(np.abs(ampl_dB - ref['screen_FDATA'].to_numpy())))
        checks.append(('vs screen FDATA', f'max {scr_dev:.4f} dB  (tol {tol_dB:.2f})',
                       scr_dev <= tol_dB))

    for label, detail, passed in checks:
        verdict = '' if passed is None else ('OK' if passed else 'FAIL')
        print(f'{label:<15}: {detail:<47}{verdict}')

    ok = all(p for _, _, p in checks if p is not None)
    print(f'{"VERDICT":<15}: '
          + ('MATCHES BASELINE' if ok else 'DIFFERS FROM BASELINE'))
    if not ok and snap is not None:
        print('                In live mode this is not automatically a bug — '
              'check the VNA is\n                in the same state as the '
              'baseline (same span, power, IF bandwidth,\n                '
              'averaging) and that nothing was recabled.')

    if plot:
        import matplotlib.pyplot as plt
        fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(8, 5),
                                       gridspec_kw={'height_ratios': [2, 1]})
        ax1.plot(ref_freq / 1e9, ref_dB, lw=2, alpha=.5, label='baseline')
        ax1.plot(freqs / 1e9, ampl_dB, '--', lw=1, label='this run')
        ax1.set_ylabel('[dB]')
        ax1.legend(fontsize=8)
        ax1.set_title(f'vs baseline — max dev {max_dev:.4f} dB'
                      + ('  [MATCH]' if ok else '  [DIFFERS]'))
        ax2.plot(freqs / 1e9, dev, lw=1)
        ax2.axhline(0, color='k', lw=.5)
        ax2.set_ylabel('this - baseline\n[dB]')
        ax2.set_xlabel('frequency [GHz]')
        fig.tight_layout()
        if close_fig:
            plt.close(fig)

    return {'ok': ok, 'n_points': len(freqs), 'freq_dev_Hz': freq_dev,
            'max_dev_dB': max_dev, 'rms_dev_dB': rms_dev,
            'phase_dev_deg': ph_dev, 'min_freq_Hz': float(f_min),
            'baseline_min_freq_Hz': float(f_min_ref)}


# ====================================================================== #
#  Resonator spectroscopy (VNA only)                                     #
# ====================================================================== #

def resonator_scan(st, center, span,
                   power=-30,
                   if_bandwidth=config.DEFAULT_IF_BANDWIDTH,
                   npts=2001,
                   averages=100,
                   delay_t=config.DEFAULT_DELAY_S21,
                   measure='S21',
                   analyze=True,
                   close_fig=False,
                   timeout=config.DEFAULT_VNA_TIMEOUT):
    """Single VNA scan of the readout resonator (S21 or S11), via direct SCPI.

    Needs a direct-SCPI station: ``st = qlab.connect_scpi()``. The acquisition
    talks straight to the N5222B (our code -> pyvisa -> VNA); no PycQED.

    S11 note: use delay_t ~ 1e-9 (reflection path), see config.DEFAULT_DELAY_S11.
    """
    if not hasattr(st.vna, 'acquire_trace'):
        raise TypeError(
            "resonator_scan needs a direct-SCPI station: st = qlab.connect_scpi().")

    start_f = center - span / 2
    stop_f = center + span / 2
    return _direct_trace_scan(st, start_f, stop_f, npts, power, averages,
                              if_bandwidth, delay_t, measure,
                              name=f'resonator_scan_{measure}',
                              plot=analyze, close_fig=close_fig, timeout=timeout)


def resonator_power_sweep(st, center, span,
                          power_start=-30, power_stop=20, power_step=1,
                          if_bandwidth=1000, npts=1001, averages=300,
                          delay_t=config.DEFAULT_DELAY_S21,
                          measure='S21', analyze=True, close_fig=False,
                          max_retries=2, address=config.VNA_ADDRESS):
    """Punch-out: repeat the resonator scan across probe powers.

    Low power -> dressed frequency; high power -> bare cavity.
    Size of the jump ~ dispersive shift.

    This runs for hours, unattended, over a single USB link -- so one flaky
    scan should not throw away everything already saved. Each power point
    gets up to ``max_retries`` extra attempts: on failure (a dead USB
    transfer, or ``acquire_trace``'s stale-sweep check tripping) the link is
    torn down and reopened with ``reset_link()`` + ``connect_scpi()`` before
    retrying that same power. A power that still fails after all retries is
    skipped -- logged in ``failed_powers`` -- rather than aborting every power
    point after it.
    """
    from . import instruments

    results = []
    failed_powers = []
    for p in range(power_start, power_stop, power_step):
        print(p)
        for attempt in range(max_retries + 1):
            try:
                results.append(resonator_scan(
                    st, center, span, power=p,
                    if_bandwidth=if_bandwidth, npts=npts, averages=averages,
                    delay_t=delay_t, measure=measure,
                    analyze=analyze, close_fig=close_fig))
                break
            except Exception as exc:
                print(f'  scan at {p} dBm failed ({exc!r}), '
                      f'attempt {attempt + 1}/{max_retries + 1}')
                if attempt == max_retries:
                    print(f'  giving up on {p} dBm after {max_retries + 1} '
                          'attempts -- moving on to the next power')
                    failed_powers.append(p)
                    break
                try:
                    instruments.reset_link(address=address)
                    fresh = instruments.connect_scpi(
                        datadir=st.datadir, address=address)
                    st.vna = fresh.vna
                except Exception as reconnect_exc:
                    print(f'  reconnect failed too ({reconnect_exc!r}); '
                          'giving up on this power')
                    failed_powers.append(p)
                    break
    if failed_powers:
        print(f'\n{len(failed_powers)} power point(s) never succeeded: '
              f'{failed_powers}')
    return results


def stability_monitor(st, center, span,
                      interval_s=7200, n_runs=None,
                      power=-30, if_bandwidth=1000, npts=2001, averages=100,
                      delay_t=6.24e-8, measure='S21',
                      analyze=True, close_fig=False):
    """Long-term stability: rescan the resonator every `interval_s` seconds.

    n_runs=None runs forever (original behavior: while True + 2 h sleep).
    """
    runcount = 0
    while n_runs is None or runcount < n_runs:
        runcount += 1
        print(runcount)
        resonator_scan(st, center, span, power=power,
                       if_bandwidth=if_bandwidth, npts=npts,
                       averages=averages, delay_t=delay_t, measure=measure,
                       analyze=analyze, close_fig=close_fig)
        if n_runs is not None and runcount >= n_runs:
            break
        time.sleep(interval_s)


# ====================================================================== #
#  Direct-SCPI single-scan helper                                        #
# ====================================================================== #

def _direct_trace_scan(st, start_f, stop_f, npts, power, averages,
                       if_bandwidth, delay_t, measure, name,
                       plot=True, close_fig=False,
                       timeout=config.DEFAULT_VNA_TIMEOUT):
    """Take one averaged scan straight over SCPI, then save CSV (+ PNG) and
    return the trace. The acquisition itself lives in ``SCPIVNA.acquire_trace``
    (mirrors the notebook's measure_resonator_spectroscopy_vna); this wrapper
    just handles the on-disk output and the plot, exactly like the notebooks.

    Phase is reported in DEGREES with a correct label — the notebook's
    detector emitted degrees too, but mislabeled them 'radians' (see
    CALL_CHAIN §4); we keep the value, fix the label.
    """
    import pandas as pd

    st.vna.timeout(timeout)
    freqs, complex_data = st.vna.acquire_trace(
        start_f, stop_f, npts, power, if_bandwidth, delay_t, averages, measure)

    real_data, imag_data = complex_data.real, complex_data.imag
    ampl_dB = 20 * np.log10(np.abs(complex_data))
    phase_deg = np.unwrap(np.angle(complex_data)) * 180 / np.pi

    run_dir = config.make_run_dir(st.datadir, name)
    csv_path = os.path.join(run_dir, f'{name}.csv')
    pd.DataFrame({'freq_Hz': freqs, 'real': real_data, 'imag': imag_data,
                  'amplitude_dB': ampl_dB, 'phase_deg': phase_deg}
                 ).to_csv(csv_path, index=False)

    f0 = freqs[np.argmin(ampl_dB)]
    print(f'{name}: min |{measure}| at {f0/1e9:.6f} GHz  ->  {csv_path}')

    if plot:
        import matplotlib.pyplot as plt
        fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(7, 5))
        ax1.plot(freqs / 1e9, ampl_dB)
        ax1.set_ylabel(f'|{measure}| [dB]')
        ax1.set_title(f'{name}   power={power} dBm')
        ax2.plot(freqs / 1e9, phase_deg)
        ax2.set_ylabel('phase [deg]')
        ax2.set_xlabel('frequency [GHz]')
        fig.tight_layout()
        fig.savefig(csv_path.replace('.csv', '.png'), dpi=110)
        if close_fig:
            plt.close(fig)

    return {'freq_Hz': freqs, 's': complex_data,
            'amplitude_dB': ampl_dB, 'phase_deg': phase_deg,
            'f_min_Hz': f0, 'csv_path': csv_path}
