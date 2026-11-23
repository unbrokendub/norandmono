#!/usr/bin/env python3
"""Level bench regression; --firmware also checks the actual DSP's VCA CV.

The renderer must not apply the diagnostic ADSR value to CV13 again.
Measured reference: at unity OutVol and sustain 1/4, the firmware publishes
CV13 ~= .89966, which already represents gain 1/4 (not 1/16).
"""
import argparse
import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/bench'))
from bench import Bench, ensure_renderer, RENDER
from levels import K48, retime, metrics
from filter_ab import shifted
from fast_capture import AudioReplay


class LevelsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_renderer()

    def render(self, cv):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            cv.astype('<f4').tofile(p / 'cv.f32')
            subprocess.run([str(RENDER), str(p/'cv.f32'), str(p/'preview.wav'),
                            '0', str(p/'raw.f32')], check=True, capture_output=True)
            return np.fromfile(p/'raw.f32', dtype='<f4'), (p/'preview.wav').read_bytes()

    def tone(self):
        cv = np.zeros((16000, 31), dtype='<f4')
        cv[:, 2] = cv[:, 9] = cv[:, 13] = cv[:, 30] = 1
        cv[:, 10:12] = 69
        cv[:, 12] = 149
        cv[:, 15] = .5
        return cv

    def test_envelope_is_already_in_vca_cv(self):
        cv = self.tone()
        full, _ = self.render(cv)
        cv[:, 30] = .25
        diagnostic_only, _ = self.render(cv)
        np.testing.assert_array_equal(diagnostic_only, full)
        # A quarter VCA gain, encoded as the stock CV mapper encodes it.
        cv[:, 13] = 1 - .0502 * 2
        quarter, _ = self.render(cv)
        # The hardware emits a 16-bit DAC word, not the continuous CV.
        cvq = 1-int((1-float(cv[0,13]))*65535)/65535
        gain = 2**((cvq-1)/.0502)
        np.testing.assert_allclose(quarter, full*gain, atol=2e-7, rtol=2e-6)

    def test_raw_output_preserves_level_preview_does_not(self):
        cv = self.tone()
        full, wav1 = self.render(cv)
        cv[:, 13] -= .0502
        half, wav2 = self.render(cv)
        delta = metrics(half)['level_db'] - metrics(full)['level_db']
        cvq = 1-int((1-float(cv[0,13]))*65535)/65535
        expected_db=20*math.log10(2)*(cvq-1)/.0502
        self.assertAlmostEqual(delta, expected_db, places=4)
        a = np.frombuffer(wav1, dtype='<i2', offset=44)
        b = np.frombuffer(wav2, dtype='<i2', offset=44)
        self.assertLessEqual(np.max(np.abs(a.astype(int) - b)), 1)

    def test_filter_retiming(self):
        f = np.geomspace(20, 14000, 300)
        def response(fs):
            q = np.exp(-2j*np.pi*f/fs)
            h = np.ones(len(f), complex)
            for b, a in K48:
                bb, aa = retime(b, a, fs)
                if fs == 48000:
                    np.testing.assert_allclose(bb, b, atol=1e-15)
                    np.testing.assert_allclose(aa, a, atol=1e-15)
                h *= (bb[0]+bb[1]*q+bb[2]*q*q)/(aa[0]+aa[1]*q+aa[2]*q*q)
            return h
        error = 20*np.log10(np.abs(response(32000)/response(48000)))
        self.assertLess(np.max(np.abs(error)), .05)

    def test_meter_rejects_invalid_capture(self):
        for x in ([], np.zeros(300), np.full(16000, np.nan)):
            with self.assertRaises(ValueError):
                metrics(x)
        self.assertEqual(metrics(np.zeros(16000))['peak'], 0)

    def test_filter_replay_uses_preclip_sum(self):
        cv = np.zeros((4, 31), dtype='<f4')
        unclip = np.array([-20., 40., 149., 180.], dtype='<f4')
        cv[:, 12] = np.clip(unclip, 0, 149)
        np.testing.assert_array_equal(shifted(cv, unclip, -40)[:, 12], [0, 0, 109, 140])
        np.testing.assert_array_equal(shifted(cv, unclip, 40)[:, 12], [20, 80, 149, 149])
        np.testing.assert_array_equal(shifted(cv, unclip, 0), cv)
        with self.assertRaises(AssertionError):
            shifted(cv, unclip+1, 0)

    def test_intermediate_cv_does_not_bypass_dac_word(self):
        cv=self.tone(); cv[:,2]=0; cv[:,5]=1
        full,_=self.render(cv)
        cv[:,5]=6.5848794e14  # real stock mapper zero-crossing capture
        saturated,_=self.render(cv)
        np.testing.assert_array_equal(saturated,full)
        self.assertTrue(np.isfinite(saturated).all())


