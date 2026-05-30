#!/usr/bin/env python3
"""
sv_check.py — Check SystemVerilog assertions against a VCD file.

Usage:
    python3 sv_check.py <vcd_file> <sv_file> [options]

Options:
    --top   SCOPE   VCD scope prefix to prefer when resolving signal names
                    e.g.  --top tb.dut
    --from  TIME    start time (default 0)
    --to    TIME    end time   (default: end of VCD)
    --fail-only     print only failing checks
    --pass-only     print only passing checks
    --verbose       also print SKIP (vacuous) rows

Supported SVA subset:
    assert (<cond>);
    assert property (@(posedge|negedge CLK) COND);
    assert property (@(posedge|negedge CLK) ANT |->  CONS);
    assert property (@(posedge|negedge CLK) ANT |=>  CONS);  (= |-> ##1)
    ##N in consequent (N-cycle look-ahead on clock edges)
    property NAME; @(...) BODY; endproperty   named property
    LABEL: assert ...                         labeled assertion
    $rose(SIG)  $fell(SIG)  $stable(SIG)
    Bit literals: 1'b1  8'hFF  4'd10  4'b1010
    Operators:  &&  ||  !  &  |  ^  ~  ==  !=  <  >  <=  >=
"""

import sys
import os
import re
import argparse

from vcd_core import load_vcd, get_value_at, to_numeric, build_name_map


# ── Parsed assertion data class ───────────────────────────────────────────────

class Assertion:
    """One parsed assertion from the .sv file."""
    __slots__ = ('label', 'concurrent', 'clk_name', 'edge',
                 'antecedent_sv', 'consequent_sv', 'cons_delay',
                 'raw')

    def __init__(self):
        self.label         = None    # str or None
        self.concurrent    = False   # True = assert property
        self.clk_name      = None    # str
        self.edge          = 'posedge'
        self.antecedent_sv = None    # str or None  (None = no implication)
        self.consequent_sv = None    # str
        self.cons_delay    = 0       # ## delay on consequent
        self.raw           = ''      # original text for display

    def display_name(self):
        return self.label if self.label else '(unnamed)'

    def describe(self):
        if self.concurrent:
            clk = '@({} {})'.format(self.edge, self.clk_name)
            if self.antecedent_sv:
                op  = '|->' if self.cons_delay == 0 else '|-> ##{}'.format(self.cons_delay)
                return '{} {} {} {}'.format(clk, self.antecedent_sv, op, self.consequent_sv)
            return '{} {}'.format(clk, self.consequent_sv)
        return 'immediate: {}'.format(self.consequent_sv)


# ── SV source pre-processing ──────────────────────────────────────────────────

def _strip_comments(src):
    src = re.sub(r'/\*.*?\*/', ' ', src, flags=re.DOTALL)
    src = re.sub(r'//[^\n]*', ' ', src)
    return src


def _find_close_paren(s, pos):
    """Return index of ')' that closes the '(' at s[pos], or -1."""
    depth = 0
    for i in range(pos, len(s)):
        if s[i] == '(':
            depth += 1
        elif s[i] == ')':
            depth -= 1
            if depth == 0:
                return i
    return -1


# ── SV assertion parser ───────────────────────────────────────────────────────

_RE_PROPERTY_BLOCK = re.compile(
    r'\bproperty\s+(\w+)\s*;(.*?)endproperty', re.DOTALL)

_RE_CLOCK = re.compile(
    r'@\s*\(\s*(posedge|negedge)\s+(\w+)\s*\)')

_RE_DELAY = re.compile(r'^##(\d+)\s*')


