"""Prove that Python is really controlling the VNA — four escalating checks.

    python3 check_control.py            # on the lab PC, with the N5222B connected

or from a notebook cell:

    from check_control import run_control_check
    run_control_check()                 # pauses and asks you to look at the screen
    run_control_check(no_pause=True)    # same checks, no pauses (unattended)

WHAT THIS IS FOR
----------------
"Can you control the VNA?" bundles four different claims together. This script
separates them, so you can say exactly which ones you have evidence for:

    1. REACH   VISA opens the instrument.            (cable + drivers work)
    2. TALK    It answers *IDN? with its name.       (it can speak to you)
    3. OBEY    You change a setting and it changes.  (<- this is "control")
    4. MEASURE You trigger a sweep and get numbers.  (it can do physics)

Most people prove 2 and then claim 4. Step 3 is the one that settles the
argument, and the proof is not in this terminal — it is on the instrument's
own screen. The script pauses and asks you to look.

DOES IT CHANGE ANYTHING?
------------------------
Yes — step 3 has to write, or it would not be testing anything. It changes the
start/stop frequency, shows you that it changed, and then puts back the exact
values it found. Step 4 additionally turns averaging off (inheriting a few
hundred point-averages makes a 201-point sweep take a minute) and puts that
back too. It never touches the signal generator and never turns RF on, and it
restores the data format on the way out. If someone else is mid-measurement on
this VNA, wait for them to finish anyway.

It also Device Clears the link on open, before reading a single value. Opening a
VISA session does not empty the instrument's output queue, and every value read
here is written back at the end — so a link still carrying bytes from a session
that died mid-transfer would have us "restore" the tail of an old trace.
"""

import argparse
import sys

import numpy as np

from qlab import config
from qlab.scpi_vna import SCPIVNA


def say(level, name, detail=''):
    """One line per check, in a shape that is easy to scan."""
    print(f'  [{level}] {name}')
    if detail:
        for line in str(detail).splitlines():
            print(f'         {line}')


def pause(prompt):
    print()
    try:
        input(f'  >>> {prompt}\n  >>> Press Enter to continue. ')
    except EOFError:
        print('  (not a terminal — continuing without pausing)')
    print()


