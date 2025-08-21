from typing import List, Optional
from pydantic import BaseModel, confloat

# ---- Planet
class PlanetAspectInsight(BaseModel):
    to: str
    type: str
    meaning: str

class PlanetLifeAreas(BaseModel):
    career: str
    love: str
    finances: str
    health: str

class PlanetInterpretation(BaseModel):
    entity: str
    summary: str
    strengths: List[str]
    risks: List[str]
    life_areas: PlanetLifeAreas
    aspect_insights: List[PlanetAspectInsight]
    advice: List[str]
    confidence: confloat(ge=0, le=1)
    data_gaps: List[str]
    entity_key: str
    prompt_version: str
    calc_version: Optional[str] = ""

# ---- House
class HouseInterpretation(BaseModel):
    house: int
    summary: str
    relationships: str
    risks: List[str]
    advice: List[str]
    confidence: confloat(ge=0, le=1)
    data_gaps: List[str]
    entity_key: str
    prompt_version: str
    calc_version: Optional[str] = ""

# ---- Ruler
class RulerInterpretation(BaseModel):
    ruler: str
    rules_house: int
    summary: str
    scenarios: List[str]
    advice: List[str]
    confidence: confloat(ge=0, le=1)
    data_gaps: List[str]
    entity_key: str
    prompt_version: str
    calc_version: Optional[str] = ""

# ---- Aspect
class AspectSpheres(BaseModel):
    career: str
    relationships: str
    health: str
    finance: str

class AspectInterpretation(BaseModel):
    between: str
    type: str
    polarity: str  # "flow" | "tension"
    spheres: AspectSpheres
    actions: List[str]
    confidence: confloat(ge=0, le=1)
    data_gaps: List[str]
    entity_key: str
    prompt_version: str
    calc_version: Optional[str] = ""

# ---- Transit
class TransitWindow(BaseModel):
    from_: Optional[str] = None  # YYYY-MM-DD
    to: Optional[str] = None
    class Config: fields = {"from_": "from"}

class TransitInterpretation(BaseModel):
    pair: str
    type: str
    date: str   # YYYY-MM-DD
    effect: str
    themes: List[str]
    window: Optional[TransitWindow] = None
    recommended_actions: List[str]
    confidence: confloat(ge=0, le=1)
    data_gaps: List[str]
    entity_key: str
    prompt_version: str
    calc_version: Optional[str] = ""

# ---- Forecast Day
class ForecastKeyAspect(BaseModel):
    pair: str
    type: str
    note: str

class ForecastDayInterpretation(BaseModel):
    date: str  # YYYY-MM-DD
    day_summary: str
    key_aspects: List[ForecastKeyAspect]
    advice: List[str]
    confidence: confloat(ge=0, le=1)
    data_gaps: List[str]
    entity_key: str
    prompt_version: str
    calc_version: Optional[str] = ""