def _parse_property_body(body, name):
    """
    Parse a property body string such as:
        @(posedge clk) ant |-> ##1 cons
        @(posedge clk) cond
    Returns a partially-filled Assertion or None on failure.
    """
    body = body.strip().rstrip(';').strip()
    a = Assertion()
    a.raw = body

    m = _RE_CLOCK.search(body)
    if m:
        a.concurrent = True
        a.edge       = m.group(1)
        a.clk_name   = m.group(2)
        body = body[m.end():].strip()
    else:
        a.concurrent = False

    # Implication operator  |->  (overlapping) or  |=>  (non-overlapping, +1 cycle)
    impl_re = re.compile(r'\|(?:->|=>)')
    im = impl_re.search(body)
    if im:
        a.antecedent_sv = body[:im.start()].strip()
        cons_part = body[im.end():].strip()
        a.cons_delay = 1 if body[im.start():im.end()] == '|=>' else 0
        dm = _RE_DELAY.match(cons_part)
        if dm:
            a.cons_delay += int(dm.group(1))
            cons_part = cons_part[dm.end():]
        a.consequent_sv = cons_part.strip()
    else:
        a.antecedent_sv = None
        a.consequent_sv = body.strip()

    if not a.consequent_sv:
        return None
    return a


def parse_sv_file(src):
    """
    Parse a .sv source string.
    Returns list of Assertion objects.
    """
    src = _strip_comments(src)

    # ── collect named properties ──────────────────────────────────────────────
    named = {}
    for m in _RE_PROPERTY_BLOCK.finditer(src):
        pname = m.group(1)
        body  = m.group(2)
        a = _parse_property_body(body, pname)
        if a:
            named[pname] = a

    # Remove property blocks from src so we don't double-parse their bodies
    src_no_props = _RE_PROPERTY_BLOCK.sub(' ', src)

    assertions = []
    i = 0
    n = len(src_no_props)

    while i < n:
        # skip whitespace / semicolons
        if src_no_props[i] in ' \t\n\r;':
            i += 1
            continue

        # ── look for optional label:  WORD  ':'  ─────────────────────────────
        label = None
        lm = re.match(r'(\w+)\s*:', src_no_props[i:])
        if lm:
            candidate = lm.group(1)
            # make sure it isn't a keyword
            if candidate not in ('assert', 'property', 'module', 'endmodule',
                                  'begin', 'end', 'always', 'initial',
                                  'posedge', 'negedge', 'input', 'output',
                                  'wire', 'reg', 'logic', 'integer'):
                label = candidate
                i += lm.end()
                # skip any whitespace between the label and 'assert'
                while i < n and src_no_props[i] in ' \t\n\r':
                    i += 1

        # ── assert keyword ────────────────────────────────────────────────────
        am = re.match(r'assert\s*', src_no_props[i:])
        if not am:
            i += 1
            continue

        i += am.end()

        # optional 'property'
        is_property = False
        pm = re.match(r'property\s*', src_no_props[i:])
        if pm:
            is_property = True
            i += pm.end()

        # expect '('
        if i >= n or src_no_props[i] != '(':
            continue

        close = _find_close_paren(src_no_props, i)
        if close < 0:
            continue

        inner = src_no_props[i+1:close].strip()
        i = close + 1

        # ── resolve named property reference ─────────────────────────────────
        if is_property and inner in named:
            a = named[inner]
            a2 = Assertion()
            for _s in Assertion.__slots__:
                setattr(a2, _s, getattr(a, _s))
            a2.label = label or inner
            assertions.append(a2)
            continue

        # ── inline property or immediate assertion ────────────────────────────
        a = _parse_property_body(inner, label or '?')
        if a:
            if not is_property:
                a.concurrent = False
            a.label = label
            assertions.append(a)

    return assertions


# ── SV expression → Python ────────────────────────────────────────────────────

_BIT_LIT = re.compile(
    r"(?:\d+)?'([bBoOdDhH])([0-9a-fA-FxXzZ_]+)")

def _convert_bit_literal(m):
    base = m.group(1).lower()
    digits = m.group(2).replace('_', '').lower()
    digits_clean = digits.replace('x', '0').replace('z', '0')
    try:
        if base == 'b':
            return str(int(digits_clean, 2))
        if base == 'o':
            return str(int(digits_clean, 8))
        if base in ('d', ''):
            return str(int(digits_clean, 10))
        if base == 'h':
            return '0x' + digits_clean
    except ValueError:
        pass
    return '0'


