# Control roadmap

> **Status: design and test plan only.** The current working setup is
> read-only (Raspberry Pi 3 + CH340 USB-RS485). No ESP32 has been connected
> to this unit yet. The MITM topology and ESPHome sketches below are planned
> work, not a verified installation.

## What I want

Control this air conditioner from **ESPHome → Home Assistant** while keeping
the XK19 panel usable. The current repository is read-only. Control is the
next milestone, not a finished feature.

## What has been tried

| Experiment | Result |
|---|---|
| Parallel injection of `1A ...` command frames | rejected; setpoint unchanged |
| Replay of a captured button burst | rejected |
| Repeated injection with different flags | rejected |
| Observe the bus after injection | new `0x4A` frame appeared |
| Two-UART MITM bridge | firmware draft written; no ESP32 connected yet |

### Panel correction observation

After a parallel injection, one capture contained a frame that was absent from
the baseline:

```text
4A 00 20 20 00 BA AA CE 3E F2 74 96 02 FA BE 7E F9
```

The status copy carries `+6 = 0x96` (26 °C), which is the wall controller's
setpoint, not the injected value. The working interpretation is:

> The XK19 is the bus master. When it sees a setting that did not originate
> from itself, it reasserts its own state with a `0x4A` frame.

This is an **observation plus hypothesis**, not a fully characterised
correction protocol. It was seen once; the exact trigger, timing and scope
still need controlled captures.

Implications:

- Parallel injection is not a viable control path while the panel is online.
- A MITM bridge must rewrite the panel's outgoing state, not merely inject
  extra frames.
- The panel may continue to display its old setting unless the bridge also
  handles the return direction.

## MITM topology

```text
[XK19 panel] ── A/B ── [RS485 #1] ── ESP32 ── [RS485 #2] ── A/B ── [indoor unit]
                                      │
                              bypass relay (NC)
```

The relay is normally closed across the cut point. ESP32 energises it on boot
to route traffic through the bridge. If ESP32 loses power or hangs, the relay
releases and the panel is connected directly to the indoor unit again.

The pin-level wiring, the XHB 1-to-2 splitter limitation and the relay
contacts are documented in [WIRING.md](WIRING.md).

Rules:

1. Use two UARTs with two independent frame buffers.
2. Rewrite only the **panel → indoor unit** direction.
3. Keep the **indoor unit → panel** direction transparent unless a specific
   display problem requires otherwise.
4. Preserve the frame prefix and rewrite only the known payload fields.
5. At 1200 baud, the byte latency is small; the hard part is arbitration and
   state reconciliation, not raw speed.

## ESPHome / HA entities

```text
climate.gree_xk19
  target_temperature  <- STATUS +6
  hvac_mode           <- STATUS +5 / +9
  fan_mode            <- STATUS +8 (1/2/3 only)
  current_temperature <- external room sensor

sensor.gree_last_frame
binary_sensor.bridge_healthy
binary_sensor.bypass_relay
```

`control()` should not send arbitrary frames. It should turn an HA request
into an override rule that is applied to the next panel→unit frame. This is
the same approach used by the existing ESPHome bridge draft.

## Test order

### Phase 0: read-only

- Keep the panel connected directly.
- Run the read-only dashboard until setpoint/mode/fan are stable.
- Record a golden baseline for every mode.

### Phase 1: MITM pass-through

- Insert the ESP32 between panel and indoor unit.
- Forward bytes unmodified in both directions.
- Run for several days; check for dropped frames, panel errors and `0x4A`
  traffic.
- The bypass relay must already be installed.

### Phase 2: setpoint only

- Rewrite only `STATUS +6` in panel→unit frames.
- Reverse formula: `byte = 0xFE - T * 4`.
- Run at least 100 setpoint changes across 16–30 °C.
- Verify the panel display and the indoor unit agree.
- Check whether `0x4A` still appears.

### Phase 3: mode and fan

- Rewrite `+5` and `+9` together; auto and cool share `+5 = 0x74`.
- Rewrite `+8` only for manual 1/2/3; do not invent an auto value.
- Verify each mode with a fresh capture, especially heat and auto.

### Phase 4: power

- Capture power off/on separately.
- The `+7` run-state byte is not yet clean enough to treat as a power command.
- Do not expose `climate.turn_on` until the power sequence is understood.

### Phase 5: fail-safe and HA

- Add a watchdog and an availability topic.
- If the bridge stops, the bypass relay must restore direct communication.
- Expose a read-only climate entity first; enable writes only after the
  hardware-in-the-loop tests pass.

## Open questions

- Does the panel reassert with `0x4A` on every write, or only for specific
  fields?
- Does the panel need to see the indoor unit's acknowledgement before it
  accepts a rewritten setpoint?
- Can the indoor unit→panel direction stay untouched while the panel shows
  the new setpoint?
- Is `+7` a command, a state, or both?
- Can X-FAN be controlled through the panel's `0xEE` sequence, or does the
  bridge need to implement the drying timer itself?

## Update log

- **2026-10-18**: goal and control roadmap added; read-only fields verified;
  panel correction recorded as an observation/hypothesis.
