"""Enumerate RTL-SDR dongles by serial — the stable way to address them.

Device *indices* (`-d 0`) are unstable: plugging in another dongle, or OP25
claiming one, renumbers them and an index-based service would suddenly drive the
wrong radio. Serial numbers don't move. We enumerate via librtlsdr's
get_device_usb_strings(), which reads USB descriptors WITHOUT opening the
device, so listing works even while OP25 (or our own service) is using a dongle.

rtl_power/rtl_fm accept a serial directly for `-d` (their verbose_device_search
matches an exact or suffix serial as well as an index), so a serial flows
straight through to the tools.
"""

import ctypes
import ctypes.util
import re
import subprocess


def _load_librtlsdr():
    for name in ("rtlsdr", "librtlsdr.so.0", "librtlsdr.so"):
        path = ctypes.util.find_library(name) or name
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    return None


def list_devices():
    """Return [{'index', 'serial', 'manufacturer', 'product'}] for all dongles.

    Falls back to parsing `rtl_test` output if librtlsdr can't be loaded via
    ctypes (the listing portion of rtl_test also doesn't open the device).
    """
    lib = _load_librtlsdr()
    if lib is not None:
        try:
            return _list_via_lib(lib)
        except Exception:
            pass
    return _list_via_rtl_test()


def _list_via_lib(lib):
    lib.rtlsdr_get_device_count.restype = ctypes.c_uint32
    lib.rtlsdr_get_device_usb_strings.argtypes = [
        ctypes.c_uint32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.rtlsdr_get_device_usb_strings.restype = ctypes.c_int

    out = []
    n = lib.rtlsdr_get_device_count()
    for i in range(n):
        manuf = ctypes.create_string_buffer(256)
        prod = ctypes.create_string_buffer(256)
        serial = ctypes.create_string_buffer(256)
        if lib.rtlsdr_get_device_usb_strings(i, manuf, prod, serial) != 0:
            continue
        out.append({
            "index": i,
            "serial": serial.value.decode(errors="replace"),
            "manufacturer": manuf.value.decode(errors="replace"),
            "product": prod.value.decode(errors="replace"),
        })
    return out


def _list_via_rtl_test():
    try:
        p = subprocess.run(["rtl_test"], capture_output=True, text=True,
                           timeout=8)
        text = p.stdout + p.stderr
    except Exception:
        return []
    out = []
    # lines like: "  0:  Nooelec, NESDR SMArt v5, SN: 19481419"
    for m in re.finditer(r"^\s*(\d+):\s*(.+?),\s*(.+?),\s*SN:\s*(\S+)",
                         text, re.M):
        out.append({
            "index": int(m.group(1)),
            "manufacturer": m.group(2).strip(),
            "product": m.group(3).strip(),
            "serial": m.group(4).strip(),
        })
    return out


def resolve_serial(spec):
    """Map a user spec (serial, suffix, or index) to a concrete serial.

    Returns the serial string, or None if no dongle matches. Used so a manual
    run can say --device 0 while the service uses a stable serial.
    """
    devs = list_devices()
    if not devs:
        return None
    s = str(spec)
    for d in devs:                       # exact serial
        if d["serial"] == s:
            return d["serial"]
    for d in devs:                       # suffix serial
        if d["serial"].endswith(s):
            return d["serial"]
    if s.isdigit():                      # index fallback
        i = int(s)
        for d in devs:
            if d["index"] == i:
                return d["serial"]
    return None


if __name__ == "__main__":
    devs = list_devices()
    if not devs:
        print("No RTL-SDR dongles found.")
    for d in devs:
        print(f"  index {d['index']}  SN {d['serial']:<12} "
              f"{d['manufacturer']} {d['product']}")
