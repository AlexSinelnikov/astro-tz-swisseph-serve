from typing import Dict, Any, List
from .versions import PROMPT_VERSION

def normalize_entities(result_json: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Ожидаемые ключи result_json:
      - type, calc_version
      - planets: [{name, sign, house, dms, aspects_to[], rulership?}]
      - houses:  [{house, sign_on_cusp, ruler, planets_in_house[]}]
      - rulers:  [{ruler, rules_house, placement{sign,house}}]
      - aspects: [{a,b,type,orb|delta|angle?,applying}]
      - transits:[{transit,to_natal,type,date}]
      - forecast:[{date, transit_aspects[...]}]
    """
    calc_version = result_json.get("calc_version", "")
    out = { "planet": [], "house": [], "ruler": [], "aspect": [], "transit": [], "forecast_day": [] }

    for p in result_json.get("planets", []):
        ek = f"planet:{p['name']}"
        out["planet"].append({ **p, "entity_type":"planet", "entity_key": ek,
                               "calc_version": calc_version, "prompt_version": PROMPT_VERSION })

    for h in result_json.get("houses", []):
        ek = f"house:{h['house']}"
        out["house"].append({ **h, "entity_type":"house", "entity_key": ek,
                              "calc_version": calc_version, "prompt_version": PROMPT_VERSION })

    for r in result_json.get("rulers", []):
        ek = f"ruler:{r['ruler']}->house{r['rules_house']}"
        out["ruler"].append({ **r, "entity_type":"ruler", "entity_key": ek,
                              "calc_version": calc_version, "prompt_version": PROMPT_VERSION })

    for a in result_json.get("aspects", []):
        ek = f"aspect:{a['a']}-{a['b']}-{a['type']}"
        out["aspect"].append({ **a, "entity_type":"aspect", "entity_key": ek,
                               "calc_version": calc_version, "prompt_version": PROMPT_VERSION })

    for t in result_json.get("transits", []):
        ek = f"transit:{t['transit']}->{t['to_natal']}-{t['type']}-{t['date']}"
        out["transit"].append({ **t, "entity_type":"transit", "entity_key": ek,
                                "calc_version": calc_version, "prompt_version": PROMPT_VERSION })

    for d in result_json.get("forecast", []):
        ek = f"forecast:{d['date']}"
        out["forecast_day"].append({ **d, "entity_type":"forecast_day", "entity_key": ek,
                                     "calc_version": calc_version, "prompt_version": PROMPT_VERSION })
    return out
