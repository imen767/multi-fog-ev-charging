"""Regression: the Fog API must serialize real ACTIVE reservations."""
from fog.api.piles import PileResponse


def test_list_piles_accepts_active_reservation_status():
    response = PileResponse(
        pile_id="93001001",
        station_id="93001",
        power_kw=50.0,
        pile_type="DC",
        status="OCCUPIED",
        is_reservable=True,
        longitude=10.3,
        latitude=36.8,
        reservation_status="ACTIVE",
        currently_reservable=False,
    )
    assert response.reservation_status == "ACTIVE"
    assert response.status == "OCCUPIED"
