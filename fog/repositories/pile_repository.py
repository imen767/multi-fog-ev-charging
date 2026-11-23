
import sqlite3

from fog.settings import DB_PATH


def get_all_piles():
    """
    Retrieve all charging piles owned by this Fog,
    with their station information and reservation status.
    """
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row

        rows = connection.execute("""
            SELECT
                p.pile_id,
                p.station_id,
                p.power_kw,
                p.pile_type,
                p.status,
                p.is_reservable,
                s.longitude,
                s.latitude,

                (
                    SELECT r.status
                    FROM reservations r
                    WHERE r.pile_id = p.pile_id
                      AND (
                          r.status IN ('CONFIRMED', 'ACTIVE')
                          OR (
                              r.status = 'HOLD'
                              AND julianday(r.expires_at) > julianday('now')
                          )
                      )
                    LIMIT 1
                ) AS reservation_status,

                CASE
                    WHEN p.status != 'AVAILABLE'
                      OR p.is_reservable = 0
                    THEN 0

                    WHEN EXISTS (
                        SELECT 1
                        FROM reservations r
                        WHERE r.pile_id = p.pile_id
                          AND (
                              r.status IN ('CONFIRMED', 'ACTIVE')
                              OR (
                                  r.status = 'HOLD'
                                  AND julianday(r.expires_at) > julianday('now')
                              )
                          )
                    )
                    THEN 0

                    ELSE 1
                END AS currently_reservable

            FROM piles p
            JOIN stations s ON s.station_id = p.station_id
            ORDER BY p.station_id, p.pile_id
        """).fetchall()

        return [dict(row) for row in rows]


def update_pile_status(pile_id: str, new_status: str):
    """
    Update the status of a pile owned by this Fog.
    """
    with sqlite3.connect(DB_PATH) as connection:
        cursor = connection.execute("""
            UPDATE piles
            SET status = ?
            WHERE pile_id = ?
        """, (new_status, pile_id))

        connection.commit()

        return cursor.rowcount > 0


def get_station_capacities():
    """Station capacities from live SQLite pile + reservation state.

    A compatible connector is only considered free when its pile is AVAILABLE,
    reservable, and without an effective HOLD/CONFIRMED/ACTIVE reservation.
    """
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""
            WITH pile_state AS (
                SELECT p.pile_id, p.station_id, p.power_kw, p.pile_type, p.status,
                  CASE WHEN p.status = 'AVAILABLE'
                     AND p.is_reservable = 1
                     AND NOT EXISTS (
                       SELECT 1 FROM reservations r
                       WHERE r.pile_id = p.pile_id
                         AND (r.status IN ('CONFIRMED', 'ACTIVE')
                           OR (r.status = 'HOLD'
                               AND julianday(r.expires_at) > julianday('now')))
                     ) THEN 1 ELSE 0 END AS usable
                FROM piles p
            )
            SELECT s.station_id,
              COUNT(ps.pile_id) AS total_piles,
              COALESCE(SUM(ps.usable), 0) AS available_piles,
              COALESCE(SUM(CASE WHEN ps.status = 'OCCUPIED' THEN 1 ELSE 0 END), 0) AS occupied_piles,
              COALESCE(SUM(CASE WHEN ps.status = 'OUT_OF_SERVICE' THEN 1 ELSE 0 END), 0) AS out_of_service_piles,
              COALESCE(SUM(CASE WHEN ps.usable = 1 THEN ps.power_kw ELSE 0 END), 0) AS available_power_kw,
              COALESCE(SUM(CASE WHEN ps.usable = 1 AND ps.pile_type = 'AC' THEN 1 ELSE 0 END), 0) AS available_ac_piles,
              COALESCE(SUM(CASE WHEN ps.usable = 1 AND ps.pile_type = 'DC' THEN 1 ELSE 0 END), 0) AS available_dc_piles,
              COALESCE(MAX(CASE WHEN ps.usable = 1 THEN ps.power_kw ELSE 0 END), 0) AS max_available_power_kw,
              COALESCE(MAX(CASE WHEN ps.usable = 1 AND ps.pile_type = 'AC' THEN ps.power_kw ELSE 0 END), 0) AS max_available_ac_kw,
              COALESCE(MAX(CASE WHEN ps.usable = 1 AND ps.pile_type = 'DC' THEN ps.power_kw ELSE 0 END), 0) AS max_available_dc_kw
            FROM stations s
            LEFT JOIN pile_state ps ON ps.station_id = s.station_id
            GROUP BY s.station_id ORDER BY s.station_id
        """).fetchall()
        return [dict(row) for row in rows]


def get_pile_capacity():
    """
    Calculate the current capacity of this Fog.

    A pile is available only if:
    - Its physical status is AVAILABLE.
    - It is reservable.
    - It has no active HOLD or CONFIRMED reservation.
    """
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row

        row = connection.execute("""
            SELECT
                COUNT(*) AS total_piles,

                COALESCE(SUM(
                    CASE
                        WHEN p.status = 'AVAILABLE'
                         AND p.is_reservable = 1
                         AND NOT EXISTS (
                            SELECT 1
                            FROM reservations r
                            WHERE r.pile_id = p.pile_id
                              AND (
                                  r.status IN ('CONFIRMED', 'ACTIVE')
                                  OR (
                                      r.status = 'HOLD'
                                      AND julianday(r.expires_at) > julianday('now')
                                  )
                              )
                         )
                        THEN 1
                        ELSE 0
                    END
                ), 0) AS available_piles,

                COALESCE(SUM(
                    CASE WHEN p.status = 'OCCUPIED'
                         THEN 1 ELSE 0 END
                ), 0) AS occupied_piles,

                COALESCE(SUM(
                    CASE WHEN p.status = 'OUT_OF_SERVICE'
                         THEN 1 ELSE 0 END
                ), 0) AS out_of_service_piles,

                COALESCE(SUM(
                    CASE WHEN p.is_reservable = 0
                         THEN 1 ELSE 0 END
                ), 0) AS non_reservable_piles,

                COALESCE(SUM(
                    CASE
                        WHEN p.status = 'AVAILABLE'
                         AND p.is_reservable = 1
                         AND NOT EXISTS (
                            SELECT 1
                            FROM reservations r
                            WHERE r.pile_id = p.pile_id
                              AND (
                                  r.status IN ('CONFIRMED', 'ACTIVE')
                                  OR (
                                      r.status = 'HOLD'
                                      AND julianday(r.expires_at) > julianday('now')
                                  )
                              )
                         )
                        THEN p.power_kw
                        ELSE 0
                    END
                ), 0) AS available_power_kw

            FROM piles p
        """).fetchone()

        return dict(row)
