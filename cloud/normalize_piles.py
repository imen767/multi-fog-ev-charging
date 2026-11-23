
import csv
from decimal import Decimal
from pathlib import Path

source = Path("piles_catalog.csv")
destination = Path("cloud/piles_normalized.csv")

with source.open("r", newline="", encoding="utf-8") as f_in:
    with destination.open("w", newline="", encoding="utf-8") as f_out:
        reader = csv.DictReader(f_in)
        writer = csv.writer(f_out)

        writer.writerow([
            "pile_id",
            "station_id",
            "power_kw",
            "pile_type",
            "fog_id"
        ])

        count = 0

        for row in reader:
            writer.writerow([
                str(int(Decimal(row["pile_id"]))),
                str(int(Decimal(row["station_id"]))),
                row["power"],
                row["pileType"],
                row["fog_id"]
            ])
            count += 1

print(f"Bornes normalisées : {count}")