def firmware_check():
    b = Bench()
    m = b.r.m
    note = b.r.index(41)
    values = {0: 0x3900, 2: 0, 3: 0xffff, 7: 0, 9: 0xffff,
              10: 0, 11: 0x8000, 12: 0x8000, 13: 0, 14: 0,
              15: 0x8000, 16: 0x4000, 17: 0xffff}
    for slot, value in values.items():
        m.submit_event(0x08164274, struct.pack('<HBBBxH', 0, note, slot, 0, value))
        m.settle(4)
    cv, _ = b.play(41, 100, 100)
    segment = cv[320:2560]
    assert len(segment) == 2240
    gain = np.exp2((segment[:, 13]-1)/.0502)
    np.testing.assert_allclose(segment[:, 30], .25, atol=.002)
    np.testing.assert_allclose(gain, .25, atol=.002)
    print('Real firmware: ADSR = %.6f, decoded VCA gain = %.6f (already includes ADSR)' %
          (np.mean(segment[:, 30]), np.mean(gain)), flush=True)


def replay_check():
    from bench import sysemu
    b = Bench(); m = b.r.m; fast = AudioReplay(m)
    used = list(range(16))+[30]
    for what in (0,3,10,19,26,36,52):
        b.r.randomize(b.r.index(41), force=what+1)
        m.press(41); m.settle(40); m.run_unmasked()
        fast.load(m)
        actual = fast.render(2)
        b.frames.clear(); b.cutoff_before_clip.clear()
        for _ in range(8):
            assert m.inject(sysemu.VEC_EXTI2)
        expected = np.frombuffer(b''.join(b.frames),dtype='<f4').reshape(-1,31)
        np.testing.assert_allclose(actual[:,used],expected[:,used],atol=1e-6,rtol=1e-5)
        np.testing.assert_allclose(actual[:,31],b.cutoff_before_clip,atol=1e-6,rtol=1e-5)
        m.release(41); m.settle(40)
    print('Host native-FP64 audio replay: 7 recipes x 64 samples match full M7 IRQ CVs')


def dac_check():
    from bench import E,sysemu
    from unicorn import UC_HOOK_CODE
    from unicorn.arm_const import UC_ARM_REG_R0
    b=Bench();m=b.r.m;packets=[];values=np.zeros(16,dtype='<f4')
    # Physical packet slots, as emitted at 0x081135B2..0x0811378A.
    mapping={0:2,1:3,2:1,3:0,4:7,5:6,6:4,7:5,8:11,9:10,13:15,14:13,15:12}
    def overwrite(u,*_):
        for k in mapping:u.mem_write(E+0x2eb4+4*k,struct.pack('<f',float(values[k])))
    def read(u,*_):
        packets.append(struct.unpack('<16H',u.mem_read(u.reg_read(UC_ARM_REG_R0),32)))
    b.uc.hook_add(UC_HOOK_CODE,overwrite,begin=0x081135b2,end=0x081135b2)
    b.uc.hook_add(UC_HOOK_CODE,read,begin=0x08115040,end=0x08115040)
    b.uc.ctl_flush_tb();m.run_unmasked()
    for val in (0,.01,.5,1,-3.1049163e-5,6.5848794e14):
        values[:]=val;packets.clear();assert m.inject(sysemu.VEC_EXTI2)
        assert len(packets)==8
        for k,slot in mapping.items():
            if k<8:
                x=float(np.float32(np.float32(1-values[k])*np.float32(65535)))
            elif k==15:
                x=float(np.float32(values[k]*np.float32(65535)))
            else:
                x=(1-float(values[k]))*65535
            word=int(min(4294967295,max(0,x)))&65535
            assert all(p[slot]==word for p in packets),(val,k,slot,word,packets[0])
    print('Real DAC packet: 13 channels x 6 cases x 8 samples match the bench conversion')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--firmware', action='store_true')
    ap.add_argument('--fast-firmware', action='store_true')
    args = ap.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(LevelsTest))
    if not result.wasSuccessful():
        sys.exit(1)
    if args.firmware:
        firmware_check()
    if args.fast_firmware:
        replay_check()
        dac_check()
