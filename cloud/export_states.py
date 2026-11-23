
import csv
import json
from pathlib import Path
from urllib.request import urlopen

FOGS = {
    "fog-a": "http://localhost:18001",
    "fog-b": "http://localhost:18002",
    "fog-c": "http://localhost:18003",
}

def get_json(url):
    with urlopen(url, timeout=60) as response:
        return json.load(response)

output = Path("cloud/pile_states_initial.csv")
total = 0
seen_ids = set()

with output.open("w", newline="", encoding="utf-8") as file:
    writer = csv.writer(file)

    writer.writerow([
        "pile_id",
        "fog_id",
        "status",
        "is_reservable",
        "reservation_status",
        "currently_reservable"
    ])

    for fog_id, base_url in FOGS.items():
        piles = get_json(f"{base_url}/api/v1/piles")
        capacity = get_json(f"{base_url}/api/v1/piles/capacity")

        if len(piles) != capacity["total_piles"]:
            raise ValueError(f"Nombre de bornes incorrect pour {fog_id}")

        for pile in piles:
            pile_id = pile["pile_id"]

            if pile_id in seen_ids:
                raise ValueError(f"Borne dupliquee : {pile_id}")

            seen_ids.add(pile_id)

            writer.writerow([
                pile_id,
                fog_id,
                pile["status"],
                pile["is_reservable"],
                pile["reservation_status"] or "",
                pile["currently_reservable"]
            ])

        total += len(piles)
        print(f"{fog_id} : {len(piles)} bornes recuperees")

print(f"TOTAL : {total} bornes")
print(f"Fichier cree : {output}")
