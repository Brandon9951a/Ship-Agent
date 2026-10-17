"""Tspeed：差分进化航速优化工具（研究系统 v2 实现）。"""

from .eval import (
    CandidateEvaluation,
    DEEvaluationContext,
    EnergyModelSpec,
    evaluate_candidate,
)
from .objective import objective_terms, speed_difference_penalty
from .predictor import (
    ANALYTIC_MODEL_VERSION,
    DEFAULT_INTEGRATION_RULE,
    AnalyticPowerPredictor,
    EnergyPredictor,
    EvaluationGrid,
    PredictionOutcome,
)
from .search import (
    ALGORITHM_VERSION,
    DEFAULT_DE_CONFIG,
    PAPER_REPRODUCTION,
    DEConfig,
    DESearchResult,
    candidate_trajectory,
    run_differential_evolution,
)
from .trajectory import (
    ExpandedTrajectory,
    SampledInterval,
    Violation,
    expand_trajectory,
    plan_segment_phases,
    sample_phases,
)

__all__ = [
    "ALGORITHM_VERSION",
    "ANALYTIC_MODEL_VERSION",
    "DEFAULT_DE_CONFIG",
    "DEFAULT_INTEGRATION_RULE",
    "PAPER_REPRODUCTION",
    "AnalyticPowerPredictor",
    "CandidateEvaluation",
    "DEConfig",
    "DEEvaluationContext",
    "DESearchResult",
    "EnergyModelSpec",
    "EnergyPredictor",
    "EvaluationGrid",
    "ExpandedTrajectory",
    "PredictionOutcome",
    "SampledInterval",
    "Violation",
    "candidate_trajectory",
    "evaluate_candidate",
    "expand_trajectory",
    "objective_terms",
    "plan_segment_phases",
    "run_differential_evolution",
    "sample_phases",
    "speed_difference_penalty",
]
