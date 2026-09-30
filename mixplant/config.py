"""Configuration values shared by the runtime and its services."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Limits:
    """Process bounds the decision layer evaluates readings against."""

    mix_time_ticks: int = 6
    moisture_max_percent: float = 9.0
    scale_tolerance_percent: float = 2.0
    cement_flow_max_kgs: float = 42.0
    water_flow_max_lps: float = 60.0
    hopper_residual_max_kg: float = 12.0
    pump_pressure_max_mpa: float = 8.0
    prime_water_min_l: float = 40.0
    sample_window_ticks: int = 8

    def as_mapping(self) -> dict[str, float]:
        return {
            "mix_time_ticks": float(self.mix_time_ticks),
            "moisture_max_percent": self.moisture_max_percent,
            "scale_tolerance_percent": self.scale_tolerance_percent,
            "cement_flow_max_kgs": self.cement_flow_max_kgs,
            "water_flow_max_lps": self.water_flow_max_lps,
            "hopper_residual_max_kg": self.hopper_residual_max_kg,
            "pump_pressure_max_mpa": self.pump_pressure_max_mpa,
            "prime_water_min_l": self.prime_water_min_l,
        }


@dataclass(frozen=True, slots=True)
class ValidityDefaults:
    """Default validity windows, expressed in ticks."""

    baseline_ttl_ticks: int = 32
    confirmation_ttl_ticks: int = 10
    snapshot_ttl_ticks: int = 64


@dataclass(frozen=True, slots=True)
class SiloSafetyConfig:
    """The fixed safety line of one binder silo.

    The line is per silo; ``grade_actions`` fixes the alarm/block reaction per
    grade and ``default_action`` covers every grade not listed.
    """

    safety_kg: float = 5000.0
    default_action: str = "alarm"
    grade_actions: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> "SiloSafetyConfig":
        return cls(
            safety_kg=float(document.get("safety_kg", 5000.0)),
            default_action=str(document.get("default_action", "alarm")),
            grade_actions={
                str(grade): str(action) for grade, action in dict(document.get("grade_actions") or {}).items()
            },
        )


@dataclass(frozen=True, slots=True)
class StationConfig:
    """Everything the runtime needs in order to boot."""

    station: str = "plant-a"
    host: str = "127.0.0.1"
    port: int = 8080
    data_dir: Path = Path("var/state")
    web_dir: Path = Path("web")
    limits: Limits = field(default_factory=Limits)
    defaults: ValidityDefaults = field(default_factory=ValidityDefaults)
    silo_safety: Mapping[str, SiloSafetyConfig] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> "StationConfig":
        """Build a config from a parsed json document."""

        limits = Limits(**dict(document.get("limits") or {}))
        defaults = ValidityDefaults(**dict(document.get("defaults") or {}))
        raw_safety = dict(document.get("silo_safety") or {})
        silo_safety = {
            str(silo): SiloSafetyConfig.from_mapping(dict(doc) if isinstance(doc, Mapping) else {})
            for silo, doc in raw_safety.items()
        }
        return cls(
            station=str(document.get("station", "plant-a")),
            host=str(document.get("host", "127.0.0.1")),
            port=int(document.get("port", 8080)),
            data_dir=Path(str(document.get("data_dir", "var/state"))),
            web_dir=Path(str(document.get("web_dir", "web"))),
            limits=limits,
            defaults=defaults,
            silo_safety=silo_safety,
        )

    @classmethod
    def load(cls, path: Path | str) -> "StationConfig":
        """Read a config file, falling back to the defaults when it is absent."""

        candidate = Path(path)
        if not candidate.exists():
            return cls()
        document = json.loads(candidate.read_text(encoding="utf-8"))
        return cls.from_mapping(document)

    def with_overrides(self, **changes: Any) -> "StationConfig":
        """Return a copy with ``changes`` applied."""

        return replace(self, **changes)

    def describe(self) -> Mapping[str, Any]:
        return {
            "station": self.station,
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "limits": self.limits.as_mapping(),
            "silo_safety": {
                silo: {
                    "safety_kg": policy.safety_kg,
                    "default_action": policy.default_action,
                    "grade_actions": dict(policy.grade_actions),
                }
                for silo, policy in sorted(self.silo_safety.items())
            },
        }