def _convert_sys_funcs(expr):
    """$rose(x) → _rose_x,  $fell(x) → _fell_x,  $stable(x) → _stable_x."""
    expr = re.sub(r'\$rose\s*\(\s*(\w+)\s*\)',   r'_rose_\1',   expr)
    expr = re.sub(r'\$fell\s*\(\s*(\w+)\s*\)',   r'_fell_\1',   expr)
    expr = re.sub(r'\$stable\s*\(\s*(\w+)\s*\)', r'_stable_\1', expr)
    return expr


def sv_to_python(expr, sv_to_safe):
    """
    Convert an SV boolean expression string to a Python eval-able string.
    sv_to_safe: {sv_signal_name: safe_python_identifier}
    """
    if not expr:
        return '1'

    expr = _convert_sys_funcs(expr)
    expr = _BIT_LIT.sub(_convert_bit_literal, expr)

    # logic operators
    expr = expr.replace('&&', ' and ')
    expr = expr.replace('||', ' or ')
    # ! → not  (but not != or <=)
    expr = re.sub(r'(?<![=!<>])!(?!=)', ' not ', expr)

    # replace signal names (longest first to avoid partial replacements)
    for sv_name in sorted(sv_to_safe, key=len, reverse=True):
        safe = sv_to_safe[sv_name]
        expr = re.sub(r'\b' + re.escape(sv_name) + r'\b', safe, expr)

    return expr.strip()


# ── signal name resolution ────────────────────────────────────────────────────

def _collect_sv_names(a):
    """
    Extract all bare identifiers from an assertion's SV expressions.
    Also includes _rose_X / _fell_X / _stable_X targets.
    """
    raw_parts = []
    if a.antecedent_sv:
        raw_parts.append(a.antecedent_sv)
    if a.consequent_sv:
        raw_parts.append(a.consequent_sv)
    if a.clk_name:
        raw_parts.append(a.clk_name)

    combined = ' '.join(raw_parts)
    # strip bit-vector literals before scanning so base chars (b, h, d…) aren't
    # mistaken for identifiers
    combined = _BIT_LIT.sub(' ', combined)
    # remove $rose/$fell/$stable wrappers but keep the argument identifier
    combined = re.sub(r'\$\w+\s*\(', '(', combined)
    # Extract identifiers including dot-chained forms (struct/interface members).
    # Greedy matching means  pkt.valid  is captured as one unit rather than as
    # two separate words; standalone names like  clk  are still captured normally.
    names = set(re.findall(r'\b([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*)\b', combined))
    # discard Python / SV keywords and single-letter base specifiers
    keywords = {
        'and', 'or', 'not', 'if', 'else', 'True', 'False',
        'posedge', 'negedge', 'property', 'endproperty',
        'assert', 'begin', 'end',
        'b', 'B', 'h', 'H', 'o', 'O', 'd', 'D', 'r', 'R',
    }
    return names - keywords


