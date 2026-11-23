"""Initialize this Fog's own database and import its UrbanEV catalog.

Idempotent thanks to INSERT OR IGNORE in import_urban_ev.py. Existing
reservations/occupancy are preserved (no RESET on container restart).
"""
from fog.database import init_db
from import_urban_ev import import_urban_ev

if __name__ == "__main__":
    init_db()
    import_urban_ev()
