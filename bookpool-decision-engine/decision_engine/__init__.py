"""BookPool decision engine (Person 3): group optimisation and buy-now decisions."""

from .engine import DecisionEngine, group_id_for
from .messages import participant_summary
from .models import (
    Action,
    BookRequest,
    EngineConfig,
    EngineEvent,
    EngineResult,
    EvaluateInput,
    EventType,
    GroupProposal,
    MerchantOffer,
    MerchantPolicy,
    ParticipantLine,
    ReasonCode,
    Recommendation,
    SoloPlan,
    SoloStatus,
    VolumeTier,
)

__all__ = [
    "Action",
    "BookRequest",
    "DecisionEngine",
    "EngineConfig",
    "EngineEvent",
    "EngineResult",
    "EvaluateInput",
    "EventType",
    "GroupProposal",
    "MerchantOffer",
    "MerchantPolicy",
    "ParticipantLine",
    "ReasonCode",
    "Recommendation",
    "SoloPlan",
    "SoloStatus",
    "VolumeTier",
    "group_id_for",
    "participant_summary",
]
