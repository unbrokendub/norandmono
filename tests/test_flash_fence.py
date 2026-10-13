#!/usr/bin/env python3
"""A2b: the project journal writer must not trip the watchdog.

0x08143378 erases four 128 KB sectors of internal flash bank 1 and programs
512 KB with main spinning in the HAL wait loop -- seconds, and past the 16 s
IWDG at the sector-erase maximum.  The stock has no watchdog and merely
freezes; patch A2 turned that into a reboot.  np_flash_write flags the
operation and np_systick_irq feeds the dog while it is flagged, for at most
60 s so a writer stuck in its own `b .` still ends in a reset.
"""
import sys, struct
sys.path.insert(0,'tests')
from sysemu import Mono, VEC_SYSTICK
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_LR
IMG='build/firmware-patched.bin'
DIAG=0x2000E400; IWDG_KR=0x58004800; UWTICK=0x20000174
img=open(IMG,'rb').read()
FENCE=struct.unpack_from('<I', img, 0x75200+0x178)[0] & ~1
m=Mono(IMG); m.boot(); m.factory_defaults(); m.settle(100, audio=1)
uc=m.uc
u32=lambda a: struct.unpack('<I', bytes(uc.mem_read(a,4)))[0]
feeds=[]
uc.hook_add(UC_HOOK_MEM_WRITE, lambda u,ac,ad,sz,v,d: feeds.append(v) if v==0xAAAA else None, begin=IWDG_KR, end=IWDG_KR+3)
# stub the real writer: return immediately, but first "spend" 5 seconds by
# ticking SysTick 20000 times with uwTick advancing (the fence is active meanwhile)
def stub(u,a,s,d):
    for i in range(20000):
        uc.mem_write(UWTICK, struct.pack('<I', u32(UWTICK)+1)) if i % 4 == 0 else None
        m.inject(VEC_SYSTICK)
    u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))
uc.hook_add(UC_HOOK_CODE, stub, begin=0x08143378, end=0x08143378)
before=len(feeds)
m.call(FENCE, 0x24000000, 0x80000, 0)
fed=len(feeds)-before
print("fence: writes=%d  max cycles=%d  active after=%d  fed=%d  refreshes during the stubbed write: %d" % (
    u32(DIAG+0xF94), u32(DIAG+0xF98), u32(DIAG+0xF90), u32(DIAG+0xF9C), fed))
assert u32(DIAG+0xF94) == 1 and u32(DIAG+0xF90) == 0 and u32(DIAG+0xF98) > 0
assert fed == 20000, fed
# and the 60 s cap: pretend the write started 61 s ago
uc.mem_write(DIAG+0xF90, struct.pack('<I', 1))
uc.mem_write(DIAG+0xFA0, struct.pack('<I', (u32(UWTICK)-61000) & 0xFFFFFFFF))
before=len(feeds)
for _ in range(100): m.inject(VEC_SYSTICK)
capped=len(feeds)-before
print("past the 60 s cap: refreshes in 100 ticks = %d (want 0)" % capped)
assert capped == 0
uc.mem_write(DIAG+0xF90, b'\0\0\0\0')
before=len(feeds)
for _ in range(100): m.inject(VEC_SYSTICK)
idle=len(feeds)-before
print("not active: refreshes in 100 ticks = %d (want 0)" % idle)
assert idle == 0
print("PASS -- the journal writer is fenced: fed while active, capped at 60 s, silent otherwise")
