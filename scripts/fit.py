"""Minimal FIT reader: who recorded an activity, the device's own totals, and
its speed series. Stdlib only.

Strava recomputes distance from a watch's GPS points. When the watch GPS
stalls, that recount runs short (up to ~28% on some Huawei runs) and the
stalled stretch counts as "stopped". The watch's own FIT session distance and
speed series don't have that problem, so the reports prefer them.
"""
import struct
from datetime import datetime, timedelta

FIT_EPOCH = datetime(1989, 12, 31)  # FIT timestamps are seconds since this, UTC
STRAVA_APP = 265                     # manufacturer id of Strava's own phone recorder
MAX_SAMPLE_S = 10                    # a longer gap between speed samples is a pause

# base type -> (struct format, invalid value)
BASE = {0x00: ("B", 0xFF), 0x01: ("b", 0x7F), 0x02: ("B", 0xFF), 0x83: ("h", 0x7FFF),
        0x84: ("H", 0xFFFF), 0x85: ("i", 0x7FFFFFFF), 0x86: ("I", 0xFFFFFFFF),
        0x0A: ("B", 0), 0x8B: ("H", 0), 0x8C: ("I", 0), 0x0D: ("B", 0xFF)}


def messages(data):
    """[(global message number, {field number: value})], or [] if not a FIT file."""
    if len(data) < 12 or data[8:12] != b".FIT":
        return []
    i, end = data[0], data[0] + struct.unpack("<I", data[4:8])[0]
    defs, out, last_ts = {}, [], None
    while i < end:
        h = data[i]
        i += 1
        if h & 0x80:  # compressed-timestamp data message
            local, offset = (h >> 5) & 0x03, h & 0x1F
        elif h & 0x40:  # definition message
            e = ">" if data[i + 1] else "<"
            gnum, nf = struct.unpack(e + "H", data[i + 2:i + 4])[0], data[i + 4]
            i += 5
            fields = [tuple(data[i + 3 * k:i + 3 * k + 3]) for k in range(nf)]
            i += 3 * nf
            dev = 0
            if h & 0x20:  # developer fields: skip their bytes
                nd = data[i]
                dev = sum(data[i + 1 + 3 * k + 1] for k in range(nd))
                i += 1 + 3 * nd
            defs[h & 0x0F] = (gnum, e, fields, dev)
            continue
        else:
            local, offset = h & 0x0F, None
        gnum, e, fields, dev = defs[local]
        vals = {}
        for num, size, base in fields:
            fmt, invalid = BASE.get(base, (None, None))
            if fmt and struct.calcsize("<" + fmt) == size:
                v = struct.unpack(e + fmt, data[i:i + size])[0]
                if v != invalid:
                    vals[num] = v
            i += size
        i += dev
        if offset is not None and last_ts is not None:
            ts = (last_ts & ~0x1F) + offset
            vals[253] = ts + 0x20 if offset < (last_ts & 0x1F) else ts
        last_ts = vals.get(253, last_ts)
        out.append((gnum, vals))
    return out


def summary(path):
    """Device totals and speed series of one FIT file, or None if it isn't one.

    speed is [(seconds from session start, m/s)]. Huawei writes the whole
    series up front under a handful of timestamps; it is evenly spaced over the
    session (every 5 s), so it is spread back out over the elapsed time.
    """
    msgs = messages(path.read_bytes())
    if not msgs:
        return None
    file_id = next((v for g, v in msgs if g == 0), {})
    session = next((v for g, v in msgs if g == 18), {})
    elapsed = session.get(7, 0) / 1000
    recs = [v for g, v in msgs if g == 20 and 6 in v]
    times = [v.get(253) for v in recs]
    if recs and (None in times or len(set(times)) < len(times) / 2):
        step = elapsed / len(recs)
        rel = [k * step for k in range(len(recs))]
    else:
        t0 = session.get(2, times[0] if times else 0)
        rel = [t - t0 for t in times]
    return {"manufacturer": file_id.get(1),
            "start": FIT_EPOCH + timedelta(seconds=session[2]) if 2 in session else None,
            "km": session[9] / 100000 if 9 in session else None,
            "elapsed_s": elapsed,
            "speed": [(t, v[6] / 1000) for t, v in zip(rel, recs)]}


def _spans(fs):
    """(start, seconds, m/s) per speed sample, long gaps capped as pauses."""
    sp = fs["speed"]
    ends = [t for t, _ in sp[1:]] + [max(fs["elapsed_s"], sp[-1][0] if sp else 0)]
    return [(t, min(e - t, MAX_SAMPLE_S), v) for (t, v), e in zip(sp, ends)]


def moving_seconds(fs, threshold):
    """Seconds of the speed series at or above threshold m/s."""
    return sum(dt for _, dt, v in _spans(fs) if v >= threshold)


def distance_curve(fs):
    """[(seconds from start, cumulative m)], scaled to end on the device's own total."""
    out, d = [], 0.0
    for t, dt, v in _spans(fs):
        out.append((t, d))
        d += v * dt
    out.append((fs["elapsed_s"], d))
    k = fs["km"] * 1000 / d if fs["km"] and d else 1
    return [(t, x * k) for t, x in out]
