import sys, json
from interp.normalizer import normalize_entities
from interp.validate import validate_interpretation

def main():
    if len(sys.argv) < 2:
        print("Usage: python tools_validate.py normalize | validate <etype> <path>")
        return

    mode = sys.argv[1]
    if mode == "normalize":
        payload = json.load(open("interp/samples/sample_result.json","r",encoding="utf-8"))
        entities = normalize_entities(payload)
        print({k: len(v) for k, v in entities.items()})
        if entities.get("planet"):
            print("example entity_key:", entities["planet"][0]["entity_key"])
    elif mode == "validate":
        if len(sys.argv) != 4:
            print("Usage: python tools_validate.py validate <etype> <path>")
            return
        etype = sys.argv[2]
        path  = sys.argv[3]
        data = json.load(open(path,"r",encoding="utf-8"))
        out = validate_interpretation(etype, data)
        print("VALID:", etype, "confidence:", out.get("confidence"))
    else:
        print("Usage: python tools_validate.py normalize | validate <etype> <path>")

if __name__ == "__main__":
    main()
