"""Double-precision VFP for Unicorn's Cortex-M7 model, which lacks it.

The stock firmware uses FP64 in a handful of places (libstdc++ formatting,
the waveform-to-CV mapper, transport maths, a few constructors).  Unicorn's
M7 raises UC_ERR_INSN_INVALID on them; this shim catches that hook, decodes
the instruction with capstone and executes the small subset the image
actually uses on the real D registers and memory, then steps PC.  Anything
outside the subset is reported, not guessed.
"""
import math
import struct
import capstone
from unicorn import UC_HOOK_INSN_INVALID
from unicorn.arm_const import *

_md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
_md.detail = True
_md_v = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
_md_v.detail = True

DREG = {('d%d' % i): globals()['UC_ARM_REG_D%d' % i] for i in range(16)}
SREG = {('s%d' % i): globals()['UC_ARM_REG_S%d' % i] for i in range(32)}
GREG = {('r%d' % i): globals()['UC_ARM_REG_R%d' % i] for i in range(13)}
GREG.update({'sb': UC_ARM_REG_R9, 'sl': UC_ARM_REG_R10, 'fp': UC_ARM_REG_R11,
             'ip': UC_ARM_REG_R12, 'sp': UC_ARM_REG_SP, 'lr': UC_ARM_REG_LR, 'pc': UC_ARM_REG_PC})

FPSCR_N, FPSCR_Z, FPSCR_C, FPSCR_V = 1 << 31, 1 << 30, 1 << 29, 1 << 28


def _u64(f):
    return struct.unpack('<Q', struct.pack('<d', f))[0]


def _f64(u):
    return struct.unpack('<d', struct.pack('<Q', u & 0xFFFFFFFFFFFFFFFF))[0]


def _f32(u):
    return struct.unpack('<f', struct.pack('<I', u & 0xFFFFFFFF))[0]


def _u32(f):
    f = max(min(f, 3.4028235e38), -3.4028235e38) if f == f else f
    return struct.unpack('<I', struct.pack('<f', f))[0]


