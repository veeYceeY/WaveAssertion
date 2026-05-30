// Sample SystemVerilog assertion file for sample.vcd
// Signals: tb.dut.clk  tb.dut.reset  tb.dut.valid  tb.dut.data  tb.dut.state

// ── Named properties ──────────────────────────────────────────────────────────

// Reset must deassert (go to 0) and stay low
property p_reset_deassert;
  @(posedge clk) reset == 1'b0;
endproperty

// When valid is high, data must be non-zero
property p_data_nonzero;
  @(posedge clk) valid == 1'b1 |-> data != 8'h00;
endproperty

// When valid rises, state must not be 0 at the next clock
property p_state_on_valid;
  @(posedge clk) $rose(valid) |=> state != 2'b00;
endproperty

// ── Assertions referencing named properties ───────────────────────────────────

a_reset:     assert property (p_reset_deassert);
a_data:      assert property (p_data_nonzero);
a_state:     assert property (p_state_on_valid);

// ── Inline concurrent assertions ──────────────────────────────────────────────

// data must be <= 8'hFF (always true for 8-bit, sanity check)
a_data_range: assert property (
  @(posedge clk) data <= 8'hFF
);

// When state==3 (2'b11), data must be 8'h1E (0x1e = 30)
a_state3_data: assert property (
  @(posedge clk) state == 2'b11 |-> data == 8'h1E
);

// valid must eventually go high – check it is non-zero at any posedge
// (this will FAIL before time 20 when valid is 0)
a_valid_high: assert property (
  @(posedge clk) valid == 1'b1
);

// ── Immediate (non-clocked) assertion ────────────────────────────────────────

// clk should never be unknown (no x/z in this simple VCD)
a_clk_known: assert (clk != 1'bx);
