// Assertions using struct-style dot notation.
// No struct typedef needed – signals are referenced as pkt.valid, pkt.data, etc.
// In the VCD these appear as tb.dut.pkt.valid, tb.dut.pkt.data, etc.

// When pkt.valid rises, pkt.data must be non-zero
a_pkt_data: assert property (
  @(posedge clk) pkt.valid == 1'b1 |-> pkt.data != 8'h00
);

// When pkt.valid is high, ack must come one cycle later
a_ack_latency: assert property (
  @(posedge clk) pkt.valid |=> rsp.ack
);

// Response payload must match packet data (same cycle as ack)
a_rsp_payload: assert property (
  @(posedge clk) rsp.ack == 1'b1 |-> rsp.payload == pkt.data
);

// pkt.kind must never be 2'b11 (reserved)
a_kind_reserved: assert property (
  @(posedge clk) pkt.kind != 2'b11
);