def run_control_check(address=config.VNA_ADDRESS, no_pause=False):
    """Run the four-step control check (REACH / TALK / OBEY / MEASURE).

    Talks to the real N5222B over pyvisa. Returns 0 on success, 1 on failure.
    """

    # ---------- 1. REACH ---------- #
    print()
    print('=' * 68)
    print('  Can Python control this VNA? Four checks.')
    print('=' * 68)
    print()
    print('--- 1. REACH: open the instrument ---')

    # USBTMC allows exactly one session per device, and this script opens its
    # own. If the notebook's `st = qlab.connect_scpi()` is still live we would
    # be the second claim on a single-session device: either the open fails
    # outright or the two sessions interleave reads on one USB endpoint. Drop
    # ours first -- re-running the setup cell afterwards is one line.
    from qlab.instruments import _close_open_vnas
    handed_back = _close_open_vnas()
    if handed_back:
        say('..', f'closed {handed_back} qlab session(s) first',
            'USBTMC is single-session. Re-run the setup cell (st = '
            'qlab.connect_scpi())\nafter this check finishes.')

    try:
        from qlab.visa_transport import VisaTransport
        transport = VisaTransport(address, timeout_ms=10000)
    except Exception as exc:
        say('FAIL', 'could not open the instrument', exc)
        print('\n  Nothing else can be tested. Check that the USB cable is in,\n'
              '  that the VNA is powered on, and that the address in\n'
              '  qlab/config.py matches what pyvisa lists:\n'
              '      import pyvisa; pyvisa.ResourceManager().list_resources()')
        return 1
    say('ok', f'opened {address}')

    # Device Clear before asking anything. Opening a VISA session does NOT clean
    # the instrument's output queue: a session that died partway through a 32 kB
    # CALC:DATA? block leaves the remainder queued, and our very first query
    # would read the tail of that old trace as its answer -- every reading after
    # it shifted by one. Every reading below is load-bearing (we write these
    # values back at the end), so a desync here silently restores garbage.
    #
    # *CLS then empties the error queue, which is what makes the "error queue
    # clean?" verdict at the end mean anything: without it we report errors left
    # by whoever used the instrument before us as if this run caused them.
    #
    # This is the same handshake SCPIVNA.open() does via handshake_link(); we
    # inline the two commands rather than call it because *IDN? is step 2's job
    # and asking it here would give the answer away.
    try:
        transport.clear()
        stale = transport.drain()
        transport.write('*CLS')
        say('ok', 'link cleared before asking anything',
            'Device Clear + *CLS'
            + (f'\ndiscarded {stale} stale bytes left by a previous session — '
               'that is\nthe junk that makes readings come back shifted by one.'
               if stale else ''))
    except Exception as exc:
        say('warn', 'could not clear the link — readings below may be stale', exc)

    # Remember how we found the instrument, so we can put it back. The sweep
    # state matters as much as the format: step 4 sends SENS:SWE:MODE SING,
    # which parks the channel in HOLD, and a held PNA keeps showing the trace
    # it last acquired. Restore the frequencies without restoring this and the
    # screen stays frozen on the 5-6 GHz picture even though the setting
    # underneath is back to normal — which makes a second run look like it did
    # nothing at all.
    original = {}
    try:
        original['format'] = transport.query('FORM:DATA?').strip()
        original['byte_order'] = transport.query('FORM:BORD?').strip()
    except Exception as exc:
        say('warn', 'could not read the current data format', exc)
    try:
        original['continuous'] = transport.query('INIT:CONT?').strip()
        original['trigger'] = transport.query('TRIG:SEQ:SOUR?').strip()
    except Exception as exc:
        say('warn', 'could not read the current sweep/trigger state', exc)
    # Averaging too, and this one is not cosmetic. Step 4 triggers a sweep; a
    # channel left by a previous resonator_scan still has SENS:AVER:STAT ON in
    # POIN mode with COUN of a few hundred, which means every point dwells that
    # many times and a 201-point sweep runs for a minute. Inheriting that is
    # what made step 4 time out at 10 s and drop -310/-420 in the error queue.
    # Step 4 turns averaging off and puts it back here.
    try:
        original['aver_state'] = transport.query('SENS:AVER:STAT?').strip()
        original['aver_count'] = transport.query('SENS:AVER:COUN?').strip()
        original['aver_mode'] = transport.query('SENS:AVER:MODE?').strip()
    except Exception as exc:
        say('warn', 'could not read the current averaging state', exc)

    # ---------- 2. TALK ---------- #
    print('\n--- 2. TALK: ask it who it is ---')
    try:
        idn = transport.query('*IDN?').strip()
    except Exception as exc:
        say('FAIL', 'no answer to *IDN?', exc)
        return 1
    say('ok', 'it identified itself', idn)
    if 'N5222' not in idn:
        say('warn', 'that is not an N5222B — are you on the right instrument?')

    vna = SCPIVNA(transport)
    say('ok', f'selected measurement {vna._measurement!r}',
        'A PNA needs a trace selected on THIS connection before it will\n'
        'hand over data. Skipping this is the usual cause of a mystery timeout.')

    # ---------- 3. OBEY ---------- #
    print('\n--- 3. OBEY: change a setting and watch it move ---')
    print('  This is the check that actually answers your question.\n')

    vna._sync_sweep_from_instrument()
    was_start, was_stop, was_npts = vna._start, vna._stop, vna._npts
    say('ok', 'read the current sweep',
        f'{was_start/1e9:.4f} GHz to {was_stop/1e9:.4f} GHz, {was_npts} points')

    # Somewhere obviously different, and well inside the N5222B's range
    # (900 Hz - 26.5 GHz) so the instrument cannot refuse it.
    new_start, new_stop = 5.0e9, 6.0e9
    if abs(new_start - was_start) < 1e6 or abs(new_stop - was_stop) < 1e6:
        new_start, new_stop = 7.0e9, 8.0e9        # already there? move elsewhere

    if not no_pause:
        pause(f'LOOK AT THE VNA SCREEN NOW. Note the frequency axis: it should '
              f'read about {was_start/1e9:.3f} to {was_stop/1e9:.3f} GHz.')

    transport.write(f'SENS:FREQ:STAR {new_start}')
    transport.write(f'SENS:FREQ:STOP {new_stop}')
    say('ok', 'sent the change',
        f'SENS:FREQ:STAR {new_start:g}\nSENS:FREQ:STOP {new_stop:g}')

    got_start = float(transport.query('SENS:FREQ:STAR?'))
    got_stop = float(transport.query('SENS:FREQ:STOP?'))
    moved = abs(got_start - new_start) < 1.0 and abs(got_stop - new_stop) < 1.0
    say('ok' if moved else 'FAIL', 'read the setting back',
        f'asked for {new_start/1e9:.4f}-{new_stop/1e9:.4f} GHz, '
        f'it reports {got_start/1e9:.4f}-{got_stop/1e9:.4f} GHz')

    if not no_pause:
        pause('LOOK AGAIN. The frequency axis should now read '
              f'{new_start/1e9:.3f} to {new_stop/1e9:.3f} GHz. If the numbers on '
              'the instrument changed, you are controlling it. That is the '
              'whole proof.')

    # ---------- 4. MEASURE ---------- #
    print('--- 4. MEASURE: trigger a sweep and read the trace ---')
    # Don't inherit whatever trigger source happened to be set: under
    # TRIG:SEQ:SOUR MAN the SENS:SWE:MODE SING below arms a sweep that never
    # fires, and we'd read back a dead trace. Put back during restore.
    vna.trigger_source('IMM')
    vna.set_sweep(new_start, new_stop, 201)

    # Own every setting this step's duration depends on. Averaging off means
    # one dwell per point, so 201 points is a fraction of a second no matter
    # what the previous measurement left behind. Restored below.
    vna.average_state('off')

    # Then ask the instrument how long its own sweep will take and give the
    # read room for it, instead of trusting the 10 s the session opened with.
    # Guessing a timeout is how a working instrument gets reported as broken.
    try:
        swp = vna.sweep_time()
        budget = max(15.0, 5 * swp + 10)
        vna.timeout(budget)
        say('ok', 'sized the read timeout to the sweep',
            f'instrument reports {swp:.3f} s per sweep -> waiting up to {budget:.0f} s')
    except Exception as exc:
        say('warn', 'could not read SENS:SWE:TIME? — using 60 s', exc)
        vna.timeout(60)

    measure_ok = False
    try:
        vna.start_sweep_all()
        trace_db = vna.get_formatted_data()
        say('ok', f'got {len(trace_db)} points back from a fresh sweep')
        measure_ok = True
    except Exception as exc:
        say('FAIL', 'could not acquire a sweep', exc)
        trace_db = None
        # A timed-out query leaves the instrument mid-answer: whatever it was
        # about to say is still queued, and the next query reads THAT as its
        # reply, so every reading after this point is off by one and the
        # restore below would silently write and verify garbage. Device Clear
        # is the only thing that resynchronises the link.
        try:
            transport.clear()
            transport.write('*CLS')
            say('..', 'link resynchronised after the failure',
                'Device Clear + *CLS, so the restore below reads real values.')
        except Exception as exc2:
            say('warn', 'Device Clear failed too — power-cycle the link', exc2)

    if trace_db is not None and len(trace_db):
        mean, ripple = float(np.mean(trace_db)), float(np.ptp(trace_db))
        say('ok', 'trace statistics',
            f'mean {mean:.1f} dB, peak-to-peak {ripple:.1f} dB')

        # The classic "thru" interpretation, spelled out.
        if mean > -6:
            verdict = ('Near 0 dB and flat. That is a THRU — signal is going in\n'
                       'port 1 and coming out port 2. Cable connected, VNA\n'
                       'measuring correctly.')
        elif mean < -50:
            verdict = ('Very low. Either the ports are open (nothing connecting\n'
                       'port 1 to port 2), or the path is broken. If you expected\n'
                       'a cable to be attached, check it is seated at BOTH ends.')
        else:
            verdict = ('In between — some real device is attached, or there is\n'
                       'loss in the path. Not the simple cable case.')
        say('..', 'what that means', verdict)

        print()
        print('  To prove it is responding to the PHYSICAL world, not just to\n'
              '  your commands: run this once with the ports open, then cable\n'
              '  port 1 to port 2 and run it again. The mean should jump from\n'
              '  roughly -90 dB to roughly 0 dB. Nothing in software can fake\n'
              '  that, so it is the check to do in front of your grad student.')

    # ---------- restore ---------- #
    print('\n--- putting the instrument back as we found it ---')
    try:
        transport.write(f'SENS:FREQ:STAR {was_start}')
        transport.write(f'SENS:FREQ:STOP {was_stop}')
        transport.write(f'SENS:SWE:POIN {was_npts}')

        # Read it back rather than assume. Writing and then reporting success
        # is how a silently-failed restore looks identical to a good one.
        back_start = float(transport.query('SENS:FREQ:STAR?'))
        back_stop = float(transport.query('SENS:FREQ:STOP?'))
        back_npts = int(float(transport.query('SENS:SWE:POIN?')))
        restored = (abs(back_start - was_start) < 1.0
                    and abs(back_stop - was_stop) < 1.0
                    and back_npts == was_npts)
        say('ok' if restored else 'warn',
            'sweep restored' if restored else 'sweep NOT fully restored',
            f'asked for {was_start/1e9:.4f}-{was_stop/1e9:.4f} GHz / {was_npts} pts, '
            f'it reports {back_start/1e9:.4f}-{back_stop/1e9:.4f} GHz / {back_npts} pts')

        if 'format' in original:
            transport.write(f"FORM:DATA {original['format']}")
            transport.write(f"FORM:BORD {original['byte_order']}")
            say('ok', 'data format restored', original['format'])

        if 'aver_state' in original:
            on = original['aver_state'] in ('1', 'ON')
            transport.write(f"SENS:AVER:MODE {original['aver_mode']}")
            transport.write(f"SENS:AVER:COUN {original['aver_count']}")
            transport.write(f"SENS:AVER:STAT {'ON' if on else 'OFF'}")
            say('ok', 'averaging restored',
                f"{'ON' if on else 'OFF'}, {original['aver_mode']} mode, "
                f"count {original['aver_count']}")

        # Hand the sweep back. Step 4's SENS:SWE:MODE SING left the channel in
        # HOLD; until it is sweeping again the front panel keeps drawing the
        # trace from step 4, so the restored frequency axis never appears on
        # screen and a second run looks like it changed nothing. THIS is why
        # the VNA "does not go back to 10 MHz - 26.5 GHz": the setting is
        # restored (we read it back above), but the picture is stale.
        trig = original.get('trigger', 'IMM').upper()
        cont = original.get('continuous', '1')
        was_sweeping = cont in ('1', 'ON')

        # Take the redraw sweep under IMM regardless of what we found. If the
        # instrument was sitting in MAN, a SENS:SWE:MODE SING here would arm a
        # sweep that never fires and the screen would stay frozen anyway.
        transport.write('TRIG:SEQ:SOUR IMM')
        if was_sweeping:
            transport.write('INIT:CONT ON')
            say('ok', 'sweeping again', 'INIT:CONT ON — the screen now redraws '
                'with the restored frequency axis.')
        else:
            # Found in hold, so leave it in hold, but take one sweep at the
            # restored settings so the display is not showing our 5-6 GHz data.
            transport.write('INIT:CONT OFF')
            transport.write('SENS:SWE:MODE SING')
            transport.query('*OPC?')
            say('ok', 'left in hold, as found',
                'INIT:CONT OFF — took one sweep first so the screen shows the '
                'restored axis, not ours.')

        # Only now put the original trigger source back, so the redraw above
        # has already happened.
        transport.write(f'TRIG:SEQ:SOUR {trig}')
        if trig.startswith('MAN'):
            say('warn', 'found (and restored) TRIG:SEQ:SOUR MANual',
                'In MAN the VNA only sweeps when someone presses Trigger on\n'
                'the front panel, so the display will look frozen. If you did\n'
                'not set that deliberately, put it back to internal:\n'
                "    st.vna.free_run()      # TRIG:SOUR IMM + INIT:CONT ON")
    except Exception as exc:
        say('warn', 'could not fully restore — press Preset on the front panel', exc)

    leftover = [e for e in vna.errors() if not e.startswith('+0')]
    say('ok' if not leftover else 'warn',
        'instrument error queue' + ('' if not leftover else ' NOT clean'),
        '\n'.join(leftover) if leftover else 'empty — it accepted every command')

    print()
    print('=' * 68)
    print('  If steps 2 and 3 passed and the screen changed when the script\n'
          '  said it would, then yes: you are controlling the VNA.')
    if not measure_ok:
        print('\n  Step 4 did NOT pass — control is proven, acquisition is not.\n'
              '  Returning 1 so this does not read as a clean run.')
    print('=' * 68)
    print()

    try:
        transport.close()
    except Exception:
        pass
    return 0 if measure_ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--address', default=config.VNA_ADDRESS,
                    help='VISA address (default: from qlab/config.py)')
    ap.add_argument('--no-pause', action='store_true',
                    help='do not stop and ask you to look at the screen')
    args = ap.parse_args()
    return run_control_check(address=args.address, no_pause=args.no_pause)


if __name__ == '__main__':
    sys.exit(main())
