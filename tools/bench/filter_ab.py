#!/usr/bin/env python3
"""Pair a filter-base-only change on identical firmware CV/noise/envelopes.

filter_ab.py BEFORE_SURVEY_DIR AFTER_SURVEY_DIR OUTPUT_DIR

Both generator builds must actually have produced the saved parameter sets.
Reject any change outside cutoff base. Shift the recorded PRE-CLIP cutoff
sum by the raw-base difference / 256, clamp as stock does, and render both
with the same digital voice. No other CV, gate or random value changes.
This isolates the filter change; it is NOT a second firmware execution or
a measurement of analog hardware. Live tests separately verify installation.
"""
import argparse
import json
from pathlib import Path
import subprocess

import numpy as np

from bench import ensure_renderer, RENDER
from levels import metrics


def records(row):
    # The surveys capture the actual installed generator's post-condition,
    # pre-volume parameters. Filter edits must leave all other values alone.
    return np.asarray(row['pre_level'], dtype=int)


def shifted(cv, unclipped, delta):
    np.testing.assert_allclose(cv[:, 12], np.clip(unclipped, 0, 149), atol=2e-5, rtol=0)
    result = cv.copy()
    result[:, 12] = np.clip(unclipped + delta, 0, 149)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('before', type=Path)
    ap.add_argument('after', type=Path)
    ap.add_argument('out', type=Path)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    ensure_renderer()
    before = json.loads((args.before/'survey.json').read_text())
    after = {(r['name'], r['seed']): r for r in json.loads((args.after/'survey.json').read_text())}
    rows = []
    for a in before:
        b = after[(a['name'], a['seed'])]
        assert (a['hold_ms'], a['tail_ms']) == (b['hold_ms'], b['tail_ms'])
        changes = records(b)-records(a)
        assert not np.any(np.delete(changes, 81)), 'replay only supports a cutoff-base change'
        emitted_a = {(s, l): v for s, l, v in a['records'] if (s, l) != (9, 0)}
        emitted_b = {(s, l): v for s, l, v in b['records'] if (s, l) != (9, 0)}
        assert emitted_a == emitted_b, 'other emitted parameters changed'
        label = '%s-%08x' % (a['name'], a['seed'])
        capture = np.load(args.before/(label+'.cv.npz'))
        cv = capture['cv']
        candidate = shifted(cv, capture['cutoff_unclipped'], changes[81]/256.)
        row = dict(name=a['name'], seed=a['seed'], before_image=a['image_sha256'],
                   after_image=b['image_sha256'], cutoff_shift_st=changes[81]/256.)
        # Where the separate live run happened to have the same noise and
        # envelope trajectory, verify the affine replay against real DSP CVs.
        live = np.load(args.after/(label+'.cv.npz'))['cv']
        n = min(len(cv), len(live), 6400)  # first 200 ms, before note-off
        cols = [i for i in range(16) if i != 12] + [30]
        same_drive = n == 6400 and np.allclose(cv[:n, cols], live[:n, cols], atol=1e-6, rtol=0)
        row['live_cv_verified'] = bool(same_drive)
        if same_drive:
            row['live_cv_max_error_st'] = float(np.max(np.abs(candidate[:n, 12]-live[:n, 12])))
            # CParam's FP interpolation can leave a small residue. Allow
            # a quarter of one raw cutoff step (1/256 st), < 0.1 cent.
            np.testing.assert_allclose(candidate[:n, 12], live[:n, 12], atol=1/1024, rtol=0)
        for version, frames in [('before', cv), ('after', candidate)]:
            src = args.out/(label+'-'+version+'.cv.f32')
            raw = args.out/(label+'-'+version+'.audio.f32')
            frames.astype('<f4').tofile(src)
            subprocess.run([str(RENDER), str(src), str(src.with_suffix('.wav')),
                            str(int(capture['sync'])), str(raw)], check=True, capture_output=True)
            row[version] = metrics(np.fromfile(raw, dtype='<f4'))
            src.unlink()
        rows.append(row)
        print('%-12s %08x %6.2f -> %6.2f dB' %
              (a['name'], a['seed'], row['before']['level_db'], row['after']['level_db']), flush=True)
    (args.out/'comparison.json').write_text(json.dumps(rows, indent=2)+'\n')


if __name__ == '__main__':
    main()
