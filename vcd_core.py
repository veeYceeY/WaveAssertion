#!/usr/bin/env python3
"""
vcd_core.py — Shared VCD parsing and signal utilities.

Used by both vcd_analyzer.py (GUI) and sv_check.py (CLI).

Public API:
    load_vcd(filepath)          -> (path_signals, all_times)
    get_value_at(signal, time)  -> raw value string or None
    to_numeric(val)             -> int or float
    build_name_map(path_signals)-> (path_to_safe, safe_to_path)
"""

import sys
import os
import re

# resolve lib/ relative to this file's location
_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB  = os.path.join(_HERE, 'lib')
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

# ── VCD backend ───────────────────────────────────────────────────────────────

try:
    from pyDigitalWaveTools.vcd.parser import VcdParser
    from pyDigitalWaveTools.vcd.common import VcdVarScope
    _HAVE_PYDWT = True
except ImportError:
    VcdParser   = None
    VcdVarScope = None
    _HAVE_PYDWT = False


# ── Built-in fallback VCD parser ──────────────────────────────────────────────

class _Signal:
    """Signal holder for the fallback parser."""
    def __init__(self, name, sig_type, width):
        self.name    = name
        self.sigType = sig_type
        self.width   = width
        self.data    = []  # list of (time, value) tuples


def _parse_vcd_builtin(content):
    """
    Minimal built-in VCD parser.
    Returns (path_signals dict, all_times list).
    path_signals: full_path -> _Signal  (data = list of (time, value) tuples)
    """
    signals     = {}   # identifier -> _Signal
    id_to_path  = {}   # identifier -> full_path
    path_sigs   = {}
    scope_stack = []
    cur_time    = 0
    times_set   = set()

    tokens = content.split()
    i, n   = 0, len(tokens)

    def collect_to_end():
        nonlocal i
        buf = []
        while i < n and tokens[i] != '$end':
            buf.append(tokens[i])
            i += 1
        i += 1
        return buf

    while i < n:
        tok = tokens[i]; i += 1

        if tok == '$scope':
            parts = collect_to_end()
            name  = parts[1] if len(parts) > 1 else (parts[0] if parts else 'scope')
            scope_stack.append(name)

        elif tok == '$upscope':
            collect_to_end()
            if scope_stack:
                scope_stack.pop()

        elif tok == '$var':
            parts = collect_to_end()
            if len(parts) >= 4:
                sig_type = parts[0]
                width    = int(parts[1]) if parts[1].isdigit() else 1
                var_id   = parts[2]
                var_name = parts[3]
                full     = '.'.join(scope_stack + [var_name])
                sig = _Signal(var_name, sig_type, width)
                signals[var_id]    = sig
                id_to_path[var_id] = full
                path_sigs[full]    = sig

        elif tok.startswith('$'):
            collect_to_end()

        elif tok.startswith('#'):
            try:
                cur_time = int(tok[1:])
                times_set.add(cur_time)
            except ValueError:
                pass

        elif len(tok) >= 2 and tok[0] in '01xzXZ':
            var_id = tok[1:]
            if var_id in signals:
                signals[var_id].data.append((cur_time, tok[0]))

        elif tok[0] in 'bBrR':
            val = tok
            if i < n:
                var_id = tokens[i]; i += 1
                if var_id in signals:
                    signals[var_id].data.append((cur_time, val))

    return path_sigs, sorted(times_set)


# ── pyDigitalWaveTools traversal ──────────────────────────────────────────────

def _traverse(node, prefix, path_sigs):
    """Recursively collect VcdVarParsingInfo leaves into path_sigs."""
    name = getattr(node, 'name', '') or ''
    full = '{}.{}'.format(prefix, name) if (prefix and name) else (name or prefix)

    if hasattr(node, 'data') and node.data is not None:
        if full:
            path_sigs[full] = node

    children = getattr(node, 'children', None)
    if children:
        items = children.values() if isinstance(children, dict) else children
        for child in items:
            _traverse(child, full, path_sigs)


# ── Public API ────────────────────────────────────────────────────────────────

def load_vcd(filepath):
    """
    Parse a VCD file.  Returns (path_signals, all_times).

    path_signals  dict  full_path -> signal object
                        signal.data is a list of (time, value) tuples
    all_times     list  sorted list of every time point
    """
    with open(filepath, 'r') as fh:
        content = fh.read()

    if _HAVE_PYDWT:
        try:
            # pyDigitalWaveTools requires $enddefinitions $end.
            # Many simulators omit it, so inject it before the first #<time> marker.
            pydwt_content = content
            if '$enddefinitions' not in pydwt_content:
                pydwt_content = re.sub(
                    r'(#\d)', r'$enddefinitions $end\n\1', pydwt_content, count=1)

            parser = VcdParser()
            parser.parse_str(pydwt_content)

            path_sigs = {}
            for child in parser.scope.children.values():
                _traverse(child, '', path_sigs)

            times_set = set()
            for sig in path_sigs.values():
                for entry in sig.data:
                    times_set.add(entry[0])

            return path_sigs, sorted(times_set)

        except Exception:
            pass  # fall through to built-in parser

    return _parse_vcd_builtin(content)


def get_value_at(signal, time):
    """Return the held value of *signal* at *time* (last transition <= time)."""
    if not getattr(signal, 'data', None):
        return None
    last = None
    for entry in signal.data:
        if entry[0] <= time:
            last = entry[1]
        else:
            break
    return last


def to_numeric(val):
    """Convert a VCD value string to Python int or float."""
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return val
    s  = str(val).strip()
    sl = s.lower()
    if sl in ('x', 'z'):
        return 0
    if sl in ('0', '1'):
        return int(sl)
    if sl.startswith('b'):
        bits = sl[1:].replace('x', '0').replace('z', '0')
        try:
            return int(bits, 2)
        except ValueError:
            return 0
    if sl.startswith('r'):
        try:
            return float(sl[1:])
        except ValueError:
            return 0.0
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return 0


def build_name_map(path_signals):
    """
    Map every signal path to a unique safe Python identifier.
    Returns (path_to_safe, safe_to_path).
    """
    path_to_safe = {}
    safe_to_path = {}

    for path in path_signals:
        safe = re.sub(r'[^a-zA-Z0-9_]', '_', path)
        if safe and safe[0].isdigit():
            safe = 's_' + safe
        if not safe:
            safe = 's_unknown'

        base = safe; counter = 1
        while safe in safe_to_path:
            safe = '{}_{}'.format(base, counter)
            counter += 1

        path_to_safe[path] = safe
        safe_to_path[safe] = path

    return path_to_safe, safe_to_path