def resolve_signals(sv_names, path_signals, path_to_safe, top_scope=None):
    """
    Map each SV name to a safe Python identifier for the matching VCD signal.

    Resolution order for a name like  pkt.valid  or  clk:
      1. Exact full-path match            (tb.dut.pkt.valid  ==  tb.dut.pkt.valid)
      2. Suffix match on path components  (pkt.valid matches tb.dut.pkt.valid)
      3. Underscore-flattened suffix      (pkt.valid → pkt_valid, match last component)
      4. Last component only              (valid  matches  tb.dut.pkt.valid)
    At each step, if --top is given, paths under that scope are preferred.
    Returns (sv_to_safe dict, unresolved list).
    """
    sv_to_safe = {}
    unresolved = []

    def _pick(candidates):
        """From a list of candidate VCD paths, pick the best one."""
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        if top_scope:
            scoped = [p for p in candidates if p.startswith(top_scope + '.')]
            if scoped:
                return sorted(scoped)[0]
        return sorted(candidates)[0]

    # pre-build lookup by last N path components → list of full paths
    # e.g. 'pkt.valid' → ['tb.dut.pkt.valid', 'tb.other.pkt.valid']
    by_suffix = {}
    by_last   = {}
    for path in path_signals:
        parts = path.split('.')
        # index every tail suffix of length 1..len(parts)
        for length in range(1, len(parts) + 1):
            suffix = '.'.join(parts[-length:])
            by_suffix.setdefault(suffix, []).append(path)
        by_last.setdefault(parts[-1], []).append(path)

    for name in sv_names:
        # 1. exact full-path match
        if name in path_signals:
            sv_to_safe[name] = path_to_safe[name]
            continue

        # 2. suffix match (handles  pkt.valid, intf.req, a.b.c)
        pick = _pick(by_suffix.get(name, []))
        if pick:
            sv_to_safe[name] = path_to_safe[pick]
            continue

        # 3. underscore-flattened name (some simulators flatten  pkt.valid → pkt_valid)
        flat = name.replace('.', '_')
        pick = _pick(by_suffix.get(flat, []) or by_last.get(flat, []))
        if pick:
            sv_to_safe[name] = path_to_safe[pick]
            continue

        # 4. last component only (loose fallback for simple names)
        last = name.split('.')[-1]
        pick = _pick(by_last.get(last, []))
        if pick:
            sv_to_safe[name] = path_to_safe[pick]
            continue

        unresolved.append(name)

    return sv_to_safe, unresolved


# ── clock-edge finder ─────────────────────────────────────────────────────────

def find_clock_edges(clk_sv_name, sv_to_safe, path_signals,
                     safe_to_path, all_times, edge='posedge'):
    """Return sorted list of times where the clock has the requested edge."""
    safe = sv_to_safe.get(clk_sv_name)
    if safe is None:
        return []
    path = safe_to_path.get(safe)
    if path is None or path not in path_signals:
        return []

    sig   = path_signals[path]
    edges = []
    prev  = None
    for t in all_times:
        curr = to_numeric(get_value_at(sig, t))
        if prev is not None:
            if edge == 'posedge' and prev == 0 and curr == 1:
                edges.append(t)
            elif edge == 'negedge' and prev == 1 and curr == 0:
                edges.append(t)
        prev = curr
    return edges


# ── evaluation environment builder ───────────────────────────────────────────

def _build_env(sv_to_safe, path_signals, safe_to_path, t, prev_t=None):
    """
    Build the eval namespace for time *t*.
    Includes signal values and _rose_X / _fell_X / _stable_X helpers.
    """
    env = {}
    for sv_name, safe in sv_to_safe.items():
        path = safe_to_path.get(safe)
        if path is None or path not in path_signals:
            env[safe] = 0
            continue
        sig      = path_signals[path]
        curr_num = to_numeric(get_value_at(sig, t))
        env[safe] = curr_num

        if prev_t is not None:
            prev_num = to_numeric(get_value_at(sig, prev_t))
            env['_rose_'   + sv_name] = int(prev_num == 0 and curr_num != 0)
            env['_fell_'   + sv_name] = int(prev_num != 0 and curr_num == 0)
            env['_stable_' + sv_name] = int(prev_num == curr_num)
        else:
            env['_rose_'   + sv_name] = 0
            env['_fell_'   + sv_name] = 0
            env['_stable_' + sv_name] = 1

    return env


def _safe_eval(code, env):
    try:
        return bool(eval(code, {"__builtins__": {}}, env))
    except Exception:
        return None   # indeterminate


# ── per-assertion checker ─────────────────────────────────────────────────────

# result tags
PASS    = 'PASS'
FAIL    = 'FAIL'
SKIP    = 'SKIP'    # antecedent false  (vacuously true)
INDETER = '????'    # eval error


