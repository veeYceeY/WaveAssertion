# VCD Signal Analyzer & Assertion Checker

Two tools for working with Value Change Dump (VCD) waveform files:

| Tool | What it does |
|---|---|
| `vcd_analyzer.py` | GUI — browse signal hierarchy, build expressions, scan matching time points |
| `sv_check.py` | CLI — parse a `.sv` assertion file, evaluate each assertion against a VCD, print pass/fail with signal values |

**Requirements:** Python 3.6.8+, `tkinter` (built-in). No `pip install` needed — the only third-party library (`pyDigitalWaveTools`) is vendored in `lib/`.

---

## Directory layout

```
vcdyser/
├── vcd_analyzer.py          # GUI tool
├── sv_check.py              # CLI assertion checker
├── sample.vcd               # example VCD file
├── sample_assertions.sv     # example SVA file
└── lib/
    └── pyDigitalWaveTools/  # vendored VCD parser (v1.2)
        ├── vcd/
        │   ├── parser.py
        │   ├── common.py
        │   └── ...
        └── ...
```

---

## vcd_analyzer.py — GUI tool

### Launch

```
python3 vcd_analyzer.py
```

### Opening a file

**File › Open VCD…** or `Ctrl+O`. Accepts any `.vcd` file.

The status bar shows how many signals and time steps were loaded.

### Signal hierarchy panel (left)

Signals are shown in a collapsible tree that mirrors the VCD scope structure.

- **Scopes** are shown in green (expandable).
- **Signals** are shown in blue. Multi-bit signals show their width in brackets, e.g. `data [8]`.
- Type in the **Filter** box to flatten the tree to matching signals only.

### Building an expression

Signals are referenced by a **safe Python identifier** derived from their full hierarchical path — non-alphanumeric characters become underscores:

```
tb.dut.clk      →  tb_dut_clk
tb.dut.data[7:0] →  tb_dut_data_7_0_
```

Three ways to insert a signal name into the expression:

1. Select a leaf in the tree → click **Add to Expression**.
2. Choose a path in the **Signal path** combo box → click **Insert**.
3. Type the safe name directly.

Use the **Insert** row of operator buttons (`==`, `!=`, `and`, `or`, …) to build the expression without typing.

#### Expression syntax

Standard Python boolean expressions. Examples:

```python
tb_dut_clk == 1
tb_dut_reset == 0 and tb_dut_valid == 1
tb_dut_data_bus >= 0xFF
(tb_dut_state == 2) and not tb_dut_err
tb_dut_wr_en | tb_dut_rd_en
```

Multi-bit vector signals are decoded as unsigned integers; `x`/`z` bits are treated as `0`.

Click **Validate** to check syntax before scanning (evaluates with all signals = 0).

### Scanning

Set **From** / **To** (time units matching the VCD timescale; `end` means last time step) and **Max results**, then click **Scan**.

The **Scan Results** table shows every time point where the expression evaluated to `True`, with the current value of every signal used in the expression.

### Show Values

Select any signal leaf → click **Show Values** to open a popup listing every transition with its raw VCD value and decoded numeric value.

---

## sv_check.py — CLI assertion checker

### Usage

```
python3 sv_check.py <vcd_file> <sv_file> [options]

Options:
  --top   SCOPE    VCD scope prefix to prefer when resolving signal names
                   e.g. --top tb.dut
  --from  TIME     start time (default 0)
  --to    TIME     end time   (default: end of VCD)
  --fail-only      print only rows where the assertion failed
  --pass-only      print only rows where the assertion passed
  --verbose        also show SKIP rows (vacuously true checks)
```

Exit code is `0` if every assertion passed, `1` if any failed.

### Example

```
python3 sv_check.py sample.vcd sample_assertions.sv --top tb.dut
python3 sv_check.py sim.vcd checks.sv --top tb.dut --fail-only
python3 sv_check.py sim.vcd checks.sv --from 1000 --to 5000
```

### Supported SVA subset

