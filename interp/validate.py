from typing import Dict, Any, Type
from pydantic import BaseModel
from .schemas import (
    PlanetInterpretation, HouseInterpretation, RulerInterpretation,
    AspectInterpretation, TransitInterpretation, ForecastDayInterpretation
)

MODEL_BY_TYPE: Dict[str, Type[BaseModel]] = {
    "planet": PlanetInterpretation,
    "house": HouseInterpretation,
    "ruler": RulerInterpretation,
    "aspect": AspectInterpretation,
    "transit": TransitInterpretation,
    "forecast_day": ForecastDayInterpretation,
}

def validate_interpretation(entity_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    Model = MODEL_BY_TYPE.get(entity_type)
    if not Model:
        raise ValueError(f"Unknown entity_type: {entity_type}")
    obj = Model(**payload)
    return obj.dict(by_alias=True)