def check_assertion(a, path_signals, all_times, path_to_safe, safe_to_path,
                    top_scope, t_from, t_to):
    """
    Evaluate assertion *a* over the VCD.
    Returns list of dicts:
        time, result (PASS/FAIL/SKIP/????),
        ant_val (bool|None), cons_val (bool|None),
        env (dict of signal_name → numeric_value)
    """
    # resolve names used in this assertion
    sv_names   = _collect_sv_names(a)
    sv_to_safe, unresolved = resolve_signals(
        sv_names, path_signals, path_to_safe, top_scope)

    if unresolved:
        print('  [warn] could not resolve signals: {}'.format(
            ', '.join(sorted(unresolved))))

    safe_to_path_local = {v: k2
                          for k2, v in path_to_safe.items()}

    # compile expressions
    ant_code  = None
    cons_code = None
    try:
        if a.antecedent_sv:
            ant_py  = sv_to_python(a.antecedent_sv, sv_to_safe)
            ant_code = compile(ant_py, '<antecedent>', 'eval')
        cons_py  = sv_to_python(a.consequent_sv, sv_to_safe)
        cons_code = compile(cons_py, '<consequent>', 'eval')
    except SyntaxError as exc:
        print('  [error] expression compile failed: {}'.format(exc))
        return []

    results = []

    if a.concurrent:
        # ── concurrent: evaluate at each clock edge ───────────────────────────
        clock_edges = find_clock_edges(
            a.clk_name, sv_to_safe, path_signals,
            safe_to_path_local, all_times, a.edge)

        clock_edges = [t for t in clock_edges if t_from <= t <= t_to]

        for idx, t in enumerate(clock_edges):
            prev_t = clock_edges[idx - 1] if idx > 0 else None
            env    = _build_env(sv_to_safe, path_signals, safe_to_path_local, t, prev_t)

            # evaluate antecedent
            ant_val = None
            if ant_code is not None:
                ant_val = _safe_eval(ant_code, env)

            # vacuous pass: antecedent is false
            if ant_code is not None and ant_val is False:
                results.append(dict(time=t, result=SKIP,
                                    ant_val=False, cons_val=None, env=env))
                continue

            # resolve delay: look up the Nth subsequent clock edge
            if a.cons_delay > 0:
                future_idx = idx + a.cons_delay
                if future_idx >= len(clock_edges):
                    # can't check – not enough future edges in range
                    results.append(dict(time=t, result=SKIP,
                                        ant_val=ant_val, cons_val=None, env=env))
                    continue
                cons_t   = clock_edges[future_idx]
                cons_env = _build_env(sv_to_safe, path_signals,
                                      safe_to_path_local, cons_t, t)
            else:
                cons_t   = t
                cons_env = env

            cons_val = _safe_eval(cons_code, cons_env)

            if cons_val is None:
                tag = INDETER
            elif cons_val:
                tag = PASS
            else:
                tag = FAIL

            merged_env = dict(env)
            if cons_t != t:
                for k, v in cons_env.items():
                    merged_env['@{}_'.format(cons_t) + k] = v

            results.append(dict(time=t, result=tag,
                                 ant_val=ant_val, cons_val=cons_val,
                                 env=env, cons_t=cons_t, cons_env=cons_env))

    else:
        # ── immediate: evaluate at every time point ───────────────────────────
        times = [t for t in all_times if t_from <= t <= t_to]
        for idx, t in enumerate(times):
            prev_t = times[idx - 1] if idx > 0 else None
            env    = _build_env(sv_to_safe, path_signals,
                                safe_to_path_local, t, prev_t)
            cons_val = _safe_eval(cons_code, env)

            if cons_val is None:
                tag = INDETER
            elif cons_val:
                tag = PASS
            else:
                tag = FAIL

            results.append(dict(time=t, result=tag,
                                 ant_val=None, cons_val=cons_val,
                                 env=env, cons_t=t, cons_env=env))

    return results


