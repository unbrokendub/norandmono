#!/usr/bin/env python3
"""Compare immutable, independent-seed all-54 surveys of actual images.

Checks patch identity outside the three allowed OutVol parameters. Noise
trajectories can differ across images due to main-loop RNG reseeding;
these are aggregate model results, not exact paired analog measurements.
"""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from bench import T
from fit_levels import inputs,summary


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('before',type=Path);ap.add_argument('after',type=Path)
    ap.add_argument('--out',type=Path,required=True)
    a=ap.parse_args()
    old=json.loads(a.before.read_text());new=json.loads(a.after.read_text())
    key=lambda r:(r['name'],r['seed'])
    old=sorted(old,key=key);new=sorted(new,key=key)
    assert len(old)==len(new) and len(old)==len({key(r) for r in old})
    assert set(r['name'] for r in old)==set(T.NAMES)
    for b,c in zip(old,new):
        assert key(b)==key(c)
        assert b['pre_level']==c['pre_level'],key(b)
        assert (b['hold_ms'],b['tail_ms'],b['replay'],b['renderer_sha256'])==(
            c['hold_ms'],c['tail_ms'],c['replay'],c['renderer_sha256'])
        params=lambda r:{(s,l):v for s,l,v in r['records'] if (s,l) not in ((17,0),(17,1),(17,4))}
        assert params(b)==params(c),key(b)
    _,_,ids,bv,bvol=inputs(old);_,_,_,av,avol=inputs(new)
    per=[]
    for i,name in enumerate(T.NAMES):
        ix=ids==i
        per.append(dict(name=name,draws=int(sum(ix)),before_median=float(np.median(bv[ix])),
                        after_median=float(np.median(av[ix])),before_std=float(np.std(bv[ix])),
                        after_std=float(np.std(av[ix]))))
    before,after=summary(bv,ids),summary(av,ids)
    centered_old=bv-np.median(bv);centered_new=av-np.median(av)
    report=dict(draws=len(old),recipes=54,before=before,after=after,
                median_shift_db=after['median']-before['median'],
                median_relative_outliers=dict(
                    before_loud_6db=int(sum(centered_old>6)),after_loud_6db=int(sum(centered_new>6)),
                    before_quiet_10db=int(sum(centered_old < -10)),after_quiet_10db=int(sum(centered_new < -10))),
                base_volume_boosts=int(sum(avol>bvol)),base_volume_cuts=int(sum(avol<bvol)),
                before_survey_sha256=hashlib.sha256(a.before.read_bytes()).hexdigest(),
                after_survey_sha256=hashlib.sha256(a.after.read_bytes()).hexdigest(),
                before_image=old[0]['image_sha256'],after_image=new[0]['image_sha256'],
                renderer_sha256=old[0]['renderer_sha256'],per_recipe=per,
                limits='Uncalibrated analog model, C4, stopped transport, 300 ms gate. Noise trajectories may differ. No hardware-cycle/loudness claim.')
    a.out.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='per_recipe'},indent=2))


if __name__=='__main__':main()