#### Concurrent assertions (clock-driven)

```systemverilog
// simple condition at every posedge
a_reset: assert property (@(posedge clk) reset == 1'b0);

// implication: consequent checked at same edge (|->)
a_data: assert property (@(posedge clk) valid == 1'b1 |-> data != 8'h00);

// non-overlapping implication: consequent checked 1 cycle later (|=>)
a_ready: assert property (@(posedge clk) req |=> ack);

// explicit N-cycle delay in consequent
a_pipe: assert property (@(posedge clk) start |-> ##2 done);

// negedge clock
a_negedge: assert property (@(negedge clk) out_valid == 1'b1);
```

#### Immediate assertions (checked at every time step)

```systemverilog
a_known: assert (data !== 8'hxx);
```

#### Named properties

```systemverilog
property p_data_valid;
  @(posedge clk) valid |-> data != 8'h00;
endproperty

a_data: assert property (p_data_valid);
```

#### Temporal system functions

```systemverilog
$rose(sig)    // true when sig transitions 0 → 1
$fell(sig)    // true when sig transitions 1 → 0
$stable(sig)  // true when sig value is unchanged from previous edge
```

#### Bit-vector literals

| SV literal | Converted to |
|---|---|
| `1'b1`, `1'b0` | `1`, `0` |
| `8'hFF` | `0xff` |
| `4'd10` | `10` |
| `4'b1010` | `10` |
| `x`/`z` bits | `0` |

#### Logical operators

| SV | Python |
|---|---|
| `&&` | `and` |
| `\|\|` | `or` |
| `!expr` | `not expr` |
| `&`, `\|`, `^`, `~` | unchanged |

### Output

For each assertion a block is printed:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Assertion : a_data
Property  : @(posedge clk) valid == 1'b1 |-> data != 8'h00
  antecedent  (py): tb_dut_valid == 1
  consequent  (py): tb_dut_data != 0x00
────────────────────────────────────────────────────────────────────────────────
  Time        Result  Signal values
  ──────────  ──────  ────────────────────────────────
  25          PASS    clk=1  data=10  valid=1
  35          PASS    clk=1  data=20  valid=1
  45          PASS    clk=1  data=30  valid=1
  65          PASS    clk=1  data=255  valid=1
────────────────────────────────────────────────────────────────────────────────
  7 checks  |  4 PASS  |  3 SKIP (vacuous)
```

**Result values:**

| Tag | Meaning |
|---|---|
| `PASS` | Consequent is true (antecedent true or absent) |
| `FAIL` | Antecedent is true but consequent is false |
| `SKIP` | Antecedent is false — implication vacuously true, not a real check |
| `????` | Expression could not be evaluated (type error etc.) |

For delayed checks (`##N`) the consequent-time signal values are appended: `|@T| sig=val …`

### Signal name resolution

SV assertions use short signal names (`clk`, `data`). The tool maps these to full VCD paths (`tb.dut.clk`) by matching the **last path component**.

If multiple VCD scopes contain a signal with the same name, use `--top` to select the preferred scope:

```
--top tb.dut    # prefers  tb.dut.clk  over  tb.other.clk
```

---

## How signal values are decoded

| VCD value | Numeric result |
|---|---|
| `0`, `1` | `0`, `1` |
| `x`, `z` | `0` |
| `b0101` (binary vector) | `5` (unsigned integer) |
| `bxx01` (with x/z bits) | `1` (x bits treated as 0) |
| `r3.14` (real) | `3.14` (float) |

---

## VCD parser fallback

Both tools try `lib/pyDigitalWaveTools` first. If the import fails for any reason, a built-in VCD parser takes over automatically — no error, no reduced functionality.

The only case where the built-in parser may differ is with VCD files that rely on non-standard extensions. The built-in parser requires the file's value-change section to start with a `#<time>` marker (standard VCD).

`pyDigitalWaveTools` requires `$enddefinitions $end` in the header. If that directive is absent (some simulators omit it), the loader patches it in automatically before parsing.
