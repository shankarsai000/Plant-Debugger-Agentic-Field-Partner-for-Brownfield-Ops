"""Simplified hydraulic model of the P-101 cooling-water loop.

Topology::

    P-101 (pump) ──┬── F-17 (strainer, local ΔP gauge only) ──┬── FT-101 (flow meter)
                   └── BV-104 (manual bypass valve) ───────────┘

Model (normalised units, q = Q / Q_nominal):

* Pump curve:     H(q) = s * H0 - A * q²              (s = pump health factor)
* System curve:   H(q) = q² * (R_sys + R_par)
* Parallel block: R_par = R_STRAINER_NOM / C_par²,  C_par = C_strainer + C_bypass
  - C_strainer = 1 - clog        (clean strainer => 1.0)
  - C_bypass   = C_BYPASS_FULL * opening_pct / 100

Steady state: q² = s*H0 / (A + R_sys + R_par). The live value relaxes toward the
steady state with a first-order lag so that operator actions produce a visible,
causal telemetry response.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

Q_NOMINAL_M3H = 100.0
H0 = 1.6               # shut-off head (normalised)
A = 0.6                # pump curve droop
R_SYS = 0.8            # downstream system resistance
R_STRAINER_NOM = 0.2   # resistance of a clean strainer block
C_BYPASS_FULL = 0.8    # conductance of a fully open bypass relative to a clean strainer
DP_BAR_PER_HEAD = 1.5  # converts normalised head to bar for the local ΔP gauge
TAU_S = 3.0            # first-order response time constant (seconds)


@dataclass
class PlantState:
    pump_running: bool = True
    pump_health: float = 1.0          # s, 1.0 = healthy impeller
    f17_clog: float = 0.0             # 0 = clean, 1 = fully blocked (HIDDEN)
    bv104_pct: float = 0.0            # bypass opening 0..100 (not remotely instrumented)
    q: float = 1.0                    # live normalised flow
    bearing_temp_c: float = 41.0
    noise: random.Random = field(default_factory=lambda: random.Random(1017))

    # ---- steady-state helpers -------------------------------------------------
    def c_parallel(self) -> float:
        c_strainer = max(1e-3, 1.0 - self.f17_clog)
        c_bypass = C_BYPASS_FULL * max(0.0, min(100.0, self.bv104_pct)) / 100.0
        return c_strainer + c_bypass

    def r_parallel(self) -> float:
        return R_STRAINER_NOM / (self.c_parallel() ** 2)

    def steady_q(self) -> float:
        if not self.pump_running:
            return 0.0
        q2 = (self.pump_health * H0) / (A + R_SYS + self.r_parallel())
        return math.sqrt(max(0.0, q2))

    # ---- integration ------------------------------------------------------------
    def step(self, dt: float) -> None:
        target = self.steady_q()
        alpha = 1.0 - math.exp(-dt / TAU_S)
        self.q += (target - self.q) * alpha
        # bearing temperature drifts up when the pump runs far from its best efficiency point
        temp_target = 41.0 + 18.0 * abs(1.0 - self.q) + (4.0 if self.pump_health < 0.9 else 0.0)
        self.bearing_temp_c += (temp_target - self.bearing_temp_c) * (1.0 - math.exp(-dt / 30.0))

    # ---- observables ------------------------------------------------------------
    def flow_m3h(self) -> float:
        return max(0.0, self.q * Q_NOMINAL_M3H + self.noise.gauss(0.0, 0.6))

    def strainer_dp_bar(self) -> float:
        """Local-only gauge value. Never published as remote telemetry."""
        return DP_BAR_PER_HEAD * (self.q ** 2) * self.r_parallel()

    def discharge_pressure_bar(self) -> float:
        head = self.pump_health * H0 - A * self.q ** 2 if self.pump_running else 0.0
        return max(0.0, 2.5 * head + self.noise.gauss(0.0, 0.02))

    def vibration_mm_s(self) -> float:
        if not self.pump_running:
            return 0.1
        base = 2.0 + 15.0 * abs(1.0 - self.q) + (2.5 if self.pump_health < 0.9 else 0.0)
        return max(0.0, base + self.noise.gauss(0.0, 0.15))

    def motor_current_a(self) -> float:
        if not self.pump_running:
            return 0.0
        return 38.0 * (0.55 + 0.45 * self.q) * self.pump_health ** 0.5 + self.noise.gauss(0.0, 0.2)

