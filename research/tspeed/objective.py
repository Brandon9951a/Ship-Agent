"""航速搜索目标：最低能耗或最短航时。

最低能耗目标为 ``J = E_pred + λ·Σ(v_i - v_(i+1))²``；最短航时按秒排序。

量纲处理（任务书第 2 节要求"惩罚项需先做量纲/尺度处理"）：
``Σ(Δv)²`` 的量纲是 (km/h)²，不能与 kWh 直接相加，因此先除以航段数得到
每边界的平均速度差平方，再乘以 ``lambda_scale_kwh_per_kmh2``（默认 1.0）。
采用值必须由敏感性实验确定；导师取值 λ=10 仅用于复现实验，此处不预设为默认。
"""

from __future__ import annotations

import math

from research.contracts import ObjectiveTerms, SpeedOptimizationRequest

DEFAULT_PENALTY_SCALE = 1.0
PENALTY_DEFINITION = "J = E_scope_kwh + lambda * mean_i((v_i - v_{i+1})^2) * scale"
DEFAULT_LAMBDA_NOTE = ("lambda 与 scale 为研究参数，须由敏感性实验冻结；"
                       "论文复现实验使用 lambda=10、scale=1.0")


def speed_difference_penalty(speed_vector, *, scale: float = DEFAULT_PENALTY_SCALE
                             ) -> float:
    """相邻巡航航速差异的惩罚项（已做尺度处理）。"""
    if scale < 0:
        raise ValueError("penalty scale 不得为负")
    if len(speed_vector) < 2:
        return 0.0
    total = 0.0
    for left, right in zip(speed_vector, speed_vector[1:]):
        delta = float(left) - float(right)
        total += delta * delta
    return total / (len(speed_vector) - 1) * scale


def objective_terms(*, speed_vector, energy_kwh: float | None, request: SpeedOptimizationRequest,
                    energy_scope: str = "motor", scale: float = DEFAULT_PENALTY_SCALE,
                    duration_s: float | None = None
                    ) -> ObjectiveTerms:
    """组装目标函数分项，并如实记录能量口径与量纲处理方式。"""
    if energy_scope not in ("motor", "bus"):
        raise ValueError("energy_scope 只允许 motor 或 bus")
    if request.objective == "min_time":
        return ObjectiveTerms(
            energy_kwh=energy_kwh, penalty=0.0, lambda_value=0.0,
            total=duration_s, energy_scope=energy_scope,
            penalty_definition="J = total_duration_s；能耗另行记录，功率等硬边界独立校验",
            penalty_scale_note="时间目标单位为 s；不与 kWh 惩罚项混加",
        )
    penalty = speed_difference_penalty(speed_vector, scale=scale)
    lambda_value = float(request.speed_penalty_lambda)
    total = None if energy_kwh is None else energy_kwh + lambda_value * penalty
    return ObjectiveTerms(
        energy_kwh=energy_kwh,
        penalty=penalty,
        lambda_value=lambda_value,
        total=total,
        energy_scope=energy_scope,
        penalty_definition=PENALTY_DEFINITION,
        penalty_scale_note=(f"scale={scale}；{DEFAULT_LAMBDA_NOTE}"),
    )


def objective_value(*, energy_kwh: float | None, penalty: float, lambda_value: float,
                    objective: str = "min_energy", duration_s: float | None = None
                    ) -> float | None:
    if objective == "min_time":
        return duration_s if duration_s is not None and math.isfinite(duration_s) else None
    if objective != "min_energy":
        raise ValueError("unsupported_objective")
    if energy_kwh is None:
        return None
    if not math.isfinite(energy_kwh):
        return None
    return energy_kwh + lambda_value * penalty
