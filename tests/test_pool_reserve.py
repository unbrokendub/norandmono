"""Reproduce thread allocations starving the interrupt-only allocation path,
and an interrupt burst exhausting the pools outright.

Execute the installed operator-new splice and real bitmap allocator. Only
ordinary malloc is replaced by a bounded non-overlapping host bump allocator;
this isolates heap fragmentation from pool admission, not the failure path.
Pool exhaustion from an interrupt is no longer fatal: it falls back to the
stock heap, as every ISR allocation did before the pools existed.
"""
import argparse
from pathlib import Path
from unicorn import UC_HOOK_CODE, UcError
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_PC, UC_ARM_REG_LR, UC_ARM_REG_IPSR
from test_patch import Machine, BASE, BLOB, POOL_META, POOL_SMALL, POOL_BIG


def run(image=None, expect_old_fault=False):
    m = Machine()
    if image:
        m.img = Path(image).read_bytes()
        m.reset()
    uc = m.uc
    early = m.u32(BLOB+0x10)
    m.call(early)
    m.w32(0x2000E4AC, 1)  # entered the main loop; thread pools are enabled
    heap_next = 0x30001000
    malloc_contexts = []

    def malloc(uc, addr, size, data):
        nonlocal heap_next
        malloc_contexts.append(uc.reg_read(UC_ARM_REG_IPSR))
        n = (uc.reg_read(UC_ARM_REG_R0)+7) & ~7
        assert heap_next+n <= POOL_META
        uc.reg_write(UC_ARM_REG_R0, heap_next)
        heap_next += n
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    uc.hook_add(UC_HOOK_CODE, malloc, begin=0x0815A0C0, end=0x0815A0C0)
    # A dense project / queued event batch can keep these alive. The old
    # admission policy classified *every* <=32-byte object as transient.
    small = [m.call(0x081591D4, n) for n in (8, 12, 16, 20)*128]
    big = [m.call(0x081591D4, 512) for _ in range(32)]
    assert len(set(small+big)) == len(small+big)
    before = len(malloc_contexts)
    uc.reg_write(UC_ARM_REG_IPSR, 27)  # DMA1_Stream0 external IRQ11
    if expect_old_fault:
        try:
            m.call(0x081591D4, 12)
        except UcError:
            assert m.u32(0x2000E474) == 1
            assert len(malloc_contexts) == before
            print('OLD IMAGE: thread fills both pools -> first ISR new(12) faults, heap still free')
            return
        raise AssertionError('old-image counterfactual did not reproduce the fault')

    irq_small = [m.call(0x081591D4, 12) for _ in range(64)]
    irq_big = [m.call(0x081591D4, 512) for _ in range(8)]
    assert irq_small == [POOL_SMALL+32*i for i in range(192, 256)]
    assert irq_big == [POOL_BIG+512*i for i in range(7, 15)]
    assert len(set(small+big+irq_small+irq_big)) == len(small+big+irq_small+irq_big)
    assert m.u32(0x2000E474) == 0
    assert len(malloc_contexts) == before
    # The reserve is gone too: a longer interrupt burst -- a mod-note key
    # press queues a few hundred event objects from the key-scan ISR --
    # must not fault.  It goes to the stock heap, counted, and the heap
    # blocks are distinct from every pool block.
    burst = [m.call(0x081591D4, n) for n in (12, 20, 40, 512, 8)*20]
    assert all(0x30001000 <= a < POOL_META for a in burst), [hex(a) for a in burst[:5]]
    assert len(set(small+big+irq_small+irq_big+burst)) == len(small+big+irq_small+irq_big+burst)
    assert m.u32(0x2000E474) == 100          # pool_failures: one per fallback
    assert malloc_contexts[before:] == [27]*100
    assert m.u32(0x2000E478) == 0            # no bad frees
    for addr in burst[::3]:
        m.call(0x08159070, addr)             # heap pointers reach stock free
    # Pool blocks still release through both installed delete entry points.
    for addr in irq_small[::2]+irq_big[::2]:
        m.call(0x08159070, addr)
    assert [m.call(0x081591D4, 20) for _ in range(32)] == irq_small[::2]
    assert [m.call(0x081591D4, 512) for _ in range(4)] == irq_big[::2]
    for addr in irq_small+irq_big:
        m.call(0x0815906C, addr)
    assert m.u32(0x2000E464) == 192 and m.u32(0x2000E46C) == 7
    assert m.u32(0x2000E478) == 0
    print('ISR reserves survive 512 small + 32 big thread allocations; a 100-object'
          ' ISR burst past the pools falls back to the heap instead of faulting: OK')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--image')
    parser.add_argument('--expect-old-fault', action='store_true')
    args = parser.parse_args()
    run(args.image, args.expect_old_fault)
