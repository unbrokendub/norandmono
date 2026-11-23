CC      ?= cc
CFLAGS  ?= -std=c99 -O2 -Wall -Wextra -Wpedantic

SYX_FW  := mono_V2.2.0r.syx
SYX_BL  := mono_secondary_bootloader.syx

SRC     := src/norand/lzss.c src/norand/midi7.c src/norand/sysex_rx.c

.PHONY: all extract analyse test patch patch-dsp-ab test-patch test-patch-static test-ui test-flash clean

all: extract analyse test

## unpack both SysEx containers into raw images
extract: build/firmware.bin build/bootloader.bin

build/firmware.bin: $(SYX_FW) tools/norand_syx.py
	@mkdir -p build
	python3 tools/norand_syx.py unpack $< $@

build/bootloader.bin: $(SYX_BL) tools/norand_syx.py
	@mkdir -p build
	python3 tools/norand_syx.py unpack $< $@

## recover symbols and build the ELFs the disassemblers want
analyse: build/firmware.elf build/bootloader.elf docs/feature-map.txt \
         docs/peripherals-firmware.txt docs/peripherals-bootloader.txt

docs/feature-map.txt: build/firmware.symbols.json tools/featuremap.py
	python3 tools/featuremap.py > $@

docs/peripherals-%.txt: build/%.symbols.json tools/periph.py
	python3 tools/periph.py $* > $@

build/rtti.json: build/firmware.bin tools/rtti.py
	python3 tools/rtti.py build/firmware.bin > docs/rtti.txt

build/%.symbols.json: build/%.bin tools/scan.py build/rtti.json
	python3 tools/scan.py $*

build/%.elf: build/%.symbols.json tools/mkelf.py
	python3 tools/mkelf.py $*

build/%.asm: build/%.elf
	arm-none-eabi-objdump -d $< > $@

## verify the reconstructed C against the stock firmware file
test: build/test_container build/firmware.bin
	./build/test_container $(SYX_FW) build/firmware.bin
	python3 tools/norand_syx.py selftest $(SYX_FW) $(SYX_BL)

build/test_container: tests/test_container.c $(SRC)
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $^

## apply the audit fixes and produce a flashable .syx
patch: build/mono_V2.2.0r-patched.syx

## Build both images for a hardware DWT A/B run.  The default image is O3.
patch-dsp-ab: patch
	python3 tools/patch.py build --dsp-opt Os

build/mono_V2.2.0r-patched.syx: build/firmware.bin patch/np_patch.S patch/np_xenv.S patch/np_lfo.cpp patch/np_midi.cpp patch/np_runtime.cpp patch/np_diag.h patch/np_ui.cpp patch/np_random.cpp patch/np_mnrand.cpp patch/np.ld tools/patch.py
	python3 tools/patch.py build

## run the injected code under emulation (needs unicorn in build/venv)
test-patch: build/venv/bin/python build/mono_V2.2.0r-patched.syx
	build/venv/bin/python tests/test_patch.py
	$(MAKE) test-ui
	build/venv/bin/python tests/test_scale_random.py
	build/venv/bin/python tests/test_xenv.py
	build/venv/bin/python tests/test_pool_reserve.py
	build/venv/bin/python tests/test_modnote_dsp.py
	build/venv/bin/python tests/test_telemetry.py
	build/venv/bin/python tests/test_telemetry_p6.py
	build/venv/bin/python tests/test_ui_burst.py
	build/venv/bin/python tests/test_seq_sort.py
	build/venv/bin/python tests/test_evlog.py
	build/venv/bin/python tests/test_morph_trig.py
	build/venv/bin/python tests/test_seq_led.py
	build/venv/bin/python tests/test_qspi_defer.py
	build/venv/bin/python tests/test_key_bounce.py
	build/venv/bin/python tests/test_midi_flow.py
	build/venv/bin/python tests/test_modnote_switch.py
	build/venv/bin/python tests/test_modnote_random.py
	build/venv/bin/python tests/test_bench_levels.py --firmware --fast-firmware
	build/venv/bin/python tests/test_patch_random.py
	build/venv/bin/python tests/test_kits.py
	build/venv/bin/python tests/test_sync_load.py
	build/venv/bin/python tests/test_scale_library.py
	build/venv/bin/python tests/test_project_journal.py
	build/venv/bin/python tests/test_rotate_guard.py
	build/venv/bin/python tests/test_led_level.py
	build/venv/bin/python tests/test_bootloader_accepts.py
	build/venv/bin/python tests/test_bootloader_usb_rx.py
	build/venv/bin/python tests/test_bootloader_usb_setup.py

## tools/flash.py through the real CoreMIDI to a virtual port (macOS, no hardware)
test-flash: build/mono_V2.2.0r-patched.syx
	/usr/bin/python3 tests/test_flash_coremidi.py

test-ui: build/venv/bin/python build/mono_V2.2.0r-patched.syx
	build/venv/bin/python tests/probe_ui_chord.py
	build/venv/bin/python tests/test_ui_chord.py

## verify the final image without Unicorn (works on macOS ARM hosts too)
test-patch-static: build/mono_V2.2.0r-patched.syx tests/test_static_patch.py
	build/venv/bin/python tests/test_static_patch.py

build/venv/bin/python:
	python3 -m venv build/venv
	build/venv/bin/pip install --quiet unicorn

clean:
	rm -rf build