# ── pretty printer ────────────────────────────────────────────────────────────

_COL_W  = 80
_PASS_C = '\033[32m'   # green
_FAIL_C = '\033[31m'   # red
_SKIP_C = '\033[33m'   # yellow
_RST    = '\033[0m'


def _colourise(tag):
    c = {PASS: _PASS_C, FAIL: _FAIL_C,
         SKIP: _SKIP_C, INDETER: _SKIP_C}.get(tag, '')
    return '{}{}{}'.format(c, tag, _RST) if c else tag


def _env_str(env, sv_to_safe):
    """Format signal values in env as  name=value  pairs."""
    # invert sv_to_safe so we can show original SV names
    safe_to_sv = {v: k for k, v in sv_to_safe.items()}
    parts = []
    for safe, val in sorted(env.items()):
        if safe.startswith('_'):
            continue                 # skip _rose_ helpers
        sv = safe_to_sv.get(safe, safe)
        parts.append('{}={}'.format(sv, val))
    return '  '.join(parts)


def print_assertion_results(a, results, sv_to_safe,
                             fail_only, pass_only, verbose):
    """Print a formatted block for one assertion."""
    bar = '─' * _COL_W
    print()
    print('━' * _COL_W)
    print('Assertion : {}'.format(a.display_name()))
    print('Property  : {}'.format(a.describe()))

    ant_py = sv_to_python(a.antecedent_sv, sv_to_safe) if a.antecedent_sv else None
    cons_py = sv_to_python(a.consequent_sv, sv_to_safe)
    if ant_py:
        print('  antecedent  (py): {}'.format(ant_py))
    print('  consequent  (py): {}'.format(cons_py))
    if a.cons_delay:
        print('  delay: ##{}  clock cycles'.format(a.cons_delay))
    print(bar)

    if not results:
        print('  (no time points to evaluate)')
        return

    # header
    has_delay = a.cons_delay > 0
    print('  {:<10}  {:<6}  {}'.format('Time', 'Result', 'Signal values'))
    print('  {:<10}  {:<6}  {}'.format('──────────', '──────', '────────────────────────────────'))

    n_pass = n_fail = n_skip = n_indet = 0

    for r in results:
        tag = r['result']
        if tag == PASS:    n_pass  += 1
        elif tag == FAIL:  n_fail  += 1
        elif tag == SKIP:  n_skip  += 1
        else:              n_indet += 1

        # filter
        if fail_only and tag != FAIL:
            continue
        if pass_only and tag not in (PASS,):
            continue
        if not verbose and tag == SKIP:
            continue

        env       = r.get('env', {})
        sig_str   = _env_str(env, sv_to_safe)

        # for delayed consequent, append consequent-time values
        cons_t  = r.get('cons_t', r['time'])
        cons_env = r.get('cons_env', env)
        if has_delay and cons_t != r['time']:
            cons_str = _env_str(cons_env, sv_to_safe)
            sig_str += '  |@{}| {}'.format(cons_t, cons_str)

        if tag == SKIP:
            note = '  (antecedent false)'
        elif tag == FAIL and a.antecedent_sv:
            note = '  (antecedent TRUE, consequent FALSE)'
        else:
            note = ''

        print('  {:<10}  {}  {}{}'.format(
            r['time'], _colourise('{:<6}'.format(tag)), sig_str, note))

    print(bar)
    summary_parts = ['{} checks'.format(len(results))]
    if n_pass:   summary_parts.append('{}{} PASS{}'.format(_PASS_C, n_pass, _RST))
    if n_fail:   summary_parts.append('{}{} FAIL{}'.format(_FAIL_C, n_fail, _RST))
    if n_skip:   summary_parts.append('{} SKIP (vacuous)'.format(n_skip))
    if n_indet:  summary_parts.append('{} INDETERMINATE'.format(n_indet))
    print('  ' + '  |  '.join(summary_parts))

    return n_pass, n_fail, n_skip


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description='Check SystemVerilog assertions against a VCD file.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('Options:')[0])
    ap.add_argument('vcd',  help='VCD waveform file')
    ap.add_argument('sv',   help='SystemVerilog assertion file (.sv)')
    ap.add_argument('--top',       metavar='SCOPE',
                    help='VCD scope prefix to prefer  (e.g. tb.dut)')
    ap.add_argument('--from',      dest='t_from', metavar='TIME',
                    type=int, default=0)
    ap.add_argument('--to',        dest='t_to',   metavar='TIME',
                    type=int, default=None)
    ap.add_argument('--fail-only', action='store_true')
    ap.add_argument('--pass-only', action='store_true')
    ap.add_argument('--verbose',   action='store_true',
                    help='also print SKIP (vacuous) rows')
    args = ap.parse_args()

    if args.fail_only and args.pass_only:
        ap.error('--fail-only and --pass-only are mutually exclusive')

    # ── load VCD ──────────────────────────────────────────────────────────────
    print('Loading VCD  : {}'.format(args.vcd))
    try:
        path_signals, all_times = load_vcd(args.vcd)
    except Exception as exc:
        print('ERROR: could not load VCD: {}'.format(exc))
        sys.exit(1)

    path_to_safe, safe_to_path = build_name_map(path_signals)

    t_from = args.t_from
    t_to   = args.t_to if args.t_to is not None else (
        all_times[-1] if all_times else 0)

    print('Signals      : {}  (time {} → {})'.format(
        len(path_signals), all_times[0] if all_times else 0, t_to))
    print('VCD signals:')
    for p in sorted(path_signals):
        print('  {}  →  {}'.format(p, path_to_safe[p]))

    # ── parse SV file ─────────────────────────────────────────────────────────
    print('\nLoading SV   : {}'.format(args.sv))
    try:
        with open(args.sv) as fh:
            sv_src = fh.read()
    except Exception as exc:
        print('ERROR: could not read SV file: {}'.format(exc))
        sys.exit(1)

    assertions = parse_sv_file(sv_src)
    print('Assertions   : {}'.format(len(assertions)))
    if not assertions:
        print('No assertions found.')
        sys.exit(0)

    for a in assertions:
        print('  [{}]  {}'.format(
            'concurrent' if a.concurrent else 'immediate',
            a.display_name()))

    # ── evaluate ──────────────────────────────────────────────────────────────
    grand_pass = grand_fail = grand_skip = 0

    for a in assertions:
        sv_names = _collect_sv_names(a)
        sv_to_safe, _ = resolve_signals(
            sv_names, path_signals, path_to_safe, args.top)

        results = check_assertion(
            a, path_signals, all_times,
            path_to_safe, safe_to_path,
            args.top, t_from, t_to)

        counts = print_assertion_results(
            a, results, sv_to_safe,
            args.fail_only, args.pass_only, args.verbose)

        if counts:
            grand_pass += counts[0]
            grand_fail += counts[1]
            grand_skip += counts[2]

    # ── grand summary ─────────────────────────────────────────────────────────
    total = grand_pass + grand_fail + grand_skip
    print()
    print('━' * _COL_W)
    print('OVERALL SUMMARY')
    print('━' * _COL_W)
    print('  Total checks : {}'.format(total))
    print('  {}PASS{:<6}{}'.format(_PASS_C, grand_pass, _RST))
    print('  {}FAIL{:<6}{}'.format(_FAIL_C, grand_fail, _RST))
    if grand_skip:
        print('  SKIP (vacuous): {}'.format(grand_skip))

    overall = 'ALL PASS' if grand_fail == 0 else '{} FAILURE(S)'.format(grand_fail)
    colour  = _PASS_C if grand_fail == 0 else _FAIL_C
    print()
    print('  Result: {}{}{}'.format(colour, overall, _RST))
    print('━' * _COL_W)

    sys.exit(0 if grand_fail == 0 else 1)


if __name__ == '__main__':
    main()