class Fp64Shim:
    def __init__(self, uc, image, base):
        self.uc = uc
        self.image = image
        self.base = base
        self.count = 0
        self.unhandled = []
        self.cache = {}
        self.names = {}
        uc.hook_add(UC_HOOK_INSN_INVALID, self.invalid)

    # ---- register helpers -------------------------------------------------
    def rd(self, name):
        uc = self.uc
        if name in DREG: return _f64(uc.reg_read(DREG[name]))
        if name in SREG: return _f32(uc.reg_read(SREG[name]))
        return uc.reg_read(GREG[name])

    def wd(self, name, value):
        self.uc.reg_write(DREG[name], _u64(value))

    def cond_pass(self, cc):
        apsr = self.uc.reg_read(UC_ARM_REG_APSR)
        n, z, c, v = (apsr >> 31) & 1, (apsr >> 30) & 1, (apsr >> 29) & 1, (apsr >> 28) & 1
        return {capstone.arm.ARM_CC_EQ: z == 1, capstone.arm.ARM_CC_NE: z == 0,
                capstone.arm.ARM_CC_HS: c == 1, capstone.arm.ARM_CC_LO: c == 0,
                capstone.arm.ARM_CC_MI: n == 1, capstone.arm.ARM_CC_PL: n == 0,
                capstone.arm.ARM_CC_VS: v == 1, capstone.arm.ARM_CC_VC: v == 0,
                capstone.arm.ARM_CC_HI: c == 1 and z == 0, capstone.arm.ARM_CC_LS: c == 0 or z == 1,
                capstone.arm.ARM_CC_GE: n == v, capstone.arm.ARM_CC_LT: n != v,
                capstone.arm.ARM_CC_GT: z == 0 and n == v, capstone.arm.ARM_CC_LE: z == 1 or n != v,
                capstone.arm.ARM_CC_AL: True, capstone.arm.ARM_CC_INVALID: True}[cc]

    def mem_addr(self, op):
        m = op.mem
        base = self.uc.reg_read(UC_ARM_REG_PC) & ~3 if m.base == capstone.arm.ARM_REG_PC else \
            self.uc.reg_read(_REGMAP[m.base])
        if m.base == capstone.arm.ARM_REG_PC:
            base = (self.pc + 4) & ~3
        return (base + m.disp) & 0xFFFFFFFF

    # ---- the hook ---------------------------------------------------------
    def invalid(self, uc, data):
        self.pc = pc = uc.reg_read(UC_ARM_REG_PC)
        ins = self.cache.get(pc)
        if ins is None:
            code = bytes(uc.mem_read(pc, 4))
            ins = next(_md_v.disasm(code, pc, 1), None)
            if ins is None:
                self.unhandled.append((pc, code.hex(), '?'))
                return False
            self.cache[pc] = ins
        ok = self.execute(ins)
        if not ok:
            self.unhandled.append((pc, code.hex(), ins.mnemonic + ' ' + ins.op_str))
            return False
        self.count += 1
        uc.reg_write(UC_ARM_REG_PC, (pc + ins.size) | 1)
        return True

    def execute(self, ins):
        uc = self.uc
        mn = ins.mnemonic
        ops = ins.operands
        names = self.names.get(ins.address)
        if names is None:
            names = [(_regname(o) if o.type == capstone.arm.ARM_OP_REG else None) for o in ops]
            self.names[ins.address] = names
        cc = ins.cc
        base_mn = mn
        # strip condition suffix capstone folds into mnemonics (vmovpl, vseleq)
        if cc not in (capstone.arm.ARM_CC_AL, capstone.arm.ARM_CC_INVALID) and not mn.startswith('vsel'):
            if not self.cond_pass(cc):
                return True
            for suf in ('eq', 'ne', 'hs', 'lo', 'mi', 'pl', 'vs', 'vc', 'hi', 'ls', 'ge', 'lt', 'gt', 'le', 'cs', 'cc'):
                if mn.split('.')[0].endswith(suf) and mn.split('.')[0] != 'v' + suf:
                    base_mn = mn.split('.')[0][:-2] + ('.' + mn.split('.', 1)[1] if '.' in mn else '')
                    break
        m = base_mn
        if m == 'vldr':
            addr = self.mem_addr(ops[1])
            if names[0] in DREG:
                uc.reg_write(DREG[names[0]], struct.unpack('<Q', bytes(uc.mem_read(addr, 8)))[0])
            else:
                uc.reg_write(SREG[names[0]], struct.unpack('<I', bytes(uc.mem_read(addr, 4)))[0])
            return True
        if m == 'vstr':
            addr = self.mem_addr(ops[1])
            if names[0] in DREG:
                uc.mem_write(addr, struct.pack('<Q', uc.reg_read(DREG[names[0]])))
            else:
                uc.mem_write(addr, struct.pack('<I', uc.reg_read(SREG[names[0]])))
            return True
        if m == 'vmov.f64':
            if ops[1].type == capstone.arm.ARM_OP_FP:
                self.wd(names[0], ops[1].fp)
            else:
                uc.reg_write(DREG[names[0]], uc.reg_read(DREG[names[1]]))
            return True
        if m == 'vmov' and len(ops) == 3 and names[2] in DREG:      # vmov r0, r1, d0
            v = uc.reg_read(DREG[names[2]])
            uc.reg_write(GREG[names[0]], v & 0xFFFFFFFF); uc.reg_write(GREG[names[1]], v >> 32)
            return True
        if m == 'vmov' and len(ops) == 3 and names[0] in DREG:      # vmov d0, r0, r1
            uc.reg_write(DREG[names[0]], uc.reg_read(GREG[names[1]]) | (uc.reg_read(GREG[names[2]]) << 32))
            return True
        if m in ('vadd.f64', 'vsub.f64', 'vmul.f64', 'vdiv.f64', 'vfma.f64', 'vfms.f64', 'vnmul.f64',
                 'vmla.f64', 'vmls.f64'):
            a, b = self.rd(names[1]), self.rd(names[2])
            if m == 'vadd.f64': r = a + b
            elif m == 'vsub.f64': r = a - b
            elif m == 'vmul.f64': r = a * b
            elif m == 'vnmul.f64': r = -(a * b)
            elif m == 'vdiv.f64':
                r = a / b if b != 0 else (math.copysign(math.inf, a) * math.copysign(1, b) if a != 0 else math.nan)
            elif m in ('vfma.f64', 'vmla.f64'): r = self.rd(names[0]) + a * b
            else: r = self.rd(names[0]) - a * b
            self.wd(names[0], r)
            return True
        if m in ('vneg.f64', 'vabs.f64', 'vsqrt.f64'):
            a = self.rd(names[1])
            self.wd(names[0], -a if m == 'vneg.f64' else abs(a) if m == 'vabs.f64' else math.sqrt(a) if a >= 0 else math.nan)
            return True
        if m == 'vcvt.f64.f32':
            self.wd(names[0], self.rd(names[1])); return True
        if m == 'vcvt.f32.f64':
            uc.reg_write(SREG[names[0]], _u32(self.rd(names[1]))); return True
        if m in ('vcvt.f64.u32', 'vcvt.f64.s32'):
            v = uc.reg_read(SREG[names[1]])
            if m.endswith('s32') and v & 0x80000000: v -= 1 << 32
            self.wd(names[0], float(v)); return True
        if m in ('vcvt.u32.f64', 'vcvt.s32.f64', 'vcvtr.u32.f64', 'vcvtr.s32.f64'):
            a = self.rd(names[1])
            if a != a: v = 0
            elif m.startswith('vcvtr'): v = int(round(a))
            else: v = int(a) if abs(a) < 1e300 else (2**31 - 1 if a > 0 else -2**31)
            if 'u32' in m: v = max(0, min(v, 0xFFFFFFFF))
            else: v = max(-2**31, min(v, 2**31 - 1)) & 0xFFFFFFFF
            uc.reg_write(SREG[names[0]], v); return True
        if m in ('vcmp.f64', 'vcmpe.f64'):
            a = self.rd(names[0])
            b = ops[1].fp if ops[1].type == capstone.arm.ARM_OP_FP else self.rd(names[1])
            fpscr = uc.reg_read(UC_ARM_REG_FPSCR) & 0x0FFFFFFF
            if a != a or b != b: fpscr |= FPSCR_C | FPSCR_V
            elif a == b: fpscr |= FPSCR_Z | FPSCR_C
            elif a < b: fpscr |= FPSCR_N
            else: fpscr |= FPSCR_C
            uc.reg_write(UC_ARM_REG_FPSCR, fpscr); return True
        if m in ('vrintm.f64', 'vrinta.f64', 'vrintp.f64', 'vrintz.f64', 'vrintn.f64'):
            a = self.rd(names[1])
            if a != a or a in (math.inf, -math.inf): r = a
            elif m == 'vrintm.f64': r = math.floor(a)
            elif m == 'vrintp.f64': r = math.ceil(a)
            elif m == 'vrintz.f64': r = math.trunc(a)
            elif m == 'vrinta.f64': r = math.floor(a + 0.5) if a >= 0 else -math.floor(-a + 0.5)
            else: r = round(a)
            self.wd(names[0], float(r)); return True
        if mn.startswith('vsel') and mn.endswith('.f64'):
            cond = {'vseleq': capstone.arm.ARM_CC_EQ, 'vselge': capstone.arm.ARM_CC_GE,
                    'vselgt': capstone.arm.ARM_CC_GT, 'vselvs': capstone.arm.ARM_CC_VS}[mn.split('.')[0]]
            src = names[1] if self.cond_pass(cond) else names[2]
            uc.reg_write(DREG[names[0]], uc.reg_read(DREG[src])); return True
        if m in ('vmaxnm.f64', 'vminnm.f64'):
            a, b = self.rd(names[1]), self.rd(names[2])
            if a != a: r = b
            elif b != b: r = a
            else: r = max(a, b) if m.startswith('vmax') else min(a, b)
            self.wd(names[0], r); return True
        return False


_REGMAP = {}


def _regname(op):
    return capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB).reg_name(op.reg)


def _build_regmap():
    cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    for i in range(13):
        _REGMAP[getattr(capstone.arm, 'ARM_REG_R%d' % i)] = GREG['r%d' % i]
    _REGMAP[capstone.arm.ARM_REG_SP] = UC_ARM_REG_SP
    _REGMAP[capstone.arm.ARM_REG_LR] = UC_ARM_REG_LR
    _REGMAP[capstone.arm.ARM_REG_PC] = UC_ARM_REG_PC


_build_regmap()
