#!/usr/bin/env python3
"""Train a small generator-only gain predictor; never train on holdout data.

Input is unnormalised model audio measured by fast_levels.py. Its analog
model is not hardware calibration. The target is loudness at unit base
OutVol; modulation/clipping make this an estimate, so validate rebuilt
firmware with fresh seeds and real DSP captures before using coefficients.
"""
import argparse
import json
from pathlib import Path
import numpy as np
from bench import T


def features(raw):
    v = np.asarray(raw, float).reshape(19,9)/65536.
    p = v[:,0]
    s1, s2 = p[3]>=.09375 or v[3,4]>.5 or v[3,1]!=.5, p[7]>=.09375 or v[7,4]>.5 or v[7,1]!=.5
    pitch = min([p[i] for i,s in ((0,s1),(4,s2)) if s] or [p[0]])*256
    bp, hp = max(.5-p[11],0)*2, max(p[11]-.5,0)*2
    cube = lambda x: (2*x-1)**3
    cut = p[9]*256
    noise = any(v[i,6]*65536==4 and v[i,7]*65536==2 and v[i,1]!=.5 for i in (0,4))
    return dict(vco_max=max(p[3],p[7]), vco_min=min(p[3],p[7]),
                fenv_up=max(p[12]-.5,0), fenv_cube=cube(p[12]),
                bp=bp, hp=hp, attack=p[13], decay=p[14], sustain=p[16], release=p[15],
                cutoff=p[9], reso=p[10], tzfm=p[8],
                low_pitch=min(pitch-57,0)/24,
                lp_loss=np.clip((cut-pitch-12)/24,-3,0)*(1-hp)*(not noise),
                hp_loss=np.clip((cut-pitch-12)/24,0,4)*hp*(not noise),
                hp_cut=np.clip((cut-96)/24,0,3)*hp,
                volume_am=abs(cube(v[17,1])), volume_env=cube(v[17,4]) if not v[17,8] else 0,
                cut_env=cube(v[9,4]), cut_lfo=abs(cube(v[9,1])),
                pitch_lfo=max(abs(cube(v[0,1])),abs(cube(v[4,1]))),
                env_mod=max(abs(cube(v[14,1])),abs(cube(v[14,4]))),
                wave1=p[2], wave2=p[6])


def inputs(rows):
    feat = [features(r['pre_level']) for r in rows]
    names = list(feat[0])
    x = np.array([[f[n] for n in names] for f in feat])
    ids = np.array([T.NAMES.index(r['name']) for r in rows])
    levels = np.array([r['level_db'] for r in rows])
    vol = []
    for r in rows:
        values = {(s,l):val for s,l,val in r['records']}
        vol.append(values.get((17,0),T.reset_value(17,0))/65535)
    vol = np.array(vol)
    return names, x, ids, levels, vol


def fit(x, ids, y, ridge=.3):
    # Each recipe gets an intercept. Shrink shared slopes, not intercepts.
    z = np.column_stack((np.eye(54)[ids], x))
    penalty = np.r_[np.full(54,1e-8), np.full(x.shape[1],ridge)]
    robust = np.ones(len(y))
    for _ in range(8):
        w = np.linalg.solve(z.T@(robust[:,None]*z)+np.diag(penalty),z.T@(robust*y))
        # Rare near-silent draws must not raise the whole recipe, making
        # ordinary variants painfully loud. Huber threshold is 3 dB.
        robust = np.minimum(1,3/np.maximum(abs(y-z@w),1e-8))
    return w[:54],w[54:]


def predict_cv(x, ids, y, draws, ridge=.3):
    pred = np.zeros(len(y))
    for fold in range(4):
        test = draws%4==fold
        c,w = fit(x[~test],ids[~test],y[~test],ridge)
        pred[test] = c[ids[test]]+x[test]@w
    return pred


def summary(values, ids):
    medians=np.array([np.median(values[ids==i]) for i in range(54) if np.any(ids==i)])
    return dict(std=float(np.std(values)), p10p90=float(np.diff(np.percentile(values,[10,90]))[0]),
                median=float(np.median(values)), class_median_span=float(np.ptp(medians)),
                class_median_std=float(np.std(medians)), min=float(min(values)),max=float(max(values)))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('survey',type=Path)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--max-features',type=int,default=10)
    ap.add_argument('--target',type=float,default=-36)
    a=ap.parse_args();rows=json.loads(a.survey.read_text())
    names,x,ids,levels,vol=inputs(rows)
    assert set(ids)==set(range(54)), 'must cover the entire catalogue'
    y=levels-20*np.log10(vol)
    draws=np.array([r['draw'] for r in rows])
    selected=[]
    for _ in range(a.max_features):
        candidates=[]
        for j in range(len(names)):
            if j in selected:continue
            pred=predict_cv(x[:,selected+[j]],ids,y,draws)
            error=abs(y-pred)
            candidates.append((float(np.mean(np.where(error<3,error*error,6*error-9))),j))
        score,j=min(candidates);selected.append(j)
        print('add',names[j], 'CV Huber score',score**.5,flush=True)
    c,w=fit(x[:,selected],ids,y)
    pred=predict_cv(x[:,selected],ids,y,draws)
    newvol=np.minimum(1,np.power(10,(a.target-pred)/20))
    after=levels+20*np.log10(newvol/vol)
    result=dict(features=[names[j] for j in selected],intercepts=c.tolist(),weights=w.tolist(),
                target=a.target,train_rows=len(rows),before=summary(levels,ids),
                cv_estimate=summary(after,ids),note='gain estimate, requires actual rebuilt DSP validation')
    a.out.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('features','weights','before','cv_estimate')},indent=2))


if __name__=='__main__': main()
